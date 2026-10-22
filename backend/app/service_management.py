from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_roles
from app.database import SessionLocal, get_db
from app.models import (
    Company,
    CompanyCreditGrant,
    CompanyCreditUsage,
    CompanyInvoice,
    SLAPolicy,
    ReportJob,
    Ticket,
    TicketEvent,
    User,
    company_agents,
)

router = APIRouter(prefix="/api", tags=["SLA, rapports et facturation"])
TICKET_TYPES = {"incident", "problem", "request"}
PRIORITY_MAP = {"p1": "critical", "p2": "high", "p3": "medium", "p4": "low"}


class SLAPolicyProposal(BaseModel):
    ticket_type: str = Field(pattern="^(incident|problem|request)$")
    priority: str = Field(pattern="^p[1-4]$")
    workaround_hours: int = Field(ge=1, le=24 * 90)
    resolution_hours: int = Field(ge=1, le=24 * 365)
    support_units_per_hour: float = Field(default=1, ge=0, le=100000)
    customer_penalty_units: float = Field(default=0.5, ge=0, le=100000)


class SLAPolicyReview(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=1000)


class ComplianceFinding(BaseModel):
    finding: str = Field(pattern="^(nonconforming|no_defect)$")
    note: str = Field(min_length=5, max_length=2000)


class BillingUpdate(BaseModel):
    billing_mode: str = Field(pattern="^(per_ticket|monthly)$")
    payment_mode: str = Field(pattern="^(prepaid|postpaid)$")
    payment_method: str = Field(default="", max_length=80)
    billing_currency: str = Field(default="EUR", pattern="^[A-Z]{3}$")
    monthly_fee_cents: int = Field(default=0, ge=0, le=100_000_000)
    price_per_ticket_cents: int = Field(default=0, ge=0, le=100_000_000)
    tax_rate_basis_points: int = Field(default=0, ge=0, le=10_000)
    payment_terms_days: int = Field(default=30, ge=0, le=365)


class CreditGrantInput(BaseModel):
    ticket_credits: int = Field(default=0, ge=0, le=1_000_000)
    month_credits: int = Field(default=0, ge=0, le=120)
    note: str = Field(default="", max_length=500)


class ReportRequest(BaseModel):
    period_start: date
    period_end: date
    company_id: str | None = None


def _company_access(db: Session, company_id: str, user: User) -> bool:
    if user.role == "admin":
        return True
    if user.role == "customer":
        return user.company_id == company_id
    return user.role == "agent" and db.scalar(
        select(company_agents.c.company_id).where(
            company_agents.c.company_id == company_id,
            company_agents.c.agent_id == user.id,
        )
    ) is not None


def _require_company(db: Session, company_id: str, user: User) -> Company:
    company = db.get(Company, company_id)
    if not company or not _company_access(db, company_id, user):
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    return company


def _policy_out(policy: SLAPolicy) -> dict:
    return {
        "id": policy.id,
        "company_id": policy.company_id,
        "ticket_type": policy.ticket_type,
        "priority": policy.priority,
        "workaround_hours": policy.workaround_hours,
        "resolution_hours": policy.resolution_hours,
        "support_units_per_hour": policy.support_units_per_hour,
        "customer_penalty_units": policy.customer_penalty_units,
        "status": policy.status,
        "review_note": policy.review_note,
        "created_at": policy.created_at,
        "reviewed_at": policy.reviewed_at,
    }


@router.get("/service/sla")
def list_sla_policies(
    company_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role == "customer":
        company_ids = [user.company_id] if user.company_id else []
    elif user.role == "agent":
        company_ids = list(db.scalars(
            select(company_agents.c.company_id).where(company_agents.c.agent_id == user.id)
        ).all())
        if company_id:
            if company_id not in company_ids:
                raise HTTPException(status_code=404, detail="Entreprise introuvable.")
            company_ids = [company_id]
    else:
        company_ids = [company_id] if company_id else list(db.scalars(select(Company.id)).all())
    if user.role == "customer" and company_id and company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    if not company_ids:
        return []
    policies = db.scalars(
        select(SLAPolicy)
        .where(SLAPolicy.company_id.in_(company_ids))
        .order_by(SLAPolicy.created_at.desc())
        .limit(1000)
    ).all()
    return [_policy_out(policy) for policy in policies]


@router.post("/service/sla/proposals", status_code=201)
def propose_sla(
    payload: SLAPolicyProposal,
    db: Session = Depends(get_db),
    customer: User = Depends(require_roles("customer")),
):
    if not customer.company_id:
        raise HTTPException(status_code=403, detail="Votre compte n’est associé à aucune entreprise.")
    existing = db.scalar(select(SLAPolicy).where(
        SLAPolicy.company_id == customer.company_id,
        SLAPolicy.ticket_type == payload.ticket_type,
        SLAPolicy.priority == payload.priority,
        SLAPolicy.status == "pending",
    ))
    if existing:
        raise HTTPException(status_code=409, detail="Une proposition est déjà en attente pour ce type et cette priorité.")
    policy = SLAPolicy(
        company_id=customer.company_id,
        created_by_id=customer.id,
        **payload.model_dump(),
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return _policy_out(policy)


@router.patch("/service/sla/{policy_id}")
def review_sla(
    policy_id: str,
    payload: SLAPolicyReview,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_roles("agent", "admin")),
):
    policy = db.get(SLAPolicy, policy_id)
    if not policy or not _company_access(db, policy.company_id, reviewer):
        raise HTTPException(status_code=404, detail="Proposition SLA introuvable.")
    if policy.status != "pending":
        raise HTTPException(status_code=409, detail="Cette proposition SLA a déjà été traitée.")
    if payload.decision == "approve":
        approved = db.scalars(select(SLAPolicy).where(
            SLAPolicy.company_id == policy.company_id,
            SLAPolicy.ticket_type == policy.ticket_type,
            SLAPolicy.priority == policy.priority,
            SLAPolicy.status == "approved",
        )).all()
        for previous in approved:
            previous.status = "superseded"
    policy.status = "approved" if payload.decision == "approve" else "rejected"
    policy.reviewed_by_id = reviewer.id
    policy.review_note = payload.note.strip()
    policy.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(policy)
    return _policy_out(policy)


def consume_prepaid_credit(db: Session, company: Company) -> None:
    if company.payment_mode != "prepaid":
        return
    changed = db.execute(
        update(Company)
        .where(Company.id == company.id, Company.prepaid_ticket_credits > 0)
        .values(prepaid_ticket_credits=Company.prepaid_ticket_credits - 1)
    )
    if changed.rowcount:
        return
    now = datetime.now(timezone.utc)
    period_exists = db.scalar(select(CompanyCreditUsage.id).where(
        CompanyCreditUsage.company_id == company.id,
        CompanyCreditUsage.year == now.year,
        CompanyCreditUsage.month == now.month,
    ))
    if period_exists:
        return
    grants = db.scalars(
        select(CompanyCreditGrant)
        .where(CompanyCreditGrant.company_id == company.id, CompanyCreditGrant.month_credits > 0)
        .order_by(CompanyCreditGrant.created_at.asc())
    ).all()
    for grant in grants:
        used_months = db.scalar(select(func.count()).select_from(CompanyCreditUsage).where(
            CompanyCreditUsage.grant_id == grant.id
        )) or 0
        if used_months >= grant.month_credits:
            continue
        try:
            with db.begin_nested():
                db.add(CompanyCreditUsage(
                    company_id=company.id,
                    year=now.year,
                    month=now.month,
                    grant_id=grant.id,
                ))
                db.flush()
            return
        except IntegrityError:
            if db.scalar(select(CompanyCreditUsage.id).where(
                CompanyCreditUsage.company_id == company.id,
                CompanyCreditUsage.year == now.year,
                CompanyCreditUsage.month == now.month,
            )):
                return
            raise
    raise HTTPException(
        status_code=402,
        detail="Le crédit prépayé de votre entreprise est épuisé. Contactez votre agent support.",
    )


def apply_ticket_sla(db: Session, ticket: Ticket, company: Company) -> None:
    priority = {
        "critical": "p1",
        "high": "p2",
        "medium": "p3",
        "low": "p4",
    }[ticket.priority]
    policy = db.scalar(select(SLAPolicy).where(
        SLAPolicy.company_id == company.id,
        SLAPolicy.ticket_type == ticket.ticket_type,
        SLAPolicy.priority == priority,
        SLAPolicy.status == "approved",
    ).order_by(SLAPolicy.reviewed_at.desc()).limit(1))
    if not policy:
        return
    created_at = ticket.created_at or datetime.now(timezone.utc)
    ticket.sla_policy_id = policy.id
    ticket.workaround_due_at = created_at + timedelta(hours=policy.workaround_hours)
    ticket.resolution_due_at = created_at + timedelta(hours=policy.resolution_hours)
    ticket.sla_support_units_per_hour = policy.support_units_per_hour
    ticket.sla_customer_penalty_units = policy.customer_penalty_units


def record_compliance_finding(db: Session, ticket: Ticket, actor: User, payload: ComplianceFinding) -> None:
    if ticket.customer_penalty_at:
        raise HTTPException(status_code=409, detail="Une constatation de conformité existe déjà.")
    ticket.customer_penalty_units = ticket.sla_customer_penalty_units
    ticket.customer_penalty_reason = payload.note.strip()
    ticket.customer_penalty_at = datetime.now(timezone.utc)
    db.add(TicketEvent(
        ticket_id=ticket.id,
        actor_id=actor.id,
        event_type="customer_sla_penalty",
        details={
            "finding": payload.finding,
            "units": ticket.customer_penalty_units,
            "note": ticket.customer_penalty_reason,
        },
    ))


def _period_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    if start > end:
        raise HTTPException(status_code=422, detail="La date de début doit précéder ou être égale à la date de fin.")
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )


def _hours_penalty(ticket: Ticket, start: datetime, end: datetime) -> float:
    due = ticket.workaround_due_at
    if not due or ticket.sla_support_units_per_hour <= 0:
        return 0
    due = due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due.astimezone(timezone.utc)
    actual = ticket.workaround_at
    actual = actual.replace(tzinfo=timezone.utc) if actual and actual.tzinfo is None else actual
    actual = min(actual, end) if actual else min(datetime.now(timezone.utc), end)
    if actual <= due:
        return 0
    elapsed_at_end = max(0, (min(actual, end) - due).total_seconds())
    elapsed_before = max(0, (min(actual, start) - due).total_seconds())
    charged_hours = (int(elapsed_at_end) + 3599) // 3600 - (int(elapsed_before) + 3599) // 3600
    return max(0, charged_hours) * ticket.sla_support_units_per_hour


def _report_data(db: Session, company_id: str, start: datetime, end: datetime) -> dict:
    tickets = db.scalars(
        select(Ticket)
        .where(
            Ticket.customer.has(User.company_id == company_id),
            Ticket.created_at >= start,
            Ticket.created_at < end,
        )
        .order_by(Ticket.created_at.asc())
    ).all()
    now = datetime.now(timezone.utc)
    as_of = min(end, now)
    support_units = sum(_hours_penalty(ticket, start, as_of) for ticket in tickets)
    customer_units = sum(
        ticket.customer_penalty_units
        for ticket in tickets
        if ticket.customer_penalty_at
        and start <= (
            ticket.customer_penalty_at.replace(tzinfo=timezone.utc)
            if ticket.customer_penalty_at.tzinfo is None
            else ticket.customer_penalty_at.astimezone(timezone.utc)
        ) < end
    )
    sla_tickets = [ticket for ticket in tickets if ticket.sla_policy_id]
    on_time = sum(
        ticket.workaround_due_at is not None
        and ticket.workaround_at is not None
        and _utc_dt(ticket.workaround_at) <= _utc_dt(ticket.workaround_due_at)
        for ticket in sla_tickets
    )
    return {
        "tickets": tickets,
        "ticket_count": len(tickets),
        "resolved_count": sum(ticket.status in {"resolved", "closed"} for ticket in tickets),
        "open_count": sum(ticket.status not in {"resolved", "closed", "rejected"} for ticket in tickets),
        "sla_count": len(sla_tickets),
        "sla_on_time_count": on_time,
        "support_units": round(support_units, 2),
        "customer_units": round(customer_units, 2),
    }


def _utc_dt(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=22, leading=27, textColor=colors.HexColor("#17345e"), spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="SectionTitle", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=16, textColor=colors.HexColor("#285db8"), spaceBefore=10, spaceAfter=7,
    ))
    styles.add(ParagraphStyle(
        name="SmallCell", parent=styles["BodyText"], fontSize=8, leading=10,
        textColor=colors.HexColor("#34445c"),
    ))
    return styles


def _money(cents: int, currency: str) -> str:
    return f"{cents / 100:,.2f} {currency}".replace(",", " ").replace(".", ",")


def _build_report_pdf(company: Company, start_date: date, end_date: date, data: dict) -> bytes:
    output = BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=17 * mm, leftMargin=17 * mm,
        topMargin=18 * mm, bottomMargin=17 * mm, title=f"Rapport support {company.name}",
    )
    styles = _styles()
    story = [
        Paragraph("Rapport de service", styles["ReportTitle"]),
        Paragraph(f"<b>{company.name}</b> · période du {start_date:%d/%m/%Y} au {end_date:%d/%m/%Y}", styles["BodyText"]),
        Spacer(1, 8 * mm),
        Paragraph("Synthèse de la période", styles["SectionTitle"]),
    ]
    metrics = [
        ["Tickets reçus", "Résolus / clôturés", "Avec SLA"],
        [str(data["ticket_count"]), str(data["resolved_count"]), str(data["sla_count"])],
        ["Respect du délai de contournement", "U dues au client", "U dues au support"],
        [
            f'{data["sla_on_time_count"]}/{data["sla_count"]} dans le délai' if data["sla_count"] else "Aucun SLA applicable",
            f'{data["customer_units"]:.2f} U',
            f'{data["support_units"]:.2f} U',
        ],
    ]
    metric_table = Table(metrics, colWidths=[58 * mm] * 3)
    metric_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17345e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#eef3fb")),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#285db8")),
        ("TEXTCOLOR", (0, 2), (-1, 2), colors.white),
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#f4f7fb")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), .5, colors.white),
    ]))
    story.extend([metric_table, Spacer(1, 5 * mm)])
    story.append(Paragraph(f'{data["open_count"]} ticket(s) encore ouvert(s) à la date du rapport.', styles["BodyText"]))
    story.append(Paragraph("Détail des tickets", styles["SectionTitle"]))
    rows = [[
        Paragraph("<b>Référence</b>", styles["SmallCell"]),
        Paragraph("<b>Type / priorité</b>", styles["SmallCell"]),
        Paragraph("<b>État</b>", styles["SmallCell"]),
        Paragraph("<b>Échéance contournement</b>", styles["SmallCell"]),
        Paragraph("<b>U support</b>", styles["SmallCell"]),
        Paragraph("<b>U client</b>", styles["SmallCell"]),
    ]]
    for ticket in data["tickets"]:
        rows.append([
            Paragraph(ticket.reference, styles["SmallCell"]),
            Paragraph(f"{ticket.ticket_type} / {ticket.priority}", styles["SmallCell"]),
            Paragraph(ticket.status, styles["SmallCell"]),
            Paragraph(ticket.workaround_due_at.strftime("%d/%m/%Y %H:%M UTC") if ticket.workaround_due_at else "Non défini", styles["SmallCell"]),
            f'{_hours_penalty(ticket, datetime.combine(start_date, time.min, tzinfo=timezone.utc), datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)):.2f}',
            f'{ticket.customer_penalty_units:.2f}' if ticket.customer_penalty_at and start_date <= _utc_dt(ticket.customer_penalty_at).date() <= end_date else "0.00",
        ])
    if not data["tickets"]:
        rows.append([Paragraph("Aucun ticket sur cette période.", styles["SmallCell"]), "", "", "", "", ""])
    detail = Table(rows, colWidths=[27 * mm, 31 * mm, 25 * mm, 43 * mm, 18 * mm, 18 * mm], repeatRows=1)
    detail.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf2f8")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#30415b")),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#dfe5ed")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(detail)
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        "Les unités U indiquées sont calculées à partir des SLA approuvés et des constats enregistrés. "
        "Ce rapport est un relevé opérationnel; il ne constitue pas une facture.",
        styles["SmallCell"],
    ))
    doc.build(story)
    return output.getvalue()


def _build_invoice_pdf(company: Company, invoice: CompanyInvoice) -> bytes:
    output = BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm,
        topMargin=22 * mm, bottomMargin=20 * mm, title=invoice.invoice_number,
    )
    styles = _styles()
    heading_style = ParagraphStyle("InvoiceNumber", parent=styles["Heading2"], alignment=TA_RIGHT, textColor=colors.HexColor("#285db8"))
    story = [
        Paragraph("Facture", styles["ReportTitle"]),
        Paragraph(invoice.invoice_number, heading_style),
        Paragraph(f"<b>Émetteur :</b> Plateforme OXE Support<br/><b>Client :</b> {company.name}", styles["BodyText"]),
        Paragraph(f"<b>Période :</b> {invoice.period_month:02d}/{invoice.period_year}", styles["BodyText"]),
        Spacer(1, 12 * mm),
    ]
    if invoice.billing_mode == "monthly":
        description = "Abonnement mensuel plateforme"
        quantity = "1 mois"
        unit_price = _money(invoice.subtotal_cents, invoice.currency)
    else:
        description = "Tickets créés pendant la période"
        quantity = str(invoice.ticket_count)
        unit_price = _money(company.price_per_ticket_cents, invoice.currency)
    rows = [
        ["Description", "Quantité", "Prix unitaire", "Montant HT"],
        [description, quantity, unit_price, _money(invoice.subtotal_cents, invoice.currency)],
        ["", "", "Sous-total HT", _money(invoice.subtotal_cents, invoice.currency)],
        ["", "", f"Taxes ({invoice.tax_rate_basis_points / 100:.2f} %)", _money(invoice.tax_cents, invoice.currency)],
        ["", "", "Total TTC", _money(invoice.total_cents, invoice.currency)],
    ]
    table = Table(rows, colWidths=[83 * mm, 25 * mm, 35 * mm, 35 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17345e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (2, 2), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, 4), (-1, 4), colors.HexColor("#eef3fb")),
        ("GRID", (0, 0), (-1, 1), .5, colors.HexColor("#dfe5ed")),
        ("LINEABOVE", (2, 2), (-1, 2), .5, colors.HexColor("#aeb9c8")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.extend([table, Spacer(1, 10 * mm)])
    story.append(Paragraph(
        f"Mode de paiement : {invoice.payment_mode} · {invoice.payment_method or 'à définir'} · "
        f"Délai de paiement : {invoice.payment_terms_days} jours.",
        styles["BodyText"],
    ))
    story.append(Spacer(1, 7 * mm))
    story.append(Paragraph(
        "Document généré par la plateforme. Les informations légales de l’émetteur et toute mention fiscale "
        "obligatoire doivent être complétées avant émission d’une facture fiscale définitive.",
        styles["SmallCell"],
    ))
    doc.build(story)
    return output.getvalue()


def _run_report_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(ReportJob, job_id)
        if not job:
            return
        job.status = "processing"
        db.commit()
        start = _utc_dt(job.period_start)
        end = _utc_dt(job.period_end)
        company = db.get(Company, job.company_id)
        if not company:
            job.status = "failed"
            job.error = "Entreprise introuvable."
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            return
        try:
            data = _report_data(db, company.id, start, end)
            pdf = _build_report_pdf(company, start.date(), (end - timedelta(days=1)).date(), data)
            job.pdf_content = pdf
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            db.rollback()
            failed_job = db.get(ReportJob, job_id)
            if failed_job:
                failed_job.status = "failed"
                failed_job.error = str(exc)[:1000]
                failed_job.completed_at = datetime.now(timezone.utc)
                db.commit()


def _job_out(job: ReportJob) -> dict:
    return {
        "id": job.id,
        "company_id": job.company_id,
        "period_start": job.period_start,
        "period_end": job.period_end,
        "status": job.status,
        "error": job.error,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
    }


@router.post("/service/reports", status_code=202)
def start_report(
    payload: ReportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company_id = user.company_id if user.role == "customer" else payload.company_id
    if user.role == "agent":
        if not company_id:
            company_id = db.scalar(select(company_agents.c.company_id).where(company_agents.c.agent_id == user.id).limit(1))
    if not company_id:
        raise HTTPException(status_code=403, detail="Aucune entreprise n’est associée à votre compte.")
    _require_company(db, company_id, user)
    start, end = _period_bounds(payload.period_start, payload.period_end)
    job = ReportJob(
        company_id=company_id,
        requested_by_id=user.id,
        period_start=start,
        period_end=end,
    )
    db.add(job)
    db.commit()
    background_tasks.add_task(_run_report_job, job.id)
    return _job_out(job)


@router.get("/service/reports")
def list_reports(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role == "customer":
        company_ids = [user.company_id] if user.company_id else []
    elif user.role == "agent":
        company_ids = list(db.scalars(select(company_agents.c.company_id).where(
            company_agents.c.agent_id == user.id
        )).all())
    else:
        company_ids = list(db.scalars(select(Company.id)).all())
    if not company_ids:
        return []
    jobs = db.scalars(
        select(ReportJob).where(ReportJob.company_id.in_(company_ids))
        .order_by(ReportJob.created_at.desc()).limit(100)
    ).all()
    return [_job_out(job) for job in jobs]


@router.get("/service/reports/{job_id}")
def get_report(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.get(ReportJob, job_id)
    if not job or not _company_access(db, job.company_id, user):
        raise HTTPException(status_code=404, detail="Rapport introuvable.")
    return _job_out(job)


@router.get("/service/reports/{job_id}/download")
def download_report(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.get(ReportJob, job_id)
    if not job or not _company_access(db, job.company_id, user):
        raise HTTPException(status_code=404, detail="Rapport introuvable.")
    if job.status != "completed" or not job.pdf_content:
        raise HTTPException(status_code=409, detail="Le rapport n’est pas encore prêt.")
    filename = f"rapport-{job.period_start:%Y%m%d}-{(job.period_end - timedelta(days=1)):%Y%m%d}.pdf"
    return Response(
        content=job.pdf_content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/billing/companies")
def list_company_billing(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    companies = db.scalars(select(Company).order_by(Company.name)).all()
    return [_billing_out(db, company) for company in companies]


def _billing_out(db: Session, company: Company) -> dict:
    remaining_month_credits = 0
    grants = db.scalars(select(CompanyCreditGrant).where(
        CompanyCreditGrant.company_id == company.id,
        CompanyCreditGrant.month_credits > 0,
    )).all()
    for grant in grants:
        used = db.scalar(select(func.count()).select_from(CompanyCreditUsage).where(
            CompanyCreditUsage.grant_id == grant.id
        )) or 0
        remaining_month_credits += max(0, grant.month_credits - used)
    return {
        "id": company.id,
        "name": company.name,
        "billing_mode": company.billing_mode,
        "payment_mode": company.payment_mode,
        "payment_method": company.payment_method,
        "billing_currency": company.billing_currency,
        "monthly_fee_cents": company.monthly_fee_cents,
        "price_per_ticket_cents": company.price_per_ticket_cents,
        "tax_rate_basis_points": company.tax_rate_basis_points,
        "payment_terms_days": company.payment_terms_days,
        "prepaid_ticket_credits": company.prepaid_ticket_credits,
        "prepaid_month_credits": remaining_month_credits,
    }


@router.get("/billing/summary")
def billing_summary(
    company_id: str | None = None,
    year: int = Query(ge=2000, le=2200),
    month: int = Query(ge=1, le=12),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role == "customer":
        company_id = user.company_id
    elif user.role == "agent" and not company_id:
        company_id = db.scalar(select(company_agents.c.company_id).where(company_agents.c.agent_id == user.id).limit(1))
    if not company_id:
        raise HTTPException(status_code=422, detail="Une entreprise doit être sélectionnée.")
    company = _require_company(db, company_id, user)
    ticket_count = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.customer.has(User.company_id == company.id),
        func.extract("year", Ticket.created_at) == year,
        func.extract("month", Ticket.created_at) == month,
    )) or 0
    subtotal = company.monthly_fee_cents if company.billing_mode == "monthly" else ticket_count * company.price_per_ticket_cents
    tax = round(subtotal * company.tax_rate_basis_points / 10_000)
    invoice = db.scalar(select(CompanyInvoice).where(
        CompanyInvoice.company_id == company.id,
        CompanyInvoice.period_year == year,
        CompanyInvoice.period_month == month,
    ))
    return {
        "company": company.name,
        "period_year": year,
        "period_month": month,
        "ticket_count": ticket_count,
        "billing_mode": company.billing_mode,
        "payment_mode": company.payment_mode,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": subtotal + tax,
        "currency": company.billing_currency,
        "prepaid_ticket_credits": company.prepaid_ticket_credits,
        "prepaid_month_credits": _billing_out(db, company)["prepaid_month_credits"],
        "invoice_id": invoice.id if invoice else None,
    }


@router.put("/billing/companies/{company_id}")
def update_company_billing(
    company_id: str,
    payload: BillingUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    for key, value in payload.model_dump().items():
        setattr(company, key, value)
    db.commit()
    db.refresh(company)
    return _billing_out(db, company)


@router.post("/billing/companies/{company_id}/credits", status_code=201)
def grant_company_credits(
    company_id: str,
    payload: CreditGrantInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_roles("admin")),
):
    if payload.ticket_credits == 0 and payload.month_credits == 0:
        raise HTTPException(status_code=422, detail="Attribuez au moins un crédit de ticket ou un mois.")
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    grant = CompanyCreditGrant(
        company_id=company.id,
        granted_by_id=admin.id,
        **payload.model_dump(),
    )
    company.prepaid_ticket_credits += payload.ticket_credits
    db.add(grant)
    db.commit()
    db.refresh(company)
    return {"grant_id": grant.id, **_billing_out(db, company)}


@router.get("/billing/invoices")
def list_invoices(
    company_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role == "customer":
        company_ids = [user.company_id] if user.company_id else []
    elif user.role == "agent":
        company_ids = list(db.scalars(select(company_agents.c.company_id).where(
            company_agents.c.agent_id == user.id
        )).all())
        if company_id and company_id not in company_ids:
            raise HTTPException(status_code=404, detail="Entreprise introuvable.")
        if company_id:
            company_ids = [company_id]
    else:
        company_ids = [company_id] if company_id else list(db.scalars(select(Company.id)).all())
    if not company_ids:
        return []
    invoices = db.scalars(select(CompanyInvoice)
        .where(CompanyInvoice.company_id.in_(company_ids))
        .order_by(CompanyInvoice.period_year.desc(), CompanyInvoice.period_month.desc())
        .limit(100)).all()
    return [_invoice_out(invoice) for invoice in invoices]


def _invoice_out(invoice: CompanyInvoice) -> dict:
    return {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "company_id": invoice.company_id,
        "period_year": invoice.period_year,
        "period_month": invoice.period_month,
        "ticket_count": invoice.ticket_count,
        "subtotal_cents": invoice.subtotal_cents,
        "tax_cents": invoice.tax_cents,
        "total_cents": invoice.total_cents,
        "currency": invoice.currency,
        "billing_mode": invoice.billing_mode,
        "payment_mode": invoice.payment_mode,
        "payment_method": invoice.payment_method,
        "payment_terms_days": invoice.payment_terms_days,
        "created_at": invoice.created_at,
    }


@router.post("/billing/invoices/{year}/{month}")
def generate_invoice(
    year: int,
    month: int,
    company_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("agent", "admin")),
):
    if month < 1 or month > 12 or year < 2000 or year > 2200:
        raise HTTPException(status_code=422, detail="Période de facturation invalide.")
    if user.role == "agent" and not company_id:
        company_id = db.scalar(select(company_agents.c.company_id).where(company_agents.c.agent_id == user.id).limit(1))
    if not company_id:
        raise HTTPException(status_code=422, detail="Une entreprise doit être sélectionnée.")
    company = _require_company(db, company_id, user)
    invoice = db.scalar(select(CompanyInvoice).where(
        CompanyInvoice.company_id == company.id,
        CompanyInvoice.period_year == year,
        CompanyInvoice.period_month == month,
    ))
    if invoice:
        return _invoice_out(invoice)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    end = datetime.combine(next_month, time.min, tzinfo=timezone.utc)
    ticket_count = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.customer.has(User.company_id == company.id),
        Ticket.created_at >= start,
        Ticket.created_at < end,
    )) or 0
    subtotal = company.monthly_fee_cents if company.billing_mode == "monthly" else ticket_count * company.price_per_ticket_cents
    tax = round(subtotal * company.tax_rate_basis_points / 10_000)
    invoice = CompanyInvoice(
        invoice_number=f"OXE-{year}{month:02d}-{uuid4().hex[:8].upper()}",
        company_id=company.id,
        generated_by_id=user.id,
        period_year=year,
        period_month=month,
        ticket_count=ticket_count,
        subtotal_cents=subtotal,
        tax_cents=tax,
        total_cents=subtotal + tax,
        currency=company.billing_currency,
        billing_mode=company.billing_mode,
        payment_mode=company.payment_mode,
        payment_method=company.payment_method,
        payment_terms_days=company.payment_terms_days,
        tax_rate_basis_points=company.tax_rate_basis_points,
        pdf_content=b"",
    )
    invoice.pdf_content = _build_invoice_pdf(company, invoice)
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        invoice = db.scalar(select(CompanyInvoice).where(
            CompanyInvoice.company_id == company.id,
            CompanyInvoice.period_year == year,
            CompanyInvoice.period_month == month,
        ))
        if not invoice:
            raise
    db.refresh(invoice)
    return _invoice_out(invoice)


@router.get("/billing/invoices/{invoice_id}/download")
def download_invoice(
    invoice_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    invoice = db.get(CompanyInvoice, invoice_id)
    if not invoice or not _company_access(db, invoice.company_id, user):
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    return Response(
        content=invoice.pdf_content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{invoice.invoice_number}.pdf"'},
    )


@router.get("/billing/companies/{company_id}")
def get_company_billing(
    company_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    company = _require_company(db, company_id, user)
    return _billing_out(db, company)
