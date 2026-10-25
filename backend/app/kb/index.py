"""Index vectoriel des articles KB : collection Qdrant distincte du corpus documentaire."""

import logging
import re
import uuid

import httpx

from app import rag
from app.config import settings
from app.kb.interfaces import KBError

logger = logging.getLogger(__name__)
_BATCH_SIZE = 16


def collection_name() -> str:
    """Une collection KB par modèle d'embedding, jamais mélangée à `rag.collection_name()`."""
    slug = re.sub(r"[^a-z0-9]+", "-", rag.embedding_model().lower()).strip("-")
    return f"{settings.kb_collection}_{slug}"


class QdrantKBIndex:
    """Implémentation de `KBIndex` sur Qdrant, avec les embeddings Ollama du RAG."""

    def _collection_exists(self) -> bool:
        response = httpx.get(f"{settings.rag_qdrant_url.rstrip('/')}/collections/{collection_name()}", timeout=10)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        size = response.json()["result"]["config"]["params"]["vectors"]["size"]
        if size != rag.embedding_dimension():
            raise KBError(
                f"La collection KB utilise {size} dimensions, mais le modèle d'embedding actif "
                f"en produit {rag.embedding_dimension()}."
            )
        return True

    def _ensure_collection(self) -> None:
        if not self._collection_exists():
            rag._qdrant_request(
                "PUT",
                f"/collections/{collection_name()}",
                json={"vectors": {"size": rag.embedding_dimension(), "distance": "Cosine"}},
            )

    def upsert(self, url: str, title: str, text: str, content_hash: str, source: str = "kb") -> int:
        chunks = rag._chunks(text)
        if not chunks:
            return 0
        self._ensure_collection()
        points = []
        for start in range(0, len(chunks), _BATCH_SIZE):
            batch = chunks[start:start + _BATCH_SIZE]
            for offset, (chunk, vector) in enumerate(zip(batch, rag._embed(batch), strict=True)):
                points.append({
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}:{content_hash}:{start + offset}")),
                    "vector": vector,
                    "payload": {
                        "url": url,
                        "title": title,
                        "text": chunk,
                        "content_hash": content_hash,
                        "source": source,
                    },
                })
        # Remplace les anciens extraits seulement une fois les nouveaux vecteurs calculés.
        self.delete(url)
        for start in range(0, len(points), _BATCH_SIZE):
            rag._qdrant_request(
                "PUT",
                f"/collections/{collection_name()}/points",
                params={"wait": "true"},
                json={"points": points[start:start + _BATCH_SIZE]},
            )
        return len(chunks)

    def delete(self, url: str) -> None:
        try:
            rag._qdrant_request(
                "POST",
                f"/collections/{collection_name()}/points/delete",
                params={"wait": "true"},
                json={"filter": {"must": [{"key": "url", "match": {"value": url}}]}},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    def search(self, query: str, limit: int) -> list[dict]:
        if not self._collection_exists():
            return []
        matches = rag._qdrant_request(
            "POST",
            f"/collections/{collection_name()}/points/search",
            json={
                "vector": rag._embed([query])[0],
                "limit": limit,
                "with_payload": True,
                "score_threshold": settings.rag_score_threshold,
            },
        ).json()["result"]
        return [{
            "url": (match.get("payload") or {}).get("url", ""),
            "title": (match.get("payload") or {}).get("title", ""),
            "text": (match.get("payload") or {}).get("text", ""),
            "score": match.get("score", 0),
            "source": (match.get("payload") or {}).get("source", "kb"),
        } for match in matches]
