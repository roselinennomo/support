from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Table, Text, Float, LargeBinary, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


company_agents = Table(
    "company_agents",
    Base.metadata,
    Column("company_id", ForeignKey("companies.id"), primary_key=True),
    Column("agent_id", ForeignKey("users.id"), primary_key=True),
)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    invitation_code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    billing_mode: Mapped[str] = mapped_column(String(20), default="per_ticket")
    payment_mode: Mapped[str] = mapped_column(String(20), default="postpaid")
    payment_method: Mapped[str] = mapped_column(String(80), default="")
    billing_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    monthly_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    price_per_ticket_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_rate_basis_points: Mapped[int] = mapped_column(Integer, default=0)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    prepaid_ticket_credits: Mapped[int] = mapped_column(Integer, default=0)
    # Entreprise « individuelle » d'un client indépendant : un seul utilisateur, facturation propre, aucun code d'invitation.
    is_individual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agents: Mapped[list["User"]] = relationship(secondary=company_agents, back_populates="companies")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(20), default="customer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    account_status: Mapped[str] = mapped_column(String(20), default="approved")
    approved_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_processing_consent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ai_trace_consent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ai_consent_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company | None] = relationship(foreign_keys=[company_id])
    companies: Mapped[list[Company]] = relationship(secondary=company_agents, back_populates="agents")

    @property
    def company_name(self) -> str | None:
        return self.company.name if self.company else None

    @property
    def is_individual(self) -> bool:
        """Client indépendant : client rattaché à son entreprise individuelle."""
        return bool(self.role == "customer" and self.company and self.company.is_individual)

    @property
    def company_ids(self) -> list[str]:
        return [company.id for company in self.companies] if self.role == "agent" else []


class AIRoutingConfig(Base):
    __tablename__ = "ai_routing_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    routes: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CompanyDocument(Base):
    """Document propre à une entreprise (architecture, plan d'adressage, matrice de flux…) : privé à cette entreprise."""

    __tablename__ = "company_documents"
    __table_args__ = (UniqueConstraint("company_id", "content_hash", name="uq_company_document_content"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    uploaded_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String(500), default="")
    stored_name: Mapped[str] = mapped_column(String(100))  # <sha256><ext> dans company_docs/<company_id>/
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64))  # empreinte du fichier : pas de doublon dans l'entreprise
    content: Mapped[str] = mapped_column(Text, default="")  # texte extrait, pour réindexer sans relire le fichier
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|indexed|empty|failed
    error: Mapped[str] = mapped_column(Text, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_model: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    company: Mapped[Company] = relationship()


class HelpConversation(Base):
    """Conversation de la console d'aide : personnelle à son propriétaire, rattachée à au plus une entreprise."""

    __tablename__ = "help_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="Nouvelle conversation")
    ticket_context: Mapped[str] = mapped_column(Text, default="")  # copie du ticket, figée à la création (ou actualisée)
    use_kb_live: Mapped[bool] = mapped_column(Boolean, default=False)
    use_web: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    company: Mapped[Company | None] = relationship()
    messages: Mapped[list["HelpMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="HelpMessage.created_at",
    )
    files: Mapped[list["HelpFile"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="HelpFile.created_at",
    )


class HelpMessage(Base):
    __tablename__ = "help_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("help_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="done")  # pending | done | error
    error: Mapped[str] = mapped_column(Text, default="")
    thinking: Mapped[str] = mapped_column(Text, default="")  # raisonnement du modèle, s'il l'expose
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # fin du calcul de la réponse
    provider: Mapped[str] = mapped_column(String(30), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[HelpConversation] = relationship(back_populates="messages")


class HelpFile(Base):
    """Fichier ajouté au contexte d'une conversation : texte extrait seulement, supprimé avec la conversation."""

    __tablename__ = "help_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("help_conversations.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(10), default="upload")  # upload | ticket (pièce jointe du ticket lue)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[HelpConversation] = relationship(back_populates="files")


class RAGConfig(Base):
    __tablename__ = "rag_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    embedding_model: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KBSettings(Base):
    """Interrupteur en direct et état de la session du navigateur KB (ligne unique)."""

    __tablename__ = "kb_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    session_state: Mapped[str] = mapped_column(String(20), default="unknown")
    session_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alert: Mapped[str] = mapped_column(Text, default="")
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KBArticle(Base):
    """Article KB déjà récupéré : sert à la déduplication et à la réindexation sans re-scraper."""

    __tablename__ = "kb_articles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    url: Mapped[str] = mapped_column(String(1000), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(20), default="kb", index=True)  # kb | document
    indexed_model: Mapped[str] = mapped_column(String(200), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KBJob(Base):
    """Une recherche KB pour un ticket : journal d'exécution, métriques et aide au debug."""

    __tablename__ = "kb_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="enrich")  # enrich | reindex
    # pending | running | success | failed | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    query: Mapped[str] = mapped_column(Text, default="")
    query_source: Mapped[str] = mapped_column(String(20), default="")
    hits: Mapped[int] = mapped_column(Integer, default=0)
    new_documents: Mapped[int] = mapped_column(Integer, default=0)
    skipped_documents: Mapped[int] = mapped_column(Integer, default=0)
    urls: Mapped[list] = mapped_column(JSON, default=list)
    results: Mapped[list] = mapped_column(JSON, default=list)  # [{source, title, url, status, detail}]
    error_code: Mapped[str] = mapped_column(String(40), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    failed_selector: Mapped[str] = mapped_column(String(500), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIExecution(Base):
    __tablename__ = "ai_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    # NULL pour les appels hors ticket (console d'aide).
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id"), nullable=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    task: Mapped[str] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    evaluation_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluation_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AIStep(Base):
    """Étape horodatée d'un traitement IA lié à un ticket (voir app/ai_steps.py) : durées et compteurs, jamais de contenu."""

    __tablename__ = "ai_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id"), nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(80))  # traitement : « Diagnostic client (IA) », « Aide IA agent »…
    kind: Mapped[str] = mapped_column(String(20))  # run | rag | provider
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AIFailure(Base):
    """Échec d'un appel IA hors ticket (console d'aide) : les échecs de tickets sont des événements de ticket."""

    __tablename__ = "ai_failures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    task: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(30), index=True)
    model: Mapped[str] = mapped_column(String(200))
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AIModelPrice(Base):
    __tablename__ = "ai_model_prices"
    __table_args__ = (UniqueConstraint("provider", "model", name="uq_ai_model_price_provider_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    model: Mapped[str] = mapped_column(String(200))
    input_usd_per_million: Mapped[float] = mapped_column(Float, default=0)
    output_usd_per_million: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserAdminAudit(Base):
    __tablename__ = "user_admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    reference: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_agent_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    product: Mapped[str] = mapped_column(String(100), default="OXE")
    version: Mapped[str] = mapped_column(String(80), default="")
    ticket_type: Mapped[str] = mapped_column(String(20), default="incident")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    sla_policy_id: Mapped[str | None] = mapped_column(ForeignKey("sla_policies.id"), nullable=True)
    workaround_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workaround_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_support_units_per_hour: Mapped[float] = mapped_column(Float, default=0)
    sla_customer_penalty_units: Mapped[float] = mapped_column(Float, default=0)
    customer_penalty_units: Mapped[float] = mapped_column(Float, default=0)
    customer_penalty_reason: Mapped[str] = mapped_column(Text, default="")
    customer_penalty_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="new", index=True)
    ai_status: Mapped[str] = mapped_column(String(20), default="pending")
    ai_provider: Mapped[str] = mapped_column(String(30), default="")
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    ai_recommendations: Mapped[list] = mapped_column(JSON, default=list)
    ai_escalation_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_error: Mapped[str] = mapped_column(Text, default="")
    # Modèle choisi par le client pour son diagnostic : {"provider", "model", "think"}.
    ai_preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    escalation_team: Mapped[str] = mapped_column(String(120), default="")
    escalation_reason: Mapped[str] = mapped_column(Text, default="")
    external_reference: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    customer: Mapped[User] = relationship(foreign_keys=[customer_id])
    assigned_agent: Mapped[User | None] = relationship(foreign_keys=[assigned_agent_id])
    messages: Mapped[list["Message"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")
    events: Mapped[list["TicketEvent"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")
    attachments: Mapped[list["TicketAttachment"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")


class SLAPolicy(Base):
    __tablename__ = "sla_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewed_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    ticket_type: Mapped[str] = mapped_column(String(20))
    priority: Mapped[str] = mapped_column(String(2))
    workaround_hours: Mapped[int] = mapped_column(Integer)
    resolution_hours: Mapped[int] = mapped_column(Integer)
    support_units_per_hour: Mapped[float] = mapped_column(Float, default=1)
    customer_penalty_units: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompanyCreditGrant(Base):
    __tablename__ = "company_credit_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    granted_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    ticket_credits: Mapped[int] = mapped_column(Integer, default=0)
    month_credits: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CompanyCreditUsage(Base):
    __tablename__ = "company_credit_usage"
    __table_args__ = (UniqueConstraint("company_id", "year", "month", name="uq_company_credit_usage_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    grant_id: Mapped[str] = mapped_column(ForeignKey("company_credit_grants.id"))
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReportJob(Base):
    __tablename__ = "report_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    requested_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    pdf_content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompanyInvoice(Base):
    __tablename__ = "company_invoices"
    __table_args__ = (UniqueConstraint("company_id", "period_year", "period_month", name="uq_company_invoice_period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    invoice_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    generated_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    period_year: Mapped[int] = mapped_column(Integer)
    period_month: Mapped[int] = mapped_column(Integer)
    ticket_count: Mapped[int] = mapped_column(Integer, default=0)
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    billing_mode: Mapped[str] = mapped_column(String(20))
    payment_mode: Mapped[str] = mapped_column(String(20))
    payment_method: Mapped[str] = mapped_column(String(80), default="")
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    pdf_content: Mapped[bytes] = mapped_column(LargeBinary)
    tax_rate_basis_points: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(20), default="public")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")
    author: Mapped[User] = relationship()


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    uploader_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    file_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="attachments")
    uploader: Mapped[User] = relationship()


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="events")
    actor: Mapped[User | None] = relationship()
