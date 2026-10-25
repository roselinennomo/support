"""Extraction de texte des documents téléversés (PDF, Word, PowerPoint, Excel, texte), sans dépendance lourde.

Les fichiers Office modernes sont des archives ZIP de XML : on en lit directement le texte. Excel est rendu
ligne par ligne (« cellule | cellule ») pour conserver la structure d'un plan d'adressage ou d'une matrice de flux.
"""

import io
import re
import zipfile
from html import unescape
from pathlib import Path

MAX_CHARS = 400_000
MAX_PDF_PAGES = 200
MAX_XLSX_ROWS = 20_000
MAX_MEMBER_BYTES = 40 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".log", ".conf", ".cfg", ".json", ".yaml", ".yml"}
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx"} | TEXT_SUFFIXES


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_SUFFIXES


def extract_text(data: bytes, filename: str, max_chars: int = MAX_CHARS) -> str:
    """Texte brut du document; chaîne vide si le format n'est pas pris en charge ou ne contient aucun texte (scan)."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        text = _pdf(data)
    elif suffix == ".docx":
        text = _office(data, r"word/document\.xml", r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", r"</w:p>")
    elif suffix == ".pptx":
        text = _office(data, r"ppt/slides/slide\d+\.xml", r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", r"</a:p>")
    elif suffix == ".xlsx":
        text = _xlsx(data)
    elif suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace")
    else:
        return ""
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:max_chars]


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(io.BytesIO(data)).pages[:MAX_PDF_PAGES]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # une page illisible ne fait pas perdre le document
            pages.append("")
    return "\n\n".join(pages)


def _numbered(names: list[str]) -> list[str]:
    return sorted(names, key=lambda name: [int(part) for part in re.findall(r"\d+", name)])


def _office(data: bytes, member_pattern: str, text_pattern: str, paragraph_end: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parts = []
        for name in _numbered([n for n in archive.namelist() if re.fullmatch(member_pattern, n)]):
            if archive.getinfo(name).file_size > MAX_MEMBER_BYTES:
                continue
            xml = archive.read(name).decode("utf-8", errors="replace")
            lines = [
                "".join(unescape(item) for item in re.findall(text_pattern, paragraph, re.DOTALL))
                for paragraph in re.split(paragraph_end, xml)
            ]
            parts.append("\n".join(line for line in lines if line.strip()))
        return "\n\n".join(parts)


def _xlsx(data: bytes) -> str:
    """Feuilles Excel en lignes « A | B | C », avec le nom de chaque feuille."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            xml = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            shared = [
                "".join(unescape(t) for t in re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", item, re.DOTALL))
                for item in re.findall(r"<si>(.*?)</si>", xml, re.DOTALL)
            ]
        sheet_names = []
        if "xl/workbook.xml" in names:
            workbook = archive.read("xl/workbook.xml").decode("utf-8", errors="replace")
            sheet_names = [unescape(n) for n in re.findall(r"<sheet\s[^>]*?name=\"([^\"]*)\"", workbook)]
        sheets = _numbered([n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)])
        parts = []
        for index, name in enumerate(sheets):
            if archive.getinfo(name).file_size > MAX_MEMBER_BYTES:
                continue
            xml = archive.read(name).decode("utf-8", errors="replace")
            rows = []
            for row in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, re.DOTALL)[:MAX_XLSX_ROWS]:
                cells = []
                for attributes, body in re.findall(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row, re.DOTALL):
                    value = _cell_value(attributes, body or "", shared)
                    cells.append(value)
                if any(cell.strip() for cell in cells):
                    rows.append(" | ".join(cells).rstrip(" |"))
            if rows:
                title = sheet_names[index] if index < len(sheet_names) else f"Feuille {index + 1}"
                parts.append(f"## {title}\n" + "\n".join(rows))
        return "\n\n".join(parts)


def _cell_value(attributes: str, body: str, shared: list[str]) -> str:
    kind = re.search(r'\bt="([^"]*)"', attributes)
    kind = kind.group(1) if kind else ""
    if kind == "inlineStr":
        return " ".join(unescape(t) for t in re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", body, re.DOTALL)).strip()
    value = re.search(r"<v>(.*?)</v>", body, re.DOTALL)
    if not value:
        return ""
    raw = unescape(value.group(1)).strip()
    if kind == "s":
        try:
            return shared[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    return raw
