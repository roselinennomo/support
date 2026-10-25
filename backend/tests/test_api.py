import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import _ensure_ai_consent_columns, _ensure_ticket_lifecycle_columns, _ensure_user_workflow_columns, app
from app.models import Company, SLAPolicy, Ticket, User
from app.security import create_access_token, hash_password, verify_password


@pytest.fixture
def client(monkeypatch):
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

    monkeypatch.setattr("app.tickets.SessionLocal", testing_session)
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), testing_session
    app.dependency_overrides.clear()
    engine.dispose()


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def user_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def test_admin_and_agents_can_manage_only_authorized_users(client):
    api, testing_session = client
    with testing_session() as db:
        admin = User(
            email="user-admin@example.com",
            full_name="Admin Utilisateurs",
            password_hash=hash_password("mot-de-passe-admin"),
            role="admin",
        )
        db.add(admin)
        db.flush()
        company = Company(
            name="Entreprise utilisateurs",
            invitation_code_hash="hash-users-one",
            created_by_id=admin.id,
        )
        other_company = Company(
            name="Entreprise externe",
            invitation_code_hash="hash-users-two",
            created_by_id=admin.id,
        )
        db.add_all([company, other_company])
        db.flush()
        agent = User(
            email="managed-agent@example.com",
            full_name="Agent gestionnaire",
            password_hash=hash_password("mot-de-passe-agent"),
            role="agent",
        )
        peer_agent = User(
            email="peer-agent@example.com",
            full_name="Agent collègue",
            password_hash=hash_password("mot-de-passe-agent"),
            role="agent",
        )
        outsider = User(
            email="outsider-agent@example.com",
            full_name="Agent externe",
            password_hash=hash_password("mot-de-passe-agent"),
            role="agent",
        )
        customer = User(
            email="managed-customer@example.com",
            full_name="Client entreprise",
            password_hash=hash_password("mot-de-passe-client"),
            role="customer",
            company_id=company.id,
        )
        other_customer = User(
            email="outsider-customer@example.com",
            full_name="Client externe",
            password_hash=hash_password("mot-de-passe-client"),
            role="customer",
            company_id=other_company.id,
        )
        db.add_all([agent, peer_agent, outsider, customer, other_customer])
        db.flush()
        company.agents = [agent, peer_agent]
        other_company.agents = [outsider]
        db.commit()
        ids = {
            "admin": admin.id,
            "agent": agent.id,
            "peer_agent": peer_agent.id,
            "outsider": outsider.id,
            "customer": customer.id,
            "other_customer": other_customer.id,
        }
        admin_headers = user_headers(admin)
        agent_headers = user_headers(agent)

    admin_users = api.get("/api/users", headers=admin_headers)
    assert admin_users.status_code == 200
    assert {item["id"] for item in admin_users.json()} >= set(ids.values())

    agent_users = api.get("/api/users", headers=agent_headers)
    assert agent_users.status_code == 200
    visible_ids = {item["id"] for item in agent_users.json()}
    assert {ids["peer_agent"], ids["customer"]} <= visible_ids
    assert ids["agent"] in visible_ids
    assert ids["outsider"] not in visible_ids
    assert ids["other_customer"] not in visible_ids

    updated_customer = api.patch(
        f"/api/users/{ids['customer']}",
        headers=agent_headers,
        json={"full_name": "Client modifié", "is_active": False},
    )
    assert updated_customer.status_code == 200
    assert updated_customer.json()["full_name"] == "Client modifié"
    assert updated_customer.json()["is_active"] is False
    assert api.patch(
        f"/api/users/{ids['customer']}",
        headers=agent_headers,
        json={"ai_processing_consent": False},
    ).status_code == 403
    assert api.patch(
        f"/api/users/{ids['outsider']}",
        headers=agent_headers,
        json={"full_name": "Intrusion"},
    ).status_code == 404
    assert api.put(
        f"/api/users/{ids['admin']}/password",
        headers=agent_headers,
        json={"temporary_password": "mot-de-passe-non-autorise"},
    ).status_code == 404
    assert api.put(
        f"/api/users/{ids['customer']}/password",
        headers=agent_headers,
        json={"temporary_password": "mot-de-passe-temporaire"},
    ).status_code == 204
    assert api.get("/api/auth/me", headers=user_headers(customer)).status_code == 401

    admin_update = api.patch(
        f"/api/users/{ids['customer']}",
        headers=admin_headers,
        json={
            "full_name": "Client administré",
            "ai_processing_consent": False,
            "ai_trace_consent": False,
            "account_status": "approved",
        },
    )
    assert admin_update.status_code == 200
    assert admin_update.json()["ai_processing_consent"] is False
    assert admin_update.json()["ai_trace_consent"] is False
    assert api.put(
        f"/api/users/{ids['outsider']}/password",
        headers=admin_headers,
        json={"temporary_password": "nouveau-mot-de-passe-admin"},
    ).status_code == 204

    with testing_session() as db:
        updated_customer = db.get(User, ids["customer"])
        assert verify_password("mot-de-passe-temporaire", updated_customer.password_hash)
        assert updated_customer.ai_trace_consent is False


def test_customer_access_requires_company_code_and_agent_approval(client, monkeypatch, tmp_path):
    api, testing_session = client
    monkeypatch.setattr("app.tickets.settings.upload_dir", str(tmp_path))
    monkeypatch.setattr(
        "app.tickets.respond_to_customer",
        lambda _ticket, _conversation, _attachments: {
            "reply": "Vérifiez le périmètre des appels et revenez avec le résultat.",
            "recommendations": ["Collecter les journaux."],
            "priority": "medium",
            "escalation_recommended": False,
            "resolved": False,
        },
    )
    with testing_session() as db:
        admin = User(
            email="admin-company@example.com",
            full_name="Administrateur",
            password_hash=hash_password("mot-de-passe-admin"),
            role="admin",
        )
        agent = User(
            email="agent@example.com",
            full_name="Agent Support",
            password_hash=hash_password("mot-de-passe-agent"),
            role="agent",
        )
        other_agent = User(
            email="other-agent@example.com",
            full_name="Autre agent",
            password_hash=hash_password("mot-de-passe-agent"),
            role="agent",
        )
        legacy_customer = User(
            email="legacy@example.com",
            full_name="Ancien client",
            password_hash=hash_password("ancien-mot-de-passe"),
            role="customer",
            account_status="pending",
        )
        db.add_all([admin, agent, other_agent, legacy_customer])
        db.commit()

    admin_login = api.post("/api/auth/login", json={
        "email": "admin-company@example.com",
        "password": "mot-de-passe-admin",
    })
    company_response = api.post("/api/companies", headers=auth_headers(admin_login.json()["access_token"]), json={
        "name": "Entreprise de test",
        "agent_ids": [agent.id],
    })
    assert company_response.status_code == 201
    old_company_code = company_response.json()["invitation_code"]
    rotated_code = api.post(
        f"/api/companies/{company_response.json()['id']}/invitation-code",
        headers=auth_headers(admin_login.json()["access_token"]),
    )
    assert rotated_code.status_code == 200
    company_code = rotated_code.json()["invitation_code"]
    assert "invitation_code" not in api.get(
        "/api/companies",
        headers=auth_headers(admin_login.json()["access_token"]),
    ).json()[0]

    admin_headers = auth_headers(admin_login.json()["access_token"])
    legacy_request = next(
        item for item in api.get("/api/users/requests", headers=admin_headers).json()
        if item["email"] == "legacy@example.com"
    )
    assert legacy_request["company_id"] is None
    assigned_legacy = api.patch(
        f"/api/users/{legacy_request['id']}/company",
        headers=admin_headers,
        json={"company_id": company_response.json()["id"]},
    )
    assert assigned_legacy.status_code == 200
    assert assigned_legacy.json()["company_name"] == "Entreprise de test"
    assert api.patch(
        f"/api/users/{legacy_request['id']}/approval",
        headers=admin_headers,
        json={"decision": "approve"},
    ).status_code == 200

    missing_code = api.post("/api/auth/register", json={
        "email": "client1@example.com",
        "full_name": "Client Un",
        "password": "un-mot-de-passe-solide",
        "company_code": old_company_code,
    })
    assert missing_code.status_code == 400

    first = api.post("/api/auth/register", json={
        "email": "client1@example.com",
        "full_name": "Client Un",
        "password": "un-mot-de-passe-solide",
        "company_code": company_code,
    })
    assert first.status_code == 202
    assert first.json()["status"] == "pending"
    pending_login = api.post("/api/auth/login", json={
        "email": "client1@example.com",
        "password": "un-mot-de-passe-solide",
    })
    assert pending_login.status_code == 403
    assert api.post("/api/tickets", json={
        "title": "Les appels entrants échouent",
        "description": "Depuis ce matin, les appels externes vers notre site sont en échec.",
    }).status_code == 401

    other_agent_login = api.post("/api/auth/login", json={
        "email": "other-agent@example.com",
        "password": "mot-de-passe-agent",
    })
    other_agent_headers = auth_headers(other_agent_login.json()["access_token"])
    assert api.get("/api/users/requests", headers=other_agent_headers).json() == []
    admin_pending = api.get(
        "/api/users/requests",
        headers=auth_headers(admin_login.json()["access_token"]),
    ).json()[0]
    assert api.patch(
        f"/api/users/{admin_pending['id']}/approval",
        headers=other_agent_headers,
        json={"decision": "approve"},
    ).status_code == 404

    agent_login = api.post("/api/auth/login", json={
        "email": "agent@example.com",
        "password": "mot-de-passe-agent",
    })
    agent_headers = auth_headers(agent_login.json()["access_token"])
    pending_requests = api.get("/api/users/requests", headers=agent_headers)
    assert pending_requests.status_code == 200
    assert pending_requests.json()[0]["company_name"] == "Entreprise de test"
    approved = api.patch(
        f"/api/users/{pending_requests.json()[0]['id']}/approval",
        headers=agent_headers,
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    first_login = api.post("/api/auth/login", json={
        "email": "client1@example.com",
        "password": "un-mot-de-passe-solide",
    })
    first_headers = auth_headers(first_login.json()["access_token"])

    with testing_session() as db:
        db.add(SLAPolicy(
            company_id=company_response.json()["id"],
            created_by_id=admin.id,
            reviewed_by_id=admin.id,
            ticket_type="incident",
            priority="p3",
            workaround_hours=2,
            resolution_hours=24,
            support_units_per_hour=1,
            customer_penalty_units=0.5,
            status="approved",
        ))
        db.commit()

    created = api.post("/api/tickets", headers=first_headers, json={
        "title": "Les appels entrants échouent",
        "description": "Depuis ce matin, les appels externes vers notre site sont en échec.",
        "product": "OXE",
        "version": "R12",
    })
    assert created.status_code == 201
    ticket = created.json()
    assert ticket["workaround_due_at"] is not None
    assert ticket["resolution_due_at"] is not None
    assert ticket["status"] == "ai_triage"
    assert ticket["ai_status"] == "pending"
    ticket = api.get(f"/api/tickets/{ticket['id']}", headers=first_headers).json()
    assert ticket["ai_status"] == "complete"
    assert ticket["ai_recommendations"] == ["Collecter les journaux."]
    assert ticket["ai_summary"] == "Vérifiez le périmètre des appels et revenez avec le résultat."
    assert ticket["messages"][-1]["author_type"] == "ai"
    assert api.get("/api/tickets", headers=other_agent_headers).json() == []
    assert api.get(f"/api/tickets/{ticket['id']}", headers=other_agent_headers).status_code == 404

    premature_handoff = api.post(
        f"/api/tickets/{ticket['id']}/handoff",
        headers=first_headers,
        json={"reason": "Je souhaite parler à un agent."},
    )
    assert premature_handoff.status_code == 409
    assert "Répondez d’abord" in premature_handoff.json()["detail"]

    customer_turn = api.post(
        f"/api/tickets/{ticket['id']}/conversation",
        headers=first_headers,
        data={"body": "J’ai vérifié les postes concernés, les appels échouent toujours."},
        files={"files": ("diagnostic.log", b"journal de diagnostic", "text/plain")},
    )
    assert customer_turn.status_code == 200
    assert customer_turn.json()["attachments"][0]["file_name"] == "diagnostic.log"
    attachment_url = customer_turn.json()["attachments"][0]["download_url"]
    assert api.get(f"/api{attachment_url}", headers=first_headers).content == b"journal de diagnostic"
    assert api.get(f"/api{attachment_url}", headers=other_agent_headers).status_code == 404

    handoff_prompts = []
    monkeypatch.setattr(
        "app.tickets.assist_agent",
        lambda _ticket, conversation, _attachments, question, **kwargs: (
            handoff_prompts.append((conversation, question, kwargs))
            or "Résumé des échanges : appels toujours en échec après vérification.\n\n"
            "Premières recommandations : contrôler le routage entrant."
        ),
    )
    handed_off = api.post(
        f"/api/tickets/{ticket['id']}/handoff",
        headers=first_headers,
        json={"reason": "Le diagnostic initial n’a pas permis de résoudre l’incident."},
    )
    assert handed_off.status_code == 200
    assert handed_off.json()["status"] == "new"
    assert any(event["event_type"] == "ai_handoff" for event in handed_off.json()["events"])
    assert any(
        item["id"] == ticket["id"]
        for item in api.get("/api/tickets", headers=agent_headers).json()
    )
    assert len(handoff_prompts) == 1
    assert "J’ai vérifié les postes concernés" in str(handoff_prompts[0][0])
    assert "Premières recommandations" in handoff_prompts[0][1]
    assert handoff_prompts[0][2] == {
        "task": "handoff_summary",
        "role": "customer",
        "actor_id": ticket["customer_id"],
    }
    handoff_agent_view = api.get(f"/api/tickets/{ticket['id']}", headers=agent_headers).json()
    assert any(
        message["visibility"] == "internal"
        and message["body"].startswith("Résumé des échanges")
        for message in handoff_agent_view["messages"]
    )
    handoff_customer_view = api.get(f"/api/tickets/{ticket['id']}", headers=first_headers).json()
    assert all(message["visibility"] == "public" for message in handoff_customer_view["messages"])
    assert not any(
        event["event_type"] == "ai_handoff_summary"
        for event in handoff_customer_view["events"]
    )

    monkeypatch.setattr(
        "app.tickets.assist_agent",
        lambda *_args, **_kwargs: "Vérifier ensuite le routage entrant.",
    )
    agent_assist = api.post(
        f"/api/tickets/{ticket['id']}/ai-assist",
        headers=agent_headers,
        json={"question": "Quelle vérification sûre proposer ensuite ?"},
    )
    assert agent_assist.status_code == 202
    agent_view = api.get(f"/api/tickets/{ticket['id']}", headers=agent_headers).json()
    assert any(
        message["visibility"] == "internal" and message["body"] == "Vérifier ensuite le routage entrant."
        for message in agent_view["messages"]
    )
    assert api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={"status": "resolved"},
    ).status_code == 422
    assert api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={"status": "in_progress"},
    ).status_code == 200
    assert api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={"status": "escalated"},
    ).status_code == 422  # l'escalade manuelle n'existe plus : seule l'IA transmet au support
    assert api.post(
        f"/api/tickets/{ticket['id']}/escalate",
        headers=agent_headers,
        json={"team": "Expertise OXE", "reason": "Analyse approfondie demandée."},
    ).status_code in {404, 405}
    support_reply = api.post(
        f"/api/tickets/{ticket['id']}/messages",
        headers=agent_headers,
        json={"body": "Nous analysons votre configuration.", "visibility": "public"},
    )
    assert support_reply.status_code == 200
    assert support_reply.json()["status"] == "in_progress"
    waiting_customer = api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={"status": "waiting_customer"},
    )
    assert waiting_customer.status_code == 200
    customer_reply = api.post(
        f"/api/tickets/{ticket['id']}/conversation",
        headers=first_headers,
        data={"body": "Voici les informations demandées."},
    )
    assert customer_reply.status_code == 200
    assert customer_reply.json()["status"] == "waiting_support"
    assert customer_reply.json()["workaround_due_at"] == waiting_customer.json()["workaround_due_at"]
    assert api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={"status": "in_progress"},
    ).status_code == 200
    assert api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={"status": "resolved"},
    ).status_code == 422
    assert api.post(f"/api/tickets/{ticket['id']}/close", headers=first_headers).status_code == 409
    resolved = api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={
            "status": "resolved",
            "resolution_note": "Redémarrage du service de routage et validation par appel entrant.",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolution_note"].startswith("Redémarrage")
    reopened = api.post(
        f"/api/tickets/{ticket['id']}/conversation",
        headers=first_headers,
        data={"body": "Le problème est toujours présent après le redémarrage."},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "waiting_support"
    assert any(event["event_type"] == "ticket_reopened" for event in reopened.json()["events"])
    assert api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={"status": "in_progress"},
    ).status_code == 200
    resolved_again = api.patch(
        f"/api/tickets/{ticket['id']}/status",
        headers=agent_headers,
        json={
            "status": "resolved",
            "resolution_note": "Correction du routage puis test concluant avec le demandeur.",
        },
    )
    assert resolved_again.status_code == 200
    assert any(
        message["body"].startswith("Correction du routage") and message["visibility"] == "public"
        for message in api.get(f"/api/tickets/{ticket['id']}", headers=first_headers).json()["messages"]
    )
    closed = api.post(f"/api/tickets/{ticket['id']}/close", headers=first_headers)
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert api.post(
        f"/api/tickets/{ticket['id']}/conversation",
        headers=first_headers,
        data={"body": "Le problème revient après la clôture."},
    ).status_code == 409
    assert all(message["visibility"] == "public" for message in api.get(
        f"/api/tickets/{ticket['id']}", headers=first_headers
    ).json()["messages"])

    monkeypatch.setattr(
        "app.tickets.respond_to_customer",
        lambda *_args: {
            "reply": "Une vérification complémentaire est nécessaire; je transmets le dossier au support.",
            "recommendations": ["Transmettre les journaux à l’équipe support."],
            "priority": "high",
            "escalation_recommended": True,
            "resolved": False,
        },
    )
    ai_handoff = api.post("/api/tickets", headers=first_headers, json={
        "title": "Échecs d’appels après vérification",
        "description": "Les appels externes échouent toujours après les vérifications initiales.",
        "product": "OXE",
    })
    assert ai_handoff.status_code == 201
    assert ai_handoff.json()["status"] == "ai_triage"
    assert ai_handoff.json()["ai_status"] == "pending"
    escalated_ticket = api.get(
        f"/api/tickets/{ai_handoff.json()['id']}",
        headers=first_headers,
    ).json()
    assert escalated_ticket["status"] == "ai_triage"
    assert escalated_ticket["ai_escalation_recommended"] is True
    assert not any(event["event_type"] == "ai_handoff" for event in escalated_ticket["events"])
    after_ai_recommendation = api.post(
        f"/api/tickets/{ai_handoff.json()['id']}/conversation",
        headers=first_headers,
        data={"body": "J’ai effectué la vérification recommandée, le problème persiste."},
    )
    assert after_ai_recommendation.status_code == 200
    assert after_ai_recommendation.json()["status"] == "ai_triage"
    manual_ai_handoff = api.post(
        f"/api/tickets/{ai_handoff.json()['id']}/handoff",
        headers=first_headers,
        json={"reason": "Les vérifications initiales n’ont pas résolu l’incident."},
    )
    assert manual_ai_handoff.status_code == 200
    assert manual_ai_handoff.json()["status"] == "new"
    rejected_ticket = api.patch(
        f"/api/tickets/{ai_handoff.json()['id']}/status",
        headers=agent_headers,
        json={
            "status": "rejected",
            "rejection_reason": "La demande concerne un équipement hors du périmètre de support.",
        },
    )
    assert rejected_ticket.status_code == 200
    assert rejected_ticket.json()["status"] == "rejected"
    assert rejected_ticket.json()["rejection_reason"].startswith("La demande concerne")
    customer_rejection_view = api.get(
        f"/api/tickets/{ai_handoff.json()['id']}",
        headers=first_headers,
    ).json()
    assert any(
        message["visibility"] == "public"
        and "Motif : La demande concerne un équipement hors du périmètre de support."
        in message["body"]
        for message in customer_rejection_view["messages"]
    )
    assert api.post(
        f"/api/tickets/{ai_handoff.json()['id']}/conversation",
        headers=first_headers,
        data={"body": "Je souhaite répondre au rejet."},
    ).status_code == 409

    monkeypatch.setattr(
        "app.tickets.respond_to_customer",
        lambda *_args: {
            "reply": "Le routage était resté sur l’ancien profil. Le rétablissement du profil corrige le problème.",
            "recommendations": ["Confirmer le fonctionnement des appels."],
            "priority": "medium",
            "escalation_recommended": False,
            "resolved": True,
        },
    )
    ai_resolved = api.post("/api/tickets", headers=first_headers, json={
        "title": "Profil de routage incorrect",
        "description": "Les appels entrants sont dirigés vers un ancien profil depuis ce matin.",
        "product": "OXE",
    })
    assert ai_resolved.status_code == 201
    ai_resolved_ticket = api.get(
        f"/api/tickets/{ai_resolved.json()['id']}",
        headers=first_headers,
    ).json()
    assert ai_resolved_ticket["status"] == "resolved"
    assert ai_resolved_ticket["resolution_note"].startswith("Le routage était resté")
    assert api.post(
        f"/api/tickets/{ai_resolved.json()['id']}/close",
        headers=first_headers,
    ).json()["status"] == "closed"

    second = api.post("/api/auth/register", json={
        "email": "client2@example.com",
        "full_name": "Client Deux",
        "password": "un-autre-mot-de-passe",
        "company_code": company_code,
    })
    assert second.status_code == 202
    second_request = api.get("/api/users/requests", headers=agent_headers).json()[0]
    approved_second = api.patch(
        f"/api/users/{second_request['id']}/approval",
        headers=agent_headers,
        json={"decision": "approve"},
    )
    assert approved_second.status_code == 200
    second_login = api.post("/api/auth/login", json={
        "email": "client2@example.com",
        "password": "un-autre-mot-de-passe",
    })
    second_headers = auth_headers(second_login.json()["access_token"])
    # Les collègues d'une même entreprise partagent la visibilité des tickets.
    colleague_ids = {item["id"] for item in api.get("/api/tickets", headers=second_headers).json()}
    assert {ticket["id"], ai_resolved.json()["id"]} <= colleague_ids
    colleague_view = api.get(f"/api/tickets/{ticket['id']}", headers=second_headers)
    assert colleague_view.status_code == 200
    assert colleague_view.json()["customer_name"] != "Client Deux"
    assert all(message["visibility"] == "public" for message in colleague_view.json()["messages"])

    rejected = api.post("/api/auth/register", json={
        "email": "client-refuse@example.com",
        "full_name": "Client Refusé",
        "password": "mot-de-passe-refuse",
        "company_code": company_code,
    })
    assert rejected.status_code == 202
    rejected_request = api.get("/api/users/requests", headers=agent_headers).json()[0]
    assert api.patch(
        f"/api/users/{rejected_request['id']}/approval",
        headers=agent_headers,
        json={"decision": "reject"},
    ).status_code == 200
    assert api.post("/api/auth/login", json={
        "email": "client-refuse@example.com",
        "password": "mot-de-passe-refuse",
    }).status_code == 403

    note = api.post(f"/api/tickets/{ticket['id']}/messages", headers=agent_headers, json={
        "body": "Échange interne réservé à l’équipe.",
        "visibility": "internal",
    })
    assert note.status_code == 200
    customer_view = api.get(f"/api/tickets/{ticket['id']}", headers=first_headers).json()
    assert all(message["visibility"] == "public" for message in customer_view["messages"])
    assert not any(
        event["event_type"] == "message_added" and event["details"].get("visibility") == "internal"
        for event in customer_view["events"]
    )


def test_default_local_admin_email_can_log_in(client):
    api, testing_session = client
    with testing_session() as db:
        db.add(User(
            email="admin@support.local",
            full_name="Administrateur",
            password_hash=hash_password("mot-de-passe-admin"),
            role="admin",
        ))
        db.commit()

    response = api.post("/api/auth/login", json={
        "email": "admin@support.local",
        "password": "mot-de-passe-admin",
    })

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "admin@support.local"
    assert response.json()["user"]["role"] == "admin"


def test_admin_can_access_system_overview(client):
    api, testing_session = client
    with testing_session() as db:
        db.add(User(
            email="admin-supervision@example.com",
            full_name="Admin supervision",
            password_hash=hash_password("mot-de-passe-admin"),
            role="admin",
        ))
        db.commit()

    login = api.post("/api/auth/login", json={
        "email": "admin-supervision@example.com",
        "password": "mot-de-passe-admin",
    })
    assert login.status_code == 200

    response = api.get("/api/system/overview", headers=auth_headers(login.json()["access_token"]))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert {service["name"] for service in payload["services"]} >= {"database", "api", "ollama", "queue"}
    assert isinstance(payload["metrics"]["open_tickets"], int)


def test_reserved_non_local_email_is_still_rejected(client):
    api, _testing_session = client

    response = api.post("/api/auth/login", json={
        "email": "admin@support.test",
        "password": "mot-de-passe-admin",
    })

    assert response.status_code == 422


def test_users_can_view_profile_and_rotate_password(client):
    api, testing_session = client
    with testing_session() as db:
        admin = User(
            email="profile-admin@example.com",
            full_name="Administratrice",
            password_hash=hash_password("ancien-mot-de-passe"),
            role="admin",
        )
        db.add(admin)
        db.commit()
        headers = user_headers(admin)

    profile = api.get("/api/auth/me", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["full_name"] == "Administratrice"
    assert profile.json()["role"] == "admin"

    wrong_current_password = api.put(
        "/api/auth/password",
        headers=headers,
        json={"current_password": "mauvais-mot-de-passe", "new_password": "nouveau-mot-de-passe"},
    )
    assert wrong_current_password.status_code == 400

    changed = api.put(
        "/api/auth/password",
        headers=headers,
        json={"current_password": "ancien-mot-de-passe", "new_password": "nouveau-mot-de-passe"},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["email"] == "profile-admin@example.com"
    assert api.get("/api/auth/me", headers=headers).status_code == 401
    assert api.get(
        "/api/auth/me",
        headers=auth_headers(changed.json()["access_token"]),
    ).status_code == 200
    assert api.post("/api/auth/login", json={
        "email": "profile-admin@example.com",
        "password": "ancien-mot-de-passe",
    }).status_code == 401
    assert api.post("/api/auth/login", json={
        "email": "profile-admin@example.com",
        "password": "nouveau-mot-de-passe",
    }).status_code == 200


def test_existing_database_gets_account_workflow_columns(monkeypatch):
    legacy_engine = create_engine("sqlite://")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(254), role VARCHAR(20))"))
        connection.execute(text(
            "INSERT INTO users (id, email, role) VALUES ('legacy', 'legacy@example.com', 'customer')"
        ))
    monkeypatch.setattr("app.main.engine", legacy_engine)

    _ensure_user_workflow_columns()
    _ensure_ai_consent_columns()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("users")}
    assert {"company_id", "account_status", "approved_by_id", "approved_at", "token_version"} <= columns
    assert {"ai_processing_consent", "ai_trace_consent", "ai_consent_updated_at"} <= columns
    with legacy_engine.connect() as connection:
        account_status = connection.scalar(text("SELECT account_status FROM users WHERE id = 'legacy'"))
        consents = connection.execute(text(
            "SELECT ai_processing_consent, ai_trace_consent FROM users WHERE id = 'legacy'"
        )).one()
    assert account_status == "pending"
    assert consents == (1, 1)
    legacy_engine.dispose()


def test_existing_database_gets_ticket_lifecycle_columns(monkeypatch):
    legacy_engine = create_engine("sqlite://")
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE tickets (id VARCHAR(36) PRIMARY KEY, status VARCHAR(30), ai_summary TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO tickets (id, status, ai_summary) VALUES "
            "('legacy-resolved', 'resolved', 'Solution historique issue du diagnostic IA.')"
        ))

    monkeypatch.setattr("app.main.engine", legacy_engine)
    _ensure_ticket_lifecycle_columns()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("tickets")}
    assert {"resolution_note", "rejection_reason"} <= columns
    with legacy_engine.connect() as connection:
        note = connection.scalar(text(
            "SELECT resolution_note FROM tickets WHERE id = 'legacy-resolved'"
        ))
    assert note == "Solution historique issue du diagnostic IA."
    legacy_engine.dispose()


def test_rag_admin_routes_require_admin_and_manage_corpus(client, monkeypatch, tmp_path):
    api, testing_session = client
    monkeypatch.setattr("app.main.settings.rag_documents_dir", str(tmp_path))
    monkeypatch.setattr("app.main.settings.rag_max_file_size", 5)
    monkeypatch.setattr("app.main.settings.rag_max_files_per_upload", 5)
    monkeypatch.setattr("app.main.inspect_index", lambda: {
        "collection": "support_documents",
        "embedding_model": "nomic-embed-text",
        "embedding_dimension": 768,
        "qdrant": {"status": "healthy", "error": "", "collection_exists": True},
        "embedding": {"status": "healthy", "error": ""},
        "metrics": {
            "source_count": 1,
            "indexed_sources": 0,
            "pending_sources": 1,
            "deleted_sources": 0,
            "chunk_count": 0,
            "empty_sources": 0,
        },
        "documents": [],
        "deleted_sources": [],
    })
    monkeypatch.setattr(
        "app.main.evaluate_query",
        lambda query: {"query": query, "result_count": 0, "results": []},
    )
    monkeypatch.setattr(
        "app.main.sync_documents",
        lambda progress_callback=None: {"source_count": 1, "indexed_sources": 1, "deleted_sources": 0},
    )
    with testing_session() as db:
        admin = User(
            email="rag-admin@example.com",
            full_name="Admin RAG",
            password_hash=hash_password("mot-de-passe-admin"),
            role="admin",
        )
        agent = User(
            email="rag-agent@example.com",
            full_name="Agent RAG",
            password_hash=hash_password("mot-de-passe-agent"),
            role="agent",
        )
        db.add_all([admin, agent])
        db.commit()

    admin_login = api.post("/api/auth/login", json={
        "email": "rag-admin@example.com",
        "password": "mot-de-passe-admin",
    })
    agent_login = api.post("/api/auth/login", json={
        "email": "rag-agent@example.com",
        "password": "mot-de-passe-agent",
    })
    admin_headers = auth_headers(admin_login.json()["access_token"])
    agent_headers = auth_headers(agent_login.json()["access_token"])

    assert api.get("/api/admin/rag").status_code == 401
    assert api.get("/api/admin/rag/index").status_code == 401
    assert api.get("/api/admin/rag", headers=agent_headers).status_code == 403
    assert api.get("/api/admin/rag/index", headers=agent_headers).status_code == 403
    assert api.post("/api/admin/rag/index", headers=agent_headers).status_code == 403
    assert api.post(
        "/api/admin/rag/documents",
        headers=agent_headers,
        files={"files": ("forbidden.txt", b"hello", "text/plain")},
    ).status_code == 403
    status = api.get("/api/admin/rag", headers=admin_headers)
    assert status.status_code == 200
    assert status.json()["limits"] == {"max_file_size": 5, "max_files": 5}
    assert api.get("/api/admin/rag/index", headers=admin_headers).status_code == 200
    assert api.post(
        "/api/admin/rag/search",
        headers=admin_headers,
        json={"query": "Comment vérifier le service ?"},
    ).json()["result_count"] == 0

    unsupported = api.post(
        "/api/admin/rag/documents",
        headers=admin_headers,
        files={"files": ("image.png", b"hello", "image/png")},
    )
    assert unsupported.status_code == 415
    oversized = api.post(
        "/api/admin/rag/documents",
        headers=admin_headers,
        files={"files": ("large.txt", b"123456", "text/plain")},
    )
    assert oversized.status_code == 413
    uploaded = api.post(
        "/api/admin/rag/documents",
        headers=admin_headers,
        files={"files": ("guide.md", b"guide", "text/markdown")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {"uploaded": ["guide.md"], "indexing_required": True}
    duplicate = api.post(
        "/api/admin/rag/documents",
        headers=admin_headers,
        files={"files": ("guide.md", b"other", "text/markdown")},
    )
    assert duplicate.status_code == 409

    indexed = api.post("/api/admin/rag/index", headers=admin_headers)
    assert indexed.status_code == 202
    assert indexed.json() == {"status": "pending"}
    job = api.get("/api/admin/rag", headers=admin_headers).json()["indexing"]
    assert job["status"] == "complete"
    assert job["result"]["indexed_sources"] == 1


def test_ai_routing_is_admin_only_persistent_and_requires_active_model(client, monkeypatch):
    api, testing_session = client
    monkeypatch.setattr("app.ai_routing.settings.anthropic_api_key", "")
    monkeypatch.setattr("app.main.provider_catalog", lambda: [
        {
            "id": "ollama",
            "label": "Ollama",
            "configured": True,
            "available": True,
            "default_model": "qwen3:4b",
            "available_models": ["qwen3:4b", "qwen3:8b"],
            "error": "",
        },
        {
            "id": "anthropic",
            "label": "Anthropic / Claude",
            "configured": False,
            "available": False,
            "default_model": "claude-sonnet-5-5",
            "available_models": [],
            "error": "La clé API n’est pas configurée.",
        },
    ])
    with testing_session() as db:
        admin = User(
            email="ai-routing-admin@example.com",
            full_name="Admin IA",
            password_hash=hash_password("mot-de-passe-admin"),
            role="admin",
        )
        agent = User(
            email="ai-routing-agent@example.com",
            full_name="Agent IA",
            password_hash=hash_password("mot-de-passe-agent"),
            role="agent",
        )
        db.add_all([admin, agent])
        db.commit()

    admin_login = api.post("/api/auth/login", json={
        "email": "ai-routing-admin@example.com",
        "password": "mot-de-passe-admin",
    })
    agent_login = api.post("/api/auth/login", json={
        "email": "ai-routing-agent@example.com",
        "password": "mot-de-passe-agent",
    })
    admin_headers = auth_headers(admin_login.json()["access_token"])
    agent_headers = auth_headers(agent_login.json()["access_token"])

    assert api.get("/api/admin/ai-routing").status_code == 401
    assert api.get("/api/admin/ai-routing", headers=agent_headers).status_code == 403
    current = api.get("/api/admin/ai-routing", headers=admin_headers)
    assert current.status_code == 200
    assert set(current.json()["tasks"]) == {
        "customer_diagnosis", "agent_assistance", "handoff_summary", "ticket_analysis",
    }
    assert len(current.json()["providers"]) == 2

    routes = {
        task: {
            "customer": {
                "enabled": task in {"customer_diagnosis", "handoff_summary"},
                "models": [{"provider": "ollama", "model": "qwen3:4b"}]
                if task in {"customer_diagnosis", "handoff_summary"} else [],
            },
            "staff": {
                "enabled": task in {"agent_assistance", "ticket_analysis"},
                "models": [{"provider": "ollama", "model": "qwen3:8b"}]
                if task in {"agent_assistance", "ticket_analysis"} else [],
            },
        }
        for task in ("customer_diagnosis", "agent_assistance", "handoff_summary", "ticket_analysis")
    }
    payload = {"enabled": False, "routes": routes}
    saved = api.put("/api/admin/ai-routing", headers=admin_headers, json=payload)
    assert saved.status_code == 200
    # Les options think/timeout absentes de la requête reçoivent leurs valeurs par défaut.
    assert saved.json()["routes"]["agent_assistance"]["staff"]["models"] == [
        {"provider": "ollama", "model": "qwen3:8b", "think": False, "timeout_seconds": 120}
    ]
    reread = api.get("/api/admin/ai-routing", headers=admin_headers).json()
    assert reread["enabled"] is False
    assert reread["routes"]["agent_assistance"]["staff"]["models"][0]["model"] == "qwen3:8b"

    tuned_routes = {task: {role: dict(route) for role, route in role_routes.items()}
                    for task, role_routes in routes.items()}
    tuned_routes["agent_assistance"]["staff"] = {
        "enabled": True,
        "models": [
            {"provider": "ollama", "model": "deepseek-r1:1.5b", "think": True, "timeout_seconds": 600},
        ],
    }
    tuned = api.put("/api/admin/ai-routing", headers=admin_headers, json={"enabled": True, "routes": tuned_routes})
    assert tuned.status_code == 200
    assert tuned.json()["routes"]["agent_assistance"]["staff"]["models"][0] == {
        "provider": "ollama", "model": "deepseek-r1:1.5b", "think": True, "timeout_seconds": 600,
    }
    tuned_routes["agent_assistance"]["staff"]["models"][0]["timeout_seconds"] = 5
    assert api.put(
        "/api/admin/ai-routing", headers=admin_headers, json={"enabled": True, "routes": tuned_routes},
    ).status_code == 422

    invalid = {
        "enabled": True,
        "routes": {
            task: {
                role: {"enabled": False, "models": []}
                for role in ("customer", "staff")
            }
            for task in ("customer_diagnosis", "agent_assistance", "handoff_summary", "ticket_analysis")
        },
    }
    assert api.put("/api/admin/ai-routing", headers=admin_headers, json=invalid).status_code == 422

    anthropic_routes = {task: {role: dict(route) for role, route in role_routes.items()}
                        for task, role_routes in routes.items()}
    anthropic_routes["customer_diagnosis"]["customer"] = {
        "enabled": True,
        "models": [{"provider": "anthropic", "model": "claude-custom"}],
    }
    anthropic = api.put(
        "/api/admin/ai-routing",
        headers=admin_headers,
        json={"enabled": True, "routes": anthropic_routes},
    )
    assert anthropic.status_code == 422


def test_customers_share_tickets_within_their_company_only(client, monkeypatch):
    api, testing_session = client
    monkeypatch.setattr("app.tickets._run_customer_ai", lambda _ticket_id: None)
    with testing_session() as db:
        admin = User(email="share-admin@example.com", full_name="Admin", password_hash=hash_password("x" * 12), role="admin")
        db.add(admin)
        db.flush()
        company = Company(name="Entreprise A", invitation_code_hash="hash-share-a", created_by_id=admin.id)
        other_company = Company(name="Entreprise B", invitation_code_hash="hash-share-b", created_by_id=admin.id)
        db.add_all([company, other_company])
        db.flush()
        author = User(email="author@example.com", full_name="Auteur", password_hash=hash_password("x" * 12), role="customer", company_id=company.id)
        colleague = User(email="colleague@example.com", full_name="Collègue", password_hash=hash_password("x" * 12), role="customer", company_id=company.id)
        outsider = User(email="intruder@example.com", full_name="Intrus", password_hash=hash_password("x" * 12), role="customer", company_id=other_company.id)
        db.add_all([author, colleague, outsider])
        db.commit()
        author_headers, colleague_headers, outsider_headers = (
            user_headers(author), user_headers(colleague), user_headers(outsider),
        )

    created = api.post("/api/tickets", headers=author_headers, json={
        "title": "Postes IP hors service",
        "description": "Plusieurs postes IP Touch perdent leur enregistrement.",
        "product": "OXE",
    })
    assert created.status_code == 201
    ticket_id = created.json()["id"]

    assert [item["id"] for item in api.get("/api/tickets", headers=colleague_headers).json()] == [ticket_id]
    shared = api.get(f"/api/tickets/{ticket_id}", headers=colleague_headers)
    assert shared.status_code == 200
    assert shared.json()["customer_name"] == "Auteur"

    # Après transfert à l'équipe support, le collègue peut répondre comme l'auteur.
    with testing_session() as db:
        db.get(Ticket, ticket_id).status = "new"
        db.commit()
    reply = api.post(f"/api/tickets/{ticket_id}/messages", headers=colleague_headers, json={"body": "Même problème chez moi."})
    assert reply.status_code == 200
    assert any(message["author_name"] == "Collègue" for message in reply.json()["messages"])
    assert api.post(
        f"/api/tickets/{ticket_id}/messages", headers=colleague_headers,
        json={"body": "Note interne", "visibility": "internal"},
    ).status_code == 403
    assert api.post(
        f"/api/tickets/{ticket_id}/messages", headers=outsider_headers, json={"body": "Intrusion"},
    ).status_code == 404

    assert api.get("/api/tickets", headers=outsider_headers).json() == []
    assert api.get(f"/api/tickets/{ticket_id}", headers=outsider_headers).status_code == 404


def _routing_payload(customer_models, staff_models):
    return {
        "enabled": True,
        "routes": {
            task: {
                "customer": {"enabled": True, "models": customer_models},
                "staff": {"enabled": True, "models": staff_models},
            }
            for task in ("customer_diagnosis", "agent_assistance", "handoff_summary", "ticket_analysis")
        },
    }


def test_users_choose_models_among_those_assigned_by_admin(client, monkeypatch):
    api, testing_session = client
    monkeypatch.setattr("app.tickets._run_customer_ai", lambda _ticket_id: None)
    agent_calls = []
    monkeypatch.setattr("app.tickets._run_agent_ai", lambda *args: agent_calls.append(args))
    with testing_session() as db:
        admin = User(email="pick-admin@example.com", full_name="Admin", password_hash=hash_password("x" * 12), role="admin")
        db.add(admin)
        db.flush()
        company = Company(name="Entreprise choix", invitation_code_hash="hash-pick", created_by_id=admin.id)
        db.add(company)
        db.flush()
        customer = User(email="pick-customer@example.com", full_name="Client", password_hash=hash_password("x" * 12), role="customer", company_id=company.id)
        colleague = User(email="pick-colleague@example.com", full_name="Collègue", password_hash=hash_password("x" * 12), role="customer", company_id=company.id)
        agent = User(email="pick-agent@example.com", full_name="Agent", password_hash=hash_password("x" * 12), role="agent")
        db.add_all([customer, colleague, agent])
        db.flush()
        company.agents = [agent]
        db.commit()
        admin_h, customer_h, colleague_h, agent_h = (user_headers(user) for user in (admin, customer, colleague, agent))

    # Sans configuration enregistrée : modèle par défaut deepseek-r1:1.5b avec think activé.
    options = api.get("/api/ai/options", headers=customer_h).json()
    assert options["default"] == {"provider": "ollama", "model": "deepseek-r1:1.5b", "think": True}
    # qwen3:4b (OLLAMA_EXTRA_MODELS) est proposé dès la configuration initiale, sans raisonnement.
    assert [(item["model"], item["think"]) for item in options["models"]] == [
        ("deepseek-r1:1.5b", True), ("qwen3:4b", False),
    ]
    assert api.get("/api/ai/options").status_code == 401

    qwen = {"provider": "ollama", "model": "qwen3:4b", "think": False, "timeout_seconds": 200}
    deepseek = {"provider": "ollama", "model": "deepseek-r1:1.5b", "think": True, "timeout_seconds": 300}
    saved = api.put("/api/admin/ai-routing", headers=admin_h, json=_routing_payload([deepseek, qwen], [qwen]))
    assert saved.status_code == 200

    customer_options = api.get("/api/ai/options", headers=customer_h).json()
    assert [item["model"] for item in customer_options["models"]] == ["deepseek-r1:1.5b", "qwen3:4b"]
    assert customer_options["default"]["model"] == "deepseek-r1:1.5b"
    assert [item["model"] for item in api.get("/api/ai/options", headers=agent_h).json()["models"]] == ["qwen3:4b"]

    # Le client choisit un modèle autorisé et le raisonnement à la création du ticket.
    created = api.post("/api/tickets", headers=customer_h, json={
        "title": "Postes IP hors service",
        "description": "Plusieurs postes IP Touch perdent leur enregistrement.",
        "product": "OXE",
        "ai_model": {"provider": "ollama", "model": "qwen3:4b", "think": True},
    })
    assert created.status_code == 201
    assert created.json()["ai_preferences"] == {"provider": "ollama", "model": "qwen3:4b", "think": True}
    ticket_id = created.json()["id"]

    refused = api.post("/api/tickets", headers=customer_h, json={
        "title": "Postes IP hors service",
        "description": "Plusieurs postes IP Touch perdent leur enregistrement.",
        "product": "OXE",
        "ai_model": {"provider": "ollama", "model": "llama3:70b"},
    })
    assert refused.status_code == 422

    # Un collègue de l'entreprise peut modifier le choix; sans `think`, la valeur de l'admin s'applique.
    changed = api.put(f"/api/tickets/{ticket_id}/ai-preferences", headers=colleague_h, json={
        "provider": "ollama", "model": "deepseek-r1:1.5b",
    })
    assert changed.status_code == 200
    assert changed.json()["ai_preferences"] == {"provider": "ollama", "model": "deepseek-r1:1.5b", "think": True}
    assert api.put(
        f"/api/tickets/{ticket_id}/ai-preferences", headers=customer_h,
        json={"provider": "anthropic", "model": "claude-custom"},
    ).status_code == 422
    assert api.put(
        f"/api/tickets/{ticket_id}/ai-preferences", headers=agent_h,
        json={"provider": "ollama", "model": "qwen3:4b"},
    ).status_code == 403

    # L'agent choisit son modèle pour le résumé du ticket, parmi ceux de l'équipe support.
    with testing_session() as db:
        db.get(Ticket, ticket_id).status = "new"
        db.commit()
    summary = api.post(f"/api/tickets/{ticket_id}/ai-assist", headers=agent_h, json={
        "kind": "summary", "ai_model": {"provider": "ollama", "model": "qwen3:4b", "think": False},
    })
    assert summary.status_code == 202
    _ticket, _question, _actor, preference, is_summary = agent_calls[-1]
    assert preference == {"provider": "ollama", "model": "qwen3:4b", "think": False}
    assert is_summary is True
    assert api.post(f"/api/tickets/{ticket_id}/ai-assist", headers=agent_h, json={"question": "ok"}).status_code == 422
    assert api.post(
        f"/api/tickets/{ticket_id}/ai-assist", headers=agent_h,
        json={"question": "Que vérifier ensuite ?", "ai_model": {"provider": "ollama", "model": "deepseek-r1:1.5b"}},
    ).status_code == 422  # deepseek n'est pas affecté à l'équipe support

    reset = api.delete("/api/admin/ai-routing", headers=admin_h)
    assert reset.status_code == 200
    assert reset.json()["routes"]["customer_diagnosis"]["customer"]["models"][0]["model"] == "deepseek-r1:1.5b"
    assert api.delete("/api/admin/ai-routing", headers=customer_h).status_code == 403


def _rag_admin_and_agent(testing_session, suffix):
    with testing_session() as db:
        admin = User(email=f"rag-{suffix}-admin@example.com", full_name="Admin", password_hash=hash_password("x" * 12), role="admin")
        agent = User(email=f"rag-{suffix}-agent@example.com", full_name="Agent", password_hash=hash_password("x" * 12), role="agent")
        db.add_all([admin, agent])
        db.commit()
        return user_headers(admin), user_headers(agent)


def test_admin_switches_embedding_model_and_reindexes(client, monkeypatch):
    from app import main, rag

    api, testing_session = client
    admin_h, agent_h = _rag_admin_and_agent(testing_session, "embed")
    ran = []
    monkeypatch.setattr(main, "_run_rag_indexing", lambda: ran.append(True))
    monkeypatch.setattr(main, "installed_ollama_models", lambda: {"qwen3-embedding:0.6b", "qwen3-embedding:4b"})
    main._rag_index_job["status"] = "idle"
    try:
        assert api.put("/api/admin/rag/embedding-model", headers=agent_h, json={"model": "bge-m3"}).status_code == 403
        assert api.put("/api/admin/rag/embedding-model", headers=admin_h, json={"model": "nomic-embed-text"}).status_code == 422
        unchanged = api.put("/api/admin/rag/embedding-model", headers=admin_h, json={"model": "qwen3-embedding:0.6b"})
        assert unchanged.json()["indexing"] == "unchanged"

        # bge-m3 n'est pas installé; qwen3-embedding:4b l'est mais ne doit pas valider le :0.6b.
        missing = api.put("/api/admin/rag/embedding-model", headers=admin_h, json={"model": "bge-m3"})
        assert missing.status_code == 409
        assert "ollama-embed" in missing.json()["detail"]
        assert rag.embedding_model() == "qwen3-embedding:0.6b"

        switched = api.put("/api/admin/rag/embedding-model", headers=admin_h, json={"model": "qwen3-embedding:4b"})
        assert switched.status_code == 202
        assert switched.json() == {"embedding_model": "qwen3-embedding:4b", "indexing": "pending"}
        assert ran == [True]
        assert rag.embedding_model() == "qwen3-embedding:4b"
        assert rag.embedding_dimension() == 2560
        assert rag.collection_name().endswith("qwen3-embedding-4b")

        # Pas de second changement pendant la réindexation.
        assert api.put(
            "/api/admin/rag/embedding-model", headers=admin_h, json={"model": "qwen3-embedding:0.6b"},
        ).status_code == 409
    finally:
        rag.set_embedding_model(None)
        main._rag_index_job["status"] = "idle"


def test_admin_deletes_documents_from_corpus_and_index(client, monkeypatch, tmp_path):
    from app import main, rag

    api, testing_session = client
    admin_h, agent_h = _rag_admin_and_agent(testing_session, "delete")
    monkeypatch.setattr(main.settings, "rag_documents_dir", str(tmp_path))
    (tmp_path / "guide.md").write_text("guide", encoding="utf-8")
    (tmp_path / "old.txt").write_text("old", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path.parent / "outside.txt").write_text("outside", encoding="utf-8")
    removed = []
    monkeypatch.setattr(main, "delete_sources", lambda sources: removed.extend(sources))
    main._rag_index_job["status"] = "idle"

    assert api.request("DELETE", "/api/admin/rag/documents", headers=agent_h, json={"names": ["guide.md"]}).status_code == 403
    assert api.request("DELETE", "/api/admin/rag/documents", headers=admin_h, json={"names": []}).status_code == 422
    # Aucun fichier n'est supprimé si l'un des noms est invalide ou sort du corpus.
    for bad in ("absent.txt", "../outside.txt"):
        refused = api.request("DELETE", "/api/admin/rag/documents", headers=admin_h, json={"names": ["guide.md", bad]})
        assert refused.status_code == 404
    assert (tmp_path / "guide.md").exists() and (tmp_path.parent / "outside.txt").exists()

    main._rag_index_job["status"] = "running"
    assert api.request("DELETE", "/api/admin/rag/documents", headers=admin_h, json={"names": ["guide.md"]}).status_code == 409
    main._rag_index_job["status"] = "idle"

    deleted = api.request("DELETE", "/api/admin/rag/documents", headers=admin_h, json={"names": ["guide.md", "old.txt"]})
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": ["guide.md", "old.txt"], "index_updated": True}
    assert removed == ["guide.md", "old.txt"]
    assert not (tmp_path / "guide.md").exists() and not (tmp_path / "old.txt").exists()
    assert (tmp_path / "keep.txt").exists()

    # Qdrant indisponible : les fichiers sont supprimés, la prochaine synchronisation nettoiera l'index.
    monkeypatch.setattr(main, "delete_sources", lambda _sources: (_ for _ in ()).throw(rag.RAGError("qdrant down")))
    degraded = api.request("DELETE", "/api/admin/rag/documents", headers=admin_h, json={"names": ["keep.txt"]})
    assert degraded.status_code == 200
    assert degraded.json() == {"deleted": ["keep.txt"], "index_updated": False}
    assert not (tmp_path / "keep.txt").exists()


def test_supervision_diagnostics_and_pricing_expose_generation_and_embedding_separately(client, monkeypatch):
    from app import ai_observability, main

    api, testing_session = client
    admin_h, _agent_h = _rag_admin_and_agent(testing_session, "visibility")
    embedding = {
        "status": "warning",
        "error": "",
        "base_url": "http://ollama-embed:11434",
        "dedicated_instance": True,
        "model": "qwen3-embedding:0.6b",
        "dimension": 1024,
        "collection": "support_documents_qwen3-embedding-0-6b",
        "installed_models": ["bge-m3"],
        "options": [{"id": "bge-m3", "label": "BGE-M3", "dimension": 1024, "installed": True, "selected": False, "selectable": True}],
    }
    qdrant = {"status": "healthy", "error": "", "collection": embedding["collection"], "collection_exists": False}
    for module in (main, ai_observability):
        monkeypatch.setattr(module, "embedding_status", lambda: embedding)
        monkeypatch.setattr(module, "qdrant_status", lambda: qdrant)
    monkeypatch.setattr(ai_observability, "provider_catalog", lambda: [
        {"id": "ollama", "available_models": ["deepseek-r1:1.5b", "qwen3:4b"]},
        {"id": "anthropic", "available_models": []},
    ])

    services = {item["name"]: item for item in api.get("/api/system/overview", headers=admin_h).json()["services"]}
    assert {"ollama", "embedding", "rag"} <= set(services)
    assert services["embedding"]["status"] == "warning"
    assert "qwen3-embedding:0.6b" in services["embedding"]["detail"]
    assert "embedding" not in services["ollama"]["detail"].lower()
    assert services["rag"]["status"] == "healthy"
    assert "support_documents_qwen3-embedding-0-6b" in services["rag"]["detail"]

    diagnostics = api.get("/api/admin/ai/diagnostics", headers=admin_h).json()
    assert diagnostics["embedding"]["model"] == "qwen3-embedding:0.6b"
    assert diagnostics["embedding"]["dedicated_instance"] is True
    assert diagnostics["rag"]["collection_exists"] is False

    pricing = api.get("/api/admin/ai/pricing", headers=admin_h).json()
    assert pricing["available_models"]["ollama"] == ["deepseek-r1:1.5b", "qwen3:4b"]


class _KBScraperDouble:
    def __init__(self, healthy=True, state="valid"):
        self.healthy, self.state = healthy, state

    def health(self):
        return self.healthy

    def session_state(self):
        from app.kb.interfaces import SessionState

        return SessionState(self.state)

    def logs(self, limit=100):
        return [{"ts": "2026-01-01T00:00:00Z", "level": "warning", "event": "selector_failed", "selector": "input.q"}]


def _kb_users(testing_session):
    with testing_session() as db:
        admin = User(email="kb-admin@example.com", full_name="Admin", password_hash=hash_password("x" * 12), role="admin")
        agent = User(email="kb-agent@example.com", full_name="Agent", password_hash=hash_password("x" * 12), role="agent")
        db.add_all([admin, agent])
        db.flush()
        company = Company(name="Entreprise KB", invitation_code_hash="hash-kb", created_by_id=admin.id)
        db.add(company)
        db.flush()
        customer = User(email="kb-customer@example.com", full_name="Client", password_hash=hash_password("x" * 12), role="customer", company_id=company.id)
        db.add(customer)
        db.flush()
        company.agents = [agent]
        db.commit()
        return user_headers(admin), user_headers(agent), user_headers(customer)


def test_kb_admin_console_is_admin_only_and_inert_when_disabled_by_the_server(client, monkeypatch):
    api, testing_session = client
    admin_h, agent_h, _customer_h = _kb_users(testing_session)
    monkeypatch.setattr("app.kb.flags.settings.enable_kb_browser_scraper", False)
    monkeypatch.setattr("app.kb.router.get_scraper", lambda: (_ for _ in ()).throw(AssertionError("no browser call")))

    assert api.get("/api/admin/kb", headers=agent_h).status_code == 403
    assert api.get("/api/admin/kb").status_code == 401
    status = api.get("/api/admin/kb", headers=admin_h).json()
    assert (status["env_enabled"], status["effective"], status["browser_online"]) == (False, False, False)
    assert status["metrics"]["searches"] == 0 and status["recent_jobs"] == []
    assert api.put("/api/admin/kb/settings", headers=admin_h, json={"enabled": True}).status_code == 409
    assert api.post("/api/admin/kb/session/check", headers=admin_h).status_code == 409
    assert api.post("/api/admin/kb/reindex", headers=admin_h).status_code == 409
    services = {item["name"] for item in api.get("/api/system/overview", headers=admin_h).json()["services"]}
    assert "kb" not in services


def test_kb_admin_console_toggle_session_and_debug(client, monkeypatch):
    api, testing_session = client
    admin_h, _agent_h, _customer_h = _kb_users(testing_session)
    scraper = _KBScraperDouble()
    monkeypatch.setattr("app.kb.flags.settings.enable_kb_browser_scraper", True)
    monkeypatch.setattr("app.kb.router.get_scraper", lambda: scraper)

    status = api.get("/api/admin/kb", headers=admin_h).json()
    assert (status["env_enabled"], status["enabled"], status["effective"], status["browser_online"]) == (True, True, True, True)

    toggled = api.put("/api/admin/kb/settings", headers=admin_h, json={"enabled": False})
    assert toggled.json() == {"enabled": False, "effective": False}
    assert api.get("/api/admin/kb", headers=admin_h).json()["effective"] is False
    kb_service = next(item for item in api.get("/api/system/overview", headers=admin_h).json()["services"] if item["name"] == "kb")
    assert kb_service["status"] == "disabled"
    api.put("/api/admin/kb/settings", headers=admin_h, json={"enabled": True})

    scraper.state = "expired"
    checked = api.post("/api/admin/kb/session/check", headers=admin_h).json()
    assert checked["state"] == "expired" and "expirée" in checked["last_alert"]
    kb_service = next(item for item in api.get("/api/system/overview", headers=admin_h).json()["services"] if item["name"] == "kb")
    assert kb_service["status"] == "warning" and "expirée" in kb_service["detail"]

    scraper.state = "valid"
    assert api.post("/api/admin/kb/session/check", headers=admin_h).json()["last_alert"] == ""

    scraper.healthy = False
    assert api.get("/api/admin/kb", headers=admin_h).json()["browser_online"] is False
    kb_service = next(item for item in api.get("/api/system/overview", headers=admin_h).json()["services"] if item["name"] == "kb")
    assert kb_service["status"] == "warning" and "hors ligne" in kb_service["detail"]

    debug = api.get("/api/admin/kb/debug", headers=admin_h).json()
    assert debug["browser_logs"][0]["selector"] == "input.q" and debug["jobs"] == []


def test_ticket_creation_schedules_kb_enrichment_in_background_without_blocking(client, monkeypatch):
    api, testing_session = client
    _admin_h, _agent_h, customer_h = _kb_users(testing_session)
    monkeypatch.setattr("app.tickets._run_customer_ai", lambda _ticket_id: None)
    scheduled = []
    monkeypatch.setattr("app.tickets.enqueue_ticket_enrichment", lambda ticket_id: scheduled.append(ticket_id) or True)

    created = api.post("/api/tickets", headers=customer_h, json={
        "title": "Switch OS6860E perd le stacking",
        "description": "Après redémarrage, le lien de stacking est coupé sur le switch.",
        "product": "OmniSwitch",
    })

    assert created.status_code == 201
    assert scheduled == [created.json()["id"]]


def test_agent_can_request_kb_enrichment_only_when_enabled_and_consented(client, monkeypatch):
    api, testing_session = client
    _admin_h, agent_h, customer_h = _kb_users(testing_session)
    monkeypatch.setattr("app.tickets._run_customer_ai", lambda _ticket_id: None)
    ticket_id = api.post("/api/tickets", headers=customer_h, json={
        "title": "Switch OS6860E perd le stacking",
        "description": "Après redémarrage, le lien de stacking est coupé sur le switch.",
    }).json()["id"]
    with testing_session() as db:
        db.get(Ticket, ticket_id).status = "new"
        db.commit()
    queued = []
    monkeypatch.setattr("app.tickets.enqueue_ticket_enrichment", lambda ticket_id_: queued.append(ticket_id_) or True)

    monkeypatch.setattr("app.tickets.kb_flags.is_enabled", lambda: False)
    assert api.post(f"/api/tickets/{ticket_id}/kb-enrich", headers=agent_h).status_code == 409
    assert api.post(f"/api/tickets/{ticket_id}/kb-enrich", headers=customer_h).status_code == 403

    monkeypatch.setattr("app.tickets.kb_flags.is_enabled", lambda: True)
    accepted = api.post(f"/api/tickets/{ticket_id}/kb-enrich", headers=agent_h)
    assert accepted.status_code == 202 and accepted.json() == {"status": "queued"}
    assert queued == [ticket_id]
    options = api.get("/api/ai/options", headers=agent_h).json()
    assert options["kb_enabled"] is True
    assert "kb_enabled" not in api.get("/api/ai/options", headers=customer_h).json()


def test_tickets_left_thinking_by_a_restart_get_their_ai_diagnosis_relaunched(client, monkeypatch):
    from app import tickets

    api, testing_session = client
    _admin_h, _agent_h, customer_h = _kb_users(testing_session)
    monkeypatch.setattr("app.tickets._run_customer_ai", lambda _ticket_id: None)
    pending = api.post("/api/tickets", headers=customer_h, json={
        "title": "Postes IP hors service",
        "description": "Plusieurs postes IP Touch perdent leur enregistrement.",
    }).json()["id"]
    done = api.post("/api/tickets", headers=customer_h, json={
        "title": "Autre incident réglé",
        "description": "Un incident déjà traité par l'assistant sans problème.",
    }).json()["id"]
    with testing_session() as db:
        db.get(Ticket, done).ai_status = "complete"
        db.commit()
    relaunched = []
    monkeypatch.setattr(tickets, "_run_customer_ai", relaunched.append)

    assert tickets.recover_stuck_ai_tickets(testing_session, background=False) == 1
    assert relaunched == [pending]



def test_kb_jobs_and_tickets_are_paginated_on_the_server(client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.models import KBJob

    api, testing_session = client
    admin_h, _agent_h, customer_h = _kb_users(testing_session)
    monkeypatch.setattr("app.kb.flags.settings.enable_kb_browser_scraper", False)
    monkeypatch.setattr("app.tickets._run_customer_ai", lambda _ticket_id: None)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with testing_session() as db:
        for index in range(7):
            db.add(KBJob(
                status="failed" if index % 3 == 0 else "success", query=f"requête {index}",
                created_at=base + timedelta(minutes=index),
            ))
        db.commit()

    first = api.get("/api/admin/kb?jobs_limit=3", headers=admin_h).json()
    assert [job["query"] for job in first["recent_jobs"]] == ["requête 6", "requête 5", "requête 4"]  # plus récent d'abord
    assert first["jobs_total"] == 7
    assert first["job_counts"] == {"all": 7, "success": 4, "failed": 3, "skipped": 0, "running": 0}

    last = api.get("/api/admin/kb?jobs_limit=3&jobs_offset=6", headers=admin_h).json()
    assert [job["query"] for job in last["recent_jobs"]] == ["requête 0"]

    failed = api.get("/api/admin/kb?jobs_limit=2&jobs_status=failed", headers=admin_h).json()
    assert failed["jobs_total"] == 3 and [job["query"] for job in failed["recent_jobs"]] == ["requête 6", "requête 3"]
    assert api.get("/api/admin/kb?jobs_status=nimporte", headers=admin_h).status_code == 422
    assert api.get("/api/admin/kb?jobs_limit=1000", headers=admin_h).status_code == 422

    debug = api.get("/api/admin/kb/debug?limit=2&offset=2", headers=admin_h).json()
    assert debug["jobs_total"] == 7 and [job["query"] for job in debug["jobs"]] == ["requête 4", "requête 3"]

    for number in range(3):
        assert api.post("/api/tickets", headers=customer_h, json={
            "title": f"Incident numéro {number}", "description": "Description suffisamment longue pour être valide.",
        }).status_code == 201
    assert len(api.get("/api/tickets", headers=customer_h).json()) == 3
    assert len(api.get("/api/tickets?limit=2", headers=customer_h).json()) == 2
    assert api.get("/api/tickets?limit=0", headers=customer_h).status_code == 422



def test_admin_can_inspect_export_and_restore_the_local_kb_archive(client, monkeypatch, tmp_path):
    from app.kb.archive import KBArchive
    from app.models import KBArticle

    api, testing_session = client
    admin_h, agent_h, _customer_h = _kb_users(testing_session)
    archive = KBArchive(tmp_path / "kb_archive", 10 * 1024 * 1024)
    monkeypatch.setattr("app.kb.router.get_archive", lambda: archive)
    queued = []
    monkeypatch.setattr("app.kb.router.enqueue_reindex", lambda: queued.append(True) or True)

    assert api.get("/api/admin/kb/archive", headers=agent_h).status_code == 403
    assert api.get("/api/admin/kb/archive", headers=admin_h).json()["items"] == 0

    with testing_session() as db:
        db.add(KBArticle(url="https://kb.example/a", title="Article A", content="Texte A", content_hash="ha", source="kb"))
        db.commit()
    exported = api.post("/api/admin/kb/archive/export", headers=admin_h).json()
    assert exported["exported"] == 1 and exported["items"] == 1
    assert api.post("/api/admin/kb/archive/export", headers=admin_h).json()["exported"] == 0  # pas de répétition

    with testing_session() as db:
        db.query(KBArticle).delete()
        db.commit()
    restored = api.post("/api/admin/kb/archive/restore", headers=admin_h).json()
    assert restored["restored"] == 1 and restored["reindex_queued"] is True and queued == [True]
    assert api.post("/api/admin/kb/archive/restore", headers=admin_h).json()["restored"] == 0

    monkeypatch.setattr("app.kb.router.get_archive", lambda: None)
    assert api.get("/api/admin/kb/archive", headers=admin_h).json() == {"enabled": False}
    assert api.post("/api/admin/kb/archive/export", headers=admin_h).status_code == 409
