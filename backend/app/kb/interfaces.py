"""Contrats du module d'enrichissement par la base de connaissances (KB) externe.

Le reste de l'application ne dépend que de ces abstractions : le scraper réel (conteneur
Playwright), le générateur de requête et l'index vectoriel sont substituables et mockables.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class KBError(RuntimeError):
    """Erreur de base du module KB : toujours rattrapée par l'orchestrateur."""

    code = "kb_error"


class KBUnavailable(KBError):
    """Le conteneur navigateur est hors ligne, injoignable ou ne répond pas à temps."""

    code = "unavailable"


class KBSessionExpired(KBError):
    """La session (cookies/MFA) du profil navigateur n'est plus valide."""

    code = "session_expired"


class KBScrapeError(KBError):
    """Échec déterministe du parcours (sélecteur introuvable, page vide, timeout de navigation)."""

    code = "scrape_failed"

    def __init__(self, message: str, selector: str = "", url: str = ""):
        super().__init__(message)
        self.selector = selector
        self.url = url


class SessionState(str, Enum):
    VALID = "valid"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class KBSearchHit:
    """Un résultat de la page de recherche de la KB."""

    title: str
    url: str
    meta: str = ""  # texte des colonnes de la ligne (type, langue, date…) pour les listes de documents


@dataclass(frozen=True)
class KBDocument:
    """Fiche d'un document constructeur : métadonnées rendues + texte extrait du fichier joint (s'il est lisible)."""

    url: str
    title: str
    metadata: str
    file_name: str = ""
    file_text: str = ""
    file_skipped: str = ""  # too_large | unsupported_format | no_attachment | download_failed | no_text
    file_bytes: bytes | None = None  # fichier original, seulement si l'archive locale est activée


@dataclass(frozen=True)
class KBArticle:
    """Article brut renvoyé par le scraper : `html` est le conteneur principal, avant nettoyage."""

    url: str
    title: str
    html: str


@dataclass(frozen=True)
class KBQuery:
    """Requête de recherche optimisée et entités techniques dont elle est issue."""

    text: str
    entities: list[str] = field(default_factory=list)
    source: str = "heuristic"  # "llm" ou "heuristic"


@dataclass(frozen=True)
class TicketSnapshot:
    """Données minimales d'un ticket nécessaires à la génération de la requête."""

    ticket_id: str
    title: str
    description: str
    product: str = ""
    version: str = ""
    ai_preferences: dict | None = None
    customer_id: str | None = None


class KBScraper(Protocol):
    """Service de navigation déterministe sur le portail KB."""

    def health(self) -> bool:
        """True si le navigateur répond. Ne lève jamais."""

    def session_state(self) -> SessionState:
        """Vérifie la session; lève KBUnavailable si le navigateur est injoignable."""

    def search(self, query: str, limit: int) -> list[KBSearchHit]:
        """Injecte la requête et retourne les `limit` premiers résultats."""

    def fetch_article(self, url: str) -> KBArticle:
        """Ouvre un article et retourne son conteneur principal."""

    def search_documents(self, query: str, limit: int) -> list[KBSearchHit]:
        """Filtre la liste de documents avec la requête et retourne les premières lignes."""

    def fetch_document(self, url: str, include_file: bool = False) -> KBDocument:
        """Lit la fiche d'un document et, si possible, le texte de son fichier joint (et le fichier si demandé)."""

    def logs(self, limit: int = 100) -> list[dict]:
        """Derniers évènements du navigateur (debug); liste vide si indisponible."""


class QueryBuilder(Protocol):
    def build(self, ticket: TicketSnapshot) -> KBQuery:
        """Retourne la requête KB du ticket; ne lève pas d'exception."""


class KBIndex(Protocol):
    """Index vectoriel dédié aux articles KB, distinct du corpus documentaire."""

    def upsert(self, url: str, title: str, text: str, content_hash: str, source: str = "kb") -> int:
        """Indexe l'article (remplace ses anciens extraits) et retourne le nombre d'extraits."""

    def search(self, query: str, limit: int) -> list[dict]:
        """Retourne des extraits {url, title, text, score, source}."""
