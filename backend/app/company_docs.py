"""Documents propres à chaque entreprise (architecture réseau, plan d'adressage, matrice de flux…).

Principes :
- **Privés** : chaque entreprise a sa propre collection vectorielle `<support_company>_<id entreprise>_<modèle>`.
  La recherche ne reçoit qu'un identifiant d'entreprise et n'interroge que cette collection : aucun filtre à oublier
  ne peut mélanger deux entreprises. Les extraits retournés sont de plus revérifiés (`company_id` du point).
- **Séparés des documents communs** (corpus Alcatel, KB) : le RAG interroge les deux sources puis les fusionne dans
  le prompt avec des étiquettes distinctes (voir `rag.retrieve_context`).
- **Droits** : les utilisateurs de l'entreprise et les administrateurs ajoutent/suppriment; les agents support n'ont
  qu'un accès en lecture seule aux entreprises qui leur sont affectées.
- Les fichiers originaux sont conservés dans `company_docs/<id entreprise>/` (dossier du projet, hors volumes Docker).
"""

import hashlib
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import rag
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.doc_text import SUPPORTED_SUFFIXES, extract_text, is_supported
from app.models import Company, CompanyDocument, User, company_agents

router = APIRouter(prefix="/api/company-docs", tags=["company-docs"])
logger = logging.getLogger(__name__)
_BATCH = 8
_EXCERPTS = 3


# ----------------------------------------------------------------------------- index vectoriel privé
def collection_name(company_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", rag.embedding_model().lower()).strip("-")
    return f"{settings.company_collection}_{company_id}_{slug}"


class CompanyIndex:
    """Index d'UNE entreprise : ne peut ni lire ni écrire dans la collection d'une autre."""

    def __init__(self, company_id: str):
        if not company_id:
            raise ValueError("Un identifiant d'entreprise est requis.")
        self.company_id = company_id
        self.collection = collection_name(company_id)

    def _exists(self) -> bool:
        response = httpx.get(f"{settings.rag_qdrant_url.rstrip('/')}/collections/{self.collection}", timeout=10)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        size = response.json()["result"]["config"]["params"]["vectors"]["size"]
        if size != rag.embedding_dimension():
            raise RuntimeError(f"La collection {self.collection} utilise {size} dimensions (modèle d'embedding différent).")
        return True

    def _ensure(self) -> None:
        if not self._exists():
            rag._qdrant_request(
                "PUT", f"/collections/{self.collection}",
                json={"vectors": {"size": rag.embedding_dimension(), "distance": "Cosine"}},
            )

    def upsert(self, doc_id: str, filename: str, chunks: list[str]) -> int:
        self._ensure()
        points = []
        for start in range(0, len(chunks), _BATCH):
            batch = chunks[start:start + _BATCH]
            for offset, (chunk, vector) in enumerate(zip(batch, rag._embed(batch), strict=True)):
                points.append({
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.company_id}:{doc_id}:{start + offset}")),
                    "vector": vector,
                    "payload": {"company_id": self.company_id, "doc_id": doc_id, "filename": filename, "text": chunk},
                })
        self.delete(doc_id)  # remplace les anciens extraits une fois les nouveaux calculés
        for start in range(0, len(points), _BATCH):
            rag._qdrant_request(
                "PUT", f"/collections/{self.collection}/points", params={"wait": "true"},
                json={"points": points[start:start + _BATCH]},
            )
        return len(chunks)

    def delete(self, doc_id: str) -> None:
        try:
            rag._qdrant_request(
                "POST", f"/collections/{self.collection}/points/delete", params={"wait": "true"},
                json={"filter": {"must": [{"key": "doc_id", "match": {"value": doc_id}}]}},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    def search(self, query: str, limit: int) -> list[dict]:
        if not self._exists():
            return []
        matches = rag._qdrant_request(
            "POST", f"/collections/{self.collection}/points/search",
            json={
                "vector": rag._embed([query])[0], "limit": limit, "with_payload": True,
                "score_threshold": settings.rag_score_threshold,
            },
        ).json()["result"]
        results = []
        for match in matches:
            payload = match.get("payload") or {}
            if payload.get("company_id") != self.company_id:  # défense en profondeur : jamais d'extrait d'une autre entreprise
                logger.error("Company index returned a foreign point for %s", self.company_id)
                continue
            results.append({"filename": payload.get("filename", ""), "text": payload.get("text", ""), "score": match.get("score", 0)})
        return results


def company_excerpts(query: str, company_id: str, index: CompanyIndex | None = None) -> list[str]:
    """Extraits des documents de CETTE entreprise, étiquetés pour le prompt. [] si aucun ou en cas d'erreur."""
    if not company_id or not query.strip():
        return []
    try:
        found = (index or CompanyIndex(company_id)).search(query, _EXCERPTS)
    except Exception as exc:  # le RAG commun répond seul
        logger.warning("Company documents retrieval skipped: %s", exc)
        return []
    return [f"[Source documentation de l'entreprise : {item['filename']}]\n{item['text']}" for item in found if item["text"]]


# ----------------------------------------------------------------------------- indexation
def index_document(doc_id: str, session_factory=None, index_factory=CompanyIndex) -> str:
    """Découpe, vectorise et indexe un document; consigne le résultat. Ne lève jamais. Retourne le statut."""
    if session_factory is None:
        from app.database import SessionLocal as session_factory
    with session_factory() as db:
        doc = db.get(CompanyDocument, doc_id)
        if not doc:
            return "missing"
        try:
            chunks = rag._chunks(doc.content)
            if not chunks:
                doc.status, doc.error, doc.chunk_count = "empty", "Aucun texte extractible (document scanné ou image ?).", 0
            else:
                doc.chunk_count = index_factory(doc.company_id).upsert(doc.id, doc.filename, chunks)
                doc.status, doc.error = "indexed", ""
                doc.indexed_model, doc.indexed_at = rag.embedding_model(), datetime.now(timezone.utc)
        except Exception as exc:
            logger.warning("Company document %s indexing failed: %s", doc_id, exc)
            doc.status, doc.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        return doc.status


def index_pending_and_stale(session_factory=None, index_factory=CompanyIndex) -> int:
    """Indexe les documents en attente et réindexe ceux d'un autre modèle d'embedding (changement de modèle, redémarrage)."""
    if session_factory is None:
        from app.database import SessionLocal as session_factory
    model = rag.embedding_model()
    with session_factory() as db:
        ids = list(db.scalars(select(CompanyDocument.id).where(
            CompanyDocument.content != "",
            (CompanyDocument.status == "pending")
            | ((CompanyDocument.status.in_(("indexed", "failed"))) & (CompanyDocument.indexed_model != model)),
        )).all())
    for doc_id in ids:
        index_document(doc_id, session_factory, index_factory)
    return len(ids)


def start_background_indexing() -> None:
    """Reprise en tâche de fond (démarrage de l'API, changement de modèle d'embedding)."""
    threading.Thread(target=lambda: _safe(index_pending_and_stale), name="company-docs-index", daemon=True).start()


def _safe(function) -> None:
    try:
        function()
    except Exception:
        logger.exception("Company documents background indexing failed")


# ----------------------------------------------------------------------------- droits
def _resolve_company(db: Session, user: User, company_id: str | None, write: bool) -> Company:
    """Entreprise ciblée par la requête, selon le rôle. 404 plutôt que 403 pour ne pas révéler l'existence d'une entreprise."""
    if user.role == "customer":
        if not user.company_id or (company_id and company_id != user.company_id):
            raise HTTPException(status_code=404, detail="Entreprise introuvable.")
        target = user.company_id
    elif user.role == "admin":
        if not company_id:
            raise HTTPException(status_code=422, detail="Précisez l'entreprise (company_id).")
        target = company_id
    elif user.role == "agent":
        if write:
            raise HTTPException(
                status_code=403,
                detail="Les documents d'entreprise sont ajoutés par l'entreprise ou par un administrateur.",
            )
        if not company_id or db.scalar(select(company_agents.c.company_id).where(
            company_agents.c.company_id == company_id, company_agents.c.agent_id == user.id,
        )) is None:
            raise HTTPException(status_code=404, detail="Entreprise introuvable.")
        target = company_id
    else:
        raise HTTPException(status_code=403, detail="Accès refusé.")
    company = db.get(Company, target)
    if not company or (write and not company.is_active):
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    return company


def _document_or_404(db: Session, user: User, doc_id: str, write: bool) -> CompanyDocument:
    doc = db.get(CompanyDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    _resolve_company(db, user, doc.company_id, write)  # mêmes règles que pour l'entreprise elle-même
    return doc


def _payload(doc: CompanyDocument) -> dict:
    return {
        "id": doc.id, "filename": doc.filename, "description": doc.description, "size": doc.size, "status": doc.status,
        "error": doc.error, "chunk_count": doc.chunk_count, "created_at": doc.created_at, "indexed_at": doc.indexed_at,
    }


def _used_bytes(db: Session, company_id: str) -> int:
    return db.scalar(select(func.coalesce(func.sum(CompanyDocument.size), 0)).where(CompanyDocument.company_id == company_id)) or 0


# ----------------------------------------------------------------------------- API
@router.get("")
def list_documents(
    company_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    company = _resolve_company(db, user, company_id, write=False)
    docs = db.scalars(
        select(CompanyDocument).where(CompanyDocument.company_id == company.id).order_by(CompanyDocument.created_at.desc())
    ).all()
    return {
        "company": {"id": company.id, "name": company.name},
        "can_edit": user.role in {"customer", "admin"},
        "used_bytes": _used_bytes(db, company.id),
        "max_bytes": settings.company_docs_max_mb_per_company * 1024 * 1024,
        "max_file_bytes": settings.company_docs_max_file_mb * 1024 * 1024,
        "max_files": settings.company_docs_max_files_per_upload,
        "formats": sorted(SUPPORTED_SUFFIXES),
        "documents": [_payload(doc) for doc in docs],
    }


@router.post("", status_code=201)
def upload_documents(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    company_id: str | None = Form(default=None),
    description: str = Form(default="", max_length=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company = _resolve_company(db, user, company_id, write=True)
    if len(files) > settings.company_docs_max_files_per_upload:
        raise HTTPException(
            status_code=413,
            detail=f"Vous pouvez ajouter au maximum {settings.company_docs_max_files_per_upload} documents par envoi.",
        )
    max_file = settings.company_docs_max_file_mb * 1024 * 1024
    quota = settings.company_docs_max_mb_per_company * 1024 * 1024
    used = _used_bytes(db, company.id)
    folder = Path(settings.company_docs_dir) / company.id
    uploaded, skipped = [], []
    for upload in files:
        name = Path(upload.filename or "").name[:255]
        if not name or not is_supported(name):
            skipped.append({"filename": name or "(sans nom)", "reason": f"Format non pris en charge. Formats : {', '.join(sorted(SUPPORTED_SUFFIXES))}."})
            continue
        data = upload.file.read(max_file + 1)
        if len(data) > max_file:
            skipped.append({"filename": name, "reason": f"Fichier trop volumineux (maximum {settings.company_docs_max_file_mb} Mo)."})
            continue
        digest = hashlib.sha256(data).hexdigest()
        existing = db.scalar(select(CompanyDocument).where(
            CompanyDocument.company_id == company.id, CompanyDocument.content_hash == digest,
        ))
        if existing:
            skipped.append({"filename": name, "reason": f"Déjà présent sous le nom « {existing.filename} »."})
            continue
        if used + len(data) > quota:
            skipped.append({"filename": name, "reason": f"Quota de l'entreprise atteint ({settings.company_docs_max_mb_per_company} Mo)."})
            continue
        try:
            text = extract_text(data, name)
        except Exception as exc:
            skipped.append({"filename": name, "reason": f"Fichier illisible ({type(exc).__name__})."})
            continue
        stored_name = f"{digest}{Path(name).suffix.lower()}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / stored_name).write_bytes(data)
        doc = CompanyDocument(
            company_id=company.id, uploaded_by_id=user.id, filename=name, description=description.strip(),
            stored_name=stored_name, size=len(data), content_hash=digest, content=text,
            status="pending" if text.strip() else "empty",
            error="" if text.strip() else "Aucun texte extractible (document scanné ou image ?).",
        )
        db.add(doc)
        db.flush()
        used += len(data)
        uploaded.append(doc)
    db.commit()
    for doc in uploaded:
        if doc.status == "pending":
            background_tasks.add_task(index_document, doc.id)
    return {"uploaded": [_payload(doc) for doc in uploaded], "skipped": skipped}


@router.delete("/{doc_id}", status_code=204)
def delete_document(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = _document_or_404(db, user, doc_id, write=True)
    try:
        CompanyIndex(doc.company_id).delete(doc.id)
    except Exception as exc:  # l'index peut être indisponible : le document disparaît quand même de l'entreprise
        logger.warning("Could not remove vectors of company document %s: %s", doc.id, exc)
    (Path(settings.company_docs_dir) / doc.company_id / doc.stored_name).unlink(missing_ok=True)
    db.delete(doc)
    db.commit()


@router.get("/{doc_id}/download")
def download_document(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = _document_or_404(db, user, doc_id, write=False)
    path = Path(settings.company_docs_dir) / doc.company_id / doc.stored_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Le fichier original n'est plus disponible.")
    return FileResponse(path, filename=doc.filename)


@router.post("/{doc_id}/reindex", status_code=202)
def reindex_document(
    doc_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    doc = _document_or_404(db, user, doc_id, write=True)
    if not doc.content.strip():
        raise HTTPException(status_code=409, detail="Ce document ne contient aucun texte à indexer.")
    doc.status, doc.error = "pending", ""
    db.commit()
    background_tasks.add_task(index_document, doc.id)
    return {"status": "pending"}
