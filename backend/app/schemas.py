from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, ValidationError

_EMAIL_STR_ADAPTER = TypeAdapter(EmailStr)


def validate_portal_email(value: str) -> str:
    try:
        return str(_EMAIL_STR_ADAPTER.validate_python(value))
    except ValidationError as exc:
        local_part, separator, domain = value.rpartition("@")
        if not separator or not domain.lower().endswith(".local"):
            raise ValueError("Adresse e-mail invalide.") from exc

        local_domain = domain[:-len(".local")]
        if not local_part or not local_domain:
            raise ValueError("Adresse e-mail invalide.") from exc

        normalized = str(
            _EMAIL_STR_ADAPTER.validate_python(f"{local_part}@{local_domain}.example.com")
        )
        normalized_local, _, normalized_domain = normalized.rpartition("@")
        return f"{normalized_local}@{normalized_domain.removesuffix('.example.com')}.local"


PortalEmail = Annotated[str, AfterValidator(validate_portal_email)]


class UserCreate(BaseModel):
    email: PortalEmail
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=128)
    company_code: str = Field(min_length=8, max_length=100)
    ai_processing_consent: bool = True
    ai_trace_consent: bool = True


class StaffUserCreate(BaseModel):
    email: PortalEmail
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=128)
    role: str = Field(pattern="^(agent|admin)$")


class UserManagementUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    email: PortalEmail | None = None
    role: str | None = Field(default=None, pattern="^(customer|agent|admin)$")
    is_active: bool | None = None
    account_status: str | None = Field(default=None, pattern="^(pending|approved|rejected)$")
    company_id: str | None = None
    company_ids: list[str] | None = Field(default=None, max_length=100)
    ai_processing_consent: bool | None = None
    ai_trace_consent: bool | None = None


class PasswordReset(BaseModel):
    temporary_password: str = Field(min_length=10, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class CompanyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    agent_ids: list[str] = Field(default_factory=list, max_length=100)


class AccountDecision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


class CompanyAssignment(BaseModel):
    company_id: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: PortalEmail
    full_name: str
    role: str
    is_active: bool
    company_id: str | None = None
    company_name: str | None = None
    company_ids: list[str] = Field(default_factory=list)
    account_status: str = "approved"
    created_at: datetime | None = None
    ai_processing_consent: bool = True
    ai_trace_consent: bool = True
    ai_consent_updated_at: datetime | None = None


class AIConsentUpdate(BaseModel):
    ai_processing_consent: bool
    ai_trace_consent: bool


class RegistrationOut(BaseModel):
    status: str
    message: str


class CompanyOut(BaseModel):
    id: str
    name: str
    is_active: bool
    agent_ids: list[str]
    created_at: datetime


class CompanyCreatedOut(CompanyOut):
    invitation_code: str


class LoginRequest(BaseModel):
    email: PortalEmail
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class TicketCreate(BaseModel):
    title: str = Field(min_length=5, max_length=180)
    description: str = Field(min_length=20, max_length=10000)
    product: str = Field(default="OXE", min_length=2, max_length=100)
    version: str = Field(default="", max_length=80)
    ticket_type: str = Field(default="incident", pattern="^(incident|problem|request)$")
    priority: str = Field(default="medium", pattern="^(low|medium|high|critical|p1|p2|p3|p4)$")


class MessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    visibility: str = Field(default="public", pattern="^(public|internal)$")


class StatusUpdate(BaseModel):
    status: str = Field(pattern="^(new|in_progress|waiting_support|waiting_customer|escalated|resolved|rejected)$")
    resolution_note: str = Field(default="", max_length=5000)
    rejection_reason: str = Field(default="", max_length=5000)


class EscalationCreate(BaseModel):
    team: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=5, max_length=5000)
    external_reference: str = Field(default="", max_length=120)


class AssignmentUpdate(BaseModel):
    agent_id: str | None


class MessageOut(BaseModel):
    id: str
    body: str
    visibility: str
    author_id: str
    author_name: str
    author_type: str
    created_at: datetime


class HandoffCreate(BaseModel):
    reason: str = Field(default="Le diagnostic IA n’a pas résolu le problème.", max_length=1000)


class BackgroundAccepted(BaseModel):
    status: str


class EventOut(BaseModel):
    id: int
    event_type: str
    details: dict
    actor_name: str
    created_at: datetime


class AttachmentOut(BaseModel):
    id: str
    file_name: str
    content_type: str
    size: int
    uploader_name: str
    created_at: datetime
    download_url: str


class TicketOut(BaseModel):
    id: str
    reference: str
    customer_id: str
    customer_name: str
    assigned_agent_id: str | None
    assigned_agent_name: str | None
    title: str
    description: str
    product: str
    version: str
    ticket_type: str = "incident"
    priority: str
    sla_policy_id: str | None = None
    workaround_due_at: datetime | None = None
    resolution_due_at: datetime | None = None
    workaround_at: datetime | None = None
    sla_support_units_per_hour: float = 0
    customer_penalty_units: float = 0
    customer_penalty_reason: str = ""
    status: str
    ai_status: str
    ai_provider: str
    ai_summary: str
    ai_recommendations: list[str]
    ai_escalation_recommended: bool
    ai_error: str
    resolution_note: str
    rejection_reason: str
    escalation_team: str
    escalation_reason: str
    external_reference: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]
    events: list[EventOut]
    attachments: list[AttachmentOut]


class AgentAIAssist(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
