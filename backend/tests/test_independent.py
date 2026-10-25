"""Clients indépendants : entreprise individuelle, validation par l'admin, console d'aide dans leur périmètre."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import help_console, rag
from app.database import Base, get_db
from app.main import app
from app.models import Company, Message, Ticket, User
from app.security import create_access_token, hash_password

PASSWORD = "Motdepasse-solide-1"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(help_console, "SessionLocal", factory)
    monkeypatch.setattr("app.tickets.SessionLocal", factory)
    with factory() as db:
        admin = User(email="adm@example.com", full_name="Admin", password_hash=hash_password(PASSWORD), role="admin")
        db.add(admin)
        db.flush()
        shared = Company(name="Entreprise A", invitation_code_hash="ha", created_by_id=admin.id)
        db.add(shared)
        db.flush()
        member = User(email="m@example.com", full_name="Membre", password_hash="x", role="customer", company_id=shared.id)
        db.add(member)
        db.commit()
        headers = {
            "admin": {"Authorization": f"Bearer {create_access_token(admin.id)}"},
            "member": {"Authorization": f"Bearer {create_access_token(member.id)}"},
        }
        ids = {"admin": admin.id, "shared": shared.id}

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), factory, headers, ids
    app.dependency_overrides.clear()
    engine.dispose()


def register(api, email="jean@example.com", name="Jean Dupont", **extra):
    return api.post("/api/auth/register", json={
        "email": email, "full_name": name, "password": PASSWORD, "account_type": "independent", **extra,
    })


def approved_independent(api, factory, headers, email="jean@example.com", name="Jean Dupont"):
    assert register(api, email, name).status_code == 202
    with factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        user_id = user.id
    decision = api.patch(f"/api/users/{user_id}/approval", headers=headers["admin"], json={"decision": "approve"})
    assert decision.status_code == 200
    login = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200
    return user_id, {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_an_independent_registration_creates_a_private_individual_company_and_waits_for_approval(env):
    api, factory, headers, _ids = env
    assert register(api).status_code == 202
    assert register(api).status_code == 409  # même e-mail
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "jean@example.com"))
        company = db.get(Company, user.company_id)
        assert user.role == "customer" and user.account_status == "pending"
        assert company.is_individual and company.name == "Jean Dupont (indépendant)" and company.created_by_id == user.id
    assert api.post("/api/auth/login", json={"email": "jean@example.com", "password": PASSWORD}).status_code == 403

    pending = api.get("/api/users/requests", headers=headers["admin"]).json()
    assert [item["email"] for item in pending] == ["jean@example.com"] and pending[0]["is_individual"] is True


def test_two_independents_with_the_same_name_get_distinct_companies_and_company_registration_still_needs_a_code(env):
    api, factory, _headers, _ids = env
    assert register(api, "a@example.com").status_code == 202 and register(api, "b@example.com").status_code == 202
    with factory() as db:
        names = sorted(company.name for company in db.scalars(select(Company).where(Company.is_individual.is_(True))))
    assert names == ["Jean Dupont (indépendant)", "Jean Dupont (indépendant) 2"]
    refused = api.post("/api/auth/register", json={"email": "c@example.com", "full_name": "Sans Code", "password": PASSWORD})
    assert refused.status_code == 422


def test_an_individual_company_cannot_be_joined_with_a_code_and_has_no_invitation_code(env):
    api, factory, headers, _ids = env
    approved_independent(api, factory, headers)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.is_individual.is_(True)))
        company_id = company.id
    rotate = api.post(f"/api/companies/{company_id}/invitation-code", headers=headers["admin"])
    assert rotate.status_code == 409
    listing = api.get("/api/companies", headers=headers["admin"]).json()
    assert next(item for item in listing if item["id"] == company_id)["is_individual"] is True


def test_an_independent_uses_the_console_in_its_own_company_only(env, monkeypatch):
    api, factory, headers, ids = env
    user_id, own = approved_independent(api, factory, headers)
    assert api.get("/api/auth/me", headers=own).json()["is_individual"] is True
    assert api.get("/api/help/conversations", headers=headers["member"]).status_code == 403  # client d'une entreprise partagée

    options = api.get("/api/help/options", headers=own).json()
    assert options["independent"] is True and len(options["companies"]) == 1
    scopes = []
    monkeypatch.setattr(rag, "retrieve_context", lambda query: scopes.append(rag._company_scope.get()) or "Aucun extrait.")
    roles = []
    monkeypatch.setattr(help_console, "help_console_answer", lambda prompt, uid, preference=None, role="staff": (roles.append(role) or "ok", "ollama", "m", ""))

    created = api.post("/api/help/conversations", headers=own, json={"company_id": ids["shared"]})  # tentative d'accès à une autre entreprise
    assert created.status_code == 201 and created.json()["company_id"] == options["companies"][0]["id"]
    conversation_id = created.json()["id"]
    api.post(f"/api/help/conversations/{conversation_id}/messages", headers=own, json={"content": "Bonjour"})
    assert scopes == [options["companies"][0]["id"]] and roles == ["customer"]
    assert api.get(f"/api/help/conversations/{conversation_id}", headers=headers["admin"]).status_code == 404


def test_ticket_mode_for_an_independent_never_copies_internal_support_notes(env):
    api, factory, headers, ids = env
    user_id, own = approved_independent(api, factory, headers)
    with factory() as db:
        company_id = db.get(User, user_id).company_id
        ticket = Ticket(reference="OXE-IND00001", customer_id=user_id, title="Pas de tonalité", description="Poste muet")
        other = Ticket(reference="OXE-MEM00001", customer_id=db.scalar(select(User).where(User.email == "m@example.com")).id,
                       title="Ticket d'une autre entreprise", description="Autre")
        db.add_all([ticket, other])
        db.flush()
        db.add_all([
            Message(ticket_id=ticket.id, author_id=user_id, body="Message du client", visibility="public"),
            Message(ticket_id=ticket.id, author_id=ids["admin"], body="NOTE INTERNE CONFIDENTIELLE", visibility="internal"),
        ])
        db.commit()
        ticket_id, other_id = ticket.id, other.id

    created = api.post("/api/help/conversations", headers=own, json={"ticket_id": ticket_id})
    assert created.status_code == 201 and created.json()["company_id"] == company_id
    with factory() as db:
        from app.models import HelpConversation

        context = db.get(HelpConversation, created.json()["id"]).ticket_context
    assert "Message du client" in context and "NOTE INTERNE" not in context
    assert api.post("/api/help/conversations", headers=own, json={"ticket_id": other_id}).status_code == 404

    admin_view = api.post("/api/help/conversations", headers=headers["admin"], json={"ticket_id": ticket_id}).json()
    with factory() as db:
        assert "NOTE INTERNE CONFIDENTIELLE" in db.get(HelpConversation, admin_view["id"]).ticket_context  # l'équipe, elle, les voit
