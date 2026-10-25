"""Feature flag du module KB : drapeau serveur (variable d'environnement) + interrupteur en direct."""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import KBSettings

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], AbstractContextManager[Session]]


def env_enabled() -> bool:
    """Drapeau serveur ENABLE_KB_BROWSER_SCRAPER : à `false`, le module est totalement inerte."""
    return bool(settings.enable_kb_browser_scraper)


def get_settings(db: Session) -> KBSettings:
    record = db.get(KBSettings, 1)
    if not record:
        record = KBSettings(id=1, enabled=True)
        db.add(record)
        db.flush()
    return record


def is_enabled(session_factory: SessionFactory | None = None) -> bool:
    """Actif seulement si le serveur l'autorise ET que l'admin ne l'a pas coupé. Ne lève jamais."""
    if not env_enabled():
        return False
    try:
        if session_factory is None:
            from app.database import SessionLocal as session_factory
        with session_factory() as db:
            record = db.get(KBSettings, 1)
            return True if record is None else bool(record.enabled)
    except Exception:  # pragma: no cover - une panne de base ne doit jamais casser l'appelant
        logger.exception("Could not read the KB feature flag")
        return False


def record_session_state(db: Session, state: str, alert: str = "") -> None:
    """Enregistre l'état de la session navigateur; une alerte est conservée tant qu'elle n'est pas résolue."""
    record = get_settings(db)
    now = datetime.now(timezone.utc)
    record.session_state = state
    record.session_checked_at = now
    if alert:
        record.last_alert = alert
        record.last_alert_at = now
        logger.error("KB browser alert: %s", alert)
    elif state == "valid":
        record.last_alert = ""
    db.commit()
