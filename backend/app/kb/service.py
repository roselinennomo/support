"""Orchestration de l'enrichissement KB : amont (requête), milieu (scraper), aval (index + note agent).

Deux sources complémentaires alimentent le même index KB (jamais mélangé au corpus documentaire) :
  - « kb »       : articles du Technical Knowledge Center;
  - « document » : fiches et fichiers joints des Business Documents (guides, présentations, packs…).

Toutes les dépendances sont injectées (scraper, générateur de requête, index, session de base) :
le service est entièrement testable sans navigateur, sans LLM et sans Qdrant. `run()` ne lève jamais.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app import rag
from app.config import settings
from app.kb import flags
from app.kb.archive import KBArchive
from app.kb.cleaning import canonical_url, clean_article_html, content_hash
from app.kb.interfaces import (
    KBDocument,
    KBIndex,
    KBScrapeError,
    KBScraper,
    KBSearchHit,
    KBSessionExpired,
    KBUnavailable,
    QueryBuilder,
    SessionState,
    TicketSnapshot,
)
from app.models import KBArticle, KBJob, Message, Ticket

logger = logging.getLogger(__name__)
MIN_ARTICLE_CHARS = 80  # en dessous, la page extraite est considérée vide (mauvais sélecteur)
SOURCE_KB = "kb"
SOURCE_DOCUMENT = "document"
SOURCE_LABELS = {SOURCE_KB: "Articles de la base de connaissances", SOURCE_DOCUMENT: "Documents constructeur"}


@dataclass
class Fetched:
    """Contenu lu sur le portail : texte nettoyé et, pour un document, son fichier original."""

    title: str
    text: str
    file_name: str = ""
    file_bytes: bytes | None = None


@dataclass
class JobOutcome:
    status: str = "success"  # success | failed | skipped
    query: str = ""
    query_source: str = ""
    hits: int = 0
    new_documents: int = 0
    skipped_documents: int = 0
    urls: list[str] = field(default_factory=list)
    error_code: str = ""
    error: str = ""
    failed_selector: str = ""
    article_errors: int = 0
    references: list[tuple[str, str, str]] = field(default_factory=list)  # (source, titre, url)
    results: list[dict] = field(default_factory=list)  # journal par résultat, pour la console admin

    def fail(self, code: str, message: str, selector: str = "") -> None:
        self.status, self.error_code, self.error = "failed", code, message[:1000]
        if selector:
            self.failed_selector = selector[:500]

    def skip(self, code: str, message: str = "") -> None:
        self.status, self.error_code, self.error = "skipped", code, message[:1000]

    def add_result(self, source: str, title: str, url: str, status: str, detail: str = "") -> None:
        self.results.append({"source": source, "title": title[:200], "url": url, "status": status, "detail": detail[:300]})


def title_key(title: str) -> str:
    """Titre normalisé (minuscules, sans ponctuation) : détecte la même fiche publiée sous plusieurs URLs."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def excluded_type(meta: str) -> str:
    """Type commercial écarté (grilles de prix, RFP…) présent dans le texte de la ligne, sinon chaîne vide."""
    lowered = meta.lower()
    return next((item for item in _excluded_types() if item in lowered), "")


def _excluded_types() -> list[str]:
    return [item.strip().lower() for item in settings.kb_docs_exclude_types.split(",") if item.strip()]


def shorter_query(text: str, size: int = 2) -> str:
    """Requête plus courte (les mots les plus longs) pour les listes dont le filtre exige tous les mots."""
    words = [word for word in re.findall(r"[A-Za-z0-9][\w.-]*", text) if word.lower() not in {"not", "no", "the", "of"}]
    chosen = set(sorted(dict.fromkeys(words), key=len, reverse=True)[:size])
    return " ".join(word for word in words if word in chosen)


class KBEnrichmentService:
    def __init__(
        self,
        scraper: KBScraper,
        query_builder: QueryBuilder,
        index: KBIndex,
        session_factory,
        max_articles: int | None = None,
        refetch_after_hours: int | None = None,
        max_documents: int | None = None,
        archive: KBArchive | None = None,
    ):
        self._archive = archive
        self._scraper = scraper
        self._query_builder = query_builder
        self._index = index
        self._session_factory = session_factory
        self._max_articles = max_articles or settings.kb_max_articles
        self._refetch_after = timedelta(hours=refetch_after_hours or settings.kb_refetch_after_hours)
        self._max_documents = settings.kb_docs_max_results if max_documents is None else max_documents

    # ------------------------------------------------------------------ point d'entrée
    def run(self, ticket_id: str) -> str:
        """Enrichit un ticket; retourne le statut final du job (success | failed | skipped)."""
        job_id = self._start_job(ticket_id, "enrich")
        started = time.perf_counter()
        outcome = JobOutcome()
        try:
            self._execute(ticket_id, outcome)
        except KBSessionExpired as exc:
            outcome.fail("session_expired", str(exc))
            self._record_session(SessionState.EXPIRED, f"Session KB expirée : {exc}")
        except KBUnavailable as exc:
            outcome.skip("unavailable", str(exc))
        except KBScrapeError as exc:
            outcome.fail("scrape_failed", str(exc), selector=exc.selector)
            if exc.url:
                outcome.urls.append(exc.url)
        except Exception as exc:  # le parcours ticket ne doit jamais être affecté
            logger.exception("Unexpected KB enrichment failure for ticket %s", ticket_id)
            outcome.fail("unexpected", f"{type(exc).__name__}: {exc}")
        finally:
            self._finish_job(job_id, outcome, round((time.perf_counter() - started) * 1000))
        return outcome.status

    def search_live(self, question: str) -> list[tuple[str, str, str]]:
        """Recherche à la demande (console d'aide, sans ticket) : lit et indexe les éléments trouvés.

        Retourne les références (source, titre, url); liste vide si la fonction est désactivée, le navigateur
        injoignable, la session expirée ou la requête inexploitable. Ne lève jamais.
        """
        outcome = JobOutcome()
        try:
            if not flags.is_enabled(self._session_factory) or not self._scraper.health():
                return []
            if self._scraper.session_state() is SessionState.EXPIRED:
                self._record_session(SessionState.EXPIRED, "Session KB expirée : redirection vers la connexion/MFA.")
                return []
            snapshot = TicketSnapshot(
                ticket_id="", title=question[:300], description="", product="", version="",
                ai_preferences=None, customer_id="",
            )
            query = self._query_builder.build(snapshot)
            if not query.text.strip():
                return []
            for hit in self._scraper.search(query.text, self._max_articles)[: self._max_articles]:
                self._process_item(SOURCE_KB, hit.url, hit.title, self._fetch_article, outcome)
            if settings.kb_docs_enabled and self._max_documents > 0:
                self._collect_documents(query.text, outcome)
            self._record_session(SessionState.VALID)
        except Exception:
            logger.warning("KB live search failed", exc_info=True)
        return list(dict.fromkeys(outcome.references))

    def run_reindex(self) -> int:
        """Réindexe les articles conservés en base pour le modèle d'embedding actif (sans scraper)."""
        job_id = self._start_job(None, "reindex")
        started = time.perf_counter()
        outcome = JobOutcome()
        try:
            model = rag.embedding_model()
            with self._session_factory() as db:
                stale = db.scalars(
                    select(KBArticle).where(KBArticle.indexed_model != model, KBArticle.content != "")
                ).all()
                for article in stale:
                    source = article.source or SOURCE_KB
                    try:
                        article.chunk_count = self._index.upsert(
                            article.url, article.title, article.content, article.content_hash, source,
                        )
                        article.indexed_model = model
                        outcome.new_documents += 1
                        outcome.add_result(source, article.title, article.url, "new", "Réindexé")
                    except Exception as exc:
                        outcome.article_errors += 1
                        outcome.error = f"{article.url}: {exc}"[:1000]
                        outcome.add_result(source, article.title, article.url, "failed", str(exc))
                        logger.warning("KB reindex failed for %s: %s", article.url, exc)
                db.commit()
            if outcome.article_errors and not outcome.new_documents:
                outcome.fail("reindex_failed", outcome.error)
        except Exception as exc:
            logger.exception("Unexpected KB reindex failure")
            outcome.fail("unexpected", f"{type(exc).__name__}: {exc}")
        finally:
            self._finish_job(job_id, outcome, round((time.perf_counter() - started) * 1000))
        return outcome.new_documents

    # ------------------------------------------------------------------ pipeline
    def _execute(self, ticket_id: str, outcome: JobOutcome) -> None:
        if not flags.is_enabled(self._session_factory):
            return outcome.skip("disabled", "Fonctionnalité désactivée.")
        snapshot = self._load_snapshot(ticket_id, outcome)
        if snapshot is None:
            return
        if not self._scraper.health():
            return outcome.skip("unavailable", "Le navigateur KB ne répond pas.")
        if self._scraper.session_state() is SessionState.EXPIRED:
            raise KBSessionExpired("Redirection vers la page de connexion/MFA détectée.")

        query = self._query_builder.build(snapshot)
        outcome.query, outcome.query_source = query.text, query.source
        if not query.text.strip():
            return outcome.skip("empty_query", "Aucune requête exploitable pour ce ticket.")

        hits = self._scraper.search(query.text, self._max_articles)[: self._max_articles]
        outcome.hits += len(hits)
        for hit in hits:
            self._process_item(SOURCE_KB, hit.url, hit.title, self._fetch_article, outcome)
        if settings.kb_docs_enabled and self._max_documents > 0:
            self._collect_documents(query.text, outcome)

        if outcome.hits and outcome.article_errors and not (outcome.new_documents or outcome.skipped_documents):
            outcome.fail("scrape_failed", outcome.error or "Aucun article n'a pu être extrait.")
        self._record_session(SessionState.VALID)
        if outcome.references:
            self._add_ticket_note(ticket_id, query.text, outcome.references)

    def _load_snapshot(self, ticket_id: str, outcome: JobOutcome) -> TicketSnapshot | None:
        with self._session_factory() as db:
            ticket = db.get(Ticket, ticket_id)
            if not ticket:
                outcome.skip("ticket_not_found", "Ticket introuvable.")
                return None
            if not ticket.customer.ai_processing_consent:
                outcome.skip("no_consent", "Le client n'a pas consenti au traitement IA.")
                return None
            return TicketSnapshot(
                ticket_id=ticket.id,
                title=ticket.title,
                description=ticket.description,
                product=ticket.product,
                version=ticket.version,
                ai_preferences=ticket.ai_preferences,
                customer_id=ticket.customer_id,
            )

    # ------------------------------------------------------------------ source « documents »
    def _collect_documents(self, query: str, outcome: JobOutcome) -> None:
        """Recherche dans la liste de documents, écarte les documents commerciaux, puis ingère les meilleurs."""
        candidates_wanted = self._max_documents * 3  # marge : certains seront écartés (prix, RFP…)
        try:
            candidates = self._scraper.search_documents(query, candidates_wanted)
            if not candidates and len(query.split()) > 2:
                # Le filtre de liste exige souvent tous les mots : on retente avec les plus distinctifs.
                candidates = self._scraper.search_documents(shorter_query(query), candidates_wanted)
        except KBSessionExpired:
            raise
        except (KBScrapeError, KBUnavailable) as exc:
            logger.warning("KB documents search skipped: %s", exc)
            outcome.add_result(SOURCE_DOCUMENT, "Recherche de documents", "", "failed", str(exc))
            return
        if not candidates:
            outcome.add_result(SOURCE_DOCUMENT, "Aucun document trouvé", "", "empty", f"La liste de documents n'a rien renvoyé pour « {query} »")
        kept: list[KBSearchHit] = []
        for hit in candidates:
            reason = excluded_type(hit.meta)
            if reason:
                outcome.add_result(
                    SOURCE_DOCUMENT, hit.title, canonical_url(hit.url), "filtered", f"Type commercial écarté ({reason})",
                )
            elif len(kept) < self._max_documents:
                kept.append(hit)
        outcome.hits += len(kept)
        for hit in kept:
            self._process_item(SOURCE_DOCUMENT, hit.url, hit.title, self._fetch_document, outcome)

    # ------------------------------------------------------------------ lecture des contenus (par source)
    def _fetch_article(self, url: str) -> Fetched:
        article = self._scraper.fetch_article(url)
        return Fetched(article.title, clean_article_html(article.html))

    def _fetch_document(self, url: str) -> Fetched:
        document: KBDocument = self._scraper.fetch_document(url, include_file=self._archive is not None)
        parts = [_plain(document.metadata)]
        if document.file_text.strip():
            parts.append(f"--- Contenu du fichier « {document.file_name} » ---\n{document.file_text.strip()}")
        return Fetched(document.title, "\n\n".join(part for part in parts if part), document.file_name, document.file_bytes)

    # ------------------------------------------------------------------ ingestion commune avec déduplication
    def _process_item(
        self,
        source: str,
        raw_url: str,
        hit_title: str,
        fetch: Callable[[str], Fetched],
        outcome: JobOutcome,
    ) -> None:
        """Lit un élément et l'indexe sauf s'il est déjà connu (même URL, même contenu ou même titre)."""
        url = canonical_url(raw_url)
        outcome.urls.append(url)
        model = rag.embedding_model()
        now = datetime.now(timezone.utc)
        with self._session_factory() as db:
            known = db.scalar(select(KBArticle).where(KBArticle.url == url))
            if known and known.indexed_model == model and _aware(known.last_fetched_at) > now - self._refetch_after:
                outcome.skipped_documents += 1  # déduplication : inutile de re-scraper
                outcome.references.append((known.source or source, known.title or hit_title or url, url))
                outcome.add_result(source, known.title or hit_title, url, "known", "Déjà indexé récemment (même URL)")
                return
        try:
            fetched = fetch(url)
            fetched_title, text = fetched.title, fetched.text
        except KBScrapeError as exc:
            self._article_error(outcome, source, hit_title, url, f"{url}: {exc}", exc.selector)
            return
        if len(text) < MIN_ARTICLE_CHARS:
            self._article_error(outcome, source, hit_title, url, f"{url}: contenu vide après nettoyage", "")
            return
        digest = content_hash(text)
        title = (hit_title or fetched_title or url)[:500]
        with self._session_factory() as db:
            known = db.scalar(select(KBArticle).where(KBArticle.url == url))
            if known and known.content_hash == digest and known.indexed_model == model:
                known.last_fetched_at = now
                db.commit()
                outcome.skipped_documents += 1
                outcome.references.append((known.source or source, title, url))
                outcome.add_result(source, title, url, "known", "Contenu inchangé depuis la dernière lecture")
                return
            twin = None if known else self._find_twin(db, url, digest, title, model)
            if twin:
                outcome.skipped_documents += 1
                outcome.references.append((twin[0].source or source, twin[0].title, twin[0].url))
                outcome.add_result(source, title, url, "duplicate", twin[1])
                return
            try:
                chunks = self._index.upsert(url, title, text, digest, source)
            except Exception as exc:  # indexation (Qdrant/Ollama) en panne : on continue sans bloquer
                logger.warning("KB indexing failed for %s: %s", url, exc)
                self._article_error(outcome, source, title, url, f"{url}: indexation impossible ({exc})", "")
                return
            first_seen = known.first_seen_at if known else now
            if known:
                known.title, known.content, known.content_hash, known.source = title, text, digest, source
                known.chunk_count, known.indexed_model, known.last_fetched_at = chunks, model, now
            else:
                db.add(KBArticle(
                    url=url, title=title, content=text, content_hash=digest, source=source,
                    chunk_count=chunks, indexed_model=model,
                ))
            db.commit()
        outcome.new_documents += 1
        outcome.references.append((source, title, url))
        archived = self._archive_item(source, url, title, text, digest, fetched, first_seen)
        outcome.add_result(source, title, url, "new", f"Indexé ({chunks} extrait{'s' if chunks > 1 else ''}){archived}")

    def _archive_item(self, source: str, url: str, title: str, text: str, digest: str, fetched: Fetched, first_seen) -> str:
        """Copie locale (fichier original + texte). Une panne d'archive ne doit jamais faire échouer la recherche."""
        if self._archive is None:
            return ""
        try:
            saved = self._archive.save_item(
                url=url, title=title, source=source, content=text, content_hash=digest,
                file_name=fetched.file_name, file_bytes=fetched.file_bytes, first_seen_at=first_seen,
            )
        except Exception:
            logger.exception("KB archive write failed for %s", url)
            return " · archive impossible"
        return {
            "stored": " · archivé avec son fichier original",
            "reused": " · archivé (fichier déjà présent)",
            "skipped_cap": " · archivé sans le fichier (archive pleine)",
        }.get(saved["file"], " · archivé")

    @staticmethod
    def _find_twin(db, url: str, digest: str, title: str, model: str) -> tuple[KBArticle, str] | None:
        """Élément déjà indexé sous une autre URL : même contenu, ou même titre (fiche publiée plusieurs fois)."""
        same_content = db.scalar(select(KBArticle).where(
            KBArticle.content_hash == digest, KBArticle.url != url, KBArticle.indexed_model == model,
        ))
        if same_content:
            return same_content, f"Contenu identique à « {same_content.title[:80]} »"
        key = title_key(title)
        if key:
            for other in db.scalars(select(KBArticle).where(KBArticle.url != url, KBArticle.indexed_model == model)):
                if title_key(other.title) == key:
                    return other, f"Même titre que « {other.title[:80]} » déjà indexé"
        return None

    @staticmethod
    def _article_error(outcome: JobOutcome, source: str, title: str, url: str, message: str, selector: str) -> None:
        outcome.article_errors += 1
        outcome.error = message[:1000]
        if selector:
            outcome.failed_selector = selector[:500]
        outcome.add_result(source, title, url, "failed", message.split(": ", 1)[-1])

    # ------------------------------------------------------------------ persistance
    def _start_job(self, ticket_id: str | None, kind: str) -> str | None:
        try:
            with self._session_factory() as db:
                job = KBJob(ticket_id=ticket_id, kind=kind, status="running")
                db.add(job)
                db.commit()
                return job.id
        except Exception:  # pragma: no cover - la télémétrie ne doit pas bloquer
            logger.exception("Could not record the KB job start")
            return None

    def _finish_job(self, job_id: str | None, outcome: JobOutcome, duration_ms: int) -> None:
        if not job_id:
            return
        try:
            with self._session_factory() as db:
                job = db.get(KBJob, job_id)
                if not job:
                    return
                job.status, job.query, job.query_source = outcome.status, outcome.query, outcome.query_source
                job.hits, job.new_documents, job.skipped_documents = (
                    outcome.hits, outcome.new_documents, outcome.skipped_documents,
                )
                job.urls, job.error_code, job.error = outcome.urls, outcome.error_code, outcome.error
                job.results = outcome.results
                job.failed_selector, job.duration_ms = outcome.failed_selector, duration_ms
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:  # pragma: no cover
            logger.exception("Could not record the KB job result")

    def _record_session(self, state: SessionState, alert: str = "") -> None:
        try:
            with self._session_factory() as db:
                flags.record_session_state(db, state.value, alert)
        except Exception:  # pragma: no cover
            logger.exception("Could not record the KB session state")

    def _add_ticket_note(self, ticket_id: str, query: str, references: list[tuple[str, str, str]]) -> None:
        """Note interne (agents uniquement) listant les articles et documents KB pertinents, sans doublon."""
        try:
            from app.tickets import _assistant_user

            lines = [f"Éléments de la base constructeur potentiellement utiles (recherche : « {query} ») :"]
            for source in (SOURCE_KB, SOURCE_DOCUMENT):
                items = list(dict.fromkeys((title, url) for src, title, url in references if src == source))
                if items:
                    lines.append(f"\n**{SOURCE_LABELS[source]}**")
                    lines.extend(f"- [{title}]({url})" for title, url in items)
            with self._session_factory() as db:
                ticket = db.get(Ticket, ticket_id)
                if not ticket:
                    return
                db.add(Message(
                    ticket_id=ticket.id, author_id=_assistant_user(db).id, body="\n".join(lines), visibility="internal",
                ))
                db.commit()
        except Exception:
            logger.exception("Could not add the KB note to ticket %s", ticket_id)


def _plain(text: str) -> str:
    """Texte brut normalisé (le scraper renvoie le texte rendu de la fiche, pas du HTML)."""
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def compute_metrics(db) -> dict:
    """Métriques de la console admin, calculées à partir du journal des jobs."""
    executed = KBJob.status.in_(("success", "failed"))
    enrich = KBJob.kind == "enrich"
    success = db.scalar(select(func.count()).select_from(KBJob).where(enrich, KBJob.status == "success")) or 0
    failed = db.scalar(select(func.count()).select_from(KBJob).where(enrich, KBJob.status == "failed")) or 0
    skipped = db.scalar(select(func.count()).select_from(KBJob).where(enrich, KBJob.status == "skipped")) or 0
    searches = db.scalar(
        select(func.count()).select_from(KBJob).where(enrich, executed, KBJob.query != "")
    ) or 0
    average = db.scalar(select(func.avg(KBJob.duration_ms)).where(enrich, executed))
    indexed = db.scalar(select(func.coalesce(func.sum(KBJob.new_documents), 0)).where(enrich)) or 0
    by_source = dict(db.execute(select(KBArticle.source, func.count()).group_by(KBArticle.source)).all())
    attempts = success + failed
    return {
        "searches": searches,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "success_rate": round(success / attempts * 100, 1) if attempts else None,
        "average_duration_ms": round(average) if average is not None else None,
        "documents_indexed": int(indexed),
        "articles_known": sum(by_source.values()),
        "known_by_source": {SOURCE_KB: by_source.get(SOURCE_KB, 0), SOURCE_DOCUMENT: by_source.get(SOURCE_DOCUMENT, 0)},
    }


def default_service() -> KBEnrichmentService:
    from app.database import SessionLocal
    from app.kb.client import default_scraper
    from app.kb.index import QdrantKBIndex
    from app.kb.query import default_query_builder

    return KBEnrichmentService(
        default_scraper(), default_query_builder(), QdrantKBIndex(), SessionLocal, archive=KBArchive.from_settings(),
    )
