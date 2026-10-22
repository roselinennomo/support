import hashlib
from types import SimpleNamespace

from app import rag


def test_chunking_keeps_overlap_between_parts():
    text = "a" * 1300 + " " + "b" * 500

    chunks = rag._chunks(text)

    assert len(chunks) == 2
    assert chunks[0][-100:] == chunks[1][:100]


def test_sync_indexes_changed_sources_and_removes_deleted(monkeypatch, tmp_path):
    (tmp_path / "guide.txt").write_text("Nouvelle procédure", encoding="utf-8")
    monkeypatch.setattr(rag.settings, "rag_documents_dir", str(tmp_path))
    monkeypatch.setattr(rag, "_all_points", lambda: [
        {"payload": {"source": "removed.txt", "content_hash": "old"}},
        {"payload": {"source": "guide.txt", "content_hash": "old"}},
    ])
    deleted = []
    indexed = []
    progress = []
    monkeypatch.setattr(rag, "_delete_source", deleted.append)
    monkeypatch.setattr(rag, "_index_source", lambda source, digest, pages: indexed.append((source, digest, pages)))

    result = rag._sync_documents(progress.append)

    assert deleted == ["removed.txt"]
    assert len(indexed) == 1
    assert indexed[0][0] == "guide.txt"
    assert indexed[0][2] == [(None, "Nouvelle procédure")]
    assert result == {"source_count": 1, "indexed_sources": 1, "deleted_sources": 1}
    assert progress[0]["phase"] == "scanning"
    assert progress[1]["total"] == 2
    assert progress[-1]["phase"] == "complete"


def test_retrieve_context_formats_qdrant_references(monkeypatch):
    monkeypatch.setattr(rag, "_ensure_collection", lambda: None)
    monkeypatch.setattr(rag, "_sync_documents", lambda: None)
    monkeypatch.setattr(rag, "_embed", lambda _texts: [[0.0] * rag.settings.rag_embedding_dimension])
    monkeypatch.setattr(
        rag,
        "_qdrant_request",
        lambda *_args, **_kwargs: SimpleNamespace(json=lambda: {
            "result": [{
                "payload": {
                    "source": "manual.pdf",
                    "page": 7,
                    "text": "Contrôler l'état du service.",
                }
            }]
        }),
    )

    context = rag.retrieve_context("Le service ne répond plus")

    assert "[Source : manual.pdf, page 7]" in context
    assert "Contrôler l'état du service." in context


def test_pdf_extraction_skips_a_malformed_page(monkeypatch, tmp_path):
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"pdf")

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            if self.text is None:
                raise ValueError("flux PDF invalide")
            return self.text

    monkeypatch.setattr(rag, "PdfReader", lambda _file: SimpleNamespace(
        pages=[Page("page valide"), Page(None), Page("autre page valide")]
    ))

    assert rag._read_source(source) == [
        (1, "page valide"),
        (2, ""),
        (3, "autre page valide"),
    ]


def test_unextractable_document_is_marked_and_not_reparsed(monkeypatch):
    requests = []
    monkeypatch.setattr(rag, "_embed", lambda _texts: [[0.0] * rag.settings.rag_embedding_dimension])
    monkeypatch.setattr(rag, "_delete_source", lambda _source: None)
    monkeypatch.setattr(rag, "_qdrant_request", lambda *args, **kwargs: requests.append((args, kwargs)))

    rag._index_source("empty.pdf", "digest", [(1, "")])

    assert len(requests) == 1
    payload = requests[0][1]["json"]["points"][0]["payload"]
    assert payload["indexed_empty"] is True
    assert payload["content_hash"] == "digest"


def test_inspect_index_reports_documents_services_and_orphaned_sources(monkeypatch, tmp_path):
    guide = tmp_path / "guide.md"
    guide.write_text("Documentation indexée", encoding="utf-8")
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"pdf")
    (tmp_path / "pending.txt").write_text("À indexer", encoding="utf-8")
    monkeypatch.setattr(rag.settings, "rag_documents_dir", str(tmp_path))
    guide_hash = hashlib.sha256(guide.read_bytes()).hexdigest()
    empty_hash = hashlib.sha256(empty.read_bytes()).hexdigest()
    monkeypatch.setattr(rag, "_all_points", lambda: [
        {"payload": {"source": "guide.md", "content_hash": guide_hash, "text": "Texte"}},
        {"payload": {"source": "empty.pdf", "content_hash": empty_hash, "indexed_empty": True}},
        {"payload": {"source": "removed.txt", "content_hash": "ancien"}},
    ])

    class Response:
        status_code = 200

        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, **_kwargs):
        if url.endswith("/api/tags"):
            return Response({"models": [{"name": "nomic-embed-text:latest"}]})
        return Response({"result": {"config": {}}})

    monkeypatch.setattr(rag.httpx, "get", fake_get)

    result = rag.inspect_index()

    assert result["qdrant"]["status"] == "healthy"
    assert result["embedding"]["status"] == "healthy"
    assert result["metrics"] == {
        "source_count": 3,
        "indexed_sources": 2,
        "pending_sources": 1,
        "deleted_sources": 1,
        "chunk_count": 1,
        "empty_sources": 1,
    }
    documents = {document["source"]: document for document in result["documents"]}
    assert documents["guide.md"]["status"] == "indexed"
    assert documents["pending.txt"]["status"] == "pending"
    assert documents["empty.pdf"]["status"] == "empty"
    assert result["deleted_sources"] == ["removed.txt"]


def test_evaluate_query_searches_existing_index_without_syncing(monkeypatch):
    class CollectionResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"config": {"params": {"vectors": {"size": rag.settings.rag_embedding_dimension}}}}}

    monkeypatch.setattr(
        rag.httpx,
        "get",
        lambda *_args, **_kwargs: CollectionResponse(),
    )
    monkeypatch.setattr(
        rag,
        "_ensure_collection",
        lambda: (_ for _ in ()).throw(AssertionError("search must not create a collection")),
    )
    monkeypatch.setattr(rag, "_embed", lambda _texts: [[0.0] * rag.settings.rag_embedding_dimension])
    monkeypatch.setattr(
        rag,
        "_sync_documents",
        lambda: (_ for _ in ()).throw(AssertionError("search must not trigger indexing")),
    )
    monkeypatch.setattr(
        rag,
        "_qdrant_request",
        lambda *_args, **_kwargs: SimpleNamespace(json=lambda: {
            "result": [{
                "score": 0.82,
                "payload": {"source": "guide.md", "text": "Procédure de vérification."},
            }]
        }),
    )

    result = rag.evaluate_query("Comment vérifier le service ?")

    assert result["result_count"] == 1
    assert result["results"][0]["source"] == "guide.md"
    assert result["results"][0]["score"] == 0.82
