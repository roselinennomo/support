import logging
import mimetypes
import secrets
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.ai import AIRoutingDisabledError, AIAnalysisError, assist_agent, respond_to_customer
from app.ai_routing import ModelSelection, resolve_selection, routing_role
from app.auth import get_current_user, require_roles
from app.config import settings
from app.kb import flags as kb_flags
from app.kb.jobs import enqueue_ticket_enrichment
from app.database import SessionLocal, get_db
from app.models import Company, Message, Ticket, TicketAttachment, TicketEvent, User, company_agents
from app.schemas import (
    AssignmentUpdate,
    AgentAIAssist,
    BackgroundAccepted,
    HandoffCreate,
    MessageCreate,
    StatusUpdate,
    TicketCreate,
    TicketOut,
)
from app.security import hash_password
from app.service_management import (
    ComplianceFinding,
    apply_ticket_sla,
    consume_prepaid_credit,
    record_compliance_finding,
)

router = APIRouter(prefix="/api/tickets", tags=["tickets"])
logger = logging.getLogger(__name__)
STATUSES = {"ai_triage", "new", "in_progress", "waiting_support", "waiting_customer", "escalated", "resolved", "closed", "rejected"}
STAFF_STATUS_TRANSITIONS = {
    "new": {"in_progress", "waiting_support", "waiting_customer", "resolved", "rejected"},
    "in_progress": {"new", "waiting_support", "waiting_customer", "resolved", "rejected"},
    "waiting_support": {"new", "in_progress", "waiting_customer", "resolved", "rejected"},
    "waiting_customer": {"new", "in_progress", "waiting_support", "resolved", "rejected"},
    "escalated": {"in_progress", "waiting_support", "waiting_customer", "resolved", "rejected"},
}
TERMINAL_STATUSES = {"closed", "rejected"}
CUSTOMER_REPLY_STATUSES = {"new", "in_progress", "waiting_support", "waiting_customer", "escalated", "resolved"}
ALLOWED_ATTACHMENT_EXTENSIONS = {".csv", ".jpg", ".jpeg", ".log", ".pdf", ".pcap", ".png", ".txt", ".webp", ".zip"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _event(db: Session, ticket: Ticket, actor: User | None, event_type: str, details: dict) -> None:
    db.add(TicketEvent(ticket_id=ticket.id, actor_id=actor.id if actor else None, event_type=event_type, details=details))


def _reopen_resolved_ticket(db: Session, ticket: Ticket, actor: User) -> None:
    if ticket.status != "resolved":
        return
    ticket.status = "waiting_support"
    ticket.resolution_note = ""
    ticket.updated_at = datetime.now(timezone.utc)
    _event(db, ticket, actor, "ticket_reopened", {"from": "resolved", "to": "waiting_support"})


def _mark_customer_reply_waiting_support(db: Session, ticket: Ticket, actor: User) -> None:
    if ticket.status == "resolved":
        _reopen_resolved_ticket(db, ticket, actor)
    elif ticket.status in CUSTOMER_REPLY_STATUSES and ticket.status != "waiting_support":
        previous = ticket.status
        ticket.status = "waiting_support"
        ticket.updated_at = datetime.now(timezone.utc)
        _event(db, ticket, actor, "status_changed", {"from": previous, "to": "waiting_support"})


def _customer_replied_after_first_ai_response(ticket: Ticket) -> bool:
    return _has_customer_ai_exchange(ticket)


def _has_customer_ai_exchange(ticket: Ticket) -> bool:
    messages = sorted(ticket.messages, key=lambda message: _utc(message.created_at))
    first_ai_response_index = next(
        (
            index for index, message in enumerate(messages)
            if message.author.role == "system" and message.visibility == "public"
        ),
        None,
    )
    return first_ai_response_index is not None and any(
        message.author.role == "customer" and message.visibility == "public"
        for message in messages[first_ai_response_index + 1:]
    )


def _ticket_or_404(db: Session, ticket_id: str, user: User) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if not ticket or (user.role == "customer" and not _customer_can_access(ticket, user)):
        raise HTTPException(status_code=404, detail="Incident introuvable.")
    if user.role == "agent" and not _agent_can_access_company(db, user, ticket.customer.company_id):
        raise HTTPException(status_code=404, detail="Incident introuvable.")
    return ticket


def _validated_preference(db: Session, user: User, selection: ModelSelection | None) -> dict | None:
    if selection is None:
        return None
    try:
        return resolve_selection(db, routing_role(user.role), selection)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _customer_can_access(ticket: Ticket, user: User) -> bool:
    """Un client voit ses tickets et ceux de ses collègues de la même entreprise."""
    if ticket.customer_id == user.id:
        return True
    return bool(user.company_id) and ticket.customer.company_id == user.company_id


def _agent_can_access_company(db: Session, agent: User, company_id: str | None) -> bool:
    if not company_id:
        return False
    return db.scalar(
        select(company_agents.c.company_id).where(
            company_agents.c.company_id == company_id,
            company_agents.c.agent_id == agent.id,
        )
    ) is not None


def _assistant_user(db: Session) -> User:
    assistant = db.scalar(select(User).where(User.email == "assistant@support.local"))
    if assistant:
        return assistant
    assistant = User(
        email="assistant@support.local",
        full_name="Assistant IA",
        password_hash=hash_password(secrets.token_urlsafe(32)),
        role="system",
        is_active=False,
        account_status="approved",
    )
    db.add(assistant)
    db.flush()
    return assistant


def _conversation_context(ticket_id: str, include_internal: bool) -> tuple[SimpleNamespace, list[dict[str, str]], list[str]]:
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        if not ticket:
            raise ValueError("Ticket introuvable pendant le traitement IA.")
        ticket_snapshot = SimpleNamespace(
            id=ticket.id,
            customer_id=ticket.customer_id,
            ai_processing_consent=ticket.customer.ai_processing_consent,
            title=ticket.title,
            description=ticket.description,
            product=ticket.product,
            version=ticket.version,
            ai_preferences=ticket.ai_preferences,
            company_id=ticket.customer.company_id,
        )
        messages = sorted(ticket.messages, key=lambda message: message.created_at)
        conversation = [
            {
                "role": "assistant" if message.author.role == "system" else (
                    "agent" if message.author.role in {"agent", "admin"} else "client"
                ),
                "body": message.body,
            }
            for message in messages
            if include_internal or message.visibility == "public"
        ]
        attachment_names = [attachment.file_name for attachment in ticket.attachments]
    return ticket_snapshot, conversation, attachment_names


def _run_customer_ai(ticket_id: str) -> None:
    try:
        ticket_snapshot, conversation, attachment_names = _conversation_context(ticket_id, include_internal=False)
        if not ticket_snapshot.ai_processing_consent:
            with SessionLocal() as db:
                ticket = db.get(Ticket, ticket_id)
                if ticket and ticket.status == "ai_triage":
                    assistant = _assistant_user(db)
                    ticket.status = "new"
                    ticket.ai_status = "disabled"
                    db.add(Message(
                        ticket_id=ticket.id,
                        author_id=assistant.id,
                        body="Le client n’a pas consenti au traitement IA. La demande a été transmise à l’équipe support.",
                        visibility="public",
                    ))
                    _event(db, ticket, None, "ai_consent_required", {})
                    db.commit()
            return
        result = respond_to_customer(ticket_snapshot, conversation, attachment_names)
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if not ticket or ticket.status != "ai_triage":
                return
            assistant = _assistant_user(db)
            if result["priority"]:  # None : l'IA n'a pas fourni de priorité exploitable, on garde celle du client
                ticket.priority = result["priority"]
            ticket.ai_summary = result["reply"]
            ticket.ai_recommendations = result["recommendations"]
            ticket.ai_escalation_recommended = result["escalation_recommended"]
            ticket.ai_provider = result.get("provider", "")
            ticket.ai_status = "complete"
            if result["resolved"]:
                ticket.status = "resolved"
                ticket.resolution_note = result["reply"].strip()
                _event(db, ticket, None, "status_changed", {
                    "from": "ai_triage",
                    "to": "resolved",
                    "resolution_note": ticket.resolution_note,
                })
            db.add(Message(
                ticket_id=ticket.id,
                author_id=assistant.id,
                body=result["reply"],
                visibility="public",
            ))
            _event(db, ticket, None, "ai_response", {
                "resolved": result["resolved"],
                "escalation_recommended": result["escalation_recommended"],
            })
            ticket.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        if isinstance(exc, AIAnalysisError):
            logger.warning("Customer AI triage failed for ticket %s: %s", ticket_id, exc)
        else:
            logger.exception("Unexpected customer AI triage failure for ticket %s", ticket_id)
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if not ticket or ticket.status != "ai_triage":
                return
            assistant = _assistant_user(db)
            ticket.ai_status = "error"
            ticket.ai_error = str(exc)[:1000]
            ticket.ai_provider = ""
            ticket.status = "new"
            customer_message = (
                "L’assistance IA est désactivée pour ce diagnostic. Votre demande a été transmise à l’équipe support."
                if isinstance(exc, AIRoutingDisabledError)
                else "Je n’arrive pas à terminer le diagnostic automatiquement. Votre demande et ses pièces jointes ont été transmises à l’équipe support."
            )
            db.add(Message(
                ticket_id=ticket.id,
                author_id=assistant.id,
                body=customer_message,
                visibility="public",
            ))
            _event(db, ticket, None, "ai_analysis_failed", {"message": ticket.ai_error})
            _event(db, ticket, None, "ai_handoff", {"from": "ai_triage"})
            ticket.updated_at = datetime.now(timezone.utc)
            db.commit()


TICKET_SUMMARY_QUESTION = (
    "Résume ce ticket pour l'équipe support : contexte et symptômes, périmètre impacté, "
    "vérifications déjà réalisées, état actuel et prochaines étapes de diagnostic sûres et priorisées. "
    "Distingue les faits confirmés des hypothèses, n'invente aucun fait, indique les informations "
    "manquantes et cite les sources documentaires utilisées si elles sont disponibles. Réponds en français."
)


def recover_stuck_ai_tickets(session_factory=None, background: bool = True) -> int:
    """Relance le diagnostic IA des tickets restés « en réflexion » (redémarrage de l'API en cours de tâche).

    Les tâches de fond vivent dans le processus : un redémarrage les perd et le ticket resterait
    indéfiniment en attente (le client ne pourrait ni répondre ni transférer).
    """
    factory = session_factory or SessionLocal
    try:
        with factory() as db:
            ticket_ids = list(db.scalars(
                select(Ticket.id).where(Ticket.status == "ai_triage", Ticket.ai_status == "pending")
            ).all())
    except Exception:
        logger.exception("Could not look for stuck AI tickets")
        return 0

    def run_all() -> None:
        for ticket_id in ticket_ids:
            _run_customer_ai(ticket_id)  # gère et journalise ses propres erreurs

    if ticket_ids:
        logger.warning("Relance du diagnostic IA de %s ticket(s) interrompu(s)", len(ticket_ids))
        if background:
            import threading

            threading.Thread(target=run_all, name="ai-recovery", daemon=True).start()
        else:
            run_all()
    return len(ticket_ids)


def _run_agent_ai(
    ticket_id: str,
    question: str,
    actor_id: str,
    preference: dict | None = None,
    summary: bool = False,
) -> None:
    try:
        ticket_snapshot, conversation, attachment_names = _conversation_context(ticket_id, include_internal=True)
        if not ticket_snapshot.ai_processing_consent:
            raise AIAnalysisError("Le client n’a pas consenti à l’analyse IA de son ticket.")
        answer = assist_agent(
            ticket_snapshot,
            conversation,
            attachment_names,
            TICKET_SUMMARY_QUESTION if summary else question,
            task="handoff_summary" if summary else "agent_assistance",
            actor_id=actor_id,
            preference=preference,
        )
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            actor = db.get(User, actor_id)
            if not ticket or not actor:
                return
            assistant = _assistant_user(db)
            db.add(Message(ticket_id=ticket.id, author_id=assistant.id, body=answer, visibility="internal"))
            _event(db, ticket, actor, "ai_agent_assist", {})
            ticket.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        if isinstance(exc, AIAnalysisError):
            logger.warning("Agent AI assist failed for ticket %s: %s", ticket_id, exc)
        else:
            logger.exception("Unexpected agent AI assist failure for ticket %s", ticket_id)
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            actor = db.get(User, actor_id)
            if not ticket or not actor:
                return
            assistant = _assistant_user(db)
            db.add(Message(
                ticket_id=ticket.id,
                author_id=assistant.id,
                body=f"Assistance IA indisponible : {exc}",
                visibility="internal",
            ))
            _event(db, ticket, actor, "ai_agent_assist_failed", {})
            db.commit()


def _run_handoff_summary(ticket_id: str) -> None:
    try:
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if (
                not ticket
                or not ticket.customer.ai_processing_consent
                or not _has_customer_ai_exchange(ticket)
            ):
                return
        ticket_snapshot, conversation, attachment_names = _conversation_context(
            ticket_id,
            include_internal=False,
        )
        if not ticket_snapshot.ai_processing_consent:
            return
        summary = assist_agent(
            ticket_snapshot,
            conversation,
            attachment_names,
            (
                "Prépare une note interne de passation pour le technicien qui reprend ce ticket. "
                "Résume les interactions client-assistant et les vérifications déjà réalisées, "
                "en distinguant les faits confirmés des hypothèses. Termine par « Premières "
                "recommandations » avec 3 à 5 actions de diagnostic sûres, concrètes et priorisées. "
                "N’invente aucun fait ni résultat, indique les informations manquantes et cite les "
                "sources documentaires utilisées si elles sont disponibles. Réponds en français."
            ),
            task="handoff_summary",
            role="customer",
            actor_id=ticket.customer_id,
        )
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if not ticket:
                return
            assistant = _assistant_user(db)
            db.add(Message(
                ticket_id=ticket.id,
                author_id=assistant.id,
                body=summary,
                visibility="internal",
            ))
            _event(db, ticket, None, "ai_handoff_summary", {})
            ticket.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        if isinstance(exc, AIAnalysisError):
            logger.warning("AI handoff summary failed for ticket %s: %s", ticket_id, exc)
        else:
            logger.exception("Unexpected AI handoff summary failure for ticket %s", ticket_id)
        with SessionLocal() as db:
            ticket = db.get(Ticket, ticket_id)
            if not ticket:
                return
            assistant = _assistant_user(db)
            db.add(Message(
                ticket_id=ticket.id,
                author_id=assistant.id,
                body=(
                    "Le résumé IA de passation n’a pas pu être généré. "
                    "Consultez l’historique du ticket avant de poursuivre le diagnostic."
                ),
                visibility="internal",
            ))
            _event(db, ticket, None, "ai_handoff_summary_failed", {"message": str(exc)[:1000]})
            db.commit()


def _store_attachments(
    db: Session,
    ticket: Ticket,
    uploader: User,
    uploads: list[UploadFile],
) -> list[Path]:
    if len(uploads) > settings.max_attachments_per_upload:
        raise HTTPException(
            status_code=413,
            detail=f"Vous pouvez joindre au maximum {settings.max_attachments_per_upload} fichiers par envoi.",
        )
    upload_dir = Path(settings.upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    contents = [upload.file.read(settings.max_attachment_size + 1) for upload in uploads]
    if any(len(content) > settings.max_attachment_size for content in contents):
        raise HTTPException(status_code=413, detail="Chaque pièce jointe doit faire 10 Mo maximum.")

    created_paths: list[Path] = []
    try:
        for upload, content in zip(uploads, contents):
            original_name = Path(upload.filename or "").name
            extension = Path(original_name).suffix.lower()
            if not original_name or extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
                raise HTTPException(
                    status_code=415,
                    detail=f"Type de fichier non autorisé : {original_name or 'fichier sans nom'}.",
                )
            stored_name = f"{uuid4().hex}{extension}"
            destination = upload_dir / stored_name
            destination.write_bytes(content)
            created_paths.append(destination)
            db.add(TicketAttachment(
                ticket_id=ticket.id,
                uploader_id=uploader.id,
                file_name=original_name[:255],
                stored_name=stored_name,
                content_type=mimetypes.guess_type(original_name)[0] or "application/octet-stream",
                size=len(content),
            ))
        if uploads:
            _event(db, ticket, uploader, "attachments_added", {"count": len(uploads)})
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return created_paths


def _commit_with_attachment_cleanup(db: Session, paths: list[Path]) -> None:
    try:
        db.commit()
    except Exception:
        db.rollback()
        for path in paths:
            path.unlink(missing_ok=True)
        raise


def _serialize(ticket: Ticket, user: User) -> dict:
    messages = [
        {
            "id": m.id,
            "body": m.body,
            "visibility": m.visibility,
            "author_id": m.author_id,
            "author_name": m.author.full_name,
            "author_type": "ai" if m.author.role == "system" else m.author.role,
            "created_at": _utc(m.created_at),
        }
        for m in sorted(ticket.messages, key=lambda item: _utc(item.created_at))
        if user.role != "customer" or m.visibility == "public"
    ]
    events = []
    for event in sorted(ticket.events, key=lambda item: _utc(item.created_at)):
        details = event.details or {}
        if user.role == "customer":
            if event.event_type in {"ai_handoff_summary", "ai_handoff_summary_failed"}:
                continue
            if event.event_type == "message_added" and details.get("visibility") == "internal":
                continue
            if event.event_type == "ai_analysis_failed":
                details = {}
            elif event.event_type == "escalated":
                details = {key: details[key] for key in ("team", "external_reference", "from") if key in details}
        events.append({
            "id": event.id,
            "event_type": event.event_type,
            "details": details,
            "actor_name": event.actor.full_name if event.actor else "Système",
            "created_at": _utc(event.created_at),
        })
    return {
        "id": ticket.id,
        "reference": ticket.reference,
        "customer_id": ticket.customer_id,
        "customer_name": ticket.customer.full_name,
        "assigned_agent_id": ticket.assigned_agent_id,
        "assigned_agent_name": ticket.assigned_agent.full_name if ticket.assigned_agent else None,
        "title": ticket.title,
        "description": ticket.description,
        "product": ticket.product,
        "version": ticket.version,
        "ticket_type": ticket.ticket_type,
        "priority": ticket.priority,
        "sla_policy_id": ticket.sla_policy_id,
        "workaround_due_at": _utc(ticket.workaround_due_at) if ticket.workaround_due_at else None,
        "resolution_due_at": _utc(ticket.resolution_due_at) if ticket.resolution_due_at else None,
        "workaround_at": _utc(ticket.workaround_at) if ticket.workaround_at else None,
        "sla_support_units_per_hour": ticket.sla_support_units_per_hour,
        "customer_penalty_units": ticket.customer_penalty_units,
        "customer_penalty_reason": ticket.customer_penalty_reason,
        "status": ticket.status,
        "ai_status": ticket.ai_status,
        "ai_provider": ticket.ai_provider,
        "ai_summary": ticket.ai_summary,
        "ai_recommendations": ticket.ai_recommendations,
        "ai_escalation_recommended": ticket.ai_escalation_recommended,
        "ai_error": ticket.ai_error if user.role != "customer" else "",
        "ai_preferences": ticket.ai_preferences,
        "resolution_note": ticket.resolution_note,
        "rejection_reason": ticket.rejection_reason,
        "escalation_team": ticket.escalation_team,
        "escalation_reason": ticket.escalation_reason if user.role != "customer" else "",
        "external_reference": ticket.external_reference,
        "created_at": _utc(ticket.created_at),
        "updated_at": _utc(ticket.updated_at),
        "messages": messages,
        "events": events,
        "attachments": [
            {
                "id": attachment.id,
                "file_name": attachment.file_name,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "uploader_name": attachment.uploader.full_name,
                "created_at": _utc(attachment.created_at),
                "download_url": f"/tickets/{ticket.id}/attachments/{attachment.id}",
            }
            for attachment in ticket.attachments
        ],
    }


@router.post("", response_model=TicketOut, status_code=201)
def create_ticket(
    payload: TicketCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("customer")),
):
    company = db.get(Company, user.company_id)
    if not company or not company.is_active:
        raise HTTPException(status_code=403, detail="Votre compte doit être rattaché à une entreprise active.")
    consume_prepaid_credit(db, company)
    selected_priority = {
        "p1": "critical",
        "p2": "high",
        "p3": "medium",
        "p4": "low",
    }.get(payload.priority, payload.priority)
    ai_enabled_for_customer = user.ai_processing_consent
    ai_preferences = _validated_preference(db, user, payload.ai_model)
    ticket = Ticket(
        reference=f"OXE-{uuid4().hex[:8].upper()}",
        customer_id=user.id,
        title=payload.title.strip(),
        description=payload.description.strip(),
        product=payload.product.strip(),
        version=payload.version.strip(),
        ticket_type=payload.ticket_type,
        priority=selected_priority,
        status="ai_triage" if ai_enabled_for_customer else "new",
        ai_status="pending" if ai_enabled_for_customer else "disabled",
        ai_provider="",
        ai_preferences=ai_preferences,
    )
    db.add(ticket)
    db.flush()
    apply_ticket_sla(db, ticket, company)
    _event(db, ticket, user, "created", {"status": ticket.status})
    db.add(Message(ticket_id=ticket.id, author_id=user.id, body=ticket.description, visibility="public"))
    if not ai_enabled_for_customer:
        assistant = _assistant_user(db)
        db.add(Message(
            ticket_id=ticket.id,
            author_id=assistant.id,
            body="L’analyse IA n’a pas été lancée car votre consentement est désactivé. Votre demande est transmise à l’équipe support.",
            visibility="public",
        ))
    db.commit()
    if ai_enabled_for_customer:
        background_tasks.add_task(_run_customer_ai, ticket.id)
        # Les tâches de fond s'exécutent à la suite : l'enrichissement KB ne démarre qu'après la
        # première réponse IA, sans la ralentir ni ajouter de latence à la création du ticket.
        background_tasks.add_task(enqueue_ticket_enrichment, ticket.id)
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.get("", response_model=list[TicketOut])
def list_tickets(
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Ticket)
    if user.role == "customer":
        if user.company_id:
            query = query.join(User, Ticket.customer_id == User.id).where(User.company_id == user.company_id)
        else:
            query = query.where(Ticket.customer_id == user.id)
    elif user.role == "agent":
        query = query.join(User, Ticket.customer_id == User.id).join(
            company_agents, company_agents.c.company_id == User.company_id
        ).where(company_agents.c.agent_id == user.id, Ticket.status != "ai_triage")
    elif user.role == "admin":
        query = query.where(Ticket.status != "ai_triage")
    if status_filter:
        if status_filter not in STATUSES:
            raise HTTPException(status_code=422, detail="Filtre de statut invalide.")
        query = query.where(Ticket.status == status_filter)
    if search:
        term = f"%{search.strip()}%"
        query = query.where(or_(Ticket.reference.ilike(term), Ticket.title.ilike(term), Ticket.product.ilike(term)))
    tickets = db.scalars(query.order_by(Ticket.updated_at.desc()).limit(limit)).all()
    return [_serialize(ticket, user) for ticket in tickets]


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _serialize(_ticket_or_404(db, ticket_id, user), user)


@router.post("/{ticket_id}/conversation", response_model=TicketOut)
def customer_conversation(
    ticket_id: str,
    background_tasks: BackgroundTasks,
    body: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("customer")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if not body.strip() and not files:
        raise HTTPException(status_code=422, detail="Écrivez un message ou joignez au moins un fichier.")
    if ticket.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Cette demande est clôturée et ne peut pas être rouverte.")
    if ticket.status == "ai_triage" and body.strip() and ticket.ai_status == "pending":
        raise HTTPException(status_code=409, detail="Attendez la réponse de l’assistant avant de poursuivre.")
    if body.strip():
        db.add(Message(
            ticket_id=ticket.id,
            author_id=user.id,
            body=body.strip(),
            visibility="public",
        ))
        _event(db, ticket, user, "message_added", {"visibility": "public"})
    attachment_paths = _store_attachments(db, ticket, user, files)
    if ticket.status != "ai_triage":
        _mark_customer_reply_waiting_support(db, ticket, user)
    ticket.updated_at = datetime.now(timezone.utc)
    if ticket.status == "ai_triage":
        ticket.ai_status = "pending"
        background_tasks.add_task(_run_customer_ai, ticket.id)
    _commit_with_attachment_cleanup(db, attachment_paths)
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.post("/{ticket_id}/close", response_model=TicketOut)
def close_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("customer")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if ticket.status != "resolved":
        raise HTTPException(status_code=409, detail="Seul un ticket résolu peut être clôturé par le client.")
    if not ticket.resolution_note.strip():
        raise HTTPException(status_code=409, detail="La résolution doit être documentée avant la clôture.")
    ticket.status = "closed"
    ticket.updated_at = datetime.now(timezone.utc)
    _event(db, ticket, user, "customer_closed", {"from": "resolved", "to": "closed"})
    db.commit()
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.post("/{ticket_id}/handoff", response_model=TicketOut)
def handoff_to_agent(
    ticket_id: str,
    payload: HandoffCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("customer")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if ticket.status != "ai_triage":
        raise HTTPException(status_code=409, detail="Cette demande a déjà été transmise ou clôturée.")
    if not _customer_replied_after_first_ai_response(ticket):
        raise HTTPException(
            status_code=409,
            detail="Répondez d’abord à la première demande de l’assistant avant de transférer le ticket.",
        )
    previous = ticket.status
    ticket.status = "new"
    ticket.escalation_reason = payload.reason.strip()
    ticket.updated_at = datetime.now(timezone.utc)
    _event(db, ticket, user, "ai_handoff", {"from": previous})
    db.commit()
    db.refresh(ticket)
    if user.ai_processing_consent:
        background_tasks.add_task(_run_handoff_summary, ticket.id)
    return _serialize(ticket, user)


@router.post("/{ticket_id}/ai-assist", response_model=BackgroundAccepted, status_code=202)
def request_agent_ai_assistance(
    ticket_id: str,
    payload: AgentAIAssist,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if not ticket.customer.ai_processing_consent:
        raise HTTPException(
            status_code=409,
            detail="Le client n’a pas consenti à l’analyse IA de ce ticket.",
        )
    preference = _validated_preference(db, user, payload.ai_model)
    background_tasks.add_task(
        _run_agent_ai, ticket.id, payload.question.strip(), user.id, preference, payload.kind == "summary",
    )
    return BackgroundAccepted(status="pending")


@router.post("/{ticket_id}/kb-enrich", response_model=BackgroundAccepted, status_code=202)
def request_kb_enrichment(
    ticket_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    """Relance à la demande la recherche dans la KB constructeur pour ce ticket."""
    ticket = _ticket_or_404(db, ticket_id, user)
    if not ticket.customer.ai_processing_consent:
        raise HTTPException(status_code=409, detail="Le client n’a pas consenti à l’analyse IA de ce ticket.")
    if not kb_flags.is_enabled():
        raise HTTPException(status_code=409, detail="L’enrichissement par la KB constructeur est désactivé.")
    return BackgroundAccepted(status="queued" if enqueue_ticket_enrichment(ticket.id) else "ignored")


@router.put("/{ticket_id}/ai-preferences", response_model=TicketOut)
def set_ai_preferences(
    ticket_id: str,
    payload: ModelSelection,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("customer")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if ticket.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Cette demande est clôturée.")
    ticket.ai_preferences = _validated_preference(db, user, payload)
    _event(db, ticket, user, "ai_model_selected", {
        "provider": ticket.ai_preferences["provider"],
        "model": ticket.ai_preferences["model"],
        "think": ticket.ai_preferences["think"],
    })
    db.commit()
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.post("/{ticket_id}/attachments", response_model=TicketOut)
def upload_ticket_attachments(
    ticket_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if user.role == "customer" and ticket.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Cette demande est clôturée et ne peut pas être rouverte.")
    attachment_paths = _store_attachments(db, ticket, user, files)
    if user.role == "customer":
        _mark_customer_reply_waiting_support(db, ticket, user)
    ticket.updated_at = datetime.now(timezone.utc)
    _commit_with_attachment_cleanup(db, attachment_paths)
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.get("/{ticket_id}/attachments/{attachment_id}")
def download_ticket_attachment(
    ticket_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    attachment = db.get(TicketAttachment, attachment_id)
    if not attachment or attachment.ticket_id != ticket.id:
        raise HTTPException(status_code=404, detail="Pièce jointe introuvable.")
    path = (Path(settings.upload_dir).resolve() / attachment.stored_name).resolve()
    if path.parent != Path(settings.upload_dir).resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Pièce jointe introuvable.")
    return FileResponse(
        path,
        media_type=attachment.content_type,
        filename=attachment.file_name,
    )


@router.post("/{ticket_id}/messages", response_model=TicketOut)
def add_message(ticket_id: str, payload: MessageCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ticket = _ticket_or_404(db, ticket_id, user)
    if user.role == "customer" and ticket.status == "ai_triage":
        raise HTTPException(status_code=409, detail="Utilisez l’espace de diagnostic IA pour répondre à cette demande.")
    if user.role == "customer" and ticket.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Cette demande est clôturée et ne peut pas être rouverte.")
    if payload.visibility == "internal" and user.role == "customer":
        raise HTTPException(status_code=403, detail="Les notes internes sont réservées aux agents.")
    message = Message(ticket_id=ticket.id, author_id=user.id, body=payload.body.strip(), visibility=payload.visibility)
    db.add(message)
    _event(db, ticket, user, "message_added", {"visibility": payload.visibility})
    if user.role == "customer":
        _mark_customer_reply_waiting_support(db, ticket, user)
    ticket.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.post("/{ticket_id}/sla/workaround", response_model=TicketOut)
def mark_workaround_applied(
    ticket_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if not ticket.sla_policy_id:
        raise HTTPException(status_code=409, detail="Aucun SLA approuvé n’est associé à ce ticket.")
    if ticket.workaround_at:
        raise HTTPException(status_code=409, detail="Le contournement est déjà enregistré.")
    ticket.workaround_at = datetime.now(timezone.utc)
    ticket.updated_at = ticket.workaround_at
    _event(db, ticket, user, "sla_workaround_applied", {
        "due_at": ticket.workaround_due_at.isoformat() if ticket.workaround_due_at else None,
        "applied_at": ticket.workaround_at.isoformat(),
    })
    db.commit()
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.post("/{ticket_id}/sla/compliance", response_model=TicketOut)
def record_ticket_compliance(
    ticket_id: str,
    payload: ComplianceFinding,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if not ticket.sla_policy_id:
        raise HTTPException(status_code=409, detail="Aucun SLA approuvé n’est associé à ce ticket.")
    record_compliance_finding(db, ticket, user, payload)
    ticket.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.patch("/{ticket_id}/status", response_model=TicketOut)
def update_status(
    ticket_id: str, payload: StatusUpdate, db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    previous = ticket.status
    adding_legacy_resolution_note = (
        previous == "resolved"
        and payload.status == "resolved"
        and not ticket.resolution_note.strip()
    )
    if payload.status not in STAFF_STATUS_TRANSITIONS.get(previous, set()) and not adding_legacy_resolution_note:
        raise HTTPException(
            status_code=409,
            detail=f"Transition de statut non autorisée : {previous} → {payload.status}.",
        )
    if payload.status == "resolved":
        if len(payload.resolution_note.strip()) < 10:
            raise HTTPException(status_code=422, detail="Une note de résolution d’au moins 10 caractères est obligatoire.")
        ticket.resolution_note = payload.resolution_note.strip()
        ticket.rejection_reason = ""
        resolution_message = Message(
            ticket_id=ticket.id,
            author_id=user.id,
            body=ticket.resolution_note,
            visibility="public",
        )
        db.add(resolution_message)
        _event(db, ticket, user, "message_added", {"visibility": "public"})
    elif payload.status == "rejected":
        if len(payload.rejection_reason.strip()) < 10:
            raise HTTPException(status_code=422, detail="Un motif de rejet d’au moins 10 caractères est obligatoire.")
        ticket.rejection_reason = payload.rejection_reason.strip()
        ticket.resolution_note = ""
        db.add(Message(
            ticket_id=ticket.id,
            author_id=user.id,
            body=f"Cette demande ne peut pas être prise en charge. Motif : {ticket.rejection_reason}",
            visibility="public",
        ))
        _event(db, ticket, user, "message_added", {"visibility": "public"})
    else:
        ticket.resolution_note = ""
        ticket.rejection_reason = ""
    ticket.status = payload.status
    ticket.updated_at = datetime.now(timezone.utc)
    details = {"from": previous, "to": payload.status}
    if payload.status == "resolved":
        details["resolution_note"] = ticket.resolution_note
    elif payload.status == "rejected":
        details["rejection_reason"] = ticket.rejection_reason
    event_type = "resolution_note_added" if adding_legacy_resolution_note else "status_changed"
    _event(db, ticket, user, event_type, details)
    db.commit()
    db.refresh(ticket)
    return _serialize(ticket, user)


@router.patch("/{ticket_id}/assignment", response_model=TicketOut)
def assign_ticket(
    ticket_id: str, payload: AssignmentUpdate, db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    ticket = _ticket_or_404(db, ticket_id, user)
    if ticket.status in TERMINAL_STATUSES or ticket.status == "resolved":
        raise HTTPException(status_code=409, detail="Un ticket résolu ou terminal ne peut pas être réattribué.")
    agent = db.get(User, payload.agent_id) if payload.agent_id else None
    if payload.agent_id and (not agent or agent.role not in {"agent", "admin"} or not agent.is_active):
        raise HTTPException(status_code=422, detail="Agent indisponible ou invalide.")
    if agent and agent.role == "admin" and user.role != "admin":
        # Un administrateur peut prendre un ticket exceptionnellement; seul un administrateur peut le lui attribuer.
        raise HTTPException(status_code=403, detail="Seul un administrateur peut assigner un ticket à un administrateur.")
    company_id = ticket.customer.company_id
    if agent and agent.role == "agent" and company_id:
        if not _agent_can_access_company(db, agent, company_id):
            raise HTTPException(status_code=422, detail="Cet agent n’est pas affecté à l’entreprise du client.")
    ticket.assigned_agent_id = agent.id if agent else None
    ticket.updated_at = datetime.now(timezone.utc)
    _event(db, ticket, user, "assignment_changed", {"agent_id": agent.id if agent else None, "agent_name": agent.full_name if agent else None})
    db.commit()
    db.refresh(ticket)
    return _serialize(ticket, user)
