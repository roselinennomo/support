"""Reconstitution de bout en bout du parcours d'un ticket pour la page « Diagnostic ticket ».

Assemble, dans l'ordre chronologique : création, messages, événements, traitements IA et leurs étapes
(`ai_steps`), tentatives auprès des fournisseurs, recherches KB. Pour les tickets antérieurs au traçage des
étapes, les appels réussis (`ai_executions`) et les échecs consignés en événements servent de repli.

Aucun contenu de message, de prompt ou de réponse n'est renvoyé : titres, durées, états et compteurs seulement.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIExecution, AIStep, KBJob, Message, Ticket, TicketEvent

PARTICIPANTS = [
    {"id": "actor", "label": "Client / Agent"},
    {"id": "api", "label": "API"},
    {"id": "task", "label": "Tâche IA"},
    {"id": "rag", "label": "RAG (embeddings, Qdrant)"},
    {"id": "llm", "label": "Fournisseur LLM"},
    {"id": "kb", "label": "KB constructeur"},
    {"id": "db", "label": "Base de données"},
]
RAG_TOP_LEVEL = {"rag.sync", "rag.search", "rag.company", "rag.kb"}  # rag.embedding et rag.qdrant sont inclus dans rag.search
EVENT_LABELS = {
    "ai_response": "Réponse IA enregistrée", "ai_handoff": "Transfert du client au support",
    "ai_handoff_summary": "Note de passation IA enregistrée", "ai_provider_failed": "Échec d'un fournisseur IA",
    "ai_analysis_failed": "Échec du diagnostic IA", "status_changed": "Changement de statut",
    "assignment_changed": "Changement d'assignation", "ai_agent_assist": "Aide IA agent enregistrée",
    "ai_agent_assist_failed": "Échec de l'aide IA agent", "ai_model_selected": "Modèle IA choisi par le client",
    "attachments_added": "Pièces jointes ajoutées", "kb_enrichment": "Enrichissement KB",
    "created": "Ticket enregistré", "message_added": "Message ajouté", "ai_handoff_summary_failed": "Échec de la note de passation IA",
}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _entry(at, duration_ms, source, target, kind, title, status="ok", detail=None, lane=None) -> dict:
    at = _utc(at)
    return {
        "at": at, "end_at": at + timedelta(milliseconds=duration_ms or 0), "duration_ms": duration_ms or 0,
        "from": source, "to": target, "kind": kind, "title": title, "status": status, "detail": detail or {},
    }


def build_timeline(db: Session, ticket: Ticket) -> dict:
    entries: list[dict] = []
    steps = db.scalars(select(AIStep).where(AIStep.ticket_id == ticket.id).order_by(AIStep.started_at)).all()
    has_steps = any(item.kind == "provider" for item in steps)

    entries.append(_entry(
        ticket.created_at, 0, "actor", "api", "ticket", "Création du ticket",
        detail={"reference": ticket.reference, "priorité": ticket.priority, "produit": ticket.product},
    ))

    trigger_messages = []  # instants des messages qui déclenchent ou attendent une réponse
    assistant_messages = []
    internal_notes: list[datetime] = []
    handoffs: list[datetime] = []
    for message in sorted(ticket.messages, key=lambda item: item.created_at):
        role = message.author.role
        detail = {"auteur": message.author.full_name, "visibilité": message.visibility, "caractères": len(message.body)}
        if role == "system":
            if message.visibility == "public":
                assistant_messages.append(_utc(message.created_at))
                entries.append(_entry(message.created_at, 0, "api", "actor", "message", "Réponse IA publiée (public)", detail=detail))
            else:  # note interne : résumé de passation, aide agent ou liste d'articles KB trouvés
                internal_notes.append(_utc(message.created_at))
                entries.append(_entry(message.created_at, 0, "api", "db", "message", "Note interne de l'assistant", detail=detail))
        else:
            who = "client" if role == "customer" else "agent"
            if message.visibility == "public" and role == "customer":
                trigger_messages.append(_utc(message.created_at))
            entries.append(_entry(
                message.created_at, 0, "actor", "api", "message", f"Message du {who} ({message.visibility})", detail=detail,
            ))
    trigger_messages.insert(0, _utc(ticket.created_at))

    for event in db.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id).order_by(TicketEvent.created_at)):
        details = event.details or {}
        if event.event_type == "ai_handoff":  # déclenche la note de passation interne
            handoffs.append(_utc(event.created_at))
        if event.event_type == "ai_provider_failed" and has_steps:
            continue  # déjà décrit par les étapes de fournisseur
        if event.event_type == "ai_provider_failed":  # repli : ticket antérieur au traçage des étapes
            duration = int(details.get("duration_ms") or 0)
            entries.append(_entry(
                _utc(event.created_at) - timedelta(milliseconds=duration), duration, "task", "llm", "provider",
                f"{details.get('provider', '?')}/{details.get('model', '?')}", "failed",
                {"tâche": details.get("task", ""), "erreur": details.get("error", ""), "durée estimée à partir de l'événement": True},
            ))
            continue
        safe = {key: value for key, value in details.items() if key in {"from", "to", "provider", "model", "task", "agent_name", "resolved", "escalation_recommended", "count"}}
        entries.append(_entry(
            event.created_at, 0, "api", "db", "event", EVENT_LABELS.get(event.event_type, f"Événement : {event.event_type}"),
            "failed" if "failed" in event.event_type else "ok", safe,
        ))

    if not has_steps:  # repli : appels réussis connus seulement par leur durée
        for execution in db.scalars(select(AIExecution).where(AIExecution.ticket_id == ticket.id).order_by(AIExecution.created_at)):
            entries.append(_entry(
                _utc(execution.created_at) - timedelta(milliseconds=execution.duration_ms), execution.duration_ms, "task", "llm",
                "provider", f"{execution.provider}/{execution.model}", "ok",
                {"tâche": execution.task, "tokens entrée": execution.input_tokens, "tokens sortie": execution.output_tokens,
                 "durée estimée à partir de l'enregistrement": True},
            ))

    for item in steps:
        detail = dict(item.detail or {})
        if item.kind == "run":
            entries.append(_entry(item.started_at, item.duration_ms, "api", "task", "run", f"Traitement : {item.label}", item.status, {"exécution": item.run_id[:8]}))
        elif item.kind == "rag":
            entries.append(_entry(item.started_at, item.duration_ms, "task", "rag", "rag", item.name, item.status, detail))
        elif item.kind == "provider":
            entries.append(_entry(item.started_at, item.duration_ms, "task", "llm", "provider", item.name, item.status, detail))
        elif item.kind == "db":
            entries.append(_entry(item.started_at, item.duration_ms, "task", "db", "db", item.name, item.status, detail))

    for job in db.scalars(select(KBJob).where(KBJob.ticket_id == ticket.id).order_by(KBJob.created_at)):
        duration = job.duration_ms or (round((_utc(job.finished_at) - _utc(job.created_at)).total_seconds() * 1000) if job.finished_at else 0)
        entries.append(_entry(
            job.created_at, duration, "api", "kb", "kb", f"Recherche KB constructeur ({job.status})",
            "failed" if job.status == "failed" else "ok",
            {"requête": job.query, "résultats": job.hits, "nouveaux": job.new_documents, "déjà connus": job.skipped_documents,
             "erreur": job.error},
        ))

    for note_at in internal_notes:
        if any(handoff <= note_at for handoff in handoffs):
            trigger_messages.append(max(handoff for handoff in handoffs if handoff <= note_at))
            assistant_messages.append(note_at)
    assistant_messages.sort()
    entries.extend(_wait_entries(entries, sorted(trigger_messages), assistant_messages))
    entries.sort(key=lambda item: item["at"])
    for index, item in enumerate(entries):
        item["index"] = index

    return {
        "participants": PARTICIPANTS,
        "entries": entries,
        "tracing_complete": has_steps,
        "runs": [
            {"label": item.label, "started_at": _utc(item.started_at), "duration_ms": item.duration_ms, "status": item.status}
            for item in steps if item.kind == "run"
        ],
        "latencies": _latencies(entries, sorted(trigger_messages), assistant_messages),
    }


def _wait_entries(entries: list[dict], triggers: list[datetime], responses: list[datetime]) -> list[dict]:
    """Intervalles sans étape propre : attente avant le traitement, temps non détaillé, préparation d'un ticket non tracé."""
    waits = []
    for response_at in responses:
        earlier = [moment for moment in triggers if moment <= response_at]
        if not earlier:
            continue
        start = max(earlier)
        inside = [item for item in entries if start <= item["at"] <= response_at]
        runs = [item for item in inside if item["kind"] == "run"]
        for run in runs:
            queue = round((run["at"] - start).total_seconds() * 1000)
            if queue >= 300:
                waits.append(_entry(start, queue, "api", "task", "wait", "Attente avant le démarrage du traitement", detail={
                    "explication": "délai entre le message déclencheur et le début de la tâche de fond (file d'attente du serveur)",
                }))
            children = [
                item for item in inside
                if run["at"] <= item["at"] <= run["end_at"]
                and (item["kind"] in {"provider", "db"} or (item["kind"] == "rag" and item["title"] in RAG_TOP_LEVEL))
            ]
            rest = run["duration_ms"] - sum(item["duration_ms"] for item in children)
            if rest >= 500:
                waits.append(_entry(run["at"], rest, "api", "task", "wait", "Temps du traitement non détaillé", detail={
                    "explication": "construction du prompt, validation de la réponse, enregistrement : aucune étape propre",
                }))
        if not runs:  # ticket antérieur au traçage : tout ce qui précède l'appel au modèle est regroupé
            providers = [item for item in inside if item["kind"] == "provider"]
            if providers:
                first = min(providers, key=lambda item: item["at"])
                gap = round((first["at"] - start).total_seconds() * 1000)
                if gap >= 1000:
                    waits.append(_entry(start, gap, "api", "task", "wait", "Préparation avant l'appel au modèle (non détaillée)", detail={
                        "explication": "recherche documentaire (embeddings, Qdrant, KB), file d'attente, chargement : non tracé pour ce ticket",
                    }))
    unique, seen = [], set()
    for wait in waits:
        key = (wait["at"], wait["title"])
        if key not in seen:
            seen.add(key)
            unique.append(wait)
    return unique


def _latencies(entries: list[dict], triggers: list[datetime], responses: list[datetime]) -> list[dict]:
    """Pour chaque réponse IA : temps perçu par l'utilisateur, comparé au temps passé chez les fournisseurs."""
    results = []
    for response_at in responses:
        earlier = [moment for moment in triggers if moment <= response_at]
        if not earlier:
            continue
        start = max(earlier)
        inside = [item for item in entries if start <= item["at"] <= response_at]
        provider_ok = sum(item["duration_ms"] for item in inside if item["kind"] == "provider" and item["status"] == "ok")
        provider_failed = sum(item["duration_ms"] for item in inside if item["kind"] == "provider" and item["status"] == "failed")
        rag = sum(item["duration_ms"] for item in inside if item["kind"] == "rag" and item["title"] in RAG_TOP_LEVEL)
        db_ms = sum(item["duration_ms"] for item in inside if item["kind"] == "db")
        queue = sum(item["duration_ms"] for item in inside if item["kind"] == "wait" and item["title"].startswith("Attente avant le démarrage"))
        perceived = round((response_at - start).total_seconds() * 1000)
        failed_attempts = [item["title"] for item in inside if item["kind"] == "provider" and item["status"] == "failed"]
        results.append({
            "trigger_at": start, "response_at": response_at, "perceived_ms": perceived,
            "provider_ok_ms": provider_ok, "provider_failed_ms": provider_failed, "rag_ms": rag, "db_ms": db_ms, "queue_ms": queue,
            "other_ms": max(0, perceived - provider_ok - provider_failed - rag - db_ms - queue),
            "failed_attempts": failed_attempts,
        })
    return results
