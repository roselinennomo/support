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


DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 1800


class ModelChoice(BaseModel):
    provider: Literal["ollama", "anthropic"]
    model: str = Field(min_length=1, max_length=200)
    # Raisonnement interne des modèles « thinking » (Ollama uniquement) : désactivé par défaut car il
    # multiplie le temps de génération sur CPU sans améliorer sensiblement le diagnostic.
    think: bool = False
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=10, le=MAX_TIMEOUT_SECONDS)

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


def _default_model_choice() -> dict:
    provider = settings.ai_provider.lower()
    if provider == "anthropic" and settings.anthropic_api_key:
        return {
            "provider": "anthropic",
            "model": settings.anthropic_model,
            "think": False,
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        }
    return {
        "provider": "ollama",
        "model": settings.ollama_model,
        "think": settings.ollama_think,
        "timeout_seconds": min(max(settings.ollama_timeout_seconds, 10), MAX_TIMEOUT_SECONDS),
    }


def normalize_choice(choice: dict) -> dict:
    """Complète les affectations enregistrées avant l'ajout des options think/timeout."""
    return {
        **choice,
        "think": bool(choice.get("think", False)),
        "timeout_seconds": int(choice.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
    }


def _normalized_routes(routes: dict) -> dict:
    return {
        task: {
            role: {**route, "models": [normalize_choice(choice) for choice in route.get("models", [])]}
            for role, route in role_routes.items()
        }
        for task, role_routes in routes.items()
    }


def _default_model_choices() -> list[dict]:
    """Modèle principal, puis les modèles Ollama supplémentaires (sans raisonnement par défaut)."""
    primary = _default_model_choice()
    choices = [primary]
    if primary["provider"] == "ollama":
        for model in settings.ollama_extra_models.split():
            if model != primary["model"] and len(choices) < 4:
                choices.append({
                    "provider": "ollama",
                    "model": model,
                    "think": False,
                    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                })
    return choices


def default_routes() -> dict[str, dict[str, dict]]:
    return {
        task: {
            role: {"enabled": True, "models": [dict(choice) for choice in _default_model_choices()]}
            for role in AI_ROLES
        }
        for task in AI_TASKS
    }


def get_routing_config(db: Session) -> dict:
    record = db.get(AIRoutingConfig, 1)
    if not record:
        return {"enabled": True, "routes": default_routes()}
    return {"enabled": record.enabled, "routes": _normalized_routes(record.routes)}


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
    return {"enabled": record.enabled, "routes": _normalized_routes(record.routes)}


def reset_routing_config(db: Session) -> dict:
    """Supprime la configuration enregistrée : les valeurs par défaut de l'environnement s'appliquent."""
    record = db.get(AIRoutingConfig, 1)
    if record:
        db.delete(record)
        db.commit()
    return get_routing_config(db)


class ModelSelection(BaseModel):
    """Modèle choisi par un utilisateur parmi ceux que l'administrateur a affectés à son rôle."""

    provider: Literal["ollama", "anthropic"]
    model: str = Field(min_length=1, max_length=200)
    think: bool | None = None

    @model_validator(mode="after")
    def trim_model_name(self):
        self.model = self.model.strip()
        if not self.model:
            raise ValueError("Le nom du modèle ne peut pas être vide.")
        return self


# Tâche dont la première affectation sert de modèle par défaut proposé à chaque rôle.
ROLE_DEFAULT_TASK = {"customer": "customer_diagnosis", "staff": "agent_assistance"}


def routing_role(user_role: str) -> str:
    return "customer" if user_role == "customer" else "staff"


def allowed_choices(db: Session, role: str) -> list[dict]:
    """Modèles que l'administrateur a affectés à ce rôle, toutes tâches actives confondues."""
    config = get_routing_config(db)
    if not config["enabled"]:
        return []
    seen: set[tuple[str, str]] = set()
    choices: list[dict] = []
    for task in AI_TASKS:
        route = config["routes"].get(task, {}).get(role)
        if not route or not route["enabled"]:
            continue
        for choice in route["models"]:
            key = (choice["provider"], choice["model"])
            if key not in seen:
                seen.add(key)
                choices.append(dict(choice))
    return choices


def resolve_selection(db: Session, role: str, selection: ModelSelection) -> dict:
    """Valide un choix utilisateur et retourne la préférence normalisée à enregistrer."""
    for choice in allowed_choices(db, role):
        if choice["provider"] == selection.provider and choice["model"] == selection.model:
            think = selection.think if selection.think is not None else choice["think"]
            return {
                "provider": choice["provider"],
                "model": choice["model"],
                "think": bool(think) if choice["provider"] == "ollama" else False,
            }
    raise ValueError("Ce modèle n’est pas proposé pour votre profil.")


def apply_preference(choices: list[dict], allowed: list[dict], preference: dict | None) -> list[dict]:
    """Place le modèle préféré en tête de la priorité, les autres restant en secours.

    Le délai maximal reste celui défini par l'administrateur; un modèle qui n'est plus
    autorisé est ignoré et la priorité par défaut s'applique.
    """
    if not preference:
        return choices
    for choice in allowed:
        if choice["provider"] == preference.get("provider") and choice["model"] == preference.get("model"):
            chosen = dict(choice)
            if chosen["provider"] == "ollama" and preference.get("think") is not None:
                chosen["think"] = bool(preference["think"])
            rest = [
                item for item in choices
                if (item["provider"], item["model"]) != (chosen["provider"], chosen["model"])
            ]
            return [chosen, *rest]
    return choices


def user_ai_options(db: Session, user_role: str) -> dict:
    role = routing_role(user_role)
    choices = allowed_choices(db, role)
    config = get_routing_config(db)
    default_route = config["routes"].get(ROLE_DEFAULT_TASK[role], {}).get(role)
    default = None
    if config["enabled"] and default_route and default_route["enabled"] and default_route["models"]:
        default = default_route["models"][0]
    elif choices:
        default = choices[0]
    return {
        "enabled": bool(choices),
        "models": [
            {
                "provider": choice["provider"],
                "model": choice["model"],
                "supports_think": choice["provider"] == "ollama",
                "think": choice["think"],
            }
            for choice in choices
        ],
        "default": {"provider": default["provider"], "model": default["model"], "think": default["think"]}
        if default else None,
    }


def routing_for(task: str, role: str) -> tuple[bool, list[dict]]:
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
            "default_think": settings.ollama_think,
            "default_timeout_seconds": _default_model_choice()["timeout_seconds"]
            if settings.ai_provider.lower() != "anthropic" else DEFAULT_TIMEOUT_SECONDS,
            "available_models": ollama_models,
            "error": ollama_error,
        },
        {
            "id": "anthropic",
            "label": "Anthropic / Claude",
            "configured": anthropic_configured,
            "status": "configured" if anthropic_configured else "unconfigured",
            "default_model": settings.anthropic_model,
            "default_think": False,
            "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "available_models": [settings.anthropic_model] if anthropic_configured else [],
            "error": "" if anthropic_configured else "La clé ANTHROPIC_API_KEY n’est pas configurée.",
        },
    ]
