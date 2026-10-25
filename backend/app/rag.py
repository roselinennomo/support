import hashlib
import logging
import re
import threading
import uuid
from contextvars import ContextVar
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
_EMBED_BATCH_SIZE = 8  # petits lots : une requête de recherche n'attend pas derrière un gros lot d'indexation
# Au-delà, la mise à jour de l'index n'est plus faite pendant la réponse IA mais en arrière-plan.
_INLINE_SYNC_MAX_SOURCES = 2
_background_sync_guard = threading.Lock()
_background_sync_active = False


# Modèles d'embedding proposés à l'administrateur (dimension de sortie fixe par modèle).
EMBEDDING_MODELS = {
    "qwen3-embedding:0.6b": {"label": "Qwen3 Embedding 0.6B", "dimension": 1024},
    "bge-m3": {"label": "BGE-M3", "dimension": 1024},
    "qwen3-embedding:4b": {"label": "Qwen3 Embedding 4B", "dimension": 2560},
}
_embedding_model_override: str | None = None


class RAGError(RuntimeError):
    pass


# Entreprise dont les documents privés peuvent compléter la recherche (posée par l'appelant, jamais déduite de la requête).
_company_scope: ContextVar[str | None] = ContextVar("rag_company_scope", default=None)


def set_company_scope(company_id: str | None):
    return _company_scope.set(company_id or None)


def reset_company_scope(token) -> None:
    _company_scope.reset(token)


def embedding_model() -> str:
    """Modèle d'embedding actif : choix de l'administrateur, sinon OLLAMA_EMBEDDING_MODEL."""
    return _embedding_model_override or settings.ollama_embedding_model


def set_embedding_model(model: str | None) -> None:
    global _embedding_model_override
    if model is not None and model not in EMBEDDING_MODELS:
        raise RAGError(f"Modèle d'embedding non pris en charge : {model}")
    _embedding_model_override = model


def embedding_dimension() -> int:
    known = EMBEDDING_MODELS.get(embedding_model())
    return known["dimension"] if known else settings.rag_embedding_dimension


def collection_name() -> str:
    """Une collection Qdrant par modèle : les vecteurs de modèles différents ne sont pas comparables."""
    slug = re.sub(r"[^a-z0-9]+", "-", embedding_model().lower()).strip("-")
    return f"{settings.rag_collection}_{slug}"


def model_installed(model: str, installed_names: set[str]) -> bool:
    """Correspondance exacte (qwen3-embedding:0.6b ≠ :4b); sans étiquette, Ollama ajoute :latest."""
    return model in installed_names or (":" not in model and f"{model}:latest" in installed_names)


def embedding_base_url() -> str:
    """Instance Ollama dédiée aux embeddings, ou l'instance de chat si aucune n'est séparée."""
    return (settings.ollama_embedding_base_url or settings.ollama_base_url).rstrip("/")


def installed_ollama_models() -> set[str]:
    response = httpx.get(f"{embedding_base_url()}/api/tags", timeout=3)
    response.raise_for_status()
    return {item.get("name", "") for item in response.json().get("models", [])}


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
    collection_path = f"/collections/{collection_name()}"
    response = httpx.get(f"{settings.rag_qdrant_url.rstrip('/')}{collection_path}", timeout=10)
    if response.status_code == 200:
        collection = response.json()["result"]
        configured_size = collection["config"]["params"]["vectors"]["size"]
        if configured_size != embedding_dimension():
            raise RAGError(
                f"La collection Qdrant utilise {configured_size} dimensions, "
                f"mais RAG_EMBEDDING_DIMENSION vaut {embedding_dimension()}. "
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
                "size": embedding_dimension(),
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
        f"{embedding_base_url()}/api/embed",
        json={"model": embedding_model(), "input": texts},
        timeout=120,
    )
    response.raise_for_status()
    embeddings = response.json().get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RAGError("Ollama n'a pas renvoyé un embedding par extrait.")
    if any(
        not isinstance(vector, list) or len(vector) != embedding_dimension()
        for vector in embeddings
    ):
        raise RAGError(
            f"Dimension d'embedding inattendue pour le modèle {embedding_model()}; "
            f"dimension attendue : {embedding_dimension()}."
        )
    return embeddings


def _all_points() -> list[dict]:
    points = []
    offset = None
    while True:
        response = _qdrant_request(
            "POST",
            f"/collections/{collection_name()}/points/scroll",
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
            f"/collections/{collection_name()}/points",
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
            f"/collections/{collection_name()}/points",
            params={"wait": "true"},
            json={"points": points[batch_start:batch_start + _EMBED_BATCH_SIZE]},
        )


def _delete_source(source: str) -> None:
    _qdrant_request(
        "POST",
        f"/collections/{collection_name()}/points/delete",
        params={"wait": "true"},
        json={"filter": {"must": [{"key": "source", "match": {"value": source}}]}},
    )


def delete_sources(sources: list[str]) -> None:
    """Retire des sources de la collection active; une collection absente n'a rien à retirer."""
    try:
        with _sync_lock:
            for source in sources:
                try:
                    _delete_source(source)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
        raise RAGError(f"Échec de la suppression dans l'index RAG : {exc}") from exc


def _sync_documents(
    progress_callback: Callable[[dict[str, str | int]], None] | None = None,
    max_sources: int | None = None,
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
    if max_sources is not None and total > max_sources:
        # Trop de travail pour une réponse en direct : l'appelant l'exécutera en arrière-plan.
        return {"source_count": len(current), "indexed_sources": 0, "deleted_sources": 0, "deferred_sources": total}
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


def qdrant_status() -> dict:
    """Disponibilité de Qdrant et existence de la collection du modèle d'embedding actif."""
    base = settings.rag_qdrant_url.rstrip("/")
    try:
        httpx.get(f"{base}/healthz", timeout=3).raise_for_status()
        response = httpx.get(f"{base}/collections/{collection_name()}", timeout=5)
        exists = response.status_code == 200
        if not exists and response.status_code != 404:
            response.raise_for_status()
        return {"status": "healthy", "error": "", "collection": collection_name(), "collection_exists": exists}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return {"status": "down", "error": str(exc), "collection": collection_name(), "collection_exists": False}


def embedding_status() -> dict:
    """État de l'instance Ollama d'embeddings et des modèles proposés."""
    try:
        available = installed_ollama_models()
        error = ""
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        available = set()
        error = str(exc)
    active = embedding_model()
    ready = model_installed(active, available)
    options = [
        {
            "id": model_id,
            "label": details["label"],
            "dimension": details["dimension"],
            "installed": model_installed(model_id, available),
            "selected": model_id == active,
            "selectable": True,
        }
        for model_id, details in EMBEDDING_MODELS.items()
    ]
    if active not in EMBEDDING_MODELS:
        # Modèle imposé par l'environnement : affiché mais non sélectionnable.
        options.insert(0, {
            "id": active,
            "label": f"{active} (configuration serveur)",
            "dimension": embedding_dimension(),
            "installed": ready,
            "selected": True,
            "selectable": False,
        })
    return {
        "status": "healthy" if ready else ("down" if error else "warning"),
        "error": error,
        "base_url": embedding_base_url(),
        "dedicated_instance": embedding_base_url() != settings.ollama_base_url.rstrip("/"),
        "model": active,
        "dimension": embedding_dimension(),
        "collection": collection_name(),
        "installed_models": sorted(available),
        "options": options,
    }


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
            f"{settings.rag_qdrant_url.rstrip('/')}/collections/{collection_name()}",
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

    embedding = embedding_status()

    stale_count = sum(document["status"] in {"pending", "modified"} for document in documents)
    return {
        "collection": collection_name(),
        "embedding_model": embedding_model(),
        "embedding_dimension": embedding_dimension(),
        "qdrant": {
            "status": "healthy" if not qdrant_error else "down",
            "error": qdrant_error,
            "collection_exists": collection_exists,
        },
        "embedding": {"status": embedding["status"], "error": embedding["error"]},
        "embedding_options": embedding["options"],
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
            f"{settings.rag_qdrant_url.rstrip('/')}/collections/{collection_name()}",
            timeout=10,
        )
        collection_response.raise_for_status()
        collection = collection_response.json()["result"]
        configured_size = collection["config"]["params"]["vectors"]["size"]
        if configured_size != embedding_dimension():
            raise RAGError(
                f"La collection Qdrant utilise {configured_size} dimensions, "
                f"mais RAG_EMBEDDING_DIMENSION vaut {embedding_dimension()}."
            )
        query_vector = _embed([query])[0]
        matches = _qdrant_request(
            "POST",
            f"/collections/{collection_name()}/points/search",
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


def _start_background_sync() -> None:
    """Lance (une seule fois à la fois) la synchronisation complète du corpus hors du parcours utilisateur."""
    global _background_sync_active
    with _background_sync_guard:
        if _background_sync_active:
            return
        _background_sync_active = True

    def work() -> None:
        global _background_sync_active
        try:
            logger.warning("RAG: indexation du corpus en arrière-plan (la recherche utilise l'index partiel)")
            sync_documents()
        except RAGError as exc:
            logger.warning("RAG background sync failed: %s", exc)
        finally:
            with _background_sync_guard:
                _background_sync_active = False

    threading.Thread(target=work, name="rag-background-sync", daemon=True).start()


def _sync_before_search() -> None:
    """Met l'index à jour sans jamais faire attendre l'utilisateur sur une réindexation volumineuse.

    - une indexation est déjà en cours (admin, autre ticket) : on cherche dans l'index actuel;
    - peu de changements : synchronisation immédiate (nouveau document ajouté à l'instant);
    - beaucoup de changements (nouveau modèle d'embedding, gros dépôt) : arrière-plan.
    """
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        _ensure_collection()
        result = _sync_documents(max_sources=_INLINE_SYNC_MAX_SOURCES)
    finally:
        _sync_lock.release()
    if result and result.get("deferred_sources"):
        _start_background_sync()


def retrieve_context(query: str) -> str:
    try:
        _sync_before_search()
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
    # Documents propres à l'entreprise en premier (les plus spécifiques), puis corpus commun et KB constructeur.
    excerpts = _company_excerpts(query) + excerpts
    excerpts.extend(_kb_excerpts(query))
    return "\n\n".join(excerpts) or "Aucun extrait documentaire pertinent n'a été trouvé."


def _company_excerpts(query: str) -> list[str]:
    """Extraits privés de l'entreprise courante; [] sans entreprise, sans document ou en cas de panne."""
    company_id = _company_scope.get()
    if not company_id:
        return []
    try:
        from app.company_docs import company_excerpts

        return company_excerpts(query, company_id)
    except Exception:
        logger.debug("Company excerpts unavailable", exc_info=True)
        return []


def _kb_excerpts(query: str) -> list[str]:
    """Extraits de la KB constructeur (index séparé); toujours [] si le module est désactivé ou en panne."""
    try:
        from app.kb.retrieval import kb_excerpts

        return kb_excerpts(query)
    except Exception:
        logger.debug("KB excerpts unavailable", exc_info=True)
        return []
