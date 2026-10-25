"""Nettoyage du HTML d'un article KB : ne conserver que le corps technique utile."""

import hashlib
import re

from bs4 import BeautifulSoup, Comment

# Balises sans contenu technique (navigation, scripts, formulaires…).
_REMOVED_TAGS = (
    "script", "style", "noscript", "template", "iframe", "svg", "canvas", "object", "embed",
    "form", "button", "input", "select", "textarea", "nav", "header", "footer", "aside",
)
# Conteneurs de décor repérés par leur class/id (« cookie-banner », « breadcrumb », « sidebar »…).
_BOILERPLATE = re.compile(
    r"(?:^|[-_\s])(cookie|consent|breadcrumb|sidebar|navbar|nav|menu|footer|header|social|share|"
    r"related|pagination|toc|banner|newsletter|feedback|rating)(?:$|[-_\s])",
    re.IGNORECASE,
)
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_article_html(html: str) -> str:
    """Retourne le texte lisible d'un fragment HTML, sans scripts, menus, en-têtes ni pieds de page."""
    soup = BeautifulSoup(html or "", "html.parser")
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for tag in soup.find_all(_REMOVED_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.attrs is None:  # déjà supprimé avec un parent
            continue
        marker = " ".join([*tag.get("class", []), tag.get("id") or ""])
        if marker.strip() and _BOILERPLATE.search(marker):
            tag.decompose()
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    text = "\n".join(lines)
    return _BLANK_LINES.sub("\n\n", text).strip()


def content_hash(text: str) -> str:
    """Empreinte stable du contenu (insensible à la casse et aux espaces) pour la déduplication."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """URL sans fragment ni paramètres de suivi, pour éviter les doublons d'un même article."""
    base = url.split("#", 1)[0].strip()
    if "?" not in base:
        return base.rstrip("/")
    path, query = base.split("?", 1)
    kept = [
        part for part in query.split("&")
        if part and not part.lower().startswith(("utm_", "fbclid", "gclid", "session", "q=", "query="))
    ]
    return (path.rstrip("/") + ("?" + "&".join(kept) if kept else ""))
