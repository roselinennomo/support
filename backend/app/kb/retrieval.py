"""Lecture de la KB indexée pour enrichir le contexte RAG (étape « aval »)."""

import logging

from app.kb import flags

logger = logging.getLogger(__name__)
_EXCERPTS = 3
_LABELS = {"kb": "KB constructeur", "document": "documentation constructeur"}


def kb_excerpts(query: str, index=None) -> list[str]:
    """Extraits KB formatés pour le prompt. Retourne [] si désactivé ou en cas de moindre erreur."""
    if not query.strip() or not flags.is_enabled():
        return []
    try:
        if index is None:
            from app.kb.index import QdrantKBIndex

            index = QdrantKBIndex()
        return [
            f"[Source {_LABELS.get(match.get('source', 'kb'), 'KB constructeur')} : "
            f"{match['title'] or match['url']} — {match['url']}]\n{match['text']}"
            for match in index.search(query, _EXCERPTS)
            if match.get("text")
        ]
    except Exception as exc:  # repli transparent sur le RAG documentaire seul
        logger.warning("KB retrieval skipped: %s", exc)
        return []
