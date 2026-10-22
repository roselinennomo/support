import hashlib
import logging
import re
import threading
import uuid
from io import BytesIO
from pathlib import Path
from collections.abc import Callable

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.config import settings

logger = logging.getLogger(__name__)
_sync_lock = threading.Lock()
_SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}
_CHUNK_SIZE = 1400
_CHUNK_OVERLAP = 200
_EMBED_BATCH_SIZE = 32


class RAGError(RuntimeError):
    pass


def _qdrant_request(method: str, path: str, **kwargs) -> httpx.Response:
    response = httpx.request(
        method,
        f"{settings.rag_qdrant_url.rstrip('/')}{path}",
        timeout=30,
        **kwargs,
    )
    response.raise_for_status()
    return response


def _ensure_collection() -> None:
    collection_path = f"/collections/{settings.rag_collection}"
    response = httpx.get(f"{settings.rag_qdrant_url.rstrip('/')}{collection_path}", timeout=10)
    if response.status_code == 200:
        collection = response.json()["result"]
        configured_size = collection["config"]["params"]["vectors"]["size"]
        if configured_size != settings.rag_embedding_dimension:
            raise RAGError(
                f"La collection Qdrant utilise {configured_size} dimensions, "
                f"mais RAG_EMBEDDING_DIMENSION vaut {settings.rag_embedding_dimension}. "
                "Utilisez une collection vide ou adaptez sa configuration."
            )
        return
    if response.status_code != 404:
        response.raise_for_status()
    _qdrant_request(
        "PUT",
        collection_path,
        json={
            "vectors": {
                "size": settings.rag_embedding_dimension,
                "distance": "Cosine",
            }
        },
    )


def _read_source(path: Path) -> list[tuple[int | None, str]]:
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(BytesIO(path.read_bytes()))
        pages = []
        unreadable_pages = 0
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except (PdfReadError, ValueError) as exc:
                logger.debug("Could not extract text from %s page %s: %s", path.name, page_number, exc)
                unreadable_pages += 1
                text = ""
            pages.append((page_number, text))
        if unreadable_pages:
            logger.warning(
                "Could not extract text from %s of %s page(s) in %s",
                unreadable_pages,
                len(reader.pages),
                path.name,
            )
        return pages
    return [(None, path.read_text(encoding="utf-8", errors="replace"))]


def _chunks(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    chunks = []
    start = 0
    while start < len(normalized):
        end = min(start + _CHUNK_SIZE, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return chunks


def _embed(texts: list[str]) -> list[list[float]]:
    response = httpx.post(
        f"{settings.ollama_base_url.rstrip('/')}/api/embed",
        json={"model": settings.ollama_embedding_model, "input": texts},
        timeout=120,
    )
    response.raise_for_status()
    embeddings = response.json().get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RAGError("Ollama n'a pas renvoyé un embedding par extrait.")
    if any(
        not isinstance(vector, list) or len(vector) != settings.rag_embedding_dimension
        for vector in embeddings
    ):
        raise RAGError(
            f"Dimension d'embedding inattendue pour le modèle {settings.ollama_embedding_model}; "
            f"dimension attendue : {settings.rag_embedding_dimension}."
        )
    return embeddings


def _all_points() -> list[dict]:
    points = []
    offset = None
    while True:
        response = _qdrant_request(
            "POST",
            f"/collections/{settings.rag_collection}/points/scroll",
            json={
                "limit": 256,
                "offset": offset,
                "with_payload": ["source", "content_hash", "indexed_empty"],
                "with_vector": False,
            },
        ).json()["result"]
        points.extend(response["points"])
        offset = response.get("next_page_offset")
        if offset is None:
            return points


def _index_source(source: str, digest: str, pages: list[tuple[int | None, str]]) -> None:
    chunks = [
        (page_number, chunk)
        for page_number, page_text in pages
        for chunk in _chunks(page_text)
    ]
    if not chunks:
        logger.warning("RAG source contains no extractable text: %s", source)
        marker_vector = _embed(["No extractable text for this document."])[0]
        _delete_source(source)
        _qdrant_request(
            "PUT",
            f"/collections/{settings.rag_collection}/points",
            params={"wait": "true"},
            json={"points": [{
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{digest}:empty")),
                "vector": marker_vector,
                "payload": {
                    "source": source,
                    "content_hash": digest,
                    "indexed_empty": True,
                },
            }]},
        )
        return

    points = []
    for batch_start in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[batch_start:batch_start + _EMBED_BATCH_SIZE]
        embeddings = _embed([chunk for _, chunk in batch])
        points.extend(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{digest}:{index}")),
                "vector": embedding,
                "payload": {
                    "source": source,
                    "content_hash": digest,
                    "page": page_number,
                    "text": chunk,
                },
            }
            for index, ((page_number, chunk), embedding) in enumerate(
                zip(batch, embeddings, strict=True),
                start=batch_start,
            )
        )
    _delete_source(source)
    for batch_start in range(0, len(points), _EMBED_BATCH_SIZE):
        _qdrant_request(
            "PUT",
            f"/collections/{settings.rag_collection}/points",
            params={"wait": "true"},
            json={"points": points[batch_start:batch_start + _EMBED_BATCH_SIZE]},
        )


def _delete_source(source: str) -> None:
    _qdrant_request(
        "POST",
        f"/collections/{settings.rag_collection}/points/delete",
        params={"wait": "true"},
        json={"filter": {"must": [{"key": "source", "match": {"value": source}}]}},
    )


def _sync_documents(
    progress_callback: Callable[[dict[str, str | int]], None] | None = None,
) -> dict[str, int]:
    root = Path(settings.rag_documents_dir).resolve()
    if not root.is_dir():
        raise RAGError(f"Le dossier de documentation RAG est introuvable : {root}")
    if progress_callback:
        progress_callback({
            "phase": "scanning",
            "processed": 0,
            "total": 0,
            "current": "",
            "message": "Analyse des documents sources…",
        })

    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _SUPPORTED_EXTENSIONS
    )
    current: dict[str, tuple[str, Path]] = {}
    for path in files:
        source = path.relative_to(root).as_posix()
        content = path.read_bytes()
        current[source] = (hashlib.sha256(content).hexdigest(), path)

    existing: dict[str, str] = {}
    for point in _all_points():
        payload = point.get("payload") or {}
        source = payload.get("source")
        if source:
            existing[source] = payload.get("content_hash", "")

    deleted_sources = sorted(existing.keys() - current.keys())
    changed_sources = [
        (source, digest, path)
        for source, (digest, path) in current.items()
        if existing.get(source) != digest
    ]
    total = len(deleted_sources) + len(changed_sources)
    processed = 0
    if progress_callback:
        progress_callback({
            "phase": "indexing",
            "processed": processed,
            "total": total,
            "current": "",
            "message": "Aucune modification à appliquer." if total == 0 else "Mise à jour des documents et des vecteurs…",
        })

    for source in deleted_sources:
        _delete_source(source)
        processed += 1
        if progress_callback:
            progress_callback({
                "phase": "indexing",
                "processed": processed,
                "total": total,
                "current": source,
                "message": "Suppression d’une source absente du corpus.",
            })
        logger.info("Removed deleted RAG source: %s", source)

    indexed_sources = 0
    for source, digest, path in changed_sources:
        _index_source(source, digest, _read_source(path))
        indexed_sources += 1
        processed += 1
        if progress_callback:
            progress_callback({
                "phase": "indexing",
                "processed": processed,
                "total": total,
                "current": source,
                "message": "Document indexé.",
            })
        logger.info("Indexed RAG source: %s", source)
    if progress_callback:
        progress_callback({
            "phase": "complete",
            "processed": total,
            "total": total,
            "current": "",
            "message": "Synchronisation terminée.",
        })
    return {
        "source_count": len(current),
        "indexed_sources": indexed_sources,
        "deleted_sources": len(deleted_sources),
    }


def sync_documents(
    progress_callback: Callable[[dict[str, str | int]], None] | None = None,
) -> dict[str, int]:
    try:
        with _sync_lock:
            _ensure_collection()
            return _sync_documents(progress_callback)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, RAGError) as exc:
        raise RAGError(f"Échec de la synchronisation RAG : {exc}") from exc


def inspect_index() -> dict:
    root = Path(settings.rag_documents_dir).resolve()
    if not root.is_dir():
        raise RAGError(f"Le dossier de documentation RAG est introuvable : {root}")

    try:
        files = sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in _SUPPORTED_EXTENSIONS
        )
        current = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        }
    except OSError as exc:
        raise RAGError(f"Impossible de lire le corpus documentaire RAG : {exc}") from exc
    try:
        health_response = httpx.get(f"{settings.rag_qdrant_url.rstrip('/')}/healthz", timeout=3)
        health_response.raise_for_status()
        collection_response = httpx.get(
            f"{settings.rag_qdrant_url.rstrip('/')}/collections/{settings.rag_collection}",
            timeout=5,
        )
        collection_exists = collection_response.status_code == 200
        if collection_exists:
            collection_response.raise_for_status()
            points = _all_points()
        elif collection_response.status_code == 404:
            points = []
        else:
            collection_response.raise_for_status()
            points = []
        qdrant_error = ""
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        collection_exists = False
        points = []
        qdrant_error = str(exc)

    indexed: dict[str, dict] = {}
    for point in points:
        payload = point.get("payload") or {}
        source = payload.get("source")
        if not source:
            continue
        source_data = indexed.setdefault(source, {
            "content_hash": payload.get("content_hash", ""),
            "chunks": 0,
            "empty": False,
        })
        source_data["empty"] = source_data["empty"] or payload.get("indexed_empty", False)
        if not payload.get("indexed_empty", False):
            source_data["chunks"] += 1

    documents = []
    for source, digest in current.items():
        indexed_source = indexed.get(source)
        if not indexed_source:
            status = "pending"
        elif indexed_source["content_hash"] != digest:
            status = "modified"
        elif indexed_source["empty"]:
            status = "empty"
        else:
            status = "indexed"
        documents.append({
            "source": source,
            "size_bytes": (root / source).stat().st_size,
            "status": status,
            "chunks": indexed_source["chunks"] if indexed_source else 0,
        })
    deleted_sources = sorted(indexed.keys() - current.keys())

    try:
        embedding_response = httpx.get(
            f"{settings.ollama_base_url.rstrip('/')}/api/tags",
            timeout=3,
        )
        embedding_response.raise_for_status()
        available_models = {
            item.get("name", "").split(":")[0]
            for item in embedding_response.json().get("models", [])
        }
        embedding_ready = settings.ollama_embedding_model.split(":")[0] in available_models
        embedding_error = ""
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        embedding_ready = False
        embedding_error = str(exc)

    stale_count = sum(document["status"] in {"pending", "modified"} for document in documents)
    return {
        "collection": settings.rag_collection,
        "embedding_model": settings.ollama_embedding_model,
        "embedding_dimension": settings.rag_embedding_dimension,
        "qdrant": {
            "status": "healthy" if not qdrant_error else "down",
            "error": qdrant_error,
            "collection_exists": collection_exists,
        },
        "embedding": {
            "status": "healthy" if embedding_ready else ("down" if embedding_error else "warning"),
            "error": embedding_error,
        },
        "metrics": {
            "source_count": len(current),
            "indexed_sources": sum(document["status"] in {"indexed", "empty"} for document in documents),
            "pending_sources": stale_count,
            "deleted_sources": len(deleted_sources),
            "chunk_count": sum(document["chunks"] for document in documents),
            "empty_sources": sum(document["status"] == "empty" for document in documents),
        },
        "documents": documents,
        "deleted_sources": deleted_sources,
    }


def evaluate_query(query: str) -> dict:
    try:
        if not query.strip():
            raise RAGError("La requête de test ne peut pas être vide.")
        collection_response = httpx.get(
            f"{settings.rag_qdrant_url.rstrip('/')}/collections/{settings.rag_collection}",
            timeout=10,
        )
        collection_response.raise_for_status()
        collection = collection_response.json()["result"]
        configured_size = collection["config"]["params"]["vectors"]["size"]
        if configured_size != settings.rag_embedding_dimension:
            raise RAGError(
                f"La collection Qdrant utilise {configured_size} dimensions, "
                f"mais RAG_EMBEDDING_DIMENSION vaut {settings.rag_embedding_dimension}."
            )
        query_vector = _embed([query])[0]
        matches = _qdrant_request(
            "POST",
            f"/collections/{settings.rag_collection}/points/search",
            json={
                "vector": query_vector,
                "limit": settings.rag_top_k,
                "with_payload": True,
                "score_threshold": settings.rag_score_threshold,
                "filter": {
                    "must_not": [{"key": "indexed_empty", "match": {"value": True}}]
                },
            },
        ).json()["result"]
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, RAGError) as exc:
        raise RAGError(f"Échec de l’évaluation de recherche RAG : {exc}") from exc
    return {
        "query": query,
        "result_count": len(matches),
        "results": [
            {
                "source": (match.get("payload") or {}).get("source", "source inconnue"),
                "page": (match.get("payload") or {}).get("page"),
                "score": match.get("score", 0),
                "text": (match.get("payload") or {}).get("text", ""),
            }
            for match in matches
        ],
    }


def retrieve_context(query: str) -> str:
    try:
        with _sync_lock:
            _ensure_collection()
            _sync_documents()
        if not query.strip():
            return "Aucun extrait documentaire pertinent n'a été fourni."
        evaluation = evaluate_query(query)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, RAGError) as exc:
        raise RAGError(f"Échec de la synchronisation ou de la recherche RAG : {exc}") from exc

    excerpts = []
    for match in evaluation["results"]:
        source = match["source"]
        page = match["page"]
        reference = f"{source}, page {page}" if page else source
        excerpts.append(f"[Source : {reference}]\n{match['text']}")
    return "\n\n".join(excerpts) or "Aucun extrait documentaire pertinent n'a été trouvé."
