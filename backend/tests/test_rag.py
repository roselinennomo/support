import hashlib

import pytest
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
    monkeypatch.setattr(rag, "_sync_documents", lambda **_kwargs: None)
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
            # qwen3-embedding:4b installé ne doit pas valider le modèle actif qwen3-embedding:0.6b.
            return Response({"models": [{"name": "qwen3-embedding:0.6b"}, {"name": "qwen3-embedding:4b"}]})
        return Response({"result": {"config": {}}})

    monkeypatch.setattr(rag.httpx, "get", fake_get)

    result = rag.inspect_index()

    assert result["qdrant"]["status"] == "healthy"
    assert result["embedding"]["status"] == "healthy"
    assert {option["id"]: option["installed"] for option in result["embedding_options"]} == {
        "qwen3-embedding:0.6b": True, "bge-m3": False, "qwen3-embedding:4b": True,
    }
    assert [option["id"] for option in result["embedding_options"] if option["selected"]] == ["qwen3-embedding:0.6b"]
    assert result["collection"] == "support_documents_qwen3-embedding-0-6b"
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


def test_large_reindexing_is_deferred_to_the_background_instead_of_blocking_the_answer(monkeypatch, tmp_path):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(f"contenu {name}", encoding="utf-8")
    monkeypatch.setattr(rag.settings, "rag_documents_dir", str(tmp_path))
    monkeypatch.setattr(rag, "_all_points", lambda: [])
    indexed = []
    monkeypatch.setattr(rag, "_index_source", lambda *args: indexed.append(args))

    assert rag._sync_documents(max_sources=2)["deferred_sources"] == 3
    assert indexed == []  # rien n'est indexé dans la réponse en direct
    assert rag._sync_documents(max_sources=3)["indexed_sources"] == 3  # petit lot : fait tout de suite


def test_retrieval_triggers_background_sync_and_answers_immediately(monkeypatch):
    started = []
    monkeypatch.setattr(rag, "_ensure_collection", lambda: None)
    monkeypatch.setattr(rag, "_sync_documents", lambda **_kwargs: {"deferred_sources": 40})
    monkeypatch.setattr(rag, "_start_background_sync", lambda: started.append(True))
    monkeypatch.setattr(rag, "evaluate_query", lambda _query: {"results": []})
    monkeypatch.setattr(rag, "_kb_excerpts", lambda _query: [])

    assert "Aucun extrait" in rag.retrieve_context("Le service ne répond plus")
    assert started == [True]


def test_retrieval_does_not_wait_while_another_indexing_is_running(monkeypatch):
    monkeypatch.setattr(rag, "_ensure_collection", lambda: None)
    monkeypatch.setattr(
        rag, "_sync_documents",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("ne doit pas synchroniser pendant une indexation")),
    )
    monkeypatch.setattr(rag, "evaluate_query", lambda _query: {"results": [
        {"source": "guide.pdf", "page": 3, "text": "Vérifier le routage.", "score": 0.8},
    ]})
    monkeypatch.setattr(rag, "_kb_excerpts", lambda _query: [])

    with rag._sync_lock:  # une indexation (admin ou autre ticket) tient le verrou
        context = rag.retrieve_context("routage")

    assert "[Source : guide.pdf, page 3]" in context


# ----------------------------------------------------------------------------- indexation reprenable, taille du travail, file
class FakeStore:
    """Mini Qdrant en mémoire : upsert, scroll filtré (source, content_hash) et suppression d'autres versions."""

    def __init__(self):
        self.points: dict[str, dict] = {}
        self.puts = 0

    def request(self, method, path, **kwargs):
        body = kwargs.get("json") or {}
        result = {}
        if path.endswith("/points") and method == "PUT":
            self.puts += 1
            for point in body["points"]:
                self.points[point["id"]] = point["payload"]
        elif path.endswith("/points/scroll"):
            must = body.get("filter", {}).get("must", [])
            found = [
                {"id": point_id, "payload": payload} for point_id, payload in self.points.items()
                if all(payload.get(item["key"]) == item["match"]["value"] for item in must)
            ]
            result = {"points": found, "next_page_offset": None}
        elif path.endswith("/points/delete"):
            flt = body["filter"]
            for point_id, payload in list(self.points.items()):
                if all(payload.get(i["key"]) == i["match"]["value"] for i in flt.get("must", [])) and not any(
                    payload.get(i["key"]) == i["match"]["value"] for i in flt.get("must_not", [])
                ):
                    del self.points[point_id]
        return SimpleNamespace(json=lambda: {"result": result})


def _fake_pages(count):
    return [(1, f"Paragraphe {index} " + "mot " * 300) for index in range(count)]


def test_indexing_writes_batch_by_batch_and_resumes_where_it_stopped(monkeypatch):
    store = FakeStore()
    embedded = []
    monkeypatch.setattr(rag, "_qdrant_request", store.request)
    monkeypatch.setattr(rag, "_EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(rag, "collection_name", lambda: "c")

    def embed(texts):
        embedded.append(len(texts))
        if len(embedded) == 3:
            raise RuntimeError("redémarrage de l'API")  # interruption au troisième lot
        return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(rag, "_embed", embed)
    pages = _fake_pages(6)
    total = len([chunk for _, text in pages for chunk in rag._chunks(text)])

    with pytest.raises(RuntimeError):
        rag._index_source("gros.pdf", "h1", pages)
    assert len(store.points) == 4 and all(p["chunk_total"] == total for p in store.points.values())  # 2 lots déjà écrits

    embedded.clear()
    monkeypatch.setattr(rag, "_embed", lambda texts: embedded.append(len(texts)) or [[0.0] * 4 for _ in texts])
    rag._index_source("gros.pdf", "h1", pages)  # reprise : seuls les lots manquants sont calculés
    assert len(store.points) == total and sum(embedded) == total - 4


def test_a_partial_version_is_not_considered_indexed_and_old_versions_are_removed_when_complete(monkeypatch, tmp_path):
    (tmp_path / "guide.txt").write_text("contenu", encoding="utf-8")
    digest = rag.hashlib.sha256(b"contenu").hexdigest()
    monkeypatch.setattr(rag.settings, "rag_documents_dir", str(tmp_path))
    real_index_source = rag._index_source
    indexed = []
    monkeypatch.setattr(rag, "_index_source", lambda *args: indexed.append(args[0]))
    monkeypatch.setattr(rag, "_all_points", lambda: [{"payload": {"source": "guide.txt", "content_hash": digest, "chunk_total": 3}}] * 2)
    assert rag._sync_documents()["indexed_sources"] == 1  # 2 extraits sur 3 : à reprendre
    monkeypatch.setattr(rag, "_all_points", lambda: [{"payload": {"source": "guide.txt", "content_hash": digest, "chunk_total": 3}}] * 3)
    indexed.clear()
    assert rag._sync_documents()["indexed_sources"] == 0 and indexed == []  # complet : rien à faire

    store = FakeStore()
    monkeypatch.setattr(rag, "_qdrant_request", store.request)
    monkeypatch.setattr(rag, "collection_name", lambda: "c")
    monkeypatch.setattr(rag, "_embed", lambda texts: [[0.0] * 4 for _ in texts])
    store.points["ancien"] = {"source": "g.pdf", "content_hash": "v1", "text": "ancienne version"}
    real_index_source("g.pdf", "v2", [(1, "nouvelle version")])
    assert {payload["content_hash"] for payload in store.points.values()} == {"v2"}  # v1 supprimée à la fin


def test_inspect_reports_interrupted_indexing_as_partial(monkeypatch, tmp_path):
    (tmp_path / "gros.txt").write_text("x" * 50, encoding="utf-8")
    digest = rag.hashlib.sha256(b"x" * 50).hexdigest()
    monkeypatch.setattr(rag.settings, "rag_documents_dir", str(tmp_path))
    monkeypatch.setattr(rag, "_all_points", lambda: [{"payload": {"source": "gros.txt", "content_hash": digest, "chunk_total": 4}}] * 3)
    monkeypatch.setattr(rag, "embedding_status", lambda: {"status": "healthy", "error": "", "options": []})
    monkeypatch.setattr(rag.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {}))
    report = rag.inspect_index()
    assert report["documents"][0]["status"] == "partial" and report["documents"][0]["chunks"] == 3
    assert report["metrics"]["pending_sources"] == 1


def test_heavy_files_are_never_indexed_inside_a_user_request_even_for_a_single_source(monkeypatch, tmp_path):
    (tmp_path / "gros.txt").write_text("a" * 2_000_000, encoding="utf-8")
    (tmp_path / "petit.txt").write_text("court", encoding="utf-8")
    monkeypatch.setattr(rag.settings, "rag_documents_dir", str(tmp_path))
    monkeypatch.setattr(rag, "_all_points", lambda: [])
    indexed = []
    monkeypatch.setattr(rag, "_index_source", lambda *args: indexed.append(args[0]))
    result = rag._sync_documents(max_sources=2, max_bytes=rag._INLINE_SYNC_MAX_BYTES)
    assert result["deferred_sources"] == 2 and indexed == []  # 2 sources seulement, mais 2 Mo à indexer
    (tmp_path / "gros.txt").unlink()
    assert rag._sync_documents(max_sources=2, max_bytes=rag._INLINE_SYNC_MAX_BYTES)["indexed_sources"] == 1


def test_only_one_indexing_batch_runs_at_a_time_while_questions_are_not_blocked(monkeypatch):
    import threading
    import time

    running, peak, lock = [0], [0], threading.Lock()

    def slow_embed(texts):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return [[0.0] for _ in texts]

    monkeypatch.setattr(rag, "_embed", slow_embed)
    rag._query_cache.clear()
    workers = [threading.Thread(target=rag._embed_for_index, args=(["a"],)) for _ in range(4)]
    for worker in workers:
        worker.start()
    time.sleep(0.02)
    started = time.perf_counter()
    rag._embed_query("une question")  # la question ne fait pas la queue derrière l'indexation
    assert time.perf_counter() - started < 0.2
    for worker in workers:
        worker.join()
    assert peak[0] <= 2  # 1 lot d'indexation + 1 question au maximum


def test_the_question_is_embedded_once_for_the_three_searches(monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "_embed", lambda texts: calls.append(texts) or [[0.5, 0.5] for _ in texts])
    rag._query_cache.clear()
    first = rag._embed_query("Pas de tonalité sur le poste 4012")
    second = rag._embed_query("Pas de tonalité sur le poste 4012")
    other = rag._embed_query("Autre question")
    assert first == second == [0.5, 0.5] and len(calls) == 2  # corpus puis (cache) entreprise et KB
    assert other == [0.5, 0.5]
