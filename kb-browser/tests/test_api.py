from fastapi.testclient import TestClient

from app import main
from app.browser import NavigationFailed, SelectorFailed, SessionExpired


def client(monkeypatch, token="secret"):
    monkeypatch.setattr(main.config.__class__, "token", token, raising=False)
    object.__setattr__(main.config, "token", token)
    return TestClient(main.app)


def test_health_is_public_but_everything_else_needs_the_shared_token(monkeypatch):
    api = client(monkeypatch)

    assert api.get("/health").json()["status"] == "ok"
    assert api.get("/session").status_code == 401
    assert api.get("/logs", headers={"X-KB-Token": "mauvais"}).status_code == 401
    assert api.get("/logs", headers={"X-KB-Token": "secret"}).status_code == 200


def test_navigation_errors_are_translated_to_structured_http_errors(monkeypatch):
    api = client(monkeypatch)
    headers = {"X-KB-Token": "secret"}

    async def expired(*_args):
        raise SessionExpired("redirigé vers /login")

    async def selector(*_args):
        raise SelectorFailed("a.result", "https://kb/search")

    async def navigation(*_args):
        raise NavigationFailed("https://evil", "Hôte non autorisé")

    async def crash(*_args):
        raise RuntimeError("chromium planté")

    monkeypatch.setattr(main.browser, "search", expired)
    response = api.post("/search", headers=headers, json={"query": "stacking", "limit": 3})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "session_expired"

    monkeypatch.setattr(main.browser, "search", selector)
    detail = api.post("/search", headers=headers, json={"query": "stacking", "limit": 3}).json()["detail"]
    assert (detail["code"], detail["selector"]) == ("selector_failed", "a.result")

    monkeypatch.setattr(main.browser, "fetch_article", navigation)
    assert api.post("/article", headers=headers, json={"url": "https://evil/x"}).json()["detail"]["code"] == "navigation_failed"

    monkeypatch.setattr(main.browser, "fetch_article", crash)
    response = api.post("/article", headers=headers, json={"url": "https://kb.example/a"})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "unavailable"
