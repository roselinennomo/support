"""Documents propres à chaque entreprise : extraction, droits, isolation entre entreprises, fusion dans le RAG."""

import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import ai, company_docs, doc_text, rag
from app.database import Base, get_db
from app.main import app
from app.models import Company, CompanyDocument, User
from app.security import create_access_token, hash_password


# ----------------------------------------------------------------------------- extraction
def make_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


XLSX_SHARED = (
    '<sst><si><t>Source</t></si><si><t>Destination</t></si><si><t>Port</t></si>'
    '<si><t>VLAN voix 10.10.0.0/24</t></si><si><r><t>Call </t></r><r><t>Server</t></r></si></sst>'
)
XLSX_SHEET = (
    '<worksheet><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c><c r="C2"><v>5060</v></c></row>'
    '<row r="3"><c r="A3" t="inlineStr"><is><t>DHCP</t></is></c><c r="B3"/><c r="C3"><v>67</v></c></row>'
    '<row r="4"><c r="A4"/></row>'
    '</sheetData></worksheet>'
)


def test_excel_plans_are_extracted_row_by_row_with_the_sheet_name():
    data = make_zip({
        "xl/sharedStrings.xml": XLSX_SHARED,
        "xl/workbook.xml": '<workbook><sheets><sheet name="Matrice de flux" sheetId="1"/></sheets></workbook>',
        "xl/worksheets/sheet1.xml": XLSX_SHEET,
    })

    text = doc_text.extract_text(data, "matrice.xlsx")

    assert text.splitlines() == [
        "## Matrice de flux",
        "Source | Destination | Port",
        "VLAN voix 10.10.0.0/24 | Call Server | 5060",
        "DHCP |  | 67",
    ]


def test_word_powerpoint_text_and_pdf_are_extracted_and_other_formats_are_not():
    docx = make_zip({"word/document.xml": "<w:p><w:r><w:t>Plan </w:t></w:r><w:r><w:t>d'adressage</w:t></w:r></w:p>"})
    pptx = make_zip({"ppt/slides/slide1.xml": "<a:p><a:r><a:t>Architecture</a:t></a:r></a:p><a:tbl><a:tr/></a:tbl>"})
    assert doc_text.extract_text(docx, "plan.docx") == "Plan d'adressage"
    assert doc_text.extract_text(pptx, "archi.pptx") == "Architecture"
    assert doc_text.extract_text("vlan=10\nvoix".encode(), "switch.conf") == "vlan=10\nvoix"
    assert doc_text.extract_text(b"MZ\x90", "outil.exe") == "" and not doc_text.is_supported("outil.exe")

    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Topologie du reseau coeur")
    pdf.save()
    assert "Topologie du reseau coeur" in doc_text.extract_text(buffer.getvalue(), "topologie.pdf")


# ----------------------------------------------------------------------------- isolation de l'index
class FakeQdrant:
    """Simule Qdrant : retient dans quelle collection on lit/écrit, et peut renvoyer un point étranger."""

    def __init__(self, monkeypatch, points=None):
        self.requests = []
        self.points = points or []
        monkeypatch.setattr(rag, "_embed", lambda texts: [[0.0] * rag.embedding_dimension() for _ in texts])
        monkeypatch.setattr(rag, "_qdrant_request", self._request)
        monkeypatch.setattr(company_docs.CompanyIndex, "_exists", lambda _self: True)

    def _request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        return SimpleNamespace(json=lambda: {"result": self.points})


def test_each_company_has_its_own_collection_distinct_from_shared_ones(monkeypatch):
    first, second = company_docs.collection_name("company-a"), company_docs.collection_name("company-b")

    assert first != second and "company-a" in first and "company-b" not in first
    assert not first.startswith(rag.collection_name()) and "kb" not in first
    monkeypatch.setattr(rag, "_embedding_model_override", "bge-m3")
    assert company_docs.collection_name("company-a") != first  # un autre modèle d'embedding = une autre collection
    with pytest.raises(ValueError):
        company_docs.CompanyIndex("")


def test_search_only_reads_the_collection_of_the_requested_company(monkeypatch):
    qdrant = FakeQdrant(monkeypatch, points=[{"score": 0.9, "payload": {"company_id": "company-a", "filename": "plan.xlsx", "text": "VLAN 10"}}])

    results = company_docs.CompanyIndex("company-a").search("vlan", 3)

    assert [r["filename"] for r in results] == ["plan.xlsx"]
    search_paths = [path for _method, path, _kw in qdrant.requests if path.endswith("/points/search")]
    assert search_paths == [f"/collections/{company_docs.collection_name('company-a')}/points/search"]


def test_a_foreign_point_is_never_returned_even_if_the_store_misbehaves(monkeypatch):
    FakeQdrant(monkeypatch, points=[
        {"score": 0.9, "payload": {"company_id": "company-B", "filename": "secret-de-B.xlsx", "text": "Plan confidentiel de B"}},
        {"score": 0.8, "payload": {"company_id": "company-a", "filename": "plan-de-A.xlsx", "text": "Plan de A"}},
    ])

    excerpts = company_docs.company_excerpts("plan", "company-a")

    assert len(excerpts) == 1 and "plan-de-A.xlsx" in excerpts[0] and "secret" not in excerpts[0] and "B" not in excerpts[0].split("\n")[1]


def test_company_excerpts_are_empty_without_company_or_on_failure(monkeypatch):
    assert company_docs.company_excerpts("question", "") == [] and company_docs.company_excerpts("   ", "company-a") == []
    monkeypatch.setattr(company_docs.CompanyIndex, "search", lambda *_args: (_ for _ in ()).throw(RuntimeError("qdrant down")))
    assert company_docs.company_excerpts("question", "company-a") == []  # le RAG commun répond seul


def test_rag_adds_company_documents_first_and_only_for_the_scoped_company(monkeypatch):
    asked = []
    monkeypatch.setattr(rag, "_sync_before_search", lambda: None)
    monkeypatch.setattr(rag, "evaluate_query", lambda _q: {"results": [{"source": "guide.pdf", "page": 2, "text": "Commun", "score": 0.8}]})
    monkeypatch.setattr(rag, "_kb_excerpts", lambda _q: ["[Source KB constructeur : x]\nKB"])
    monkeypatch.setattr(company_docs, "company_excerpts", lambda query, company_id: asked.append(company_id) or [f"[Source documentation de l'entreprise : p.xlsx]\nPrivé {company_id}"])

    assert "Privé" not in rag.retrieve_context("routage") and asked == []  # sans entreprise : documents communs seulement

    token = rag.set_company_scope("company-a")
    try:
        context = rag.retrieve_context("routage")
    finally:
        rag.reset_company_scope(token)

    assert asked == ["company-a"]
    assert context.index("Privé company-a") < context.index("guide.pdf") < context.index("KB constructeur")
    assert rag._company_scope.get() is None


def test_ai_runs_set_the_company_scope_for_the_whole_answer_and_always_reset_it(monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "retrieve_context", lambda _q: seen.append(rag._company_scope.get()) or "Aucun extrait.")
    monkeypatch.setattr(ai, "_call_provider", lambda *_a: (
        '{"reply": "ok", "priority": "low", "recommendations": [], "resolved": false, "escalation_recommended": false}', "ollama", "m"))
    snapshot = SimpleNamespace(title="t", description="d", product="OXE", version="", company_id="company-a")

    ai.respond_to_customer(snapshot, [{"role": "client", "body": "d"}], [])
    monkeypatch.setattr(ai, "_call_provider", lambda *_a: ("réponse", "ollama", "m"))
    ai.assist_agent(snapshot, [{"role": "client", "body": "d"}], [], "Que vérifier ?")
    orm_like = SimpleNamespace(title="t", description="d", product="OXE", version="", customer=SimpleNamespace(company_id="company-b"))
    monkeypatch.setattr(ai, "_call_provider", lambda *_a: (
        '{"summary": "s", "recommendations": [], "priority": "low", "escalation_recommended": false}', "ollama", "m"))
    ai.analyze_ticket(orm_like)

    assert seen == ["company-a", "company-a", "company-b"]
    assert rag._company_scope.get() is None  # jamais de portée qui « fuit » vers la requête suivante


# ----------------------------------------------------------------------------- indexation
@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.company_docs.settings.company_docs_dir", str(tmp_path / "company_docs"))
    with factory() as db:
        admin = User(email="adm@example.com", full_name="Admin", password_hash=hash_password("x" * 12), role="admin")
        db.add(admin)
        db.flush()
        company_a = Company(name="Entreprise A", invitation_code_hash="ha", created_by_id=admin.id)
        company_b = Company(name="Entreprise B", invitation_code_hash="hb", created_by_id=admin.id)
        db.add_all([company_a, company_b])
        db.flush()
        users = {
            "admin": admin,
            "customer_a": User(email="a@example.com", full_name="Client A", password_hash="x", role="customer", company_id=company_a.id),
            "customer_b": User(email="b@example.com", full_name="Client B", password_hash="x", role="customer", company_id=company_b.id),
            "agent_a": User(email="ag@example.com", full_name="Agent A", password_hash="x", role="agent"),
            "agent_other": User(email="ao@example.com", full_name="Agent externe", password_hash="x", role="agent"),
        }
        db.add_all([users["customer_a"], users["customer_b"], users["agent_a"], users["agent_other"]])
        db.flush()
        company_a.agents = [users["agent_a"]]
        db.commit()
        headers = {name: {"Authorization": f"Bearer {create_access_token(user.id)}"} for name, user in users.items()}
        ids = {"a": company_a.id, "b": company_b.id}

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), factory, headers, ids, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()


class FakeIndex:
    instances: list = []

    def __init__(self, company_id):
        self.company_id, self.upserts, self.deleted, self.fail = company_id, [], [], False
        FakeIndex.instances.append(self)

    def upsert(self, doc_id, filename, chunks):
        if self.fail:
            raise RuntimeError("qdrant indisponible")
        self.upserts.append((doc_id, filename, len(chunks)))
        return len(chunks)

    def delete(self, doc_id):
        self.deleted.append(doc_id)


def upload(api, headers, name="plan.txt", content=b"Plan d'adressage : VLAN voix 10 - 10.10.0.0/24.", **data):
    return api.post("/api/company-docs", headers=headers, files=[("files", (name, content, "application/octet-stream"))], data=data)


def test_company_users_and_admins_add_documents_but_support_agents_cannot(env, monkeypatch):
    api, factory, headers, ids, tmp_path = env
    indexed = []
    monkeypatch.setattr(company_docs, "index_document", lambda doc_id: indexed.append(doc_id))

    mine = upload(api, headers["customer_a"])
    assert mine.status_code == 201 and mine.json()["uploaded"][0]["status"] == "pending" and len(indexed) == 1
    assert (tmp_path / "company_docs" / ids["a"]).is_dir()  # fichier original conservé hors Docker

    assert upload(api, headers["agent_a"], company_id=ids["a"]).status_code == 403  # un agent support n'ajoute pas
    assert upload(api, headers["customer_a"], name="autre.txt", content=b"x" * 40, company_id=ids["b"]).status_code == 404
    assert upload(api, headers["admin"], name="admin.txt", content=b"Plan fourni par l'admin " * 3).status_code == 422  # entreprise à préciser
    assert upload(api, headers["admin"], name="admin.txt", content=b"Plan fourni par l'admin " * 3, company_id=ids["b"]).status_code == 201
    doc_id = mine.json()["uploaded"][0]["id"]
    assert api.delete(f"/api/company-docs/{doc_id}", headers=headers["agent_a"]).status_code == 403


def test_a_company_never_sees_nor_touches_the_documents_of_another(env, monkeypatch):
    api, factory, headers, ids, _tmp = env
    monkeypatch.setattr(company_docs, "index_document", lambda _doc_id: None)
    monkeypatch.setattr(company_docs, "CompanyIndex", FakeIndex)
    doc_a = upload(api, headers["customer_a"], name="reseau-A.txt", content=b"Confidentiel A " * 5).json()["uploaded"][0]["id"]

    assert [d["filename"] for d in api.get("/api/company-docs", headers=headers["customer_a"]).json()["documents"]] == ["reseau-A.txt"]
    assert api.get("/api/company-docs", headers=headers["customer_b"]).json()["documents"] == []
    assert api.get(f"/api/company-docs?company_id={ids['a']}", headers=headers["customer_b"]).status_code == 404
    assert api.get(f"/api/company-docs/{doc_a}/download", headers=headers["customer_b"]).status_code == 404
    assert api.delete(f"/api/company-docs/{doc_a}", headers=headers["customer_b"]).status_code == 404
    assert api.post(f"/api/company-docs/{doc_a}/reindex", headers=headers["customer_b"]).status_code == 404
    assert api.get(f"/api/company-docs/{doc_a}/download", headers=headers["customer_a"]).content.startswith(b"Confidentiel A")
    with factory() as db:
        assert db.get(CompanyDocument, doc_a) is not None  # la tentative de B n'a rien supprimé


def test_support_agents_have_read_only_access_to_their_assigned_companies_only(env, monkeypatch):
    api, factory, headers, ids, _tmp = env
    monkeypatch.setattr(company_docs, "index_document", lambda _doc_id: None)
    upload(api, headers["customer_a"], name="archi.txt", content=b"Architecture du site A " * 4)

    listing = api.get(f"/api/company-docs?company_id={ids['a']}", headers=headers["agent_a"])
    assert listing.status_code == 200 and listing.json()["can_edit"] is False and len(listing.json()["documents"]) == 1
    assert api.get(f"/api/company-docs?company_id={ids['a']}", headers=headers["agent_other"]).status_code == 404  # non affecté
    assert api.get(f"/api/company-docs?company_id={ids['b']}", headers=headers["agent_a"]).status_code == 404
    assert api.get("/api/company-docs", headers=headers["agent_a"]).status_code == 404  # l'agent doit viser une entreprise
    assert api.get("/api/company-docs").status_code == 401


def test_upload_rules_duplicates_formats_size_quota_and_count(env, monkeypatch):
    api, factory, headers, ids, _tmp = env
    monkeypatch.setattr(company_docs, "index_document", lambda _doc_id: None)
    first = upload(api, headers["customer_a"], name="plan.txt", content=b"Plan A " * 10).json()
    assert len(first["uploaded"]) == 1

    duplicate = upload(api, headers["customer_a"], name="copie.txt", content=b"Plan A " * 10).json()
    assert duplicate["uploaded"] == [] and "Déjà présent sous le nom « plan.txt »" in duplicate["skipped"][0]["reason"]
    assert upload(api, headers["customer_b"], name="plan.txt", content=b"Plan A " * 10).status_code == 201  # autre entreprise : autorisé
    assert "Format non pris en charge" in upload(api, headers["customer_a"], name="outil.exe", content=b"MZ").json()["skipped"][0]["reason"]

    monkeypatch.setattr(company_docs.settings, "company_docs_max_file_mb", 0)
    assert "trop volumineux" in upload(api, headers["customer_a"], name="gros.txt", content=b"x").json()["skipped"][0]["reason"]
    monkeypatch.setattr(company_docs.settings, "company_docs_max_file_mb", 10)
    monkeypatch.setattr(company_docs.settings, "company_docs_max_mb_per_company", 0)
    assert "Quota" in upload(api, headers["customer_a"], name="quota.txt", content=b"y" * 50).json()["skipped"][0]["reason"]
    monkeypatch.setattr(company_docs.settings, "company_docs_max_mb_per_company", 500)
    monkeypatch.setattr(company_docs.settings, "company_docs_max_files_per_upload", 1)
    two = api.post("/api/company-docs", headers=headers["customer_a"], files=[("files", ("a.txt", b"a" * 30)), ("files", ("b.txt", b"b" * 30))])
    assert two.status_code == 413
    scan = upload(api, headers["customer_a"], name="scan.pdf", content=b"%PDF-1.4 \n%%EOF").json()
    assert scan["skipped"] or scan["uploaded"][0]["status"] == "empty"  # sans texte : signalé, jamais indexé à vide


def test_delete_removes_vectors_original_file_and_row(env, monkeypatch):
    api, factory, headers, ids, tmp_path = env
    FakeIndex.instances = []
    monkeypatch.setattr(company_docs, "index_document", lambda _doc_id: None)
    monkeypatch.setattr(company_docs, "CompanyIndex", FakeIndex)
    doc = upload(api, headers["customer_a"], name="flux.txt", content=b"Matrice de flux " * 5).json()["uploaded"][0]
    stored = next((tmp_path / "company_docs" / ids["a"]).glob("*.txt"))

    assert api.delete(f"/api/company-docs/{doc['id']}", headers=headers["customer_a"]).status_code == 204

    assert not stored.exists() and FakeIndex.instances[-1].company_id == ids["a"] and FakeIndex.instances[-1].deleted == [doc["id"]]
    with factory() as db:
        assert db.get(CompanyDocument, doc["id"]) is None
    assert api.get("/api/company-docs", headers=headers["customer_a"]).json()["documents"] == []


def test_indexing_records_success_empty_and_failure_and_resumes_after_a_model_change(env, monkeypatch):
    _api, factory, _headers, ids, _tmp = env
    with factory() as db:
        for name, content in (("ok.txt", "Contenu à indexer. " * 30), ("ko.txt", "Autre contenu. " * 30), ("scan.pdf", "")):
            db.add(CompanyDocument(
                company_id=ids["a"], filename=name, stored_name=name, size=len(content), content=content,
                content_hash=name, status="pending" if content else "empty",
            ))
        db.commit()
        by_name = {d.filename: d.id for d in db.scalars(select(CompanyDocument))}
    FakeIndex.instances = []

    assert company_docs.index_document(by_name["ok.txt"], factory, FakeIndex) == "indexed"
    failing = lambda company_id: SimpleNamespace(upsert=lambda *_a: (_ for _ in ()).throw(RuntimeError("qdrant down")))
    assert company_docs.index_document(by_name["ko.txt"], factory, failing) == "failed"
    assert company_docs.index_document(by_name["scan.pdf"], factory, FakeIndex) == "empty"  # rien à indexer, jamais d'index vide
    assert company_docs.index_document("inconnu", factory, FakeIndex) == "missing"
    with factory() as db:
        good = db.get(CompanyDocument, by_name["ok.txt"])
        assert (good.indexed_model, good.status) == (rag.embedding_model(), "indexed") and good.chunk_count >= 1
        assert "qdrant down" in db.get(CompanyDocument, by_name["ko.txt"]).error
    assert FakeIndex.instances[0].company_id == ids["a"]  # indexé dans l'espace de SON entreprise

    # Reprise : le document en échec est retenté; avec un autre modèle d'embedding, tout est recalculé.
    assert company_docs.index_pending_and_stale(factory, FakeIndex) == 1
    assert company_docs.index_pending_and_stale(factory, FakeIndex) == 0  # rien de nouveau : pas de travail inutile
    monkeypatch.setattr(rag, "_embedding_model_override", "bge-m3")
    assert company_docs.index_pending_and_stale(factory, FakeIndex) == 2
    with factory() as db:
        models = {d.indexed_model for d in db.scalars(select(CompanyDocument)) if d.status == "indexed"}
        assert models == {"bge-m3"}
