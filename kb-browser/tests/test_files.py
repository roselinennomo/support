import io
import zipfile

from app import files

PPTX_SLIDE = (
    '<p:sld><p:sp><a:ln><a:tailEnd type="triangle"/></a:ln><p:txBody><a:bodyPr/><a:p><a:r><a:rPr lang="fr-FR"/>'
    "<a:t>SIP Trunking</a:t></a:r></a:p><a:p><a:r><a:t xml:space=\"preserve\">Configuration d&apos;un trunk</a:t></a:r></a:p>"
    "<a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Cellule</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></p:txBody></p:sp></p:sld>"
)


def make_zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_pptx_text_ignores_look_alike_tags_and_keeps_slide_order(tmp_path):
    """Régression : `<a:t…>` capturait aussi <a:tailEnd>, <a:tbl>, <a:tr>… et renvoyait du XML brut."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(make_zip({
        "ppt/slides/slide10.xml": PPTX_SLIDE.replace("SIP Trunking", "Dixième"),
        "ppt/slides/slide2.xml": PPTX_SLIDE,
        "ppt/slides/_rels/slide2.xml.rels": "<ignored/>",
    }))

    text = files.extract_file_text(path, "deck.pptx")

    assert "<" not in text and "tailEnd" not in text
    assert text.index("SIP Trunking") < text.index("Dixième")  # slide2 avant slide10
    assert "Configuration d'un trunk" in text and "Cellule" in text


def test_runs_of_one_paragraph_are_joined_not_split_on_separate_lines(tmp_path):
    """Régression : « Public SIP » + « Trunking » sortaient sur deux lignes."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(make_zip({"ppt/slides/slide1.xml": (
        "<p:sld><a:p><a:r><a:t>Public SIP </a:t></a:r><a:r><a:t>Trunking</a:t></a:r></a:p>"
        "<a:p><a:r><a:t>Edition 06</a:t></a:r></a:p></p:sld>"
    )}))

    assert files.extract_file_text(path, "deck.pptx") == "Public SIP Trunking\nEdition 06"


def test_docx_text(tmp_path):
    path = tmp_path / "guide.docx"
    path.write_bytes(make_zip({"word/document.xml": "<w:document><w:p><w:r><w:t>Installer le serveur</w:t></w:r></w:p></w:document>"}))

    assert files.extract_file_text(path, "guide.docx") == "Installer le serveur"


def test_zip_reads_supported_members_only_and_labels_them(tmp_path):
    path = tmp_path / "pack.zip"
    path.write_bytes(make_zip({
        "notes.txt": "Procédure d'interopérabilité",
        "image.png": b"\x89PNG",
        "nested.zip": b"PK",
        "sub/readme.md": "Lire d'abord",
    }))

    text = files.extract_file_text(path, "pack.zip")

    assert "=== notes.txt ===\nProcédure d'interopérabilité" in text and "=== readme.md ===" in text
    assert "PNG" not in text and "nested" not in text


def test_oversized_archives_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_ZIP_TOTAL_BYTES", 10)
    path = tmp_path / "bomb.zip"
    path.write_bytes(make_zip({"big.txt": "x" * 100}))

    assert files.extract_file_text(path, "bomb.zip") == ""


def test_formats_sizes_and_unsupported_files(tmp_path):
    assert files.is_supported("a.PDF") and files.is_supported("a.zip") and not files.is_supported("a.exe")
    assert files.parse_size_mb("2.84 MB - POWER_POINT_X") == 2.84
    assert files.parse_size_mb("512 KB") == 0.5
    assert files.parse_size_mb("pas de taille") is None
    (tmp_path / "tool.exe").write_bytes(b"MZ")
    assert files.extract_file_text(tmp_path / "tool.exe", "tool.exe") == ""
