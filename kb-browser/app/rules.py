"""Règles pures (sans navigateur) : détection de page de connexion et filtrage des URLs."""

from urllib.parse import urlparse


def is_login_url(url: str, patterns: list[str]) -> bool:
    """Vrai si l'URL ressemble à une page de connexion / MFA / SSO."""
    lowered = (url or "").lower()
    return any(pattern and pattern in lowered for pattern in patterns)


def host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    """Vrai pour une URL http(s) dont l'hôte est (un sous-domaine d')un hôte autorisé."""
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts if allowed)


def clean_hits(raw_hits: list[dict], limit: int, allowed_hosts: list[str]) -> list[dict]:
    """Garde les `limit` premiers résultats uniques, http(s), sur un hôte autorisé."""
    seen: set[str] = set()
    hits: list[dict] = []
    for raw in raw_hits:
        url = str(raw.get("url", "")).split("#", 1)[0].strip()
        if not url or url in seen or not host_allowed(url, allowed_hosts):
            continue
        seen.add(url)
        hits.append({
            "title": " ".join(str(raw.get("title", "")).split())[:300],
            "url": url,
            "meta": " ".join(str(raw.get("meta", "")).split())[:500],
        })
        if len(hits) >= limit:
            break
    return hits
