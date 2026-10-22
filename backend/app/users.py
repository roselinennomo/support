import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_roles
from app.database import get_db
from app.models import Company, User, UserAdminAudit, company_agents
from app.schemas import (
    AccountDecision,
    CompanyAssignment,
    CompanyCreate,
    CompanyCreatedOut,
    CompanyOut,
    PasswordReset,
    StaffUserCreate,
    UserManagementUpdate,
    UserOut,
)
from app.security import hash_password

router = APIRouter(prefix="/api", tags=["users"])


def _can_access_company(db: Session, company_id: str | None, user: User) -> bool:
    if user.role == "admin":
        return True
    if user.role != "agent" or not company_id:
        return False
    return db.scalar(
        select(company_agents.c.company_id).where(
            company_agents.c.company_id == company_id,
            company_agents.c.agent_id == user.id,
        )
    ) is not None


def _company_out(company: Company) -> dict:
    return {
        "id": company.id,
        "name": company.name,
        "is_active": company.is_active,
        "agent_ids": [agent.id for agent in company.agents],
        "created_at": company.created_at,
    }


def _user_can_be_managed(db: Session, user: User, actor: User) -> bool:
    if actor.role == "admin":
        return True
    if actor.role != "agent" or actor.id == user.id or user.role not in {"agent", "customer"}:
        return False
    assigned_company_ids = select(company_agents.c.company_id).where(
        company_agents.c.agent_id == actor.id
    )
    if user.role == "customer":
        return user.company_id is not None and db.scalar(
            select(company_agents.c.company_id).where(
                company_agents.c.agent_id == actor.id,
                company_agents.c.company_id == user.company_id,
            )
        ) is not None
    return db.scalar(
        select(company_agents.c.company_id).where(
            company_agents.c.agent_id == actor.id,
            company_agents.c.company_id.in_(
                select(company_agents.c.company_id).where(company_agents.c.agent_id == user.id)
            ),
        )
    ) is not None


def _audit_user_change(db: Session, actor: User, target: User, action: str, details: dict) -> None:
    db.add(UserAdminAudit(
        actor_id=actor.id,
        target_user_id=target.id,
        action=action,
        details=details,
    ))


@router.get("/users", response_model=list[UserOut])
def list_staff(db: Session = Depends(get_db), _staff: User = Depends(require_roles("agent", "admin"))):
    query = select(User)
    if _staff.role == "agent":
        assigned_company_ids = select(company_agents.c.company_id).where(company_agents.c.agent_id == _staff.id)
        company_customer_ids = select(User.id).where(
            User.role == "customer",
            User.company_id.in_(assigned_company_ids),
        )
        company_agent_ids = select(company_agents.c.agent_id).where(
            company_agents.c.company_id.in_(assigned_company_ids)
        )
        query = query.where(
            or_(User.id.in_(company_customer_ids), User.id.in_(company_agent_ids))
        )
    return db.scalars(query.order_by(User.full_name)).all()


@router.post("/users", response_model=UserOut, status_code=201)
def create_staff(payload: StaffUserCreate, db: Session = Depends(get_db), _admin: User = Depends(require_roles("admin"))):
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Cette adresse e-mail est déjà utilisée.")
    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        account_status="approved",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UserManagementUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("agent", "admin")),
):
    target = db.get(User, user_id)
    if not target or target.role == "system" or not _user_can_be_managed(db, target, actor):
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    changes = payload.model_dump(exclude_unset=True)
    if actor.role == "agent":
        forbidden = set(changes) - {"full_name", "email", "is_active"}
        if forbidden:
            raise HTTPException(status_code=403, detail="Les agents ne peuvent modifier que le nom, l’e-mail et l’activation du compte.")
    if target.id == actor.id and actor.role == "admin" and changes.get("is_active") is False:
        raise HTTPException(status_code=409, detail="Vous ne pouvez pas désactiver votre propre compte administrateur.")
    if target.role == "admin" and (
        changes.get("is_active") is False or changes.get("account_status") in {"pending", "rejected"}
    ):
        active_admins = db.scalars(select(User).where(
            User.role == "admin",
            User.is_active.is_(True),
            User.account_status == "approved",
        )).all()
        if len(active_admins) <= 1:
            raise HTTPException(status_code=409, detail="Le dernier administrateur actif ne peut pas être désactivé.")

    if "email" in changes and changes["email"]:
        changes["email"] = changes["email"].lower()
        existing = db.scalar(select(User).where(User.email == changes["email"], User.id != target.id))
        if existing:
            raise HTTPException(status_code=409, detail="Cette adresse e-mail est déjà utilisée.")

    effective_role = changes.get("role", target.role)
    if "company_id" in changes and effective_role != "customer":
        raise HTTPException(status_code=422, detail="Le rattachement direct à une entreprise est réservé aux clients.")
    if "company_ids" in changes and effective_role != "agent":
        raise HTTPException(status_code=422, detail="Les entreprises affectées ne concernent que les agents.")
    if "company_id" in changes and changes["company_id"] is not None:
        company = db.get(Company, changes["company_id"])
        if not company or not company.is_active:
            raise HTTPException(status_code=422, detail="Entreprise inexistante ou inactive.")
    if "company_ids" in changes:
        company_ids = set(changes["company_ids"] or [])
        companies = db.scalars(select(Company).where(Company.id.in_(company_ids), Company.is_active.is_(True))).all()
        if len(companies) != len(company_ids):
            raise HTTPException(status_code=422, detail="Une ou plusieurs entreprises sont inexistantes ou inactives.")
        target.companies = companies

    role_change = changes.get("role")
    if role_change and role_change != target.role:
        if target.role == "admin" and role_change != "admin":
            admins = db.scalars(select(User).where(User.role == "admin", User.is_active.is_(True))).all()
            if len(admins) <= 1:
                raise HTTPException(status_code=409, detail="Le dernier administrateur actif ne peut pas être rétrogradé.")
        target.role = role_change
        if role_change != "customer":
            target.company_id = None
        if role_change != "agent":
            target.companies = []

    for field in (
        "full_name",
        "email",
        "is_active",
        "account_status",
        "company_id",
        "ai_processing_consent",
        "ai_trace_consent",
    ):
        if field in changes:
            value = changes[field]
            if field == "full_name" and value:
                value = value.strip()
                if len(value) < 2:
                    raise HTTPException(status_code=422, detail="Le nom doit contenir au moins deux caractères.")
            setattr(target, field, value)

    if changes:
        _audit_user_change(db, actor, target, "updated", {
            "fields": sorted(changes),
            "role": target.role,
        })
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Les modifications entrent en conflit avec un compte existant.") from exc
    db.refresh(target)
    return target


@router.put("/users/{user_id}/password", status_code=204)
def reset_user_password(
    user_id: str,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("agent", "admin")),
):
    target = db.get(User, user_id)
    if not target or target.role == "system" or not _user_can_be_managed(db, target, actor):
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    if actor.role == "agent" and target.role not in {"agent", "customer"}:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    target.password_hash = hash_password(payload.temporary_password)
    target.token_version += 1
    _audit_user_change(db, actor, target, "password_reset", {})
    db.commit()
    return None


@router.get("/companies", response_model=list[CompanyOut])
def list_companies(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    query = select(Company).where(Company.is_active.is_(True)).order_by(Company.name)
    if user.role == "agent":
        query = query.join(company_agents).where(company_agents.c.agent_id == user.id)
    return [_company_out(company) for company in db.scalars(query).unique().all()]


@router.post("/companies", response_model=CompanyCreatedOut, status_code=201)
def create_company(
    payload: CompanyCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("agent", "admin")),
):
    name = payload.name.strip()
    if db.scalar(select(Company).where(Company.name.ilike(name))):
        raise HTTPException(status_code=409, detail="Une entreprise porte déjà ce nom.")

    agent_ids = set(payload.agent_ids)
    if actor.role == "agent":
        agent_ids.add(actor.id)
    agents = db.scalars(
        select(User).where(
            User.id.in_(agent_ids),
            User.role == "agent",
            User.is_active.is_(True),
            User.account_status == "approved",
        )
    ).all() if agent_ids else []
    if len(agents) != len(agent_ids):
        raise HTTPException(status_code=422, detail="Un ou plusieurs agents sélectionnés sont invalides.")
    if not agents:
        raise HTTPException(status_code=422, detail="Affectez au moins un agent support à l’entreprise.")

    invitation_code = secrets.token_urlsafe(18)
    company = Company(
        name=name,
        invitation_code_hash=hashlib.sha256(invitation_code.encode()).hexdigest(),
        created_by_id=actor.id,
        agents=agents,
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return {**_company_out(company), "invitation_code": invitation_code}


@router.post("/companies/{company_id}/invitation-code", response_model=CompanyCreatedOut)
def rotate_company_invitation_code(
    company_id: str,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("agent", "admin")),
):
    company = db.get(Company, company_id)
    if not company or not company.is_active or not _can_access_company(db, company.id, actor):
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    invitation_code = secrets.token_urlsafe(18)
    company.invitation_code_hash = hashlib.sha256(invitation_code.encode()).hexdigest()
    db.commit()
    db.refresh(company)
    return {**_company_out(company), "invitation_code": invitation_code}


@router.get("/users/requests", response_model=list[UserOut])
def list_account_requests(
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("agent", "admin")),
):
    query = select(User).where(User.role == "customer", User.account_status == "pending")
    if actor.role == "agent":
        query = query.join(company_agents, company_agents.c.company_id == User.company_id).where(
            company_agents.c.agent_id == actor.id
        )
    return db.scalars(query.order_by(User.created_at)).unique().all()


@router.patch("/users/{user_id}/company", response_model=UserOut)
def assign_customer_company(
    user_id: str,
    payload: CompanyAssignment,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    customer = db.get(User, user_id)
    company = db.get(Company, payload.company_id)
    if not customer or customer.role != "customer" or customer.account_status != "pending":
        raise HTTPException(status_code=404, detail="Demande client introuvable.")
    if not company or not company.is_active:
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    customer.company_id = company.id
    db.commit()
    db.refresh(customer)
    return customer


@router.patch("/users/{user_id}/approval", response_model=UserOut)
def decide_account_request(
    user_id: str,
    payload: AccountDecision,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("agent", "admin")),
):
    customer = db.get(User, user_id)
    if not customer or customer.role != "customer":
        raise HTTPException(status_code=404, detail="Demande client introuvable.")
    if not _can_access_company(db, customer.company_id, actor):
        raise HTTPException(status_code=404, detail="Demande client introuvable.")
    if customer.account_status != "pending":
        raise HTTPException(status_code=409, detail="Cette demande a déjà été traitée.")
    if payload.decision == "approve" and not customer.company_id:
        raise HTTPException(status_code=409, detail="Rattachez d’abord ce compte client à une entreprise.")

    customer.account_status = "approved" if payload.decision == "approve" else "rejected"
    if payload.decision == "approve":
        customer.approved_by_id = actor.id
        customer.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(customer)
    return customer
