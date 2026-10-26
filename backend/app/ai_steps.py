"""Traçage de bout en bout des traitements IA d'un ticket (étapes horodatées).

Une « exécution » (`begin_run`) regroupe les étapes d'un traitement de fond (diagnostic client, aide agent,
résumé de passation) : chargement, recherche documentaire (synchronisation, embeddings, Qdrant, KB), chaque
tentative auprès d'un fournisseur de modèle (réussie ou en échec). La page « Diagnostic ticket » les assemble
avec les messages, événements et recherches KB pour reconstituer ce qui s'est passé à chaque instant.

Aucune étape n'enregistre de contenu (ni prompt, ni réponse, ni extrait de document) : durées, noms et compteurs.
L'enregistrement ne doit jamais faire échouer ni ralentir sensiblement un traitement.
"""

import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

logger = logging.getLogger(__name__)
_run: ContextVar[dict | None] = ContextVar("ai_step_run", default=None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_step(kind: str, name: str, status: str, started_at: datetime, duration_ms: int, detail: dict | None = None) -> None:
    """Enregistre une étape dans l'exécution courante; sans exécution en cours, ne fait rien."""
    run = _run.get()
    if not run:
        return
    try:
        from app.database import SessionLocal
        from app.models import AIStep

        with SessionLocal() as db:
            db.add(AIStep(
                run_id=run["run_id"], ticket_id=run["ticket_id"], label=run["label"], kind=kind, name=name[:200],
                status=status, started_at=started_at, duration_ms=max(0, duration_ms), detail=detail or {},
            ))
            db.commit()
    except Exception:
        logger.debug("Could not persist AI step %s", name, exc_info=True)


@contextmanager
def step(kind: str, name: str, **detail):
    """Mesure un bloc. `holder["detail"]` peut être enrichi dans le bloc (compteurs, tailles)."""
    holder = {"status": "ok", "detail": dict(detail)}
    started_at, started = _now(), time.perf_counter()
    try:
        yield holder["detail"]
    except BaseException as exc:
        holder["status"] = "failed"
        holder["detail"]["error"] = str(exc)[:500]
        raise
    finally:
        record_step(kind, name, holder["status"], started_at, round((time.perf_counter() - started) * 1000), holder["detail"])


def traced_run(label: str, ticket_arg: int = 0) -> Callable:
    """Décorateur d'un traitement de fond dont l'argument `ticket_arg` est l'identifiant du ticket."""

    def decorate(function: Callable) -> Callable:
        @wraps(function)
        def wrapper(*args, **kwargs):
            ticket_id = args[ticket_arg] if len(args) > ticket_arg else kwargs.get("ticket_id")
            token = _run.set({"run_id": str(uuid4()), "ticket_id": ticket_id, "label": label})
            started_at, started = _now(), time.perf_counter()
            status = "ok"
            try:
                return function(*args, **kwargs)
            except BaseException:
                status = "failed"
                raise
            finally:
                record_step("run", label, status, started_at, round((time.perf_counter() - started) * 1000))
                _run.reset(token)

        return wrapper

    return decorate
