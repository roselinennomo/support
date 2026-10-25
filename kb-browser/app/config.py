"""Configuration du service navigateur, entièrement pilotée par variables d'environnement."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _env(name: str, default: str = "") -> str:
    """Variable d'environnement; une valeur vide (compose `${VAR:-}`) équivaut à « non définie »."""
    return os.environ.get(name) or default


def _list(name: str, default: str) -> list[str]:
    return [item.strip().lower() for item in _env(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    """Les sélecteurs CSS valent pour la plupart des portails; adaptez-les à la KB réelle."""

    base_url: str = field(default_factory=lambda: _env("KB_BASE_URL", "https://example.invalid/kb"))
    search_url: str = field(default_factory=lambda: _env("KB_SEARCH_URL", ""))
    profile_dir: str = field(default_factory=lambda: _env("KB_PROFILE_DIR", "/data/profile"))
    headless: bool = field(default_factory=lambda: _env("KB_BROWSER_HEADLESS", "false").lower() == "true")
    token: str = field(default_factory=lambda: _env("KB_BROWSER_TOKEN", ""))
    search_input_selector: str = field(default_factory=lambda: _env("KB_SEARCH_INPUT_SELECTOR", "input[type='search'], input[name='q'], input[name='query']"))
    search_submit_selector: str = field(default_factory=lambda: _env("KB_SEARCH_SUBMIT_SELECTOR", ""))
    result_link_selector: str = field(default_factory=lambda: _env("KB_RESULT_LINK_SELECTOR", "a.search-result, .search-results a, article h2 a, article h3 a"))
    no_results_selector: str = field(default_factory=lambda: _env("KB_NO_RESULTS_SELECTOR", ""))
    article_selector: str = field(default_factory=lambda: _env("KB_ARTICLE_SELECTOR", "article, main, #content, .article-body"))
    # Vrai : concaténer TOUS les éléments correspondant à `article_selector` (articles découpés en sections).
    article_join: bool = field(default_factory=lambda: _env("KB_ARTICLE_JOIN", "false").lower() == "true")
    # « text » : lire le texte rendu (innerText), seul moyen de lire le contenu des composants à Shadow DOM
    # (Salesforce Lightning…); « html » : lire le HTML du conteneur.
    article_format: str = field(default_factory=lambda: _env("KB_ARTICLE_FORMAT", "html").lower())
    title_selector: str = field(default_factory=lambda: _env("KB_TITLE_SELECTOR", "h1"))
    # Présence de ce sélecteur = connecté (facultatif); absence = session expirée.
    logged_in_selector: str = field(default_factory=lambda: _env("KB_LOGGED_IN_SELECTOR", ""))
    # Sélecteur propre aux pages de connexion/MFA (facultatif), en plus des motifs d'URL.
    login_selector: str = field(default_factory=lambda: _env("KB_LOGIN_SELECTOR", "input[type='password'], input[autocomplete='one-time-code']"))
    login_url_patterns: list[str] = field(default_factory=lambda: _list(
        "KB_LOGIN_URL_PATTERNS", "login,signin,sign-in,sso,mfa,2fa,oauth,authorize,auth/"))
    allowed_hosts: list[str] = field(default_factory=lambda: _list("KB_ALLOWED_HOSTS", ""))
    nav_timeout_ms: int = field(default_factory=lambda: int(_env("KB_NAV_TIMEOUT_MS", "30000")))
    selector_timeout_ms: int = field(default_factory=lambda: int(_env("KB_SELECTOR_TIMEOUT_MS", "15000")))
    # Portails à rendu asynchrone (Salesforce/Coveo) : attendre l'hydratation avant de saisir la requête.
    ready_selector: str = field(default_factory=lambda: _env("KB_READY_SELECTOR", ""))
    settle_ms: int = field(default_factory=lambda: int(_env("KB_SETTLE_MS", "1500")))
    results_refresh_ms: int = field(default_factory=lambda: int(_env("KB_RESULTS_REFRESH_MS", "8000")))
    # --- Source « documents » (liste Salesforce de Business Documents, avec fichier joint) ---
    docs_url: str = field(default_factory=lambda: _env("KB_DOCS_URL", ""))
    docs_search_input_selector: str = field(default_factory=lambda: _env("KB_DOCS_SEARCH_INPUT_SELECTOR", "input[type='search']"))
    docs_row_link_selector: str = field(default_factory=lambda: _env("KB_DOCS_ROW_LINK_SELECTOR", "a[href*='/s/business-document/']"))
    docs_download_selector: str = field(default_factory=lambda: _env("KB_DOCS_DOWNLOAD_SELECTOR", "lightning-icon[title='Download'], .download_icon"))
    docs_start_marker: str = field(default_factory=lambda: _env("KB_DOCS_START_MARKER", "Business Document File"))
    docs_end_marker: str = field(default_factory=lambda: _env("KB_DOCS_END_MARKER", "Terms of Use"))
    docs_max_file_mb: float = field(default_factory=lambda: float(_env("KB_DOCS_MAX_FILE_MB", "15")))
    docs_download_timeout_ms: int = field(default_factory=lambda: int(_env("KB_DOCS_DOWNLOAD_TIMEOUT_MS", "60000")))
    request_delay_ms: int = field(default_factory=lambda: int(_env("KB_REQUEST_DELAY_MS", "1500")))

    def hosts(self) -> list[str]:
        """Hôtes autorisés pour la navigation : par défaut, celui de la KB (anti-SSRF)."""
        return self.allowed_hosts or [(urlparse(self.base_url).hostname or "").lower()]
