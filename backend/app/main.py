from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import logging
import threading
from datetime import datetime, timezone

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.ai_routing import (
    AI_ROLES,
    AI_TASKS,
    AIRoutingUpdate,
    get_routing_config,
    provider_catalog,
    save_routing_config,
)
from app.ai_observability import router as ai_observability_router
from app.ai_observability import purge_expired_trace_content
from app.auth import require_roles, router as auth_router
from app.config import settings
from app.database import Base, SessionLocal, engine, get_db
from app.models import Company, Ticket, User, company_agents
from app.rag import RAGError, evaluate_query, inspect_index, sync_documents
from app.security import hash_password
from app.tickets import router as tickets_router
from app.users import router as users_router
from app.service_management import router as service_management_router

logger = logging.getLogger(__name__)
_RAG_DOCUMENT_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}
_rag_index_lock = threading.Lock()
_rag_index_job = {
    "status": "idle",
    "started_at": None,
    "completed_at": None,
    "result": None,
    "error": "",
    "progress": {
        "phase": "idle",
        "processed": 0,
        "total": 0,
        "current": "",
        "message": "",
    },
}


def _ensure_user_workflow_columns() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    additions = {
        "company_id": "VARCHAR(36)",
        "account_status": "VARCHAR(20) NOT NULL DEFAULT 'approved'",
        "approved_by_id": "VARCHAR(36)",
        "approved_at": "TIMESTAMP WITH TIME ZONE",
        "token_version": "INTEGER NOT NULL DEFAULT 0",
    }
    with engine.begin() as connection:
        for name, column_type in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {column_type}"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_company_id ON users (company_id)"))
        connection.execute(text(
            "UPDATE users SET account_status = 'pending' "
            "WHERE role = 'customer' AND company_id IS NULL AND account_status = 'approved'"
        ))


def _ensure_ai_consent_columns() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    with engine.begin() as connection:
        for name in ("ai_processing_consent", "ai_trace_consent"):
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {name} BOOLEAN NOT NULL DEFAULT TRUE")
                )
        if "ai_consent_updated_at" not in columns:
            connection.execute(
                text("ALTER TABLE users ADD COLUMN ai_consent_updated_at TIMESTAMP WITH TIME ZONE")
            )
        if "token_version" not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"))


def _ensure_ticket_lifecycle_columns() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("tickets")}
    with engine.begin() as connection:
        for name in ("resolution_note", "rejection_reason"):
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE tickets ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
                )
        connection.execute(text(
            "UPDATE tickets SET resolution_note = ai_summary "
            "WHERE status = 'resolved' AND resolution_note = '' AND trim(ai_summary) != ''"
        ))


def _ensure_service_management_columns() -> None:
    company_columns = {column["name"] for column in inspect(engine).get_columns("companies")}
    company_additions = {
        "billing_mode": "VARCHAR(20) NOT NULL DEFAULT 'per_ticket'",
        "payment_mode": "VARCHAR(20) NOT NULL DEFAULT 'postpaid'",
        "payment_method": "VARCHAR(80) NOT NULL DEFAULT ''",
        "billing_currency": "VARCHAR(3) NOT NULL DEFAULT 'EUR'",
        "monthly_fee_cents": "INTEGER NOT NULL DEFAULT 0",
        "price_per_ticket_cents": "INTEGER NOT NULL DEFAULT 0",
        "tax_rate_basis_points": "INTEGER NOT NULL DEFAULT 0",
        "payment_terms_days": "INTEGER NOT NULL DEFAULT 30",
        "prepaid_ticket_credits": "INTEGER NOT NULL DEFAULT 0",
    }
    ticket_columns = {column["name"] for column in inspect(engine).get_columns("tickets")}
    ticket_additions = {
        "ticket_type": "VARCHAR(20) NOT NULL DEFAULT 'incident'",
        "sla_policy_id": "VARCHAR(36)",
        "workaround_due_at": "TIMESTAMP WITH TIME ZONE",
        "resolution_due_at": "TIMESTAMP WITH TIME ZONE",
        "workaround_at": "TIMESTAMP WITH TIME ZONE",
        "sla_support_units_per_hour": "FLOAT NOT NULL DEFAULT 0",
        "sla_customer_penalty_units": "FLOAT NOT NULL DEFAULT 0",
        "customer_penalty_units": "FLOAT NOT NULL DEFAULT 0",
        "customer_penalty_reason": "TEXT NOT NULL DEFAULT ''",
        "customer_penalty_at": "TIMESTAMP WITH TIME ZONE",
    }
    with engine.begin() as connection:
        for name, column_type in company_additions.items():
            if name not in company_columns:
                connection.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {column_type}"))
        for name, column_type in ticket_additions.items():
            if name not in ticket_columns:
                connection.execute(text(f"ALTER TABLE tickets ADD COLUMN {name} {column_type}"))


async def _purge_expired_ai_traces() -> None:
    while True:
        try:
            await asyncio.to_thread(purge_expired_trace_content)
        except Exception:
            logger.exception("Unable to purge expired AI trace content")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_user_workflow_columns()
    _ensure_ai_consent_columns()
    _ensure_ticket_lifecycle_columns()
    _ensure_service_management_columns()
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        email = settings.initial_admin_email.lower()
        if not db.scalar(select(User).where(User.email == email)):
            db.add(User(
                email=email,
                full_name="Administrateur",
                role="admin",
                password_hash=hash_password(settings.initial_admin_password),
            ))
            db.commit()
    purge_task = asyncio.create_task(_purge_expired_ai_traces())
    try:
        yield
    finally:
        purge_task.cancel()
        try:
            await purge_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Alcatel OXE Support API",
    description="Portail de suivi d'incidents et assistance augmentée pour les solutions Alcatel-Lucent.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(tickets_router)
app.include_router(users_router)
app.include_router(ai_observability_router)
app.include_router(service_management_router)


class RAGQuery(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


@app.get("/api/admin/ai-routing", tags=["ai"])
def get_ai_routing(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    return {
        **get_routing_config(db),
        "tasks": AI_TASKS,
        "roles": AI_ROLES,
        "providers": provider_catalog(),
    }


@app.put("/api/admin/ai-routing", tags=["ai"])
def update_ai_routing(
    payload: AIRoutingUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    try:
        return save_routing_config(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _run_rag_indexing() -> None:
    with _rag_index_lock:
        _rag_index_job.update({
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "result": None,
            "error": "",
            "progress": {
                "phase": "starting",
                "processed": 0,
                "total": 0,
                "current": "",
                "message": "Préparation de l’indexation…",
            },
        })

    def update_progress(progress: dict[str, str | int]) -> None:
        with _rag_index_lock:
            _rag_index_job["progress"] = dict(progress)

    try:
        result = sync_documents(progress_callback=update_progress)
        with _rag_index_lock:
            _rag_index_job.update({
                "status": "complete",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
                "progress": {
                    "phase": "complete",
                    "processed": result["indexed_sources"] + result["deleted_sources"],
                    "total": result["indexed_sources"] + result["deleted_sources"],
                    "current": "",
                    "message": "Synchronisation terminée.",
                },
            })
    except RAGError as exc:
        logger.warning("Manual RAG indexing failed: %s", exc)
        with _rag_index_lock:
            _rag_index_job.update({
                "status": "error",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
                "progress": {
                    **_rag_index_job["progress"],
                    "phase": "error",
                    "message": "La synchronisation a échoué.",
                },
            })
    except Exception:
        logger.exception("Unexpected manual RAG indexing failure")
        with _rag_index_lock:
            _rag_index_job.update({
                "status": "error",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": "Erreur inattendue pendant l’indexation; consulter les journaux API.",
                "progress": {
                    **_rag_index_job["progress"],
                    "phase": "error",
                    "message": "La synchronisation a échoué.",
                },
            })


@app.get("/api/admin/rag", tags=["rag"])
def rag_admin_status(_admin: User = Depends(require_roles("admin"))):
    try:
        overview = inspect_index()
    except RAGError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    overview["limits"] = {
        "max_file_size": settings.max_attachment_size,
        "max_files": settings.max_attachments_per_upload,
    }
    with _rag_index_lock:
        overview["indexing"] = dict(_rag_index_job)
    return overview


@app.get("/api/admin/rag/index", tags=["rag"])
def rag_indexing_status(_admin: User = Depends(require_roles("admin"))):
    with _rag_index_lock:
        return dict(_rag_index_job)


@app.post("/api/admin/rag/search", tags=["rag"])
def search_rag_index(payload: RAGQuery, _admin: User = Depends(require_roles("admin"))):
    try:
        return evaluate_query(payload.query)
    except RAGError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/admin/rag/documents", tags=["rag"])
def upload_rag_documents(
    files: list[UploadFile] = File(...),
    _admin: User = Depends(require_roles("admin")),
):
    if not files:
        raise HTTPException(status_code=422, detail="Sélectionnez au moins un document.")
    if len(files) > settings.max_attachments_per_upload:
        raise HTTPException(
            status_code=413,
            detail=f"Vous pouvez ajouter au maximum {settings.max_attachments_per_upload} documents par envoi.",
        )

    root = Path(settings.rag_documents_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing_names = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    uploads = []
    requested_names = set()
    for upload in files:
        filename = Path(upload.filename or "").name
        extension = Path(filename).suffix.lower()
        if not filename or extension not in _RAG_DOCUMENT_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail="Formats acceptés : PDF, Markdown (.md/.markdown) et texte (.txt).",
            )
        if filename in existing_names or filename in requested_names:
            raise HTTPException(status_code=409, detail=f"Un document nommé {filename} existe déjà.")
        content = upload.file.read(settings.max_attachment_size + 1)
        if len(content) > settings.max_attachment_size:
            raise HTTPException(
                status_code=413,
                detail=f"Le document {filename} dépasse la limite de {settings.max_attachment_size} octets.",
            )
        requested_names.add(filename)
        uploads.append((filename, content))

    created_paths = []
    try:
        for filename, content in uploads:
            destination = (root / filename).resolve()
            if destination.parent != root:
                raise HTTPException(status_code=400, detail="Nom de document invalide.")
            try:
                with destination.open("xb") as destination_file:
                    destination_file.write(content)
            except FileExistsError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Un document nommé {filename} existe déjà.",
                ) from exc
            created_paths.append(destination)
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return {"uploaded": [filename for filename, _ in uploads], "indexing_required": True}


@app.post("/api/admin/rag/index", tags=["rag"], status_code=202)
def start_rag_indexing(
    background_tasks: BackgroundTasks,
    _admin: User = Depends(require_roles("admin")),
):
    with _rag_index_lock:
        if _rag_index_job["status"] in {"pending", "running"}:
            raise HTTPException(status_code=409, detail="Une indexation RAG est déjà en cours.")
        _rag_index_job.update({
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": "",
            "progress": {
                "phase": "pending",
                "processed": 0,
                "total": 0,
                "current": "",
                "message": "Indexation en attente…",
            },
        })
    background_tasks.add_task(_run_rag_indexing)
    return {"status": "pending"}


@app.get("/api/system/overview", tags=["system"])
def system_overview(db: Session = Depends(get_db), _admin: User = Depends(require_roles("admin"))):
    def _service(name: str, status: str, detail: str):
        return {"name": name, "status": status, "detail": detail}

    try:
        db.execute(text("SELECT 1"))
        database_status = "healthy"
        database_detail = "Base de données PostgreSQL / SQLite accessible."
    except Exception as exc:  # pragma: no cover - defensive branch
        database_status = "down"
        database_detail = f"Base de données inaccessible : {exc}"

    api_status = "healthy"
    api_detail = "API FastAPI active et répondante."

    routing = get_routing_config(db)
    active_providers = {
        model["provider"]
        for role_routes in routing["routes"].values()
        for route in role_routes.values()
        if routing["enabled"] and route["enabled"]
        for model in route["models"]
    }

    ollama_status = "disabled"
    ollama_detail = "Ollama est utilisé pour les embeddings documentaires RAG."
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=3.0)
        response.raise_for_status()
        models = response.json().get("models") or []
        embedding_model_present = any(
            model.get("name", "").split(":")[0] == settings.ollama_embedding_model.split(":")[0]
            for model in models
        )
        if not models:
            ollama_status = "warning"
            ollama_detail = "Ollama répond mais aucun modèle n’est encore disponible."
        elif not embedding_model_present:
            ollama_status = "warning"
            ollama_detail = (
                f"Ollama est disponible, mais l’embedding RAG {settings.ollama_embedding_model} "
                "n’est pas téléchargé."
            )
        else:
            ollama_status = "healthy"
            generation_note = " et le routage IA" if "ollama" in active_providers else ""
            ollama_detail = (
                f"Ollama disponible avec {len(models)} modèle(s); embeddings RAG{generation_note}."
            )
    except Exception as exc:  # pragma: no cover - defensive branch
        ollama_status = "down"
        ollama_detail = f"Ollama indisponible : {exc}"

    if "anthropic" in active_providers:
        anthropic_status = "warning" if settings.anthropic_api_key else "down"
        anthropic_detail = (
            "Clé configurée; la connectivité sera confirmée lors d’un appel."
            if settings.anthropic_api_key
            else "Une règle utilise Anthropic mais ANTHROPIC_API_KEY n’est pas configurée."
        )
    else:
        anthropic_status = "disabled"
        anthropic_detail = "Anthropic n’est pas affecté à une règle IA active."

    try:
        qdrant_response = httpx.get(f"{settings.rag_qdrant_url.rstrip('/')}/healthz", timeout=3.0)
        qdrant_response.raise_for_status()
        embedding_response = httpx.get(
            f"{settings.ollama_base_url.rstrip('/')}/api/tags",
            timeout=3.0,
        )
        embedding_response.raise_for_status()
        models = embedding_response.json().get("models") or []
        embedding_model_present = any(
            model.get("name", "").split(":")[0] == settings.ollama_embedding_model.split(":")[0]
            for model in models
        )
        if embedding_model_present:
            rag_status = "healthy"
            rag_detail = "Qdrant et le modèle d'embedding Ollama sont disponibles."
        else:
            rag_status = "warning"
            rag_detail = (
                f"Qdrant est disponible, mais le modèle d'embedding "
                f"{settings.ollama_embedding_model} n'est pas encore téléchargé."
            )
    except Exception as exc:  # pragma: no cover - defensive branch
        rag_status = "down"
        rag_detail = f"Service RAG indisponible : {exc}"

    open_tickets = db.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.status.notin_(["resolved", "closed", "rejected"]))
    ) or 0
    escalated_tickets = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "escalated")) or 0
    staff_count = db.scalar(select(func.count()).select_from(User).where(User.role.in_(["agent", "admin"]))) or 0
    customer_count = db.scalar(select(func.count()).select_from(User).where(User.role == "customer")) or 0
    queue_status = "healthy"
    queue_detail = f"{open_tickets} incident(s) ouvert(s) sans blocage majeur."
    if open_tickets > 40:
        queue_status = "warning"
        queue_detail = f"{open_tickets} incidents ouverts : priorité à la répartition du traitement."
    if open_tickets > 80:
        queue_status = "down"
        queue_detail = f"{open_tickets} incidents ouverts : saturation probable de la file de support."

    services = [
        _service("database", database_status, database_detail),
        _service("api", api_status, api_detail),
        _service("ollama", ollama_status, ollama_detail),
        _service("anthropic", anthropic_status, anthropic_detail),
        _service("rag", rag_status, rag_detail),
        _service("queue", queue_status, queue_detail),
    ]
    overall_status = "ok" if all(service["status"] in {"healthy", "warning", "disabled"} for service in services) else "degraded"
    return {
        "status": overall_status,
        "services": services,
        "metrics": {
            "open_tickets": open_tickets,
            "escalated_tickets": escalated_tickets,
            "staff_count": staff_count,
            "customer_count": customer_count,
        },
    }


@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok"}
