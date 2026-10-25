"""File d'attente asynchrone : un seul worker, car un profil Chromium persistant ne se partage pas."""

import logging
import queue
import threading
import time
from collections.abc import Callable

from sqlalchemy import update

from app.config import settings
from app.kb import flags

logger = logging.getLogger(__name__)
Task = Callable[[], object]


class KBJobQueue:
    """File bornée traitée en tâche de fond. Une panne d'une tâche ne tue jamais le worker."""

    def __init__(self, maxsize: int = 50):
        self._queue: queue.Queue[tuple[str, Task]] = queue.Queue(maxsize=maxsize)
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def submit(self, key: str, task: Task) -> bool:
        """Met la tâche en file. False si une tâche identique attend déjà ou si la file est pleine."""
        with self._lock:
            if key in self._pending:
                return False
            try:
                self._queue.put_nowait((key, task))
            except queue.Full:
                logger.warning("KB queue is full, dropping %s", key)
                return False
            self._pending.add(key)
            self._ensure_worker()
        return True

    @property
    def size(self) -> int:
        return self._queue.qsize()

    def join(self, timeout: float | None = None) -> None:
        """Attend la fin des tâches en file (tests, arrêt propre)."""
        self._queue.join() if timeout is None else self._join_with_timeout(timeout)

    def _join_with_timeout(self, timeout: float) -> None:
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        done.wait(timeout)

    def stop(self) -> None:
        self._stopping.set()
        if self._thread:
            try:
                self._queue.put_nowait(("", lambda: None))
            except queue.Full:
                pass

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._work, name="kb-worker", daemon=True)
        self._thread.start()

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                key, task = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if key:
                    task()
            except Exception:
                logger.exception("KB task %s crashed", key)
            finally:
                with self._lock:
                    self._pending.discard(key)
                self._queue.task_done()


_queue: KBJobQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> KBJobQueue:
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = KBJobQueue(settings.kb_queue_size)
        return _queue


def enqueue_ticket_enrichment(ticket_id: str) -> bool:
    """Planifie l'enrichissement KB d'un ticket. N'échoue jamais et ne bloque jamais l'appelant."""
    if not flags.env_enabled():
        return False
    try:
        from app.kb.service import default_service

        return get_queue().submit(f"ticket:{ticket_id}", lambda: default_service().run(ticket_id))
    except Exception:
        logger.exception("Could not schedule KB enrichment for ticket %s", ticket_id)
        return False


def live_search(question: str, timeout: float = 120.0) -> list[tuple[str, str, str]]:
    """Recherche KB à la demande, via l'unique worker (le profil Chromium ne se partage pas).

    Attend au plus `timeout` secondes; retourne [] si la fonction est coupée, la file saturée ou le délai dépassé.
    """
    if not flags.env_enabled():
        return []
    import concurrent.futures

    result: concurrent.futures.Future = concurrent.futures.Future()

    def task() -> None:
        try:
            from app.kb.service import default_service

            result.set_result(default_service().search_live(question))
        except Exception as exc:
            result.set_exception(exc)

    try:
        if not get_queue().submit(f"live:{threading.get_ident()}:{time.monotonic()}", task):
            return []
        return result.result(timeout=timeout)
    except Exception:
        logger.warning("KB live search unavailable", exc_info=True)
        return []


def enqueue_reindex() -> bool:
    """Planifie la réindexation des articles KB après un changement de modèle d'embedding."""
    if not flags.env_enabled():
        return False
    try:
        from app.kb.service import default_service

        return get_queue().submit("reindex", lambda: default_service().run_reindex())
    except Exception:
        logger.exception("Could not schedule KB reindexing")
        return False


def recover_interrupted_jobs(session_factory) -> int:
    """Au démarrage, marque en échec les jobs restés `running` après un arrêt brutal."""
    from app.models import KBJob

    try:
        with session_factory() as db:
            result = db.execute(
                update(KBJob).where(KBJob.status == "running").values(
                    status="failed", error_code="interrupted", error="Interrompu par un redémarrage de l'API.",
                )
            )
            db.commit()
            return result.rowcount or 0
    except Exception:
        logger.exception("Could not recover interrupted KB jobs")
        return 0
