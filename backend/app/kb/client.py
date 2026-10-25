"""Client HTTP du conteneur `kb-browser` (implémentation de `KBScraper`)."""

import base64
import binascii
import hashlib
import logging

import httpx

from app.config import settings
from app.kb.interfaces import (
    KBArticle,
    KBDocument,
    KBScrapeError,
    KBSearchHit,
    KBSessionExpired,
    KBUnavailable,
    SessionState,
)

logger = logging.getLogger(__name__)


class HttpKBScraper:
    """Parle au service Playwright; toute panne réseau devient `KBUnavailable`."""

    def __init__(self, base_url: str, token: str = "", timeout: float = 90):
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-KB-Token": token} if token else {}
        self._timeout = timeout

    def _request(self, method: str, path: str, timeout: float | None = None, **kwargs) -> dict:
        try:
            response = httpx.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
                timeout=timeout or self._timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise KBUnavailable(f"Navigateur KB injoignable : {exc}") from exc
        if response.status_code < 400:
            return response.json()
        detail = _detail(response)
        code = detail.get("code", "")
        if response.status_code == 409 and code == "session_expired":
            raise KBSessionExpired(detail.get("message", "Session KB expirée."))
        if response.status_code == 422:
            raise KBScrapeError(
                detail.get("message", "Échec de l'extraction KB."),
                selector=detail.get("selector", ""),
                url=detail.get("url", ""),
            )
        raise KBUnavailable(f"Navigateur KB indisponible (HTTP {response.status_code}) : {detail.get('message', '')}")

    def health(self) -> bool:
        try:
            return self._request("GET", "/health", timeout=3).get("status") == "ok"
        except KBUnavailable:
            return False
        except Exception:  # pragma: no cover - ne doit jamais remonter
            logger.exception("Unexpected KB health check failure")
            return False

    def session_state(self) -> SessionState:
        state = self._request("GET", "/session").get("state", "unknown")
        return SessionState(state) if state in {item.value for item in SessionState} else SessionState.UNKNOWN

    def search(self, query: str, limit: int) -> list[KBSearchHit]:
        payload = self._request("POST", "/search", json={"query": query, "limit": limit})
        return [
            KBSearchHit(title=str(hit.get("title", "")), url=str(hit["url"]), meta=str(hit.get("meta", "")))
            for hit in payload.get("hits", [])
            if hit.get("url")
        ][:limit]

    def search_documents(self, query: str, limit: int) -> list[KBSearchHit]:
        payload = self._request("POST", "/documents/search", json={"query": query, "limit": min(limit, 10)})
        return [
            KBSearchHit(title=str(hit.get("title", "")), url=str(hit["url"]), meta=str(hit.get("meta", "")))
            for hit in payload.get("hits", [])
            if hit.get("url")
        ][:limit]

    def fetch_document(self, url: str, include_file: bool = False) -> KBDocument:
        payload = self._request("POST", "/documents/fetch", json={"url": url, "include_file": include_file})
        file = payload.get("file") or {}
        return KBDocument(
            url=payload.get("url") or url,
            title=str(payload.get("title", "")),
            metadata=str(payload.get("metadata", "")),
            file_name=str(file.get("name", "")),
            file_text=str(file.get("text", "")),
            file_skipped=str(file.get("skipped", "")),
            file_bytes=_decode_file(file) if include_file else None,
        )

    def fetch_article(self, url: str) -> KBArticle:
        payload = self._request("POST", "/article", json={"url": url})
        return KBArticle(url=payload.get("url") or url, title=str(payload.get("title", "")), html=str(payload.get("html", "")))

    def logs(self, limit: int = 100) -> list[dict]:
        try:
            return list(self._request("GET", f"/logs?limit={int(limit)}", timeout=5).get("entries", []))
        except Exception:
            return []


def _decode_file(file: dict) -> bytes | None:
    """Fichier original renvoyé en base64; ignoré s'il est illisible ou si son empreinte ne correspond pas."""
    try:
        data = base64.b64decode(file.get("content_b64") or "", validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or (file.get("sha256") and hashlib.sha256(data).hexdigest() != file["sha256"]):
        return None
    return data


def _detail(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    detail = body.get("detail", body) if isinstance(body, dict) else {}
    return detail if isinstance(detail, dict) else {"message": str(detail)}


def default_scraper() -> HttpKBScraper:
    return HttpKBScraper(settings.kb_browser_url, settings.kb_browser_token, settings.kb_browser_timeout_seconds)
