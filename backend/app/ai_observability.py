from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.orm import Session, aliased, load_only

from app.auth import require_roles
from app.ai_routing import AI_ROLES, AI_TASKS, get_routing_config, provider_catalog
from app.rag import embedding_status, qdrant_status
from app.database import get_db
from app.models import AIExecution, AIFailure, AIModelPrice, AIRoutingConfig, Ticket, TicketEvent, User

router = APIRouter(prefix="/api/admin/ai", tags=["ai"])
TRACE_RETENTION_DAYS = 30


def purge_expired_trace_content() -> int:
    from app.database import SessionLocal

    cutoff = datetime.now(timezone.utc) - timedelta(days=TRACE_RETENTION_DAYS)
    with SessionLocal() as db:
        result = db.execute(
            update(AIExecution)
            .where(
                AIExecution.created_at < cutoff,
                AIExecution.prompt.is_not(None),
            )
            .values(prompt=None, response=None)
        )
        from app.models import AIStep

        db.execute(AIStep.__table__.delete().where(AIStep.started_at < cutoff))
        db.commit()
        return result.rowcount or 0


class TraceEvaluation(BaseModel):
    score: int | None = Field(default=None, ge=1, le=5)
    note: str = Field(default="", max_length=2000)


class ModelPriceInput(BaseModel):
    provider: str = Field(pattern="^(ollama|anthropic)$")
    model: str = Field(min_length=1, max_length=200)
    input_usd_per_million: float = Field(ge=0, le=100000)
    output_usd_per_million: float = Field(ge=0, le=100000)


class PricingUpdate(BaseModel):
    prices: list[ModelPriceInput] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_unique_models(self):
        keys = [(item.provider, item.model.strip()) for item in self.prices]
        if any(not model for _, model in keys) or len(keys) != len(set(keys)):
            raise ValueError("Chaque modèle doit être renseigné une seule fois.")
        return self


def record_execution(
    *,
    ticket_id: str | None,
    customer_id: str,
    actor_id: str | None,
    task: str,
    provider: str,
    model: str,
    prompt: str,
    response: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
) -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        customer = db.get(User, customer_id)
        if not customer:
            raise RuntimeError("Client introuvable lors de l’enregistrement de l’appel IA.")
        capture_content = customer.ai_trace_consent
        db.add(AIExecution(
            ticket_id=ticket_id,
            customer_id=customer_id,
            actor_id=actor_id,
            task=task,
            provider=provider,
            model=model,
            prompt=prompt if capture_content else None,
            response=response if capture_content else None,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            duration_ms=max(0, duration_ms),
        ))
        expiration = datetime.now(timezone.utc) - timedelta(days=TRACE_RETENTION_DAYS)
        db.execute(
            AIExecution.__table__.update()
            .where(AIExecution.created_at < expiration)
            .values(prompt=None, response=None)
        )
        db.commit()


def _filtered_executions(
    *,
    ticket_id: str | None,
    customer_id: str | None,
    actor_id: str | None,
    provider: str | None,
    model: str | None,
    task: str | None,
    start: datetime | None,
    end: datetime | None,
    include_content: bool = True,
):
    Actor = aliased(User)
    query = (
        select(AIExecution, Ticket.reference, Ticket.title, User.full_name, Actor.full_name)
        .outerjoin(Ticket, AIExecution.ticket_id == Ticket.id)
        .join(User, AIExecution.customer_id == User.id)
        .outerjoin(Actor, AIExecution.actor_id == Actor.id)
    )
    if not include_content:
        query = query.options(load_only(
            AIExecution.id,
            AIExecution.ticket_id,
            AIExecution.customer_id,
            AIExecution.actor_id,
            AIExecution.task,
            AIExecution.provider,
            AIExecution.model,
            AIExecution.input_tokens,
            AIExecution.output_tokens,
            AIExecution.created_at,
        ))
    if ticket_id:
        query = query.where(AIExecution.ticket_id == ticket_id)
    if customer_id:
        query = query.where(AIExecution.customer_id == customer_id)
    if actor_id:
        query = query.where(AIExecution.actor_id == actor_id)
    if provider:
        query = query.where(AIExecution.provider == provider)
    if model:
        query = query.where(AIExecution.model == model)
    if task:
        query = query.where(AIExecution.task == task)
    if start:
        query = query.where(AIExecution.created_at >= start)
    if end:
        query = query.where(AIExecution.created_at <= end)
    return query, Actor


def _trace_filters(
    ticket_id: str | None = None,
    customer_id: str | None = None,
    actor_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    task: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
):
    return {
        "ticket_id": ticket_id,
        "customer_id": customer_id,
        "actor_id": actor_id,
        "provider": provider,
        "model": model,
        "task": task,
        "start": start,
        "end": end,
    }


@router.get("/traces")
def list_traces(
    ticket_id: str | None = None,
    customer_id: str | None = None,
    actor_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    task: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="La date de début doit précéder la date de fin.")
    filters = _trace_filters(ticket_id, customer_id, actor_id, provider, model, task, start, end)
    query, _actor_alias = _filtered_executions(**filters)
    query = query.where(
        AIExecution.prompt.is_not(None),
        AIExecution.response.is_not(None),
        AIExecution.created_at >= datetime.now(timezone.utc) - timedelta(days=TRACE_RETENTION_DAYS),
    ).order_by(AIExecution.created_at.desc())
    total_count = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = db.execute(query.limit(limit).offset(offset)).all()
    return {
        "retention_days": TRACE_RETENTION_DAYS,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "items": [{
            "id": execution.id,
            "ticket_id": execution.ticket_id,
            "ticket_reference": reference or "Console d’aide",
            "ticket_title": title or "Conversation hors ticket",
            "customer_id": execution.customer_id,
            "customer_name": customer_name,
            "actor_id": execution.actor_id,
            "actor_name": actor_name,
            "task": execution.task,
            "provider": execution.provider,
            "model": execution.model,
            "prompt": execution.prompt,
            "response": execution.response,
            "input_tokens": execution.input_tokens,
            "output_tokens": execution.output_tokens,
            "duration_ms": execution.duration_ms,
            "evaluation_score": execution.evaluation_score,
            "evaluation_note": execution.evaluation_note,
            "created_at": execution.created_at,
        } for execution, reference, title, customer_name, actor_name in rows],
    }


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("/catalog")
def filter_catalog(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    people = db.execute(
        select(User.id, User.full_name, User.role)
        .where(User.role.in_(("customer", "agent", "admin")))
        .order_by(User.full_name)
        .limit(1000)
    ).all()
    tickets = db.execute(
        select(Ticket.id, Ticket.reference, Ticket.title)
        .order_by(Ticket.created_at.desc())
        .limit(1000)
    ).all()
    return {
        "people": [{
            "id": person_id,
            "name": name,
            "role": role,
        } for person_id, name, role in people],
        "tickets": [{
            "id": ticket_id,
            "reference": reference,
            "title": title,
        } for ticket_id, reference, title in tickets],
    }


@router.get("/diagnostics")
def ai_diagnostics(
    reference: str | None = None,
    source: str = Query(default="all", pattern="^(all|ticket|console)$"),
    user_id: str | None = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    config = get_routing_config(db)
    routes = []
    for task, task_label in AI_TASKS.items():
        for role, role_label in AI_ROLES.items():
            route = config["routes"].get(task, {}).get(role, {})
            routes.append({
                "task": task,
                "task_label": task_label,
                "role": role,
                "role_label": role_label,
                "enabled": bool(config["enabled"] and route.get("enabled")),
                "models": route.get("models", []),
            })

    failure_event_types = ("ai_provider_failed", "ai_analysis_failed", "ai_handoff_summary_failed")
    failure_rows = [] if source == "console" else db.execute(
        select(TicketEvent, Ticket.reference, Ticket.title, Ticket.status, Ticket.ai_status)
        .join(Ticket, TicketEvent.ticket_id == Ticket.id)
        .where(TicketEvent.event_type.in_(failure_event_types))
        .order_by(TicketEvent.created_at.desc())
        .limit(20)
    ).all()
    recent_failures = [{
        "source": "ticket",
        "reference": ticket_reference,
        "title": title,
        "status": status,
        "ai_status": ai_status,
        "event_type": event.event_type,
        "message": (event.details or {}).get("message") or (event.details or {}).get("error", ""),
        "provider": (event.details or {}).get("provider", ""),
        "model": (event.details or {}).get("model", ""),
        "task": (event.details or {}).get("task", ""),
        "created_at": event.created_at,
    } for event, ticket_reference, title, status, ai_status in failure_rows]
    if source != "ticket":
        console_failures = select(AIFailure, User.full_name).outerjoin(User, AIFailure.user_id == User.id)
        if user_id:
            console_failures = console_failures.where(AIFailure.user_id == user_id)
        for failure, person in db.execute(console_failures.order_by(AIFailure.created_at.desc()).limit(20)).all():
            recent_failures.append({
                "source": "console", "reference": "Console d’aide", "title": person or "Utilisateur supprimé",
                "status": "", "ai_status": "", "event_type": "ai_provider_failed", "message": failure.error,
                "provider": failure.provider, "model": failure.model, "task": failure.task, "created_at": failure.created_at,
            })
        recent_failures.sort(key=lambda item: _utc(item["created_at"]), reverse=True)
        recent_failures = recent_failures[:20]

    providers = provider_catalog()
    for provider in providers:
        provider_id = provider["id"]
        last_success = db.execute(
            select(AIExecution.model, AIExecution.created_at)
            .where(AIExecution.provider == provider_id)
            .order_by(AIExecution.created_at.desc())
            .limit(1)
        ).first()
        latest_failure = db.execute(
            select(TicketEvent.details, TicketEvent.created_at)
            .where(
                TicketEvent.event_type == "ai_provider_failed",
                TicketEvent.details["provider"].as_string() == provider_id,
            )
            .order_by(TicketEvent.created_at.desc())
            .limit(1)
        ).first()
        console_failure = db.execute(
            select(AIFailure.error, AIFailure.created_at)
            .where(AIFailure.provider == provider_id)
            .order_by(AIFailure.created_at.desc())
            .limit(1)
        ).first()
        if console_failure and (not latest_failure or _utc(console_failure.created_at) > _utc(latest_failure.created_at)):
            latest_failure = type("Failure", (), {"details": {"error": console_failure.error}, "created_at": console_failure.created_at})()
        provider["last_success_at"] = last_success.created_at if last_success else None
        provider["last_success_model"] = last_success.model if last_success else ""
        provider["last_failure_at"] = latest_failure.created_at if latest_failure else None
        provider["last_failure_message"] = (
            (latest_failure.details or {}).get("error", "") if latest_failure else ""
        )
        failure_is_newer = bool(
            latest_failure
            and (
                not last_success
                or (
                    latest_failure.created_at.replace(tzinfo=timezone.utc)
                    if latest_failure.created_at.tzinfo is None
                    else latest_failure.created_at
                ) > (
                    last_success.created_at.replace(tzinfo=timezone.utc)
                    if last_success.created_at.tzinfo is None
                    else last_success.created_at
                )
            )
        )
        if failure_is_newer and provider.get("configured"):
            provider["status"] = "warning"
            provider["error"] = provider["last_failure_message"]
        elif last_success and provider.get("configured"):
            provider["status"] = "responded"
            provider["error"] = ""

    ticket_diagnostic = None
    if reference:
        ticket = db.scalar(select(Ticket).where(Ticket.reference == reference.strip().upper()))
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket introuvable.")
        events = db.scalars(
            select(TicketEvent)
            .where(
                TicketEvent.ticket_id == ticket.id,
                TicketEvent.event_type.in_(
                    failure_event_types + ("ai_response", "ai_handoff", "ai_handoff_summary")
                ),
            )
            .order_by(TicketEvent.created_at.asc())
            .limit(100)
        ).all()
        executions = db.scalars(
            select(AIExecution)
            .where(AIExecution.ticket_id == ticket.id)
            .order_by(AIExecution.created_at.asc())
            .limit(100)
        ).all()
        ticket_diagnostic = {
            "reference": ticket.reference,
            "title": ticket.title,
            "status": ticket.status,
            "ai_status": ticket.ai_status,
            "ai_provider": ticket.ai_provider,
            "ai_error": ticket.ai_error,
            "created_at": ticket.created_at,
            "updated_at": ticket.updated_at,
            "events": [{
                "event_type": event.event_type,
                "message": (event.details or {}).get("message") or (event.details or {}).get("error", ""),
                "provider": (event.details or {}).get("provider", ""),
                "model": (event.details or {}).get("model", ""),
                "task": (event.details or {}).get("task", ""),
                "created_at": event.created_at,
            } for event in events],
            "executions": [{
                "task": execution.task,
                "provider": execution.provider,
                "model": execution.model,
                "input_tokens": execution.input_tokens,
                "output_tokens": execution.output_tokens,
                "duration_ms": execution.duration_ms,
                "created_at": execution.created_at,
            } for execution in executions],
        }

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    console_rows = db.execute(
        select(AIExecution, User.full_name)
        .join(User, AIExecution.customer_id == User.id)
        .where(AIExecution.ticket_id.is_(None), *([AIExecution.customer_id == user_id] if user_id else []))
        .order_by(AIExecution.created_at.desc())
        .limit(20)
    ).all()
    console_users = db.execute(
        select(User.id, User.full_name).where(User.id.in_(
            select(AIExecution.customer_id).where(AIExecution.ticket_id.is_(None)).union(select(AIFailure.user_id))
        )).order_by(User.full_name)
    ).all()
    console = {
        "executions_24h": db.scalar(select(func.count()).select_from(AIExecution).where(
            AIExecution.ticket_id.is_(None), AIExecution.created_at >= since)) or 0,
        "failures_24h": db.scalar(select(func.count()).select_from(AIFailure).where(AIFailure.created_at >= since)) or 0,
        "users": [{"id": uid, "name": name} for uid, name in console_users],
        "recent_executions": [{
            "user": person, "task": execution.task, "provider": execution.provider, "model": execution.model,
            "input_tokens": execution.input_tokens, "output_tokens": execution.output_tokens,
            "duration_ms": execution.duration_ms, "created_at": execution.created_at,
        } for execution, person in console_rows],
    }

    return {
        "routing_enabled": config["enabled"],
        "console": console,
        "providers": providers,
        "routes": routes,
        "embedding": embedding_status(),
        "rag": qdrant_status(),
        "recent_failures": recent_failures,
        "ticket": ticket_diagnostic,
    }


@router.get("/ticket-timeline")
def ticket_timeline(
    reference: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    """Parcours complet d'un ticket (messages, traitements IA, recherche documentaire, fournisseurs, KB)."""
    from app.ticket_timeline import build_timeline

    ticket = db.scalar(select(Ticket).where(Ticket.reference == reference.strip().upper()))
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    return {
        "reference": ticket.reference, "title": ticket.title, "status": ticket.status, "ai_status": ticket.ai_status,
        "ai_error": ticket.ai_error, "created_at": ticket.created_at, **build_timeline(db, ticket),
    }


@router.patch("/traces/{execution_id}")
def evaluate_trace(
    execution_id: str,
    payload: TraceEvaluation,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    execution = db.get(AIExecution, execution_id)
    if not execution or execution.prompt is None or execution.response is None:
        raise HTTPException(status_code=404, detail="Trace détaillée introuvable ou expirée.")
    execution.evaluation_score = payload.score
    execution.evaluation_note = payload.note.strip()
    db.commit()
    return {"id": execution.id, "evaluation_score": execution.evaluation_score, "evaluation_note": execution.evaluation_note}


@router.get("/costs")
def list_costs(
    ticket_id: str | None = None,
    customer_id: str | None = None,
    actor_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    task: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="La date de début doit précéder la date de fin.")
    filters = _trace_filters(ticket_id, customer_id, actor_id, provider, model, task, start, end)
    query, _actor_alias = _filtered_executions(**filters, include_content=False)
    query = query.order_by(AIExecution.created_at.desc())
    filtered_ids = query.with_only_columns(AIExecution.id).order_by(None).subquery()
    cost_expression = case(
        (
            AIModelPrice.id.is_not(None),
            (
                AIExecution.input_tokens * AIModelPrice.input_usd_per_million
                + AIExecution.output_tokens * AIModelPrice.output_usd_per_million
            ) / 1_000_000,
        ),
        (AIExecution.provider == "ollama", 0.0),
        else_=None,
    )
    unpriced_expression = case(
        (
            and_(AIExecution.provider != "ollama", AIModelPrice.id.is_(None)),
            1,
        ),
        else_=0,
    )
    total_count, total_usd, unknown_rates = db.execute(
        select(
            func.count(AIExecution.id),
            func.coalesce(func.sum(cost_expression), 0),
            func.coalesce(func.sum(unpriced_expression), 0),
        )
        .select_from(AIExecution)
        .outerjoin(
            AIModelPrice,
            and_(
                AIModelPrice.provider == AIExecution.provider,
                AIModelPrice.model == AIExecution.model,
            ),
        )
        .where(AIExecution.id.in_(select(filtered_ids.c.id)))
    ).one()
    rows = db.execute(query.limit(limit).offset(offset)).all()
    prices = {
        (price.provider, price.model): price
        for price in db.scalars(select(AIModelPrice)).all()
    }
    items = []
    for execution, reference, title, customer_name, actor_name in rows:
        price = prices.get((execution.provider, execution.model))
        if price:
            cost = (
                execution.input_tokens * price.input_usd_per_million
                + execution.output_tokens * price.output_usd_per_million
            ) / 1_000_000
            rate_source = "configured"
        elif execution.provider == "ollama":
            cost = 0.0
            rate_source = "local"
        else:
            cost = None
            rate_source = "missing"
        items.append({
            "id": execution.id,
            "ticket_id": execution.ticket_id,
            "ticket_reference": reference or "Console d’aide",
            "ticket_title": title or "Conversation hors ticket",
            "customer_id": execution.customer_id,
            "customer_name": customer_name,
            "actor_id": execution.actor_id,
            "actor_name": actor_name,
            "task": execution.task,
            "provider": execution.provider,
            "model": execution.model,
            "input_tokens": execution.input_tokens,
            "output_tokens": execution.output_tokens,
            "cost_usd": cost,
            "rate_source": rate_source,
            "created_at": execution.created_at,
        })
    return {
        "total_estimated_usd": round(float(total_usd), 8),
        "unpriced_count": unknown_rates,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/pricing")
def get_pricing(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    rates = {
        (price.provider, price.model): price
        for price in db.scalars(select(AIModelPrice)).all()
    }
    used_models = db.execute(select(AIExecution.provider, AIExecution.model).distinct()).all()
    config = db.get(AIRoutingConfig, 1)
    configured_models = []
    if config:
        for route_set in config.routes.values():
            for route in route_set.values():
                configured_models.extend(
                    (choice["provider"], choice["model"])
                    for choice in route.get("models", [])
                )
    models = sorted(set(used_models) | set(configured_models) | set(rates))
    return {
        "currency": "USD",
        # Modèles de génération détectés par fournisseur, pour les listes déroulantes.
        "available_models": {item["id"]: item["available_models"] for item in provider_catalog()},
        "prices": [{
            "provider": provider,
            "model": model,
            "input_usd_per_million": rates[(provider, model)].input_usd_per_million if (provider, model) in rates else (0 if provider == "ollama" else None),
            "output_usd_per_million": rates[(provider, model)].output_usd_per_million if (provider, model) in rates else (0 if provider == "ollama" else None),
            "configured": (provider, model) in rates or provider == "ollama",
        } for provider, model in models],
    }


@router.put("/pricing")
def update_pricing(
    payload: PricingUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    for item in payload.prices:
        provider, model = item.provider, item.model.strip()
        price = db.scalar(select(AIModelPrice).where(
            and_(AIModelPrice.provider == provider, AIModelPrice.model == model)
        ))
        if price:
            price.input_usd_per_million = item.input_usd_per_million
            price.output_usd_per_million = item.output_usd_per_million
        else:
            db.add(AIModelPrice(
                provider=provider,
                model=model,
                input_usd_per_million=item.input_usd_per_million,
                output_usd_per_million=item.output_usd_per_million,
            ))
    db.commit()
    return get_pricing(db)
