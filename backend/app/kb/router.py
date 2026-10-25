"""Console d'administration de l'enrichissement KB : statut, interrupteur, métriques, débogage."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_roles
from app.config import settings
from app.database import get_db
from app.kb import flags
from app.kb.archive import KBArchive
from app.kb.client import default_scraper
from app.kb.interfaces import KBError, KBScraper, KBSessionExpired, KBUnavailable, SessionState
from app.kb.jobs import enqueue_reindex, get_queue
from app.kb.service import compute_metrics
from app.models import KBJob, Ticket, User

router = APIRouter(prefix="/api/admin/kb", tags=["kb"])
logger = logging.getLogger(__name__)


def get_scraper() -> KBScraper:
    """Point d'injection du scraper (remplaçable dans les tests)."""
    return default_scraper()


def get_archive() -> KBArchive | None:
    """Point d'injection de l'archive locale (remplaçable dans les tests)."""
    return KBArchive.from_settings()


class KBSettingsUpdate(BaseModel):
    enabled: bool


def _session_payload(record) -> dict:
    return {
        "state": record.session_state,
        "checked_at": record.session_checked_at,
        "last_alert": record.last_alert,
        "last_alert_at": record.last_alert_at,
    }


JOB_STATUSES = ("success", "failed", "skipped", "running")


def _recent_jobs(
    db: Session, limit: int, offset: int = 0, status: str | None = None,
) -> tuple[list[tuple[KBJob, str | None, str | None]], int]:
    """Page de jobs (du plus récent) avec la référence et le titre de leur ticket, et le total du filtre."""
    condition = [KBJob.status == status] if status else []
    total = db.scalar(select(func.count()).select_from(KBJob).where(*condition)) or 0
    rows = db.execute(
        select(KBJob, Ticket.reference, Ticket.title)
        .outerjoin(Ticket, Ticket.id == KBJob.ticket_id)
        .where(*condition)
        .order_by(KBJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [(job, reference, title) for job, reference, title in rows], total


def _focused_job(db: Session, job_id: str | None) -> dict | None:
    if not job_id:
        return None
    row = db.execute(
        select(KBJob, Ticket.reference, Ticket.title).outerjoin(Ticket, Ticket.id == KBJob.ticket_id).where(KBJob.id == job_id)
    ).first()
    return _job_payload(row[0], row[1], row[2], detailed=True) if row else None


def _job_counts(db: Session) -> dict[str, int]:
    """Nombre de jobs par statut, pour les filtres de la console."""
    by_status = dict(db.execute(select(KBJob.status, func.count()).group_by(KBJob.status)).all())
    return {"all": sum(by_status.values()), **{status: by_status.get(status, 0) for status in JOB_STATUSES}}


def _job_payload(job: KBJob, reference: str | None = None, title: str | None = None, detailed: bool = False) -> dict:
    payload = {
        "id": job.id,
        "kind": job.kind,
        "ticket_id": job.ticket_id,
        "ticket_reference": reference,
        "ticket_title": title,
        "results": job.results or [],
        "status": job.status,
        "query": job.query,
        "query_source": job.query_source,
        "hits": job.hits,
        "new_documents": job.new_documents,
        "skipped_documents": job.skipped_documents,
        "error_code": job.error_code,
        "duration_ms": job.duration_ms,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
    if detailed:
        payload.update({"urls": job.urls or [], "error": job.error, "failed_selector": job.failed_selector})
    return payload


def supervision_entry(db: Session) -> dict | None:
    """Ligne de la console de supervision; None si le module est désactivé côté serveur."""
    if not flags.env_enabled():
        return None
    record = flags.get_settings(db)
    healthy = get_scraper().health()
    if not healthy:
        status, detail = "warning", "Navigateur KB hors ligne : l'assistance IA fonctionne sans la KB constructeur."
    elif record.session_state == SessionState.EXPIRED.value:
        status, detail = "warning", "Session KB expirée : reconnectez-vous (MFA) depuis la console KB."
    elif not record.enabled:
        status, detail = "disabled", "Enrichissement KB désactivé par l'administrateur."
    else:
        status, detail = "healthy", "Navigateur KB en ligne; enrichissement actif."
    return {"name": "kb", "status": status, "detail": detail}


@router.get("")
def kb_status(
    jobs_limit: int = Query(default=25, ge=1, le=100),
    jobs_offset: int = Query(default=0, ge=0),
    jobs_status: str | None = Query(default=None, pattern="^(success|failed|skipped|running)$"),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    record = flags.get_settings(db)
    db.commit()
    env_enabled = flags.env_enabled()
    jobs, jobs_total = _recent_jobs(db, jobs_limit, jobs_offset, jobs_status)
    return {
        "env_enabled": env_enabled,
        "enabled": bool(record.enabled),
        "effective": env_enabled and bool(record.enabled),
        "browser_online": get_scraper().health() if env_enabled else False,
        "login_url": settings.kb_login_url,
        "session": _session_payload(record),
        "queue_size": get_queue().size,
        "metrics": compute_metrics(db),
        "recent_jobs": [_job_payload(job, reference, title) for job, reference, title in jobs],
        "jobs_total": jobs_total,
        "job_counts": _job_counts(db),
    }


@router.put("/settings")
def update_kb_settings(
    payload: KBSettingsUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    if not flags.env_enabled():
        raise HTTPException(
            status_code=409,
            detail="Le module est désactivé par la configuration serveur (ENABLE_KB_BROWSER_SCRAPER=false).",
        )
    record = flags.get_settings(db)
    record.enabled = payload.enabled
    db.commit()
    return {"enabled": record.enabled, "effective": record.enabled}


@router.post("/session/check")
def check_kb_session(db: Session = Depends(get_db), _admin: User = Depends(require_roles("admin"))):
    """Vérifie la session du navigateur (ex. après une reconnexion MFA via noVNC)."""
    if not flags.env_enabled():
        raise HTTPException(status_code=409, detail="Le module est désactivé par la configuration serveur.")
    try:
        state = get_scraper().session_state()
    except KBUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KBSessionExpired:
        state = SessionState.EXPIRED
    except KBError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    alert = "Session KB expirée : reconnexion (MFA) requise." if state is SessionState.EXPIRED else ""
    flags.record_session_state(db, state.value, alert)
    return _session_payload(flags.get_settings(db))


@router.get("/debug")
def kb_debug(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    job_id: str | None = Query(default=None, max_length=36),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    """Console de débogage : journal du navigateur et détail des derniers jobs (URLs, sélecteurs en échec)."""
    jobs, jobs_total = _recent_jobs(db, limit, offset)
    return {
        "browser_logs": get_scraper().logs(200) if flags.env_enabled() else [],
        "jobs_total": jobs_total,
        "jobs": [_job_payload(job, reference, title, detailed=True) for job, reference, title in jobs],
        # Recherche ouverte depuis la liste : fournie même si elle n'est pas sur la page courante du débogage.
        "focused_job": _focused_job(db, job_id),
    }


@router.post("/reindex", status_code=202)
def reindex_kb(_admin: User = Depends(require_roles("admin"))):
    if not flags.env_enabled():
        raise HTTPException(status_code=409, detail="Le module est désactivé par la configuration serveur.")
    return {"queued": enqueue_reindex()}


def _require_archive() -> KBArchive:
    archive = get_archive()
    if archive is None:
        raise HTTPException(status_code=409, detail="L'archive locale est désactivée (KB_ARCHIVE_ENABLED=false).")
    return archive


@router.get("/archive")
def archive_status(_admin: User = Depends(require_roles("admin"))):
    """Contenu de l'archive locale (dossier `kb_archive/` du projet)."""
    archive = get_archive()
    if archive is None:
        return {"enabled": False}
    return {"enabled": True, "path": "kb_archive/", **archive.stats()}


@router.post("/archive/export")
def archive_export(db: Session = Depends(get_db), _admin: User = Depends(require_roles("admin"))):
    """Archive les éléments déjà en base mais absents de l'archive (texte seulement : leurs fichiers ne sont plus disponibles)."""
    archive = _require_archive()
    return {"exported": archive.export_all(db), **archive.stats()}


@router.post("/archive/restore")
def archive_restore(db: Session = Depends(get_db), _admin: User = Depends(require_roles("admin"))):
    """Recrée en base les éléments archivés manquants, puis planifie le recalcul de leurs vecteurs."""
    archive = _require_archive()
    restored = archive.restore(db)
    return {"restored": restored, "reindex_queued": enqueue_reindex() if restored else False, **archive.stats()}
