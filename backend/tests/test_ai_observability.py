import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import ai
from app.main import app
from app.models import AIExecution, Company, Ticket, User
from app.security import create_access_token, hash_password


@pytest.fixture
def observability_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        with testing_session() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with testing_session() as db:
        admin = User(
            email="ai-admin@example.com",
            full_name="Admin IA",
            password_hash=hash_password("mot-de-passe-admin"),
            role="admin",
        )
        db.add(admin)
        db.flush()
        company = Company(
            name="Entreprise IA",
            invitation_code_hash="hash-unique",
            created_by_id=admin.id,
        )
        db.add(company)
        db.flush()
        customer = User(
            email="ai-customer@example.com",
            full_name="Client IA",
            password_hash=hash_password("mot-de-passe-client"),
            role="customer",
            company_id=company.id,
        )
        db.add(customer)
        db.flush()
        ticket = Ticket(
            reference="OXE-AI000001",
            customer_id=customer.id,
            title="Test des appels entrants",
            description="Les appels entrants échouent depuis ce matin.",
            product="OXE",
            version="R12",
            status="new",
        )
        db.add(ticket)
        db.flush()
        execution = AIExecution(
            ticket_id=ticket.id,
            customer_id=customer.id,
            actor_id=customer.id,
            task="customer_diagnosis",
            provider="anthropic",
            model="claude-test",
            prompt="Prompt de test",
            response="Réponse de test",
            input_tokens=1000,
            output_tokens=500,
            duration_ms=1250,
        )
        db.add(execution)
        db.commit()
        ids = {"admin": admin.id, "customer": customer.id, "ticket": ticket.id, "execution": execution.id}

    yield TestClient(app), testing_session, ids
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def test_customer_can_withdraw_both_ai_consents_and_trace_content_is_purged(observability_client):
    api, testing_session, ids = observability_client
    customer_headers = _headers(ids["customer"])

    current = api.get("/api/auth/me", headers=customer_headers)
    assert current.status_code == 200
    assert current.json()["ai_processing_consent"] is True
    assert current.json()["ai_trace_consent"] is True

    updated = api.put("/api/auth/ai-consent", headers=customer_headers, json={
        "ai_processing_consent": False,
        "ai_trace_consent": False,
    })
    assert updated.status_code == 200
    assert updated.json()["ai_processing_consent"] is False
    assert updated.json()["ai_trace_consent"] is False

    with testing_session() as db:
        execution = db.get(AIExecution, ids["execution"])
        assert execution.prompt is None
        assert execution.response is None


def test_ticket_creation_skips_llm_when_processing_consent_is_disabled(observability_client):
    api, testing_session, ids = observability_client
    with testing_session() as db:
        customer = db.get(User, ids["customer"])
        customer.ai_processing_consent = False
        db.commit()

    response = api.post(
        "/api/tickets",
        headers=_headers(ids["customer"]),
        json={
            "title": "Appels entrants en échec",
            "description": "Les appels entrants vers le site ne fonctionnent plus.",
            "product": "OXE",
            "version": "R12",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "new"
    assert response.json()["ai_status"] == "disabled"
    assert "consentement" in response.json()["messages"][-1]["body"].lower()


def test_admin_can_filter_evaluate_traces_and_estimate_costs(observability_client):
    api, _testing_session, ids = observability_client
    admin_headers = _headers(ids["admin"])
    customer_headers = _headers(ids["customer"])

    assert api.get("/api/admin/ai/traces", headers=customer_headers).status_code == 403
    traces = api.get(
        f"/api/admin/ai/traces?ticket_id={ids['ticket']}&customer_id={ids['customer']}",
        headers=admin_headers,
    )
    assert traces.status_code == 200
    trace = traces.json()["items"][0]
    assert trace["prompt"] == "Prompt de test"
    assert trace["response"] == "Réponse de test"

    evaluation = api.patch(
        f"/api/admin/ai/traces/{ids['execution']}",
        headers=admin_headers,
        json={"score": 5, "note": "Réponse correcte."},
    )
    assert evaluation.status_code == 200
    assert evaluation.json()["evaluation_score"] == 5

    pricing = api.put("/api/admin/ai/pricing", headers=admin_headers, json={
        "prices": [{
            "provider": "anthropic",
            "model": "claude-test",
            "input_usd_per_million": 3,
            "output_usd_per_million": 15,
        }],
    })
    assert pricing.status_code == 200
    costs = api.get(
        f"/api/admin/ai/costs?ticket_id={ids['ticket']}",
        headers=admin_headers,
    )
    assert costs.status_code == 200
    assert costs.json()["total_count"] == 1
    assert costs.json()["items"][0]["cost_usd"] == pytest.approx(0.0105)


def test_admin_ai_diagnostics_show_provider_routes_and_ticket_failures(observability_client, monkeypatch):
    api, testing_session, ids = observability_client
    monkeypatch.setattr("app.ai_observability.provider_catalog", lambda: [{
        "id": "ollama",
        "label": "Ollama",
        "configured": True,
        "status": "healthy",
        "default_model": "qwen3:4b",
        "available_models": ["qwen3:4b"],
        "error": "",
    }, {
        "id": "anthropic",
        "label": "Anthropic / Claude",
        "configured": True,
        "status": "configured",
        "default_model": "claude-test",
        "available_models": ["claude-test"],
        "error": "Aucun appel testé.",
    }])
    monkeypatch.setattr("app.database.SessionLocal", testing_session)
    monkeypatch.setattr(ai, "routing_for", lambda *_args: (True, [
        {"provider": "ollama", "model": "qwen3:4b"},
    ]))
    monkeypatch.setattr(
        ai,
        "_call_ollama",
        lambda *_args, **_options: (_ for _ in ()).throw(RuntimeError("timed out")),
    )
    with testing_session() as db:
        ticket = db.get(Ticket, ids["ticket"])
        ticket.ai_status = "error"
        ticket.ai_error = "ollama/qwen3:4b: timed out"
        db.commit()

    context_token = ai._trace_context.set({
        "ticket_id": ids["ticket"],
        "customer_id": ids["customer"],
        "actor_id": ids["customer"],
    })
    try:
        with pytest.raises(RuntimeError, match="Tous les modèles affectés ont échoué"):
            ai._call_provider("prompt non conservé", "ticket_analysis", "customer")
    finally:
        ai._trace_context.reset(context_token)

    admin_headers = _headers(ids["admin"])
    customer_headers = _headers(ids["customer"])
    assert api.get("/api/admin/ai/diagnostics", headers=customer_headers).status_code == 403

    response = api.get(
        "/api/admin/ai/diagnostics?reference=oxe-ai000001",
        headers=admin_headers,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["providers"][0]["available_models"] == ["qwen3:4b"]
    anthropic = next(provider for provider in result["providers"] if provider["id"] == "anthropic")
    assert anthropic["status"] == "responded"
    assert anthropic["last_success_model"] == "claude-test"
    assert anthropic["last_success_at"] is not None
    ticket_analysis_route = next(
        route for route in result["routes"]
        if route["task"] == "ticket_analysis" and route["role"] == "customer"
    )
    assert ticket_analysis_route["models"]
    assert result["ticket"]["ai_error"] == "ollama/qwen3:4b: timed out"
    assert result["ticket"]["executions"][0]["provider"] == "anthropic"
    assert "prompt" not in result["ticket"]["executions"][0]
    failed_attempt = next(event for event in result["ticket"]["events"] if event["event_type"] == "ai_provider_failed")
    assert failed_attempt["provider"] == "ollama"
    assert failed_attempt["message"] == "timed out"
    assert result["recent_failures"][0]["reference"] == "OXE-AI000001"
    assert result["recent_failures"][0]["provider"] == "ollama"

    assert api.get(
        "/api/admin/ai/diagnostics?reference=OXE-UNKNOWN",
        headers=admin_headers,
    ).status_code == 404


def test_trace_consent_off_keeps_usage_without_prompt_or_response(observability_client, monkeypatch):
    _api, testing_session, ids = observability_client
    monkeypatch.setattr("app.database.SessionLocal", testing_session)
    with testing_session() as db:
        customer = db.get(User, ids["customer"])
        customer.ai_trace_consent = False
        db.commit()

    from app.ai_observability import record_execution

    record_execution(
        ticket_id=ids["ticket"],
        customer_id=ids["customer"],
        actor_id=ids["customer"],
        task="customer_diagnosis",
        provider="anthropic",
        model="claude-test",
        prompt="Contenu confidentiel",
        response="Réponse confidentielle",
        input_tokens=75,
        output_tokens=25,
        duration_ms=500,
    )

    with testing_session() as db:
        executions = db.query(AIExecution).order_by(AIExecution.created_at.desc()).all()
        newest = executions[0]
        assert newest.prompt is None
        assert newest.response is None
        assert newest.input_tokens == 75
        assert newest.output_tokens == 25
