"""Console d'aide : droits, isolation des entreprises, réponses asynchrones, sources, mode ticket, télémétrie sans ticket."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import ai, help_console, rag, web_search
from app.database import Base, get_db
from app.main import app
from app.models import AIExecution, Company, HelpConversation, HelpMessage, Ticket, User
from app.security import create_access_token, hash_password


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(help_console, "SessionLocal", factory)
    monkeypatch.setattr("app.tickets.SessionLocal", factory)
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
            "customer": User(email="c@example.com", full_name="Client", password_hash="x", role="customer", company_id=company_a.id),
            "agent": User(email="ag@example.com", full_name="Agent", password_hash="x", role="agent"),
            "other_agent": User(email="ao@example.com", full_name="Autre agent", password_hash="x", role="agent"),
        }
        db.add_all([users["customer"], users["agent"], users["other_agent"]])
        db.flush()
        company_a.agents = [users["agent"]]
        ticket = Ticket(
            reference="OXE-TEST0001", customer_id=users["customer"].id, title="Perte de tonalité",
            description="Plus de tonalité sur le poste 4012", product="OXE", version="R12.2",
        )
        db.add(ticket)
        db.commit()
        headers = {name: {"Authorization": f"Bearer {create_access_token(user.id)}"} for name, user in users.items()}
        ids = {"a": company_a.id, "b": company_b.id, "ticket": ticket.id, "agent": users["agent"].id}

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), factory, headers, ids
    app.dependency_overrides.clear()
    engine.dispose()


def create(api, headers, **payload):
    return api.post("/api/help/conversations", headers=headers, json=payload)


def test_only_admins_and_agents_use_the_console_and_conversations_are_personal(env):
    api, _factory, headers, _ids = env
    assert api.get("/api/help/conversations", headers=headers["customer"]).status_code == 403
    mine = create(api, headers["agent"])
    assert mine.status_code == 201
    conversation_id = mine.json()["id"]
    assert api.get(f"/api/help/conversations/{conversation_id}", headers=headers["other_agent"]).status_code == 404
    assert api.get(f"/api/help/conversations/{conversation_id}", headers=headers["admin"]).status_code == 404  # même l'admin
    assert api.delete(f"/api/help/conversations/{conversation_id}", headers=headers["other_agent"]).status_code == 404
    listing = api.get("/api/help/conversations", headers=headers["agent"]).json()
    assert listing["total_count"] == 1 and api.get("/api/help/conversations", headers=headers["other_agent"]).json()["total_count"] == 0


def test_an_agent_only_chooses_among_assigned_companies_and_an_admin_among_all(env):
    api, _factory, headers, ids = env
    assert create(api, headers["agent"], company_id=ids["a"]).status_code == 201
    assert create(api, headers["agent"], company_id=ids["b"]).status_code == 404
    assert create(api, headers["admin"], company_id=ids["b"]).status_code == 201
    assert create(api, headers["admin"], company_id="inconnue").status_code == 404
    options = api.get("/api/help/options", headers=headers["agent"]).json()
    assert [company["id"] for company in options["companies"]] == [ids["a"]]
    assert len(api.get("/api/help/options", headers=headers["admin"]).json()["companies"]) == 2
    assert options["kb_live"] is False and options["web_search"] is False


def test_the_answer_uses_the_conversation_company_history_and_cites_its_sources(env, monkeypatch):
    api, factory, headers, ids = env
    seen = {}

    def retrieve(query):
        seen["scope"], seen["query"] = rag._company_scope.get(), query
        return (
            "[Source : guide.pdf, page 4]\nVérifier le Call Server\n\n"
            "[Source documentation de l'entreprise : plan.xlsx]\nVLAN voix 10\n\n"
            "[Source KB constructeur : Article tonalité — https://portal.example/a1]\nRedémarrer le service"
        )

    def answer(prompt, user_id, preference=None, role="staff"):
        seen["prompt"], seen["user"] = prompt, user_id
        return "Vérifiez le VLAN voix [plan.xlsx].", "ollama", "deepseek-r1:1.5b", "Je regarde le VLAN."

    monkeypatch.setattr(rag, "retrieve_context", retrieve)
    monkeypatch.setattr(help_console, "help_console_answer", answer)
    conversation_id = create(api, headers["agent"], company_id=ids["a"]).json()["id"]

    first = api.post(f"/api/help/conversations/{conversation_id}/messages", headers=headers["agent"], json={"content": "Pas de tonalité, que vérifier ?"})
    assert first.status_code == 202
    assert [m["role"] for m in first.json()["messages"]] == ["user", "assistant"]  # visibles dès l'envoi
    detail = api.get(f"/api/help/conversations/{conversation_id}", headers=headers["agent"]).json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    reply = detail["messages"][1]
    assert reply["status"] == "done" and reply["content"].startswith("Vérifiez le VLAN voix")
    assert reply["thinking"] == "Je regarde le VLAN."
    assert {(item["kind"], item["title"]) for item in reply["sources"]} == {
        ("doc", "guide.pdf, page 4"), ("company", "plan.xlsx"), ("kb", "Article tonalité"),
    }
    assert seen["scope"] == ids["a"] and rag._company_scope.get() is None  # portée remise à zéro
    assert detail["title"].startswith("Pas de tonalité") and seen["user"] == ids["agent"]

    api.post(f"/api/help/conversations/{conversation_id}/messages", headers=headers["agent"], json={"content": "Et côté DHCP ?"})
    assert "Pas de tonalité, que vérifier ?" in seen["prompt"] and "Vérifiez le VLAN voix" in seen["prompt"]
    assert "Question : Et côté DHCP ?" in seen["prompt"] and "Entreprise A" in seen["prompt"]


def test_a_conversation_without_company_never_scopes_company_documents(env, monkeypatch):
    api, _factory, headers, _ids = env
    scopes = []
    monkeypatch.setattr(rag, "retrieve_context", lambda query: scopes.append(rag._company_scope.get()) or "Aucun extrait.")
    monkeypatch.setattr(help_console, "help_console_answer", lambda prompt, user_id, preference=None, role="staff": ("ok", "ollama", "m", ""))
    conversation_id = create(api, headers["agent"]).json()["id"]
    api.post(f"/api/help/conversations/{conversation_id}/messages", headers=headers["agent"], json={"content": "Question générale"})
    assert scopes == [None]


def test_failures_are_recorded_on_the_message_and_do_not_block_the_next_question(env, monkeypatch):
    api, _factory, headers, _ids = env
    monkeypatch.setattr(rag, "retrieve_context", lambda query: (_ for _ in ()).throw(rag.RAGError("qdrant hors ligne")))

    def failing(prompt, user_id, preference=None, role="staff"):
        raise ai.AIAnalysisError("Assistance IA impossible : délai dépassé")

    monkeypatch.setattr(help_console, "help_console_answer", failing)
    conversation_id = create(api, headers["agent"]).json()["id"]
    path = f"/api/help/conversations/{conversation_id}/messages"
    api.post(path, headers=headers["agent"], json={"content": "Première question"})
    reply = api.get(f"/api/help/conversations/{conversation_id}", headers=headers["agent"]).json()["messages"][-1]
    assert reply["status"] == "error" and "délai dépassé" in reply["error"]

    monkeypatch.setattr(help_console, "help_console_answer", lambda prompt, user_id, preference=None, role="staff": ("Réponse", "ollama", "m", ""))
    api.post(path, headers=headers["agent"], json={"content": "Deuxième question"})
    messages = api.get(f"/api/help/conversations/{conversation_id}", headers=headers["agent"]).json()["messages"]
    assert messages[-1]["status"] == "done" and "Première question" in messages[-1]["content"] + "Première question"


def test_a_second_question_is_refused_while_an_answer_is_pending_and_interrupted_ones_recover(env):
    api, factory, headers, _ids = env
    conversation_id = create(api, headers["agent"]).json()["id"]
    with factory() as db:
        db.add(HelpMessage(conversation_id=conversation_id, role="assistant", status="pending"))
        db.commit()
    blocked = api.post(f"/api/help/conversations/{conversation_id}/messages", headers=headers["agent"], json={"content": "Encore"})
    assert blocked.status_code == 409

    assert help_console.recover_interrupted_answers() == 1
    with factory() as db:
        message = db.scalar(select(HelpMessage))
        assert message.status == "error" and "redémarrage" in message.error


def test_ticket_mode_copies_the_ticket_and_forces_its_company(env):
    api, factory, headers, ids = env
    created = create(api, headers["agent"], ticket_id=ids["ticket"], company_id=ids["b"])
    assert created.status_code == 201
    body = created.json()
    assert body["company_id"] == ids["a"] and body["ticket_reference"] == "OXE-TEST0001" and body["title"] == "Ticket OXE-TEST0001"
    with factory() as db:
        context = db.get(HelpConversation, body["id"]).ticket_context
    assert "Perte de tonalité" in context and "poste 4012" in context and "R12.2" in context

    assert create(api, headers["other_agent"], ticket_id=ids["ticket"]).status_code == 404  # ticket d'une entreprise non affectée
    assert create(api, headers["customer"], ticket_id=ids["ticket"]).status_code == 403

    with factory() as db:
        db.get(Ticket, ids["ticket"]).description = "Description mise à jour"
        db.commit()
    refreshed = api.patch(f"/api/help/conversations/{body['id']}", headers=headers["agent"], json={"refresh_ticket": True})
    assert refreshed.status_code == 200
    with factory() as db:
        assert "Description mise à jour" in db.get(HelpConversation, body["id"]).ticket_context
    plain = create(api, headers["agent"]).json()["id"]
    assert api.patch(f"/api/help/conversations/{plain}", headers=headers["agent"], json={"refresh_ticket": True}).status_code == 409


def test_a_ticket_whose_customer_refused_ai_processing_is_not_copied(env):
    api, factory, headers, ids = env
    with factory() as db:
        db.scalar(select(User).where(User.email == "c@example.com")).ai_processing_consent = False
        db.commit()
    assert create(api, headers["agent"], ticket_id=ids["ticket"]).status_code == 409


def test_live_kb_and_web_search_are_refused_when_unavailable_and_used_when_enabled(env, monkeypatch):
    api, _factory, headers, _ids = env
    assert create(api, headers["agent"], use_web=True).status_code == 409
    assert create(api, headers["agent"], use_kb_live=True).status_code == 409

    monkeypatch.setattr(web_search.settings, "web_search_url", "http://searxng:8080")
    monkeypatch.setattr(help_console.kb_flags, "is_enabled", lambda *args, **kwargs: True)
    captured = {}
    monkeypatch.setattr("app.kb.jobs.live_search", lambda question: captured.setdefault("kb", question) and [("kb", "Article live", "https://portal.example/live")])
    monkeypatch.setattr(web_search, "search", lambda question: captured.setdefault("web", question) and [{"title": "Forum OXE", "url": "https://forum.example/t", "snippet": "astuce"}])
    monkeypatch.setattr(rag, "retrieve_context", lambda query: "Aucun extrait.")
    monkeypatch.setattr(help_console, "help_console_answer", lambda prompt, user_id, preference=None, role="staff": (prompt, "ollama", "m", ""))
    conversation_id = create(api, headers["agent"], use_kb_live=True, use_web=True).json()["id"]
    api.post(f"/api/help/conversations/{conversation_id}/messages", headers=headers["agent"], json={"content": "Erreur 504 sur le Call Server"})
    reply = api.get(f"/api/help/conversations/{conversation_id}", headers=headers["agent"]).json()["messages"][-1]
    assert captured == {"kb": "Erreur 504 sur le Call Server", "web": "Erreur 504 sur le Call Server"}
    assert {"kind": "kb", "title": "Article live", "url": "https://portal.example/live"} in reply["sources"]
    assert {"kind": "web", "title": "Forum OXE", "url": "https://forum.example/t"} in reply["sources"]
    assert "Forum OXE" in reply["content"]  # le prompt contient les résultats web


def test_web_search_only_sends_the_redacted_question(monkeypatch):
    sent = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"title": "Doc", "url": "https://x.example", "content": "texte"}, {"title": "Mauvais", "url": "javascript:alert(1)"}]}

    def get(url, params, timeout):
        sent.update(url=url, params=params)
        return Response()

    monkeypatch.setattr(web_search.settings, "web_search_url", "http://searxng:8080/")
    monkeypatch.setattr(web_search.httpx, "get", get)
    results = web_search.search("Poste 10.1.2.3 de jean.dupont@client.fr en erreur 504")
    assert "10.1.2.3" not in sent["params"]["q"] and "jean.dupont@client.fr" not in sent["params"]["q"]
    assert "erreur 504" in sent["params"]["q"] and sent["url"] == "http://searxng:8080/search"
    assert results == [{"title": "Doc", "url": "https://x.example", "snippet": "texte"}]
    monkeypatch.setattr(web_search.settings, "web_search_url", "")
    assert web_search.search("anything") == []


def test_console_calls_are_metered_without_ticket_and_listed_in_the_traces(env, monkeypatch):
    api, factory, headers, ids = env
    monkeypatch.setattr("app.database.SessionLocal", factory)
    monkeypatch.setattr(ai, "routing_for", lambda task, role: (True, [{"provider": "ollama", "model": "m", "think": False, "timeout_seconds": 5}]))
    monkeypatch.setattr(ai, "_call_ollama", lambda prompt, model, think, timeout: (ai._provider_thinking.set("Je réfléchis."), "Réponse")[1])
    text, provider, model, thinking = ai.help_console_answer("Question", ids["agent"])
    assert (text, provider, model, thinking) == ("Réponse", "ollama", "m", "Je réfléchis.")
    with factory() as db:
        execution = db.scalar(select(AIExecution))
        assert execution.ticket_id is None and execution.customer_id == ids["agent"] and execution.task == "agent_assistance"

    traces = api.get("/api/admin/ai/traces", headers=headers["admin"])
    assert traces.status_code == 200
    item = traces.json()["items"][0]
    assert item["ticket_id"] is None and item["ticket_reference"] == "Console d’aide"


def test_files_added_to_a_conversation_feed_the_prompt_and_stay_personal(env, monkeypatch):
    api, _factory, headers, _ids = env
    prompts = []
    monkeypatch.setattr(rag, "retrieve_context", lambda query: "Aucun extrait.")
    monkeypatch.setattr(help_console, "help_console_answer", lambda prompt, user_id, preference=None, role="staff": (prompts.append(prompt) or "ok", "ollama", "m", ""))
    conversation_id = create(api, headers["agent"]).json()["id"]
    path = f"/api/help/conversations/{conversation_id}/files"

    added = api.post(path, headers=headers["agent"], files=[("files", ("config.txt", b"vlan voix 10 trunk 4012", "text/plain"))])
    assert added.status_code == 201 and added.json()["files"][0]["filename"] == "config.txt"
    assert api.post(path, headers=headers["other_agent"], files=[("files", ("x.txt", b"x", "text/plain"))]).status_code == 404
    assert api.post(path, headers=headers["agent"], files=[("files", ("virus.exe", b"MZ", "application/octet-stream"))]).status_code == 422
    assert api.post(path, headers=headers["agent"], files=[("files", ("vide.txt", b"   ", "text/plain"))]).status_code == 422

    api.post(f"/api/help/conversations/{conversation_id}/messages", headers=headers["agent"], json={"content": "Que dit la config ?"})
    assert "[Fichier joint : config.txt]" in prompts[0] and "vlan voix 10 trunk 4012" in prompts[0]
    reply = api.get(f"/api/help/conversations/{conversation_id}", headers=headers["agent"]).json()["messages"][-1]
    assert {"kind": "file", "title": "config.txt"} in reply["sources"]

    file_id = added.json()["files"][0]["id"]
    assert api.delete(f"{path}/{file_id}", headers=headers["agent"]).json()["files"] == []


def test_large_files_are_reduced_to_the_passages_closest_to_the_question():
    filler = "Ligne neutre sans intérêt. " * 400
    text = filler + "\n\nLe trunk SIP vers l'opérateur utilise le port 5061 en TLS.\n\n" + filler
    excerpt = help_console.file_excerpts([("sip.txt", text)], "Quel port pour le trunk SIP ?", budget=2000)
    assert len(excerpt) < 2600 and "port 5061" in excerpt


def test_free_text_mode_does_not_force_json_and_reasoning_is_extracted(monkeypatch):
    sent = {}

    class Response:
        def __init__(self, message):
            self.message = message

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": self.message, "prompt_eval_count": 1, "eval_count": 1}

    def post(url, json, timeout):
        sent.update(json)
        return Response({"content": "<think>Analyse interne</think>Réponse finale", "thinking": "Raisonnement natif"})

    monkeypatch.setattr(ai.httpx, "post", post)
    token = ai._json_format.set(False)
    try:
        assert ai._call_ollama("q", "m", think=True, timeout=5) == "Réponse finale"
    finally:
        ai._json_format.reset(token)
    assert "format" not in sent and ai._provider_thinking.get() == "Raisonnement natif\nAnalyse interne"
    ai._call_ollama("q", "m", think=False, timeout=5)
    assert sent["format"] == "json"  # les analyses de ticket restent en JSON


def test_ticket_mode_reads_the_ticket_attachments_and_flags_unreadable_ones(env, monkeypatch, tmp_path):
    from app.models import TicketAttachment

    api, factory, headers, ids = env
    monkeypatch.setattr(help_console.settings, "upload_dir", str(tmp_path))
    (tmp_path / "stored-a").write_bytes(b"interface vlan 10\n ip address 10.10.0.1 255.255.255.0")
    (tmp_path / "stored-b").write_bytes(b"\x89PNG")
    with factory() as db:
        uploader = db.scalar(select(User).where(User.email == "c@example.com")).id
        db.add_all([
            TicketAttachment(ticket_id=ids["ticket"], uploader_id=uploader, file_name="switch.conf", stored_name="stored-a", size=50),
            TicketAttachment(ticket_id=ids["ticket"], uploader_id=uploader, file_name="capture.png", stored_name="stored-b", size=4),
        ])
        db.commit()
    prompts = []
    monkeypatch.setattr(rag, "retrieve_context", lambda query: "Aucun extrait.")
    monkeypatch.setattr(help_console, "help_console_answer", lambda prompt, user_id, preference=None, role="staff": (prompts.append(prompt) or "ok", "ollama", "m", ""))

    body = create(api, headers["agent"], ticket_id=ids["ticket"]).json()
    assert [(f["filename"], f["origin"]) for f in body["files"]] == [("switch.conf", "ticket")]
    api.post(f"/api/help/conversations/{body['id']}/messages", headers=headers["agent"], json={"content": "Que contient la pièce jointe ?"})
    assert "ip address 10.10.0.1" in prompts[0] and "capture.png" in prompts[0] and "non lisibles" in prompts[0]

    (tmp_path / "stored-a").write_bytes(b"interface vlan 20")  # le fichier change : l'actualisation le relit sans doublon
    refreshed = api.patch(f"/api/help/conversations/{body['id']}", headers=headers["agent"], json={"refresh_ticket": True}).json()
    assert len(refreshed["files"]) == 1 and refreshed["files"][0]["chars"] == len("interface vlan 20")


def test_assistant_messages_carry_their_completion_time(env, monkeypatch):
    api, _factory, headers, _ids = env
    monkeypatch.setattr(rag, "retrieve_context", lambda query: "Aucun extrait.")
    monkeypatch.setattr(help_console, "help_console_answer", lambda prompt, user_id, preference=None, role="staff": ("ok", "ollama", "m", ""))
    conversation_id = create(api, headers["agent"]).json()["id"]
    api.post(f"/api/help/conversations/{conversation_id}/messages", headers=headers["agent"], json={"content": "Question"})
    user_message, reply = api.get(f"/api/help/conversations/{conversation_id}", headers=headers["agent"]).json()["messages"]
    assert user_message["completed_at"] is None and reply["completed_at"] >= reply["created_at"]


def test_anthropic_health_reflects_real_calls_and_failures(env):
    from datetime import datetime, timedelta, timezone

    from app.main import _anthropic_health
    from app.models import TicketEvent

    _api, factory, _headers, ids = env
    with factory() as db:
        assert _anthropic_health(db)[0] == "warning" and "aucun appel" in _anthropic_health(db)[1]
        now = datetime.now(timezone.utc)
        db.add(AIExecution(customer_id=ids["agent"], task="agent_assistance", provider="anthropic", model="claude-x",
                           duration_ms=2500, created_at=now - timedelta(minutes=5)))
        db.commit()
        status, detail = _anthropic_health(db)
        assert status == "healthy" and "claude-x" in detail and "2.5 s" in detail

        ticket = db.scalar(select(Ticket))
        db.add(TicketEvent(ticket_id=ticket.id, event_type="ai_provider_failed", created_at=now,
                           details={"provider": "anthropic", "model": "claude-x", "error": "401 clé invalide"}))
        db.commit()
        status, detail = _anthropic_health(db)
        assert status == "warning" and "401 clé invalide" in detail


def test_ticket_assignment_rules_for_agents_admins_and_customers(env):
    api, factory, headers, ids = env
    path = f"/api/tickets/{ids['ticket']}/assignment"
    with factory() as db:
        customer_id = db.scalar(select(User).where(User.email == "c@example.com")).id
        agent_id = db.scalar(select(User).where(User.email == "ag@example.com")).id
        other_agent = db.scalar(select(User).where(User.email == "ao@example.com"))
        admin_id = db.scalar(select(User).where(User.role == "admin")).id
        other_agent.companies = [db.get(Company, ids["a"])]  # un second agent affecté à l'entreprise
        db.commit()
        other_id = other_agent.id

    assert api.patch(path, headers=headers["admin"], json={"agent_id": customer_id}).status_code == 422  # jamais un client
    assert api.patch(path, headers=headers["agent"], json={"agent_id": customer_id}).status_code == 422
    assert api.patch(path, headers=headers["agent"], json={"agent_id": admin_id}).status_code == 403  # un agent n'assigne pas à un admin
    assert api.patch(path, headers=headers["agent"], json={"agent_id": agent_id}).status_code == 200  # il peut s'assigner
    assert api.patch(path, headers=headers["agent"], json={"agent_id": other_id}).json()["assigned_agent_id"] == other_id  # et à un collègue
    assert api.patch(path, headers=headers["admin"], json={"agent_id": admin_id}).json()["assigned_agent_id"] == admin_id  # l'admin se l'attribue
    assert api.patch(path, headers=headers["agent"], json={"agent_id": agent_id}).status_code == 200  # un agent peut reprendre le ticket
    assert api.patch(path, headers=headers["agent"], json={"agent_id": None}).status_code == 200


def test_anthropic_uses_the_configured_length_limit_and_flags_truncated_free_text(monkeypatch):
    sent = {}

    class Response:
        is_error = False

        def __init__(self, stop_reason):
            self.stop_reason = stop_reason

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "Début de réponse"}], "usage": {"input_tokens": 1, "output_tokens": 4096}, "stop_reason": self.stop_reason}

    def post(url, headers, json, timeout):
        sent.update(json)
        return Response(sent_stop[0])

    sent_stop = ["max_tokens"]
    monkeypatch.setattr(ai.settings, "anthropic_api_key", "cle-de-test")
    monkeypatch.setattr(ai.httpx, "post", post)
    token = ai._json_format.set(False)
    try:
        truncated = ai._call_anthropic("q", "claude-x", timeout=5)
        sent_stop[0] = "end_turn"
        complete = ai._call_anthropic("q", "claude-x", timeout=5)
    finally:
        ai._json_format.reset(token)
    assert sent["max_tokens"] == ai.settings.anthropic_max_tokens >= 4096
    assert "Réponse tronquée" in truncated and complete == "Début de réponse"


def test_model_json_is_extracted_from_fences_and_prose_and_garbage_is_explained():
    payload = '{"reply": "Bonjour", "priority": "high", "recommendations": [], "resolved": false, "escalation_recommended": false}'
    assert ai._parse_customer_result(f"```json\n{payload}\n```")["reply"] == "Bonjour"
    assert ai._parse_customer_result(f"Voici ma réponse :\n{payload}\nBonne journée.")["priority"] == "high"
    assert ai._parse_customer_result(payload)["reply"] == "Bonjour"
    with pytest.raises(ValueError, match="début : « Je suis désolé"):
        ai._parse_customer_result("Je suis désolé, je ne peux pas répondre en JSON.")
    with pytest.raises(ValueError, match="réponse vide"):
        ai._parse_customer_result("   ")


def test_console_failures_are_recorded_and_diagnostics_can_filter_them(env, monkeypatch):
    from app.models import AIFailure

    api, factory, headers, ids = env
    monkeypatch.setattr("app.database.SessionLocal", factory)
    monkeypatch.setattr(ai, "routing_for", lambda task, role: (True, [{"provider": "anthropic", "model": "claude-x", "think": False, "timeout_seconds": 5}]))

    def boom(prompt, model=None, timeout=5):
        raise RuntimeError("Erreur API Anthropic (529) : surcharge")

    monkeypatch.setattr(ai, "_call_anthropic", boom)
    with pytest.raises(ai.AIAnalysisError):
        ai.help_console_answer("Question", ids["agent"])
    with factory() as db:
        failure = db.scalar(select(AIFailure))
        assert failure.user_id == ids["agent"] and failure.provider == "anthropic" and "surcharge" in failure.error

    monkeypatch.setattr("app.ai_observability.provider_catalog", lambda: [])
    monkeypatch.setattr("app.ai_observability.embedding_status", lambda: {})
    monkeypatch.setattr("app.ai_observability.qdrant_status", lambda: {})
    everything = api.get("/api/admin/ai/diagnostics", headers=headers["admin"]).json()
    assert [(f["source"], f["reference"]) for f in everything["recent_failures"]] == [("console", "Console d’aide")]
    assert everything["console"]["failures_24h"] == 1 and everything["console"]["users"][0]["id"] == ids["agent"]
    assert api.get("/api/admin/ai/diagnostics?source=ticket", headers=headers["admin"]).json()["recent_failures"] == []
    only_console = api.get(f"/api/admin/ai/diagnostics?source=console&user_id={ids['agent']}", headers=headers["admin"]).json()
    assert len(only_console["recent_failures"]) == 1
    assert api.get("/api/admin/ai/diagnostics?source=console&user_id=inconnu", headers=headers["admin"]).json()["recent_failures"] == []
    assert api.get("/api/admin/ai/diagnostics?source=nimporte", headers=headers["admin"]).status_code == 422


def test_traced_runs_record_rag_and_provider_steps_and_the_timeline_explains_the_delay(env, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.ai_steps import step, traced_run
    from app.models import AIStep

    api, factory, headers, ids = env
    monkeypatch.setattr("app.database.SessionLocal", factory)

    @traced_run("Diagnostic client (IA)")
    def run(ticket_id):
        with step("rag", "rag.search", query_chars=42) as detail:
            detail["results"] = 3
        ai.record_step("provider", "ollama/deepseek-r1:1.5b", "failed", datetime.now(timezone.utc), 95_000, {"error": "délai dépassé"})
        ai.record_step("provider", "anthropic/claude-x", "ok", datetime.now(timezone.utc), 4_200, {"output_tokens": 300})

    run(ids["ticket"])
    ai.record_step("provider", "hors exécution", "ok", datetime.now(timezone.utc), 1)  # sans exécution : ignoré
    with factory() as db:
        rows = db.scalars(select(AIStep).order_by(AIStep.id)).all()
        assert [(row.kind, row.name) for row in rows] == [
            ("rag", "rag.search"), ("provider", "ollama/deepseek-r1:1.5b"), ("provider", "anthropic/claude-x"), ("run", "Diagnostic client (IA)"),
        ]
        assert len({row.run_id for row in rows}) == 1 and rows[1].status == "failed" and rows[0].detail == {"query_chars": 42, "results": 3}
        # Réponse IA publiée 100 s après la création du ticket : le temps perçu dépasse largement les appels réussis.
        ticket = db.get(Ticket, ids["ticket"])
        assistant = db.scalar(select(User).where(User.role == "agent"))
        assistant.role = "system"
        from app.models import Message

        db.add(Message(ticket_id=ticket.id, author_id=assistant.id, body="SECRET-CONTENU", visibility="public",
                       created_at=ticket.created_at + timedelta(seconds=100) if ticket.created_at.tzinfo else datetime.now(timezone.utc)))
        ticket.created_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        db.commit()
        for row in rows:
            row.started_at = ticket.created_at + timedelta(seconds=2)
        db.commit()
        db.query(Message).update({"created_at": ticket.created_at + timedelta(seconds=100)})
        db.commit()

    timeline = api.get("/api/admin/ai/ticket-timeline?reference=oxe-test0001", headers=headers["admin"])
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["tracing_complete"] is True and [item["id"] for item in body["participants"]][:3] == ["actor", "api", "task"]
    kinds = [item["kind"] for item in body["entries"]]
    assert kinds[0] == "ticket" and {"run", "rag", "provider", "message"} <= set(kinds)
    latency = body["latencies"][0]
    assert 99_000 <= latency["perceived_ms"] <= 101_000 and latency["provider_ok_ms"] == 4_200 and latency["provider_failed_ms"] == 95_000
    assert latency["failed_attempts"] == ["ollama/deepseek-r1:1.5b"]
    assert "SECRET-CONTENU" not in str(body)  # aucun contenu de message
    assert api.get("/api/admin/ai/ticket-timeline?reference=OXE-INCONNU", headers=headers["admin"]).status_code == 404
    assert api.get("/api/admin/ai/ticket-timeline?reference=OXE-TEST0001", headers=headers["agent"]).status_code == 403


def test_timeline_adds_wait_entries_for_queue_untracked_time_and_legacy_preparation(env):
    from datetime import datetime, timedelta, timezone

    from app.models import AIExecution, AIStep, Message
    from app.ticket_timeline import build_timeline

    _api, factory, _headers, ids = env
    with factory() as db:
        ticket = db.get(Ticket, ids["ticket"])
        base = datetime.now(timezone.utc) - timedelta(minutes=10)
        ticket.created_at = base
        system = db.scalar(select(User).where(User.role == "agent"))
        system.role = "system"
        # ticket antérieur au traçage : appel de 5 s terminé 60 s après la création, réponse publiée juste après
        db.add(AIExecution(ticket_id=ticket.id, customer_id=ids["agent"], task="customer_diagnosis", provider="anthropic", model="claude-x",
                           duration_ms=5_000, created_at=base + timedelta(seconds=60)))
        db.add(Message(ticket_id=ticket.id, author_id=system.id, body="x", visibility="public", created_at=base + timedelta(seconds=60, milliseconds=30)))
        db.commit()
        legacy = build_timeline(db, ticket)
        waits = [item for item in legacy["entries"] if item["kind"] == "wait"]
        assert len(waits) == 1 and 54_000 <= waits[0]["duration_ms"] <= 56_000 and "non détaillée" in waits[0]["title"]

        # ticket tracé : 20 s de file d'attente, traitement de 30 s dont 10 s détaillées
        db.query(AIExecution).delete()
        db.add(AIStep(run_id="r1", ticket_id=ticket.id, label="Diagnostic client (IA)", kind="run", name="Diagnostic client (IA)", status="ok",
                      started_at=base + timedelta(seconds=20), duration_ms=40_000))
        db.add(AIStep(run_id="r1", ticket_id=ticket.id, label="Diagnostic client (IA)", kind="provider", name="anthropic/claude-x", status="ok",
                      started_at=base + timedelta(seconds=50), duration_ms=10_000, detail={}))
        db.query(Message).update({"created_at": base + timedelta(seconds=60, milliseconds=30)})
        db.commit()
        traced = build_timeline(db, ticket)
        titles = [(item["title"], item["duration_ms"]) for item in traced["entries"] if item["kind"] == "wait"]
        assert ("Attente avant le démarrage du traitement", 20_000) in [(t, round(d, -3)) for t, d in titles]
        assert any(t == "Temps du traitement non détaillé" and 29_000 <= d <= 31_000 for t, d in titles)
        latency = traced["latencies"][0]
        assert latency["queue_ms"] >= 19_000 and latency["provider_ok_ms"] == 10_000


def test_internal_notes_are_not_counted_as_responses_and_waits_are_not_duplicated(env):
    from datetime import datetime, timedelta, timezone

    from app.models import AIExecution, Message
    from app.ticket_timeline import build_timeline

    _api, factory, _headers, ids = env
    with factory() as db:
        ticket = db.get(Ticket, ids["ticket"])
        base = datetime.now(timezone.utc) - timedelta(minutes=20)
        ticket.created_at = base
        system = db.scalar(select(User).where(User.role == "agent"))
        system.role = "system"
        db.add(AIExecution(ticket_id=ticket.id, customer_id=ids["agent"], task="customer_diagnosis", provider="anthropic", model="x",
                           duration_ms=3_000, created_at=base + timedelta(seconds=40)))
        db.add_all([
            Message(ticket_id=ticket.id, author_id=system.id, body="a", visibility="public", created_at=base + timedelta(seconds=40)),
            Message(ticket_id=ticket.id, author_id=system.id, body="liste KB", visibility="internal", created_at=base + timedelta(seconds=300)),
        ])
        db.commit()
        timeline = build_timeline(db, ticket)
    assert len(timeline["latencies"]) == 1  # la note interne automatique n'est pas une réponse à un message
    waits = [item for item in timeline["entries"] if item["kind"] == "wait"]
    assert len(waits) == 1
    assert any(item["title"] == "Note interne de l'assistant" for item in timeline["entries"])
