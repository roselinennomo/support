"""Recherche web libre et gratuite pour la console d'aide, via une instance SearXNG auto-hébergée.

Seule la question de l'utilisateur, expurgée (e-mails, IP, MAC, téléphones), quitte l'application : jamais le
contenu des documents, des tickets ou des documents d'entreprise. Désactivée tant que WEB_SEARCH_URL est vide.
"""

import logging

import httpx

from app.config import settings
from app.kb.query import redact

logger = logging.getLogger(__name__)
MAX_QUERY_CHARS = 300


def enabled() -> bool:
    return bool(settings.web_search_url.strip())


def search(question: str) -> list[dict]:
    """Résultats [{title, url, snippet}]; [] si désactivée, question vide ou service en panne. Ne lève jamais."""
    query = redact(question)[:MAX_QUERY_CHARS]
    if not enabled() or not query:
        return []
    try:
        response = httpx.get(
            f"{settings.web_search_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "safesearch": 1},
            timeout=20,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Web search unavailable: %s", exc)
        return []
    found = []
    for item in results[: settings.web_search_max_results]:
        url, title = str(item.get("url", "")), str(item.get("title", "")).strip()
        if url.startswith(("http://", "https://")) and title:
            found.append({"title": title[:200], "url": url, "snippet": str(item.get("content", "")).strip()[:500]})
    return found
