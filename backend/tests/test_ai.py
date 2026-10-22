from types import SimpleNamespace

import pytest

from app import ai


def test_anthropic_request_uses_supported_sonnet_55_parameters(monkeypatch):
    captured = {}

    class Response:
        is_error = False

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": "Réponse"}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(ai.httpx, "post", fake_post)
    monkeypatch.setattr(ai.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(ai.settings, "anthropic_model", "claude-sonnet-5-5")

    assert ai._call_anthropic("Prompt") == "Réponse"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["json"]["model"] == "claude-sonnet-5-5"
    assert "temperature" not in captured["json"]
    assert captured["json"]["output_config"] == {"effort": "medium"}


def test_anthropic_api_error_includes_response_details(monkeypatch):
    class Response:
        is_error = True
        status_code = 400
        reason_phrase = "Bad Request"

        def json(self):
            return {
                "error": {"message": "Invalid request parameter"},
                "request_id": "req_example",
            }

    monkeypatch.setattr(ai.httpx, "post", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(ai.settings, "anthropic_api_key", "test-key")

    with pytest.raises(RuntimeError, match="Invalid request parameter.*req_example"):
        ai._call_anthropic("Prompt")


def test_langgraph_analysis_runs_provider_and_returns_structured_result(monkeypatch):
    expected = {
        "summary": "Les appels entrants sont concernés.",
        "recommendations": ["Vérifier le périmètre des appels.", "Collecter les journaux."],
        "priority": "high",
        "escalation_recommended": True,
    }
    monkeypatch.setattr(ai, "_call_provider", lambda *_args: (__import__("json").dumps(expected), "ollama", "qwen3:4b"))
    monkeypatch.setattr(ai, "retrieve_context", lambda _query: "[Source : guide.pdf, page 2]\nVérification du routage.")
    monkeypatch.setattr(ai.settings, "ai_provider", "ollama")
    ticket = SimpleNamespace(title="Appels entrants en échec", description="Depuis ce matin les appels entrants échouent.", product="OXE", version="R12")

    assert ai.analyze_ticket(ticket) == {**expected, "provider": "ollama", "model": "qwen3:4b"}


def test_langgraph_analysis_rejects_invalid_model_response(monkeypatch):
    monkeypatch.setattr(ai, "_call_provider", lambda *_args: ('{"summary":"Réponse incomplète"}', "ollama", "qwen3:4b"))
    monkeypatch.setattr(ai, "retrieve_context", lambda _query: "Aucun extrait.")
    monkeypatch.setattr(ai.settings, "ai_provider", "ollama")
    ticket = SimpleNamespace(title="Incident OXE", description="Description suffisamment détaillée.", product="OXE", version="")

    try:
        ai.analyze_ticket(ticket)
    except ai.AIAnalysisError as exc:
        assert "priorité valide" in str(exc)
    else:
        raise AssertionError("Une réponse IA mal formée doit être rejetée.")


def test_customer_conversation_returns_guided_diagnostic(monkeypatch):
    expected = {
        "reply": "Vérifiez un appel entrant depuis un poste concerné.",
        "recommendations": ["Noter l’heure de l’appel."],
        "priority": "high",
        "resolved": False,
        "escalation_recommended": False,
    }
    prompts = []
    selected_tasks = []
    monkeypatch.setattr(ai, "retrieve_context", lambda _query: "[Source : guide.pdf, page 2]\nContrôler le routage.")
    monkeypatch.setattr(
        ai,
        "_call_provider",
        lambda prompt, task, role: (
            prompts.append(prompt) or selected_tasks.append((task, role))
            or __import__("json").dumps(expected),
            "ollama",
            "qwen3:4b",
        ),
    )
    ticket = SimpleNamespace(
        title="Appels entrants en échec",
        description="Les appels externes n’aboutissent plus.",
        product="OXE",
        version="R12",
    )

    result = ai.respond_to_customer(
        ticket,
        [
            {"role": "client", "body": "Les appels externes n’aboutissent plus."},
            {"role": "assistant", "body": "Depuis quand le problème a-t-il commencé ?"},
            {"role": "client", "body": "Depuis ce matin."},
        ],
        ["diagnostic.log"],
    )

    assert result == {**expected, "provider": "ollama", "model": "qwen3:4b"}
    assert "Depuis ce matin." in prompts[0]
    assert "diagnostic.log" in prompts[0]
    assert "[Source : guide.pdf, page 2]" in prompts[0]
    assert selected_tasks == [("customer_diagnosis", "customer")]


def test_first_customer_response_uses_initial_ticket_analysis_route(monkeypatch):
    selected_tasks = []
    expected = {
        "reply": "Décrivez un appel test et son résultat.",
        "recommendations": ["Préciser le périmètre."],
        "priority": "medium",
        "resolved": False,
        "escalation_recommended": False,
    }
    monkeypatch.setattr(ai, "retrieve_context", lambda _query: "Aucun extrait.")
    monkeypatch.setattr(
        ai,
        "_call_provider",
        lambda _prompt, task, role: (
            selected_tasks.append((task, role)) or __import__("json").dumps(expected),
            "ollama",
            "qwen3:4b",
        ),
    )
    ticket = SimpleNamespace(
        title="Appels entrants",
        description="Appels entrants en échec.",
        product="OXE",
        version="",
    )

    ai.respond_to_customer(
        ticket,
        [{"role": "client", "body": "Appels entrants en échec."}],
        [],
    )

    assert selected_tasks == [("ticket_analysis", "customer")]


def test_agent_assistance_uses_full_history_and_attachment_names(monkeypatch):
    prompts = []
    monkeypatch.setattr(ai, "retrieve_context", lambda _query: "[Source : manuel.pdf, page 4]\nContrôler le routage.")
    monkeypatch.setattr(
        ai,
        "_call_provider",
        lambda prompt, *_args: (prompts.append(prompt) or "Vérifier le périmètre avant toute modification.", "ollama", "qwen3:4b"),
    )
    ticket = SimpleNamespace(
        title="Appels entrants en échec",
        description="Les appels externes n’aboutissent plus.",
        product="OXE",
        version="R12",
    )
    conversation = [
        {"role": "client", "body": f"Échange {index}"}
        for index in range(35)
    ]

    answer = ai.assist_agent(ticket, conversation, ["diagnostic.log"], "Que vérifier ensuite ?")

    assert answer == "Vérifier le périmètre avant toute modification."
    assert "Échange 0" in prompts[0]
    assert "Échange 34" in prompts[0]
    assert "diagnostic.log" in prompts[0]
    assert "Que vérifier ensuite ?" in prompts[0]
    assert "[Source : manuel.pdf, page 4]" in prompts[0]


def test_provider_priority_falls_back_to_next_configured_model(monkeypatch):
    attempts = []
    monkeypatch.setattr(ai, "routing_for", lambda *_args: (True, [
        {"provider": "ollama", "model": "unavailable-model"},
        {"provider": "anthropic", "model": "claude-custom"},
    ]))

    def fail_ollama(_prompt, model):
        attempts.append(("ollama", model))
        raise RuntimeError("modèle Ollama introuvable")

    def use_anthropic(_prompt, model):
        attempts.append(("anthropic", model))
        return "Réponse de secours"

    monkeypatch.setattr(ai, "_call_ollama", fail_ollama)
    monkeypatch.setattr(ai, "_call_anthropic", use_anthropic)

    assert ai._call_provider("Prompt", "agent_assistance", "staff") == (
        "Réponse de secours", "anthropic", "claude-custom"
    )
    assert attempts == [
        ("ollama", "unavailable-model"),
        ("anthropic", "claude-custom"),
    ]


def test_provider_routing_can_disable_one_task_without_calling_models(monkeypatch):
    monkeypatch.setattr(ai, "routing_for", lambda *_args: (False, []))
    monkeypatch.setattr(
        ai,
        "_call_ollama",
        lambda *_args: (_ for _ in ()).throw(AssertionError("disabled AI must not call a model")),
    )

    with pytest.raises(ai.AIRoutingDisabledError, match="désactivée"):
        ai._call_provider("Prompt", "customer_diagnosis", "customer")


def test_provider_call_persists_usage_and_consent_eligible_content(monkeypatch):
    from app import ai_observability

    recorded = {}
    monkeypatch.setattr(ai, "routing_for", lambda *_args: (True, [{"provider": "ollama", "model": "local-test"}]))

    def fake_ollama(_prompt, _model):
        ai._provider_usage.set((120, 35))
        return "Réponse évaluée"

    monkeypatch.setattr(ai, "_call_ollama", fake_ollama)
    monkeypatch.setattr(ai_observability, "record_execution", lambda **kwargs: recorded.update(kwargs))
    trace_token = ai._trace_context.set({
        "ticket_id": "ticket-id",
        "customer_id": "customer-id",
        "actor_id": "customer-id",
    })
    try:
        assert ai._call_provider("Prompt soumis", "customer_diagnosis", "customer") == (
            "Réponse évaluée",
            "ollama",
            "local-test",
        )
    finally:
        ai._trace_context.reset(trace_token)

    assert recorded["prompt"] == "Prompt soumis"
    assert recorded["response"] == "Réponse évaluée"
    assert recorded["input_tokens"] == 120
    assert recorded["output_tokens"] == 35
    assert recorded["task"] == "customer_diagnosis"
