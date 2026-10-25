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
    reset_routing_config,
    save_routing_config,
    user_ai_options,
)
from app.ai_observability import router as ai_observability_router
from app.kb import flags as kb_flags
from app.company_docs import router as company_docs_router, start_background_indexing as start_company_docs_indexing
from app.help_console import recover_interrupted_answers, router as help_router
from app.kb.archive import restore_on_startup as restore_kb_archive
from app.kb.jobs import enqueue_reindex, get_queue, recover_interrupted_jobs
from app.kb.router import router as kb_router, supervision_entry as kb_supervision_entry
from app.ai_observability import purge_expired_trace_content
from app.auth import get_current_user, require_roles, router as auth_router
from app.config import settings
from app.database import Base, SessionLocal, engine, get_db
from app.models import AIExecution, Company, RAGConfig, Ticket, TicketEvent, User, company_agents
from app.rag import (
    EMBEDDING_MODELS,
    RAGError,
    delete_sources,
    embedding_model,
    embedding_status,
    evaluate_query,
    inspect_index,
    installed_ollama_models,
    model_installed,
    qdrant_status,
    set_embedding_model,
    sync_documents,
)
from app.security import hash_password
from app.tickets import recover_stuck_ai_tickets, router as tickets_router
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
        if "ai_preferences" not in columns:
            connection.execute(text("ALTER TABLE tickets ADD COLUMN ai_preferences JSON"))
        connection.execute(text(
            "UPDATE tickets SET resolution_note = ai_summary "
            "WHERE status = 'resolved' AND resolution_note = '' AND trim(ai_summary) != ''"
        ))


def _ensure_ai_execution_nullable_ticket() -> None:
    """La console d'aide mesure des appels IA sans ticket : ai_executions.ticket_id devient facultatif."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE ai_executions ALTER COLUMN ticket_id DROP NOT NULL"))


def _ensure_individual_company_column() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("companies")}
    if "is_individual" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE companies ADD COLUMN is_individual BOOLEAN NOT NULL DEFAULT FALSE"))


def _ensure_help_columns() -> None:
    inspector = inspect(engine)
    additions = {
        "help_messages": {"thinking": "TEXT NOT NULL DEFAULT ''", "completed_at": "TIMESTAMP WITH TIME ZONE"},
        "help_files": {"origin": "VARCHAR(10) NOT NULL DEFAULT 'upload'"},
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def _ensure_kb_columns() -> None:
    """Colonnes ajoutées aux tables KB après leur création (déploiements existants)."""
    inspector = inspect(engine)
    additions = {
        "kb_articles": {"source": "VARCHAR(20) NOT NULL DEFAULT 'kb'"},
        "kb_jobs": {"results": "JSON"},
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, column_type in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"))


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
    _ensure_ai_execution_nullable_ticket()
    _ensure_help_columns()
    _ensure_individual_company_column()
    _ensure_kb_columns()
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
        rag_config = db.get(RAGConfig, 1)
        if rag_config and rag_config.embedding_model in EMBEDDING_MODELS:
            set_embedding_model(rag_config.embedding_model)
    recover_interrupted_jobs(SessionLocal)
    restore_kb_archive(SessionLocal)
    start_company_docs_indexing()
    recover_interrupted_answers()
    recover_stuck_ai_tickets()
    purge_task = asyncio.create_task(_purge_expired_ai_traces())
    try:
        yield
    finally:
        get_queue().stop()
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
app.include_router(kb_router)
app.include_router(company_docs_router)
app.include_router(help_router)
app.include_router(service_management_router)


class RAGQuery(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class RAGEmbeddingUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class RAGDocumentsDelete(BaseModel):
    names: list[str] = Field(min_length=1, max_length=200)


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


@app.delete("/api/admin/ai-routing", tags=["ai"])
def reset_ai_routing(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    """Rétablit la configuration par défaut issue de l'environnement (modèle, think, délai)."""
    return {
        **reset_routing_config(db),
        "tasks": AI_TASKS,
        "roles": AI_ROLES,
        "providers": provider_catalog(),
    }


@app.get("/api/ai/options", tags=["ai"])
def get_ai_options(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Modèles que l'utilisateur courant peut choisir, d'après les affectations de l'administrateur."""
    options = user_ai_options(db, user.role)
    if user.role != "customer":
        options["kb_enabled"] = kb_flags.is_enabled()
    return options


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
        "max_file_size": settings.rag_max_file_size,
        "max_files": settings.rag_max_files_per_upload,
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
    if len(files) > settings.rag_max_files_per_upload:
        raise HTTPException(
            status_code=413,
            detail=f"Vous pouvez ajouter au maximum {settings.rag_max_files_per_upload} documents par envoi.",
        )

    root = Path(settings.rag_documents_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing_names = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    uploads: list[tuple[str, UploadFile]] = []
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
        requested_names.add(filename)
        uploads.append((filename, upload))

    # Écriture par blocs : évite de charger jusqu'à 50 fichiers de 50 Mo en mémoire.
    created_paths = []
    try:
        for filename, upload in uploads:
            destination = (root / filename).resolve()
            if destination.parent != root:
                raise HTTPException(status_code=400, detail="Nom de document invalide.")
            try:
                with destination.open("xb") as destination_file:
                    created_paths.append(destination)
                    written = 0
                    while chunk := upload.file.read(1024 * 1024):
                        written += len(chunk)
                        if written > settings.rag_max_file_size:
                            raise HTTPException(
                                status_code=413,
                                detail=f"Le document {filename} dépasse la limite de {settings.rag_max_file_size} octets.",
                            )
                        destination_file.write(chunk)
            except FileExistsError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Un document nommé {filename} existe déjà.",
                ) from exc
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return {"uploaded": [filename for filename, _ in uploads], "indexing_required": True}


def _ensure_no_rag_indexing() -> None:
    with _rag_index_lock:
        if _rag_index_job["status"] in {"pending", "running"}:
            raise HTTPException(status_code=409, detail="Une indexation RAG est déjà en cours.")


def _mark_rag_indexing_pending() -> None:
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


@app.post("/api/admin/rag/index", tags=["rag"], status_code=202)
def start_rag_indexing(
    background_tasks: BackgroundTasks,
    _admin: User = Depends(require_roles("admin")),
):
    _mark_rag_indexing_pending()
    background_tasks.add_task(_run_rag_indexing)
    return {"status": "pending"}


@app.put("/api/admin/rag/embedding-model", tags=["rag"], status_code=202)
def update_rag_embedding_model(
    payload: RAGEmbeddingUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
):
    """Change le modèle d'embedding et réindexe le corpus dans la collection de ce modèle."""
    model = payload.model.strip()
    if model not in EMBEDDING_MODELS:
        raise HTTPException(status_code=422, detail="Modèle d'embedding non pris en charge.")
    if model == embedding_model():
        return {"embedding_model": model, "indexing": "unchanged"}
    try:
        installed = installed_ollama_models()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail=f"Ollama est indisponible : {exc}") from exc
    if not model_installed(model, installed):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Le modèle {model} n'est pas téléchargé dans Ollama. "
                f"Téléchargez-le (docker compose exec ollama-embed ollama pull {model}) puis réessayez."
            ),
        )
    _mark_rag_indexing_pending()
    record = db.get(RAGConfig, 1)
    if record:
        record.embedding_model = model
    else:
        db.add(RAGConfig(id=1, embedding_model=model))
    db.commit()
    set_embedding_model(model)
    background_tasks.add_task(_run_rag_indexing)
    background_tasks.add_task(enqueue_reindex)  # articles KB déjà récupérés, sans re-scraper
    background_tasks.add_task(start_company_docs_indexing)  # documents privés des entreprises
    return {"embedding_model": model, "indexing": "pending"}


@app.delete("/api/admin/rag/documents", tags=["rag"])
def delete_rag_documents(
    payload: RAGDocumentsDelete,
    _admin: User = Depends(require_roles("admin")),
):
    """Supprime des documents du corpus (fichier source et vecteurs de la collection active)."""
    _ensure_no_rag_indexing()
    root = Path(settings.rag_documents_dir).resolve()
    targets: list[tuple[str, Path]] = []
    for name in dict.fromkeys(payload.names):
        path = (root / name).resolve()
        if (
            root not in path.parents
            or not path.is_file()
            or path.suffix.lower() not in _RAG_DOCUMENT_EXTENSIONS
        ):
            raise HTTPException(status_code=404, detail=f"Document introuvable : {name}")
        targets.append((path.relative_to(root).as_posix(), path))

    for _source, path in targets:
        path.unlink()
    sources = [source for source, _path in targets]
    try:
        delete_sources(sources)
        index_updated = True
    except RAGError as exc:
        # Les fichiers sont supprimés : la prochaine synchronisation retirera les vecteurs restants.
        logger.warning("RAG documents deleted but index cleanup failed: %s", exc)
        index_updated = False
    return {"deleted": sources, "index_updated": index_updated}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _anthropic_health(db: Session) -> tuple[str, str]:
    """État d'Anthropic déduit des appels réels (aucun appel facturable n'est émis par la supervision)."""
    last_ok = db.scalar(
        select(AIExecution).where(AIExecution.provider == "anthropic").order_by(AIExecution.created_at.desc()).limit(1)
    )
    failure = None
    for event in db.scalars(
        select(TicketEvent).where(TicketEvent.event_type == "ai_provider_failed").order_by(TicketEvent.created_at.desc()).limit(50)
    ):
        if (event.details or {}).get("provider") == "anthropic":
            failure = event
            break
    if failure and (not last_ok or _as_utc(failure.created_at) > _as_utc(last_ok.created_at)):
        error = str((failure.details or {}).get("error", ""))[:200]
        return "warning", f"Le dernier appel Anthropic a échoué le {_as_utc(failure.created_at):%d/%m/%Y à %H:%M} UTC : {error}"
    if last_ok:
        return "healthy", (
            f"Dernier appel réussi le {_as_utc(last_ok.created_at):%d/%m/%Y à %H:%M} UTC "
            f"(modèle {last_ok.model}, {last_ok.duration_ms / 1000:.1f} s)."
        )
    return "warning", "Clé configurée; aucun appel Anthropic effectué pour l’instant (la connectivité sera confirmée au premier appel)."


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

    # Instance Ollama de génération (chat); les embeddings RAG ont leur propre instance.
    ollama_status = "disabled"
    ollama_detail = "Ollama n’est pas affecté à une règle IA active."
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=3.0)
        response.raise_for_status()
        models = response.json().get("models") or []
        if not models:
            ollama_status = "warning"
            ollama_detail = "Ollama répond mais aucun modèle n’est encore disponible."
        else:
            ollama_status = "healthy"
            usage_note = "utilisé par le routage IA" if "ollama" in active_providers else "non affecté au routage IA"
            ollama_detail = f"Ollama (génération) disponible avec {len(models)} modèle(s); {usage_note}."
    except Exception as exc:  # pragma: no cover - defensive branch
        ollama_status = "down"
        ollama_detail = f"Ollama indisponible : {exc}"

    if "anthropic" in active_providers:
        if settings.anthropic_api_key:
            anthropic_status, anthropic_detail = _anthropic_health(db)
        else:
            anthropic_status = "down"
            anthropic_detail = "Une règle utilise Anthropic mais ANTHROPIC_API_KEY n’est pas configurée."
    else:
        anthropic_status = "disabled"
        anthropic_detail = "Anthropic n’est pas affecté à une règle IA active."

    # Instance Ollama d'embeddings : service distinct de l'Ollama de génération.
    embedding = embedding_status()
    embedding_service_status = embedding["status"]
    if embedding["status"] == "healthy":
        scope = "instance dédiée" if embedding["dedicated_instance"] else "instance partagée avec la génération"
        embedding_service_detail = (
            f"Ollama (embeddings) disponible, {scope}; modèle actif {embedding['model']} "
            f"({embedding['dimension']} dimensions)."
        )
    elif embedding["status"] == "warning":
        embedding_service_detail = (
            f"Instance d'embeddings joignable, mais le modèle actif {embedding['model']} "
            "n'est pas téléchargé."
        )
    else:
        embedding_service_detail = f"Ollama (embeddings) indisponible : {embedding['error']}"

    qdrant = qdrant_status()
    if qdrant["status"] == "healthy":
        rag_status = "healthy"
        rag_detail = (
            f"Qdrant disponible; collection {qdrant['collection']} "
            + ("présente." if qdrant["collection_exists"] else "à créer (lancer l'indexation du corpus).")
        )
    else:
        rag_status = "down"
        rag_detail = f"Qdrant indisponible : {qdrant['error']}"

    open_tickets = db.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.status.notin_(["resolved", "closed", "rejected"]))
    ) or 0
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

    kb_service = kb_supervision_entry(db)
    services = [
        _service("database", database_status, database_detail),
        _service("api", api_status, api_detail),
        _service("ollama", ollama_status, ollama_detail),
        _service("embedding", embedding_service_status, embedding_service_detail),
        _service("anthropic", anthropic_status, anthropic_detail),
        _service("rag", rag_status, rag_detail),
        _service("queue", queue_status, queue_detail),
        *([kb_service] if kb_service else []),
    ]
    overall_status = "ok" if all(service["status"] in {"healthy", "warning", "disabled"} for service in services) else "degraded"
    return {
        "status": overall_status,
        "services": services,
        "metrics": {
            "open_tickets": open_tickets,
            "staff_count": staff_count,
            "customer_count": customer_count,
        },
    }


@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok"}
