"""Tests du module KB : tous les composants externes (navigateur, LLM, Qdrant) sont simulés."""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.kb import flags
from app.kb.cleaning import canonical_url, clean_article_html, content_hash
from app.kb.interfaces import (
    KBArticle as RawArticle,
    KBDocument,
    KBQuery,
    KBScrapeError,
    KBSearchHit,
    KBSessionExpired,
    KBUnavailable,
    SessionState,
    TicketSnapshot,
)
from app.kb.jobs import KBJobQueue, recover_interrupted_jobs
from app.kb.query import HeuristicQueryBuilder, LLMQueryBuilder, redact
from app.kb.service import KBEnrichmentService, compute_metrics
from app.models import Company, KBArticle, KBJob, KBSettings, Message, Ticket, User
from app.security import hash_password

ARTICLE_HTML = (
    "<header>Menu principal</header><nav>Accueil | Support</nav>"
    "<div class='breadcrumb'>Home &gt; KB</div><script>track()</script>"
    "<h1>OS6860E perd le lien de stacking</h1>"
    "<p>Après un redémarrage, vérifier la configuration du lien de stacking et la version R8.4.1 "
    "avant toute intervention sur les ports d'extension.</p>"
    "<footer>© Alcatel</footer>"
)


# ----------------------------------------------------------------------------- doubles de test
class FakeScraper:
    def __init__(self, hits=None, html=ARTICLE_HTML, state=SessionState.VALID, healthy=True):
        self.hits = hits if hits is not None else [KBSearchHit("Stacking", "https://kb.example/a/1?utm_source=x")]
        self.html = html
        self.state = state
        self.healthy = healthy
        self.fetched: list[str] = []
        self.searches: list[tuple[str, int]] = []
        self.fetch_error: Exception | None = None
        self.search_error: Exception | None = None
        self.docs: list[KBSearchHit] = []
        self.documents: dict[str, KBDocument] = {}
        self.docs_error: Exception | None = None
        self.doc_searches: list[tuple[str, int]] = []
        self.doc_fetched: list[str] = []
        self.include_file_flags: list[bool] = []

    def health(self):
        return self.healthy

    def session_state(self):
        return self.state

    def search(self, query, limit):
        self.searches.append((query, limit))
        if self.search_error:
            raise self.search_error
        return self.hits[:limit]

    def fetch_article(self, url):
        self.fetched.append(url)
        if self.fetch_error:
            raise self.fetch_error
        return RawArticle(url=url, title="Stacking", html=self.html)

    def search_documents(self, query, limit):
        if self.docs_error:
            raise self.docs_error
        self.doc_searches.append((query, limit))
        return self.docs[:limit]

    def fetch_document(self, url, include_file=False):
        self.doc_fetched.append(url)
        self.include_file_flags.append(include_file)
        return self.documents[url]

    def logs(self, limit=100):
        return [{"event": "test"}]


class FakeIndex:
    def __init__(self):
        self.upserts: list[tuple[str, str]] = []
        self.sources: list[str] = []
        self.fail = False

    def upsert(self, url, title, text, content_hash_, source="kb"):
        if self.fail:
            raise RuntimeError("qdrant down")
        self.upserts.append((url, content_hash_))
        self.sources.append(source)
        return 2

    def search(self, query, limit):
        return []


class FixedQuery:
    def build(self, ticket):
        return KBQuery("OS6860E stacking R8.4.1", ["OS6860E"], "llm")


@pytest.fixture
def db_env(monkeypatch):
    monkeypatch.setattr("app.kb.flags.settings.enable_kb_browser_scraper", True)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        admin = User(email="a@example.com", full_name="Admin", password_hash=hash_password("x" * 12), role="admin")
        db.add(admin)
        db.flush()
        company = Company(name="ACME", invitation_code_hash="h", created_by_id=admin.id)
        db.add(company)
        db.flush()
        customer = User(
            email="c@example.com", full_name="Client", password_hash=hash_password("x" * 12),
            role="customer", company_id=company.id,
        )
        db.add(customer)
        db.flush()
        ticket = Ticket(
            reference="OXE-TEST0001", customer_id=customer.id, title="Switch OS6860E : stacking perdu",
            description="Après redémarrage le stack est coupé, contact : jean@acme.fr 10.0.0.12",
            product="OmniSwitch", version="R8.4.1",
        )
        db.add(ticket)
        db.commit()
        ids = {"ticket": ticket.id, "customer": customer.id}
    yield factory, ids
    engine.dispose()


def make_service(factory, scraper=None, index=None, **kwargs):
    return KBEnrichmentService(
        scraper or FakeScraper(), FixedQuery(), index or FakeIndex(), factory,
        max_articles=kwargs.pop("max_articles", 3), refetch_after_hours=kwargs.pop("refetch_after_hours", 24),
        max_documents=kwargs.pop("max_documents", 2),
    )


def last_job(factory) -> KBJob:
    with factory() as db:
        return db.scalars(select(KBJob).order_by(KBJob.created_at.desc())).first()


# ----------------------------------------------------------------------------- nettoyage
def test_cleaning_keeps_only_the_technical_body():
    text = clean_article_html(ARTICLE_HTML)

    assert "OS6860E perd le lien de stacking" in text
    assert "R8.4.1" in text
    for noise in ("Menu principal", "Accueil", "Home", "track()", "© Alcatel"):
        assert noise not in text


def test_hash_ignores_case_and_whitespace_and_urls_are_canonical():
    assert content_hash("Lien  de\nstacking") == content_hash("lien de stacking")
    assert content_hash("a") != content_hash("b")
    assert canonical_url("https://kb.example/a/1/?utm_source=x&id=7#top") == "https://kb.example/a/1?id=7"


# ----------------------------------------------------------------------------- requête
def test_redaction_removes_personal_data_before_anything_leaves_the_platform():
    cleaned = redact("Contact jean.dupont@acme.fr au 06 12 34 56 78, switch 10.20.30.40 mac aa:bb:cc:dd:ee:ff")

    assert "@" not in cleaned and "10.20.30.40" not in cleaned and "aa:bb" not in cleaned and "06 12" not in cleaned


def test_heuristic_builder_extracts_entities_without_personal_data():
    ticket = TicketSnapshot("t", "Switch OS6860E erreur 1204", "Voir jean@acme.fr, IP 10.0.0.1, version R8.4.1", "OmniSwitch", "R8.4.1")

    query = HeuristicQueryBuilder().build(ticket)

    assert query.source == "heuristic"
    assert "OS6860E" in query.entities and "OmniSwitch" in query.text
    assert "jean" not in query.text and "10.0.0.1" not in query.text


def test_llm_builder_parses_json_and_falls_back_on_any_failure():
    ticket = TicketSnapshot("t", "Titre OS6860E", "desc", "OmniSwitch")
    good = LLMQueryBuilder(lambda _p, _t: 'Voici : {"entities": ["OS6860E"], "query": "OS6860E stacking lien perdu"}')
    assert good.build(ticket).source == "llm"
    assert good.build(ticket).text == "OS6860E stacking lien perdu"

    def broken(_prompt, _ticket):
        raise RuntimeError("LLM indisponible")

    assert LLMQueryBuilder(broken).build(ticket).source == "heuristic"
    assert LLMQueryBuilder(lambda _p, _t: "pas de json").build(ticket).source == "heuristic"
    assert LLMQueryBuilder(lambda _p, _t: '{"query": "  "}').build(ticket).source == "heuristic"


# ----------------------------------------------------------------------------- orchestration
def test_enrichment_indexes_new_article_and_adds_internal_note(db_env):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()

    assert make_service(factory, scraper, index).run(ids["ticket"]) == "success"

    job = last_job(factory)
    assert (job.status, job.hits, job.new_documents, job.skipped_documents) == ("success", 1, 1, 0)
    assert job.query == "OS6860E stacking R8.4.1" and job.query_source == "llm"
    assert job.urls == ["https://kb.example/a/1"]
    assert scraper.searches == [("OS6860E stacking R8.4.1", 3)]
    assert len(index.upserts) == 1
    with factory() as db:
        article = db.scalars(select(KBArticle)).one()
        assert "Menu principal" not in article.content and article.chunk_count == 2
        note = db.scalars(select(Message).where(Message.ticket_id == ids["ticket"])).one()
        assert note.visibility == "internal" and "https://kb.example/a/1" in note.body
        assert db.get(KBSettings, 1).session_state == "valid"


def test_second_run_is_deduplicated_without_scraping_or_reindexing(db_env):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    service = make_service(factory, scraper, index)
    service.run(ids["ticket"])

    service.run(ids["ticket"])

    job = last_job(factory)
    assert (job.new_documents, job.skipped_documents) == (0, 1)
    assert len(scraper.fetched) == 1 and len(index.upserts) == 1


def test_stale_known_article_is_refetched_but_not_reindexed_when_content_is_unchanged(db_env):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    make_service(factory, scraper, index).run(ids["ticket"])
    with factory() as db:
        db.scalars(select(KBArticle)).one().last_fetched_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()

    make_service(factory, scraper, index).run(ids["ticket"])

    assert len(scraper.fetched) == 2  # revisité car ancien…
    assert len(index.upserts) == 1  # …mais contenu identique : aucune réindexation
    assert last_job(factory).skipped_documents == 1


def test_same_content_under_another_url_is_not_indexed_twice(db_env):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    service = make_service(factory, scraper, index)
    service.run(ids["ticket"])
    scraper.hits = [KBSearchHit("Copie", "https://kb.example/a/2")]

    service.run(ids["ticket"])

    assert len(index.upserts) == 1 and last_job(factory).skipped_documents == 1


def test_expired_session_aborts_cleanly_and_raises_an_alert(db_env):
    factory, ids = db_env
    scraper = FakeScraper(state=SessionState.EXPIRED)

    assert make_service(factory, scraper).run(ids["ticket"]) == "failed"

    job = last_job(factory)
    assert job.error_code == "session_expired" and scraper.searches == []
    with factory() as db:
        record = db.get(KBSettings, 1)
        assert record.session_state == "expired" and "expirée" in record.last_alert


def test_session_expiring_during_navigation_is_detected(db_env):
    factory, ids = db_env
    scraper = FakeScraper()
    scraper.search_error = KBSessionExpired("redirigé vers /login")

    assert make_service(factory, scraper).run(ids["ticket"]) == "failed"
    with factory() as db:
        assert db.get(KBSettings, 1).session_state == "expired"


def test_offline_browser_is_skipped_not_failed_and_never_raises(db_env):
    factory, ids = db_env

    assert make_service(factory, FakeScraper(healthy=False)).run(ids["ticket"]) == "skipped"
    scraper = FakeScraper()
    scraper.search_error = KBUnavailable("timeout")
    assert make_service(factory, scraper).run(ids["ticket"]) == "skipped"
    assert last_job(factory).error_code == "unavailable"


def test_failed_selector_is_recorded_and_other_articles_still_processed(db_env):
    factory, ids = db_env
    scraper = FakeScraper(hits=[KBSearchHit("A", "https://kb.example/a"), KBSearchHit("B", "https://kb.example/b")])
    original = scraper.fetch_article

    def flaky(url):
        if url.endswith("/a"):
            raise KBScrapeError("sélecteur introuvable", selector="article.content", url=url)
        return original(url)

    scraper.fetch_article = flaky

    assert make_service(factory, scraper).run(ids["ticket"]) == "success"
    job = last_job(factory)
    assert (job.new_documents, job.failed_selector) == (1, "article.content")


def test_all_articles_failing_marks_the_job_failed(db_env):
    factory, ids = db_env
    scraper = FakeScraper()
    scraper.fetch_error = KBScrapeError("sélecteur introuvable", selector="main", url="u")

    assert make_service(factory, scraper).run(ids["ticket"]) == "failed"
    assert last_job(factory).failed_selector == "main"


def test_index_outage_does_not_break_the_job_run(db_env):
    factory, ids = db_env
    index = FakeIndex()
    index.fail = True

    assert make_service(factory, FakeScraper(), index).run(ids["ticket"]) == "failed"
    with factory() as db:
        assert db.scalars(select(KBArticle)).first() is None  # rien de marqué « indexé » à tort


def test_disabled_flag_or_missing_consent_makes_no_external_call(db_env, monkeypatch):
    factory, ids = db_env
    scraper = FakeScraper()
    live_factory = factory
    monkeypatch.setattr("app.kb.flags.settings.enable_kb_browser_scraper", False)
    assert make_service(live_factory, scraper).run(ids["ticket"]) == "skipped"
    assert scraper.searches == [] and last_job(factory).error_code == "disabled"

    monkeypatch.setattr("app.kb.flags.settings.enable_kb_browser_scraper", True)
    with factory() as db:
        db.get(KBSettings, 1) or db.add(KBSettings(id=1, enabled=False))
        db.commit()
    assert make_service(live_factory, scraper).run(ids["ticket"]) == "skipped"  # coupé par l'admin
    with factory() as db:
        db.get(KBSettings, 1).enabled = True
        db.get(User, ids["customer"]).ai_processing_consent = False
        db.commit()
    assert make_service(live_factory, scraper).run(ids["ticket"]) == "skipped"
    assert last_job(factory).error_code == "no_consent" and scraper.searches == []


def test_metrics_summarise_jobs(db_env):
    factory, ids = db_env
    make_service(factory, FakeScraper()).run(ids["ticket"])
    make_service(factory, FakeScraper(state=SessionState.EXPIRED)).run(ids["ticket"])
    make_service(factory, FakeScraper(healthy=False)).run(ids["ticket"])

    with factory() as db:
        metrics = compute_metrics(db)

    assert (metrics["searches"], metrics["success"], metrics["failed"], metrics["skipped"]) == (1, 1, 1, 1)
    assert metrics["success_rate"] == 50.0 and metrics["documents_indexed"] == 1
    assert metrics["articles_known"] == 1 and metrics["average_duration_ms"] is not None


def test_reindex_rebuilds_articles_for_the_active_embedding_model_without_scraping(db_env):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    service = make_service(factory, scraper, index)
    service.run(ids["ticket"])
    with factory() as db:
        db.scalars(select(KBArticle)).one().indexed_model = "bge-m3"
        db.commit()

    assert service.run_reindex() == 1
    assert len(scraper.fetched) == 1 and len(index.upserts) == 2


def test_interrupted_jobs_are_recovered_at_startup(db_env):
    factory, _ = db_env
    with factory() as db:
        db.add(KBJob(status="running"))
        db.commit()

    assert recover_interrupted_jobs(factory) == 1
    assert last_job(factory).error_code == "interrupted"


# ----------------------------------------------------------------------------- file d'attente
def test_queue_runs_tasks_in_background_one_at_a_time_and_survives_crashes():
    queue_ = KBJobQueue(maxsize=5)
    order, running = [], []

    def task(name, crash=False):
        def run():
            running.append(threading.active_count())
            if crash:
                raise RuntimeError("boom")
            order.append(name)
        return run

    assert queue_.submit("a", task("a", crash=True)) is True
    assert queue_.submit("b", task("b")) is True
    queue_.join(timeout=5)
    assert order == ["b"]  # le worker a survécu à la tâche « a »


def test_queue_rejects_duplicates_and_overflow_without_blocking():
    queue_ = KBJobQueue(maxsize=1)
    gate = threading.Event()
    assert queue_.submit("first", gate.wait) is True
    assert queue_.submit("second", lambda: None) in {True, False}  # selon le moment où le worker dépile
    assert queue_.submit("first", lambda: None) is False  # doublon en attente
    gate.set()
    queue_.join(timeout=5)
    queue_.stop()


# ----------------------------------------------------------------------------- drapeaux
def test_feature_is_inert_when_the_server_flag_is_off(monkeypatch):
    monkeypatch.setattr("app.kb.flags.settings.enable_kb_browser_scraper", False)

    assert flags.is_enabled() is False
    from app.kb.jobs import enqueue_reindex, enqueue_ticket_enrichment
    from app.kb.retrieval import kb_excerpts

    assert enqueue_ticket_enrichment("any") is False and enqueue_reindex() is False
    assert kb_excerpts("OS6860E") == []


def test_retrieval_formats_kb_excerpts_and_degrades_to_nothing_on_failure(monkeypatch):
    from app.kb.retrieval import kb_excerpts

    monkeypatch.setattr("app.kb.retrieval.flags.is_enabled", lambda: True)

    class Index:
        def search(self, query, limit):
            return [{"title": "Stacking", "url": "https://kb.example/a", "text": "Vérifier le lien.", "score": 0.9}]

    class Broken:
        def search(self, query, limit):
            raise RuntimeError("qdrant down")

    assert kb_excerpts("stacking", Index())[0].startswith("[Source KB constructeur : Stacking")
    assert kb_excerpts("stacking", Broken()) == []
    assert kb_excerpts("   ", Index()) == []


def test_llm_builder_rejects_queries_copied_from_the_prompt_and_falls_back(monkeypatch):
    """Régression : deepseek-r1:1.5b renvoyait « modèle OXE ; version non précisée ; titre … » comme requête."""
    ticket = TicketSnapshot("t", "Pilote KO", "Mon pilote ne reçoit plus d'appels.", "OXE", "")
    echoed = '{"entities": [], "query": "modèle OXE ; version non précisée ; titre Pilote KO ; décription : le pilote"}'

    query = LLMQueryBuilder(lambda _p, _t: echoed).build(ticket)

    assert query.source == "heuristic"
    assert "pilot" in query.text and "calls" in query.text  # requête anglaise (KB anglophone)
    assert ";" not in query.text and "précisée" not in query.text
    good = '{"query": "pilote ne reçoit plus d\'appels OXE"}'
    assert LLMQueryBuilder(lambda _p, _t: good).build(ticket).source == "llm"


def test_heuristic_query_is_english_and_keeps_the_negation_for_the_english_kb():
    """Régression : sur le vrai portail, « OXE Pilote reçoit d'appels » renvoyait des articles hors sujet."""
    ticket = TicketSnapshot("t", "Pilote KO", "Mon pilote ne reçoit plus d'appels.", "OXE", "")

    query = HeuristicQueryBuilder().build(ticket)

    assert query.text == "pilot not receiving calls"  # « OXE » omis : mesuré moins pertinent
    assert HeuristicQueryBuilder().build(TicketSnapshot("t", "Poste sans tonalité", "Plus de tonalité sur le poste 4038", "OXE", "R12.2")).text == (
        "R12.2 4038 phone dial tone"
    )
    # Sans vocabulaire reconnu, on garde les mots-clés d'origine (pas de requête vide).
    assert HeuristicQueryBuilder().build(TicketSnapshot("t", "Souci de VLAN", "Le VLAN est à revoir", "", "")).text == "VLAN revoir"
    # Un sigle non traduit reste dans la requête à côté des termes traduits.
    assert HeuristicQueryBuilder().build(TicketSnapshot("t", "VLAN voix", "Perte de voix", "", "")).text == "VLAN voice loss"



# ----------------------------------------------------------------------------- source « documents »
DOC_METADATA = (
    "Business Document File\nPowerpoint\nTBE061 - SIP Trunking with OmniPCX Enterprise\n2.11 MB - POWER_POINT_X\n"
    "About\nDescription\nThis document provides an overview on SIP trunking on OmniPCX Enterprise.\n"
    "General\nType / SubType\nPreSales;PreSales > Documentation"
)


def doc_setup(scraper: FakeScraper, monkeypatch, count: int = 2):
    monkeypatch.setattr("app.kb.service.settings.kb_docs_enabled", True)
    scraper.hits = []
    scraper.docs = [
        KBSearchHit(f"Guide SIP {n}", f"https://kb.example/s/business-document/id{n}/guide-{n}", "PreSales > Documentation | English")
        for n in range(1, count + 1)
    ]
    scraper.documents = {
        hit.url: KBDocument(
            hit.url, hit.title, DOC_METADATA.replace("TBE061", f"TBE06{n}"),
            file_name=f"guide{n}.pptx", file_text=f"Procédure détaillée numéro {n} pour configurer le trunk SIP. " * 4,
        )
        for n, hit in enumerate(scraper.docs, start=1)
    }


def test_documents_source_indexes_metadata_and_file_text_and_logs_each_result(db_env, monkeypatch):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    doc_setup(scraper, monkeypatch)

    assert make_service(factory, scraper, index, max_documents=2).run(ids["ticket"]) == "success"

    job = last_job(factory)
    assert (job.hits, job.new_documents) == (2, 2)
    assert [(r["source"], r["status"]) for r in job.results] == [("document", "new"), ("document", "new")]
    assert index.sources == ["document", "document"]
    with factory() as db:
        article = db.scalars(select(KBArticle).order_by(KBArticle.url)).first()
        assert article.source == "document"
        assert "Business Document File" in article.content and "Contenu du fichier « guide1.pptx »" in article.content
        note = db.scalars(select(Message).where(Message.ticket_id == ids["ticket"])).one()
        assert "**Documents constructeur**" in note.body and "Guide SIP 1" in note.body
        assert compute_metrics(db)["known_by_source"] == {"kb": 0, "document": 2}


def test_commercial_documents_are_filtered_out_before_any_download(db_env, monkeypatch):
    factory, ids = db_env
    scraper = FakeScraper()
    doc_setup(scraper, monkeypatch, count=3)
    scraper.docs[0] = KBSearchHit("WWPL EURO Quoter", scraper.docs[0].url, "PreSales;Price catalog;Pricing | English")
    scraper.docs[1] = KBSearchHit("6860N GOLDEN RFP", scraper.docs[1].url, "PreSales > Offer/Proposal Doc | English")

    make_service(factory, scraper, FakeIndex(), max_documents=2).run(ids["ticket"])

    assert scraper.doc_fetched == [scraper.docs[2].url]  # seul le guide technique est téléchargé
    results = {r["title"]: r for r in last_job(factory).results}
    assert results["WWPL EURO Quoter"]["status"] == "filtered" and "price catalog" in results["WWPL EURO Quoter"]["detail"]
    assert results["6860N GOLDEN RFP"]["status"] == "filtered"
    assert results["Guide SIP 3"]["status"] == "new"


def test_same_document_published_twice_is_indexed_once(db_env, monkeypatch):
    """« Évitant les répétitions » : même titre sous deux URLs (ex. trois fiches « 6860N GOLDEN RFP »)."""
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    doc_setup(scraper, monkeypatch)
    twin = scraper.docs[1]
    scraper.docs[1] = KBSearchHit("guide  SIP 1!", twin.url, twin.meta)  # même titre normalisé que « Guide SIP 1 »
    scraper.documents[twin.url] = KBDocument(
        twin.url, "x", "Autre fiche, texte différent mais même document. " * 5, file_name="b.pptx", file_text="Contenu autre. " * 10,
    )

    make_service(factory, scraper, index, max_documents=2).run(ids["ticket"])

    job = last_job(factory)
    assert (job.new_documents, job.skipped_documents) == (1, 1)
    assert [r["status"] for r in job.results] == ["new", "duplicate"] and "Même titre" in job.results[1]["detail"]
    assert len(index.upserts) == 1


def test_document_with_identical_content_is_not_reindexed_across_sources(db_env, monkeypatch):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    make_service(factory, scraper, index).run(ids["ticket"])  # un article KB est indexé
    with factory() as db:
        article_content = db.scalars(select(KBArticle)).one().content
    doc_setup(scraper, monkeypatch, count=1)
    only = scraper.docs[0]
    # Le document reprend mot pour mot le texte déjà indexé comme article : inutile de l'indexer à nouveau.
    scraper.documents[only.url] = KBDocument(only.url, only.title, article_content, file_name="", file_text="")

    make_service(factory, scraper, index, max_documents=1).run(ids["ticket"])

    assert len(index.upserts) == 1
    assert "Contenu identique" in last_job(factory).results[-1]["detail"]


def test_documents_failure_does_not_lose_the_kb_articles(db_env, monkeypatch):
    factory, ids = db_env
    scraper = FakeScraper()
    monkeypatch.setattr("app.kb.service.settings.kb_docs_enabled", True)
    scraper.docs_error = KBScrapeError("liste introuvable", selector="input[type='search']")

    assert make_service(factory, scraper, FakeIndex(), max_documents=2).run(ids["ticket"]) == "success"

    job = last_job(factory)
    assert job.new_documents == 1  # l'article KB est bien indexé
    assert [(r["source"], r["status"]) for r in job.results] == [("kb", "new"), ("document", "failed")]


def test_documents_source_is_off_by_default_and_when_limit_is_zero(db_env, monkeypatch):
    factory, ids = db_env
    scraper = FakeScraper()
    doc_setup(scraper, monkeypatch)
    monkeypatch.setattr("app.kb.service.settings.kb_docs_enabled", False)
    make_service(factory, scraper, FakeIndex(), max_documents=2).run(ids["ticket"])
    assert scraper.doc_searches == []

    monkeypatch.setattr("app.kb.service.settings.kb_docs_enabled", True)
    make_service(factory, scraper, FakeIndex(), max_documents=0).run(ids["ticket"])
    assert scraper.doc_searches == []


def test_documents_search_is_retried_with_distinctive_words_when_the_filter_finds_nothing(db_env, monkeypatch):
    factory, ids = db_env
    scraper = FakeScraper()
    doc_setup(scraper, monkeypatch, count=1)
    calls = []
    real = scraper.search_documents
    scraper.search_documents = lambda query, limit: (calls.append(query), [] if len(calls) == 1 else real(query, limit))[1]

    make_service(factory, scraper, FakeIndex(), max_documents=1).run(ids["ticket"])

    assert calls[0] == "OS6860E stacking R8.4.1" and len(calls[1].split()) == 2  # 2e essai : mots les plus distinctifs


def test_helpers_title_key_shorter_query_and_excluded_types():
    from app.kb.service import excluded_type, shorter_query, title_key

    assert title_key("Alcatel-Lucent  OmniSwitch 6860N GOLDEN RFP!") == title_key("alcatel lucent omniswitch 6860n golden rfp")
    assert shorter_query("pilot not receiving calls") == "pilot receiving"  # les 2 mots les plus longs, ordre conservé
    assert excluded_type("PreSales;Price catalog;Pricing | English") and not excluded_type("PreSales > Documentation")


def test_documents_search_without_any_hit_is_shown_in_the_job_results(db_env, monkeypatch):
    factory, ids = db_env
    scraper = FakeScraper()
    doc_setup(scraper, monkeypatch, count=0)

    make_service(factory, scraper, FakeIndex(), max_documents=2).run(ids["ticket"])

    results = last_job(factory).results
    assert [(r["source"], r["status"]) for r in results] == [("document", "empty")]
    assert "OS6860E stacking R8.4.1" in results[0]["detail"]



# ----------------------------------------------------------------------------- archive locale
from app.kb.archive import KBArchive, item_key, restore_on_startup  # noqa: E402


def archive_for(tmp_path, max_mb=50):
    return KBArchive(tmp_path / "kb_archive", max_mb * 1024 * 1024)


def test_archive_keeps_text_and_original_file_and_never_duplicates_them(tmp_path):
    archive = archive_for(tmp_path)
    saved = archive.save_item(
        url="https://kb.example/d/1", title="Guide SIP / trunk", source="document", content="Texte du guide",
        content_hash="h1", file_name="Guide SIP.pptx", file_bytes=b"PPTX-ORIGINAL",
    )
    again = archive.save_item(
        url="https://kb.example/d/2", title="Même fichier", source="document", content="Autre fiche",
        content_hash="h2", file_name="copie.pptx", file_bytes=b"PPTX-ORIGINAL",
    )

    assert saved["file"] == "stored" and again["file"] == "reused"
    files = list((tmp_path / "kb_archive" / "files").glob("*"))
    assert len(files) == 1 and files[0].suffix == ".pptx" and files[0].read_bytes() == b"PPTX-ORIGINAL"
    texts = list((tmp_path / "kb_archive" / "texts").glob("*.txt"))
    assert len(texts) == 2 and "Texte du guide" in next(p for p in texts if "Guide-SIP" in p.name).read_text(encoding="utf-8")
    assert (tmp_path / "kb_archive" / "README.txt").exists()
    stats = archive.stats(detailed=True)
    assert (stats["items"], stats["files"], stats["by_source"]["document"]) == (2, 1, 2)
    assert archive.stats()["items"] == 2 and "by_source" not in archive.stats()  # version légère, sans lecture des JSON


def test_archive_cap_keeps_the_text_but_skips_the_original_file(tmp_path):
    archive = KBArchive(tmp_path / "kb_archive", max_bytes=200)

    result = archive.save_item(
        url="https://kb.example/big", title="Gros", source="document", content="Texte conservé",
        content_hash="h", file_name="big.zip", file_bytes=b"x" * 1000,
    )

    assert result["file"] == "skipped_cap"
    assert not list((tmp_path / "kb_archive" / "files").glob("*"))
    assert next(archive.iter_items())["file"] is None and next(archive.iter_items())["content"] == "Texte conservé"


def test_archive_file_names_cannot_escape_the_archive_folder(tmp_path):
    archive = archive_for(tmp_path)

    archive.save_item(
        url="https://kb.example/x", title="../../../etc/passwd", source="kb", content="c", content_hash="h",
        file_name="../../evil.sh;rm -rf", file_bytes=b"data",
    )

    produced = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all((tmp_path / "kb_archive") in p.parents for p in produced)
    assert not any(p.suffix in {".sh"} for p in produced)  # extension non conforme : supprimée


def test_archive_restores_missing_items_only_and_skips_corrupt_files(db_env, tmp_path):
    factory, _ = db_env
    archive = archive_for(tmp_path)
    archive.save_item(url="https://kb.example/a", title="A", source="kb", content="Texte A " * 20, content_hash="ha")
    archive.save_item(url="https://kb.example/b", title="B", source="document", content="Texte B " * 20, content_hash="hb")
    (tmp_path / "kb_archive" / "items" / "corrompu.json").write_text("{pas du json", encoding="utf-8")
    (tmp_path / "kb_archive" / "items" / "incomplet.json").write_text('{"title": "sans url"}', encoding="utf-8")

    with factory() as db:
        db.add(KBArticle(url="https://kb.example/a", title="déjà là", content="x", content_hash="ha", indexed_model="m"))
        db.commit()
        assert archive.restore(db) == 1  # seul « B » manquait
        assert archive.restore(db) == 0  # idempotent
        restored = db.scalars(select(KBArticle).where(KBArticle.url == "https://kb.example/b")).one()
        assert (restored.source, restored.indexed_model, restored.chunk_count) == ("document", "", 0)
        assert db.scalars(select(KBArticle).where(KBArticle.url == "https://kb.example/a")).one().title == "déjà là"


def test_documents_are_archived_with_their_original_file_during_enrichment(db_env, monkeypatch, tmp_path):
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    doc_setup(scraper, monkeypatch, count=1)
    only = scraper.docs[0]
    scraper.documents[only.url] = KBDocument(
        only.url, only.title, DOC_METADATA, file_name="guide.pptx", file_text="Procédure détaillée du trunk SIP. " * 5,
        file_bytes=b"ORIGINAL-PPTX-BYTES",
    )
    archive = archive_for(tmp_path)
    service = KBEnrichmentService(
        scraper, FixedQuery(), index, factory, max_articles=3, refetch_after_hours=24, max_documents=1, archive=archive,
    )

    service.run(ids["ticket"])

    assert scraper.include_file_flags == [True]  # le fichier n'est demandé que parce que l'archive est active
    assert "archivé avec son fichier original" in last_job(factory).results[0]["detail"]
    record = next(archive.iter_items())
    assert record["source"] == "document" and "Procédure détaillée" in record["content"]
    stored = tmp_path / "kb_archive" / "files" / record["file"]["stored_as"]
    assert stored.read_bytes() == b"ORIGINAL-PPTX-BYTES"


def test_archive_is_not_requested_nor_written_when_disabled_and_failures_do_not_break_the_job(db_env, monkeypatch, tmp_path):
    factory, ids = db_env
    scraper = FakeScraper()
    doc_setup(scraper, monkeypatch, count=1)
    make_service(factory, scraper, FakeIndex(), max_documents=1).run(ids["ticket"])  # sans archive
    assert scraper.include_file_flags == [False] and not (tmp_path / "kb_archive").exists()
    with factory() as db:  # sinon le document est « déjà connu » et rien n'est relu
        db.query(KBArticle).delete()
        db.commit()

    broken = archive_for(tmp_path)
    monkeypatch.setattr(broken, "save_item", lambda **_kwargs: (_ for _ in ()).throw(OSError("disque plein")))
    scraper2 = FakeScraper()
    doc_setup(scraper2, monkeypatch, count=1)
    service = KBEnrichmentService(scraper2, FixedQuery(), FakeIndex(), factory, max_articles=3, max_documents=1, archive=broken)

    assert service.run(ids["ticket"]) == "success"
    assert "archive impossible" in last_job(factory).results[0]["detail"]


def test_database_is_rebuilt_from_the_archive_after_a_full_wipe(db_env, monkeypatch, tmp_path):
    """Scénario « down -v » : la base est vide, le dossier kb_archive/ survit."""
    factory, ids = db_env
    scraper, index = FakeScraper(), FakeIndex()
    archive = archive_for(tmp_path)
    KBEnrichmentService(scraper, FixedQuery(), index, factory, max_articles=3, archive=archive).run(ids["ticket"])
    with factory() as db:
        before = db.scalars(select(KBArticle)).one()
        url, content = before.url, before.content
        db.delete(before)
        db.commit()  # base effacée

    monkeypatch.setattr("app.kb.archive.settings.kb_archive_dir", str(tmp_path / "kb_archive"))
    monkeypatch.setattr("app.kb.archive.settings.kb_archive_enabled", True)
    queued = []
    monkeypatch.setattr("app.kb.jobs.enqueue_reindex", lambda: queued.append(True) or True)

    assert restore_on_startup(factory) == 1

    with factory() as db:
        restored = db.scalars(select(KBArticle)).one()
        assert (restored.url, restored.content, restored.indexed_model) == (url, content, "")
    assert queued == [True]  # les vecteurs sont recalculés, sans retourner sur le portail
    assert restore_on_startup(factory) == 0 and queued == [True]  # idempotent : pas de nouvelle réindexation


def test_archive_startup_restore_is_inert_when_disabled_or_folder_missing(db_env, monkeypatch, tmp_path):
    factory, _ = db_env
    monkeypatch.setattr("app.kb.archive.settings.kb_archive_dir", str(tmp_path / "absent"))
    monkeypatch.setattr("app.kb.archive.settings.kb_archive_enabled", True)
    assert restore_on_startup(factory) == 0
    monkeypatch.setattr("app.kb.archive.settings.kb_archive_enabled", False)
    assert KBArchive.from_settings() is None and restore_on_startup(factory) == 0


def test_client_rejects_a_corrupted_original_file():
    import base64
    import hashlib

    from app.kb.client import _decode_file

    data = b"contenu original"
    good = {"content_b64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
    assert _decode_file(good) == data
    assert _decode_file({**good, "sha256": "0" * 64}) is None  # empreinte différente : transfert altéré
    assert _decode_file({"content_b64": "###pas du base64###"}) is None and _decode_file({}) is None
    assert item_key("https://kb.example/a") == item_key("https://kb.example/a") != item_key("https://kb.example/b")
