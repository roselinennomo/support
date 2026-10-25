"""Extraction de texte des fichiers joints aux documents (sans dépendance lourde)."""

import io
import re
import zipfile
from html import unescape
from pathlib import Path

MAX_CHARS = 200_000
MAX_PDF_PAGES = 80
MAX_ZIP_MEMBERS = 12  # fichiers lus dans une archive
MAX_ZIP_MEMBER_BYTES = 12 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 80 * 1024 * 1024  # taille décompressée cumulée (protection contre les archives piégées)
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".log"}
_DOCUMENT_SUFFIXES = {".pdf", ".pptx", ".docx"} | _TEXT_SUFFIXES
_SUPPORTED = _DOCUMENT_SUFFIXES | {".zip"}


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower() in _SUPPORTED


def parse_size_mb(text: str) -> float | None:
    """Taille annoncée par le portail (« 2.84 MB », « 512 KB »), en Mo; None si absente."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(KB|MB|GB)\b", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value * {"kb": 1 / 1024, "mb": 1, "gb": 1024}[match.group(2).lower()]


def extract_file_text(path: Path, filename: str, max_chars: int = MAX_CHARS) -> str:
    """Texte brut d'un fichier PDF, PowerPoint, Word, texte ou archive ZIP; vide si le format n'est pas pris en charge."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        text = _zip_text(path, max_chars)
    else:
        text = _document_text(path.read_bytes(), suffix)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:max_chars]


def _document_text(data: bytes, suffix: str) -> str:
    if suffix == ".pdf":
        return _pdf_text(data)
    if suffix == ".pptx":
        return _office_text(data, r"ppt/slides/slide\d+\.xml", r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", r"</a:p>")
    if suffix == ".docx":
        return _office_text(data, r"word/document\.xml", r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", r"</w:p>")
    if suffix in _TEXT_SUFFIXES:
        return data.decode("utf-8", errors="replace")
    return ""


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(io.BytesIO(data)).pages[:MAX_PDF_PAGES]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # une page illisible ne doit pas faire perdre le document
            pages.append("")
    return "\n\n".join(pages)


def _office_text(data: bytes, member_pattern: str, text_pattern: str, paragraph_end: str) -> str:
    """Texte des documents Office Open XML : les `<a:t>` / `<w:t>` de chaque diapositive ou du document."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = sorted(
            (name for name in archive.namelist() if re.fullmatch(member_pattern, name)),
            key=lambda name: [int(part) for part in re.findall(r"\d+", name)],
        )
        parts = []
        for name in members:
            if archive.getinfo(name).file_size > MAX_ZIP_MEMBER_BYTES:
                continue
            xml = archive.read(name).decode("utf-8", errors="replace")
            # Les segments (« runs ») d'un même paragraphe sont recollés; un paragraphe = une ligne.
            lines = [
                "".join(unescape(item) for item in re.findall(text_pattern, paragraph, re.DOTALL))
                for paragraph in re.split(paragraph_end, xml)
            ]
            parts.append("\n".join(line for line in lines if line.strip()))
        return "\n\n".join(parts)


def _zip_text(path: Path, max_chars: int) -> str:
    """Texte des documents lisibles d'une archive (sans archives imbriquées, taille décompressée bornée)."""
    parts: list[str] = []
    total_chars = 0
    with zipfile.ZipFile(path) as archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if sum(info.file_size for info in entries) > MAX_ZIP_TOTAL_BYTES:
            return ""
        readable = [
            info for info in entries
            if Path(info.filename).suffix.lower() in _DOCUMENT_SUFFIXES and info.file_size <= MAX_ZIP_MEMBER_BYTES
            and not Path(info.filename).name.startswith(".")
        ]
        for info in readable[:MAX_ZIP_MEMBERS]:
            try:
                text = _document_text(archive.read(info), Path(info.filename).suffix.lower()).strip()
            except Exception:  # un membre corrompu n'empêche pas de lire les autres
                continue
            if text:
                parts.append(f"=== {Path(info.filename).name} ===\n{text}")
                total_chars += len(text)
            if total_chars >= max_chars:
                break
    return "\n\n".join(parts)
