from datetime import datetime, timezone
from typing import Literal

import httpx
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AIRoutingConfig

AI_TASKS = {
    "customer_diagnosis": "Diagnostic conversationnel après le premier échange",
    "agent_assistance": "Assistance privée aux agents",
    "handoff_summary": "Résumé interne lors d’un transfert client",
    "ticket_analysis": "Analyse initiale et premier diagnostic du ticket",
}
AI_ROLES = {"customer": "Client", "staff": "Équipe support"}


class ModelChoice(BaseModel):
    provider: Literal["ollama", "anthropic"]
    model: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def trim_model_name(self):
        self.model = self.model.strip()
        if not self.model:
            raise ValueError("Le nom du modèle ne peut pas être vide.")
        return self


class RoleRoute(BaseModel):
    enabled: bool
    models: list[ModelChoice] = Field(max_length=4)

    @model_validator(mode="after")
    def validate_enabled_route(self):
        if self.enabled and not self.models:
            raise ValueError("Une règle active doit contenir au moins un modèle.")
        choices = [(choice.provider, choice.model) for choice in self.models]
        if len(choices) != len(set(choices)):
            raise ValueError("Un même modèle ne peut pas apparaître deux fois dans une priorité.")
        return self


class AIRoutingUpdate(BaseModel):
    enabled: bool
    routes: dict[str, dict[str, RoleRoute]]

    @model_validator(mode="after")
    def validate_routing(self):
        if set(self.routes) != set(AI_TASKS):
            raise ValueError("La configuration doit contenir exactement les quatre tâches IA.")
        for task, role_routes in self.routes.items():
            if set(role_routes) != set(AI_ROLES):
                raise ValueError(f"La tâche {task} doit définir les rôles client et équipe support.")
        if not any(
            route.enabled and route.models
            for role_routes in self.routes.values()
            for route in role_routes.values()
        ):
            raise ValueError("Au moins une règle et un modèle doivent rester affectés.")
        return self


def _default_model_choice() -> dict[str, str]:
    provider = settings.ai_provider.lower()
    if provider == "anthropic" and settings.anthropic_api_key:
        return {"provider": "anthropic", "model": settings.anthropic_model}
    return {"provider": "ollama", "model": settings.ollama_model}


def default_routes() -> dict[str, dict[str, dict]]:
    choice = _default_model_choice()
    return {
        task: {
            role: {"enabled": True, "models": [dict(choice)]}
            for role in AI_ROLES
        }
        for task in AI_TASKS
    }


def get_routing_config(db: Session) -> dict:
    record = db.get(AIRoutingConfig, 1)
    if not record:
        return {"enabled": True, "routes": default_routes()}
    return {"enabled": record.enabled, "routes": record.routes}


def save_routing_config(db: Session, payload: AIRoutingUpdate) -> dict:
    config = payload.model_dump()
    uses_anthropic = config["enabled"] and any(
        route["enabled"] and any(model["provider"] == "anthropic" for model in route["models"])
        for role_routes in config["routes"].values()
        for route in role_routes.values()
    )
    if uses_anthropic and not settings.anthropic_api_key:
        raise ValueError("Anthropic ne peut pas être affecté sans clé ANTHROPIC_API_KEY configurée.")
    record = db.get(AIRoutingConfig, 1)
    if record:
        record.enabled = config["enabled"]
        record.routes = config["routes"]
        record.updated_at = datetime.now(timezone.utc)
    else:
        record = AIRoutingConfig(id=1, enabled=config["enabled"], routes=config["routes"])
        db.add(record)
    db.commit()
    db.refresh(record)
    return {"enabled": record.enabled, "routes": record.routes}


def routing_for(task: str, role: str) -> tuple[bool, list[dict[str, str]]]:
    from app.database import SessionLocal

    with SessionLocal() as db:
        config = get_routing_config(db)
    route = config["routes"].get(task, {}).get(role)
    if not config["enabled"] or not route or not route["enabled"]:
        return False, []
    return True, list(route["models"])


def provider_catalog() -> list[dict]:
    ollama_models: list[str] = []
    ollama_error = ""
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=3)
        response.raise_for_status()
        ollama_models = [
            model["name"] for model in response.json().get("models", [])
            if isinstance(model.get("name"), str)
        ]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        ollama_error = str(exc)

    anthropic_configured = bool(settings.anthropic_api_key)
    return [
        {
            "id": "ollama",
            "label": "Ollama",
            "configured": bool(settings.ollama_base_url),
            "status": "healthy" if ollama_models else ("warning" if not ollama_error else "down"),
            "default_model": settings.ollama_model,
            "available_models": ollama_models,
            "error": ollama_error,
        },
        {
            "id": "anthropic",
            "label": "Anthropic / Claude",
            "configured": anthropic_configured,
            "status": "configured" if anthropic_configured else "unconfigured",
            "default_model": settings.anthropic_model,
            "available_models": [settings.anthropic_model] if anthropic_configured else [],
            "error": "" if anthropic_configured else "La clé ANTHROPIC_API_KEY n’est pas configurée.",
        },
    ]
