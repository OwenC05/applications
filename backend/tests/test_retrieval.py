import json
import re

import chromadb
import pytest

from copilot.contracts import EvidenceError
from copilot.retrieval.dense import DenseIndex
from copilot.retrieval.service import EvidenceService
from copilot.store import Store


class FixtureModels:
    """Deterministic test double; not evidence of model quality."""
    fingerprint = "synthetic-fixture"

    def tokenize_offsets(self, text):
        return [m.span() for m in re.finditer(r"\S+", text)]

    def embed(self, texts):
        return [[float("python" in text.lower()), float("finance" in text.lower()), 1.] + [0.] * 381 for text in texts]

    def rerank(self, query, texts):
        return [float(sum(word.lower() in text.lower() for word in query.split())) for text in texts]


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "data")
    dense = DenseIndex(chromadb.PersistentClient(path=str(tmp_path / "chroma")))
    service = EvidenceService(store, tmp_path / "indexes", dense, FixtureModels())
    profile = store.create_profile("Synthetic", ["tech", "finance"])
    return store, dense, service, profile


def test_real_chroma_bm25_fact_roundtrip_and_revoke(setup):
    store, dense, service, profile = setup
    fact = store.confirm_fact(profile.id, "Built Python test tooling for a synthetic project.")
    store.confirm_fact(profile.id, "Studied finance valuation and cash flows.")
    manifest = service.build(profile.id, "facts")
    hits = service.search(profile.id, "facts", "Python tooling")
    assert hits[0].chunk.record_id == fact.id
    assert hits[0].dense_rank is not None and hits[0].bm25_rank is not None
    assert store.resolve_citation(profile.id, hits[0].citation)
    ticket = store.revoke_fact(profile.id, fact.id)
    with pytest.raises(EvidenceError):
        service.search(profile.id, "facts", "Python")
    service.cleanup(ticket)
    assert manifest.dense_collection not in dense.names()
    assert not store.pending_cleanup()


def test_document_selection_before_rank_and_corruption(setup):
    store, dense, service, profile = setup
    selected = store.add_source(profile.id, "tech.txt", "text/plain", b"Python tooling", ["Python tooling"], "test")
    other = store.add_source(profile.id, "finance.txt", "text/plain", b"finance", ["finance"], "test")
    manifest = service.build(profile.id, "documents")
    hits = service.search(profile.id, "documents", "finance", [selected.id])
    assert hits and all(hit.chunk.record_id == selected.id for hit in hits)
    with pytest.raises(EvidenceError):
        service.search(profile.id, "documents", "finance", [])
    path = service.index_root / manifest.sparse_relpath / "manifest.json"
    body = json.loads(path.read_text())
    body["fingerprint"] = "wrong"
    path.write_text(json.dumps(body))
    with pytest.raises(EvidenceError, match="Sparse"):
        service.search(profile.id, "documents", "finance", [other.id])


def test_final_revision_race_rejected(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, "Python evidence")
    service.build(profile.id, "facts")
    original = service.models.rerank
    def race(query, texts):
        store.confirm_fact(profile.id, "New fact during query")
        return original(query, texts)
    service.models.rerank = race
    with pytest.raises(EvidenceError):
        service.search(profile.id, "facts", "Python")


def test_empty_and_orphan_recovery(setup):
    store, dense, service, profile = setup
    service.build(profile.id, "facts")
    assert service.search(profile.id, "facts", "anything") == []
    orphan = service.index_root / profile.id / "orphan"
    orphan.mkdir()
    service.recover()
    assert not orphan.exists()


def test_query_limit_and_foreign_selection(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, "Python")
    service.build(profile.id, "facts")
    with pytest.raises(EvidenceError, match="128"):
        service.search(profile.id, "facts", "word " * 129)
    with pytest.raises(EvidenceError):
        service.search(profile.id, "documents", "Python", ["foreign"])


@pytest.mark.real_models
def test_pinned_real_pipeline(tmp_path):
    import os
    from pathlib import Path

    from copilot.models import LocalModels
    directory = os.environ.get("COPILOT_REAL_MODEL_DIR")
    if not directory:
        pytest.skip("Explicit COPILOT_REAL_MODEL_DIR required")
    store = Store(tmp_path / "data")
    dense = DenseIndex(chromadb.PersistentClient(path=str(tmp_path / "chroma")))
    service = EvidenceService(store, tmp_path / "index", dense, LocalModels(Path(directory)))
    profile = store.create_profile("Synthetic real smoke", ["tech", "finance"])
    tech = store.confirm_fact(profile.id, "Implemented Python APIs and regression tests.")
    store.confirm_fact(profile.id, "Analysed discounted cash flow and valuation in a finance project.")
    service.build(profile.id, "facts")
    hits = service.search(profile.id, "facts", "Python software tests")
    assert hits[0].chunk.record_id == tech.id
    assert all(hit.dense_rank and hit.bm25_rank for hit in hits)


def test_missing_dense_never_degrades_to_bm25(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, "Python regression tests")
    manifest = service.build(profile.id, "facts")
    dense.delete(manifest.dense_collection)
    with pytest.raises(EvidenceError, match="Dense"):
        service.search(profile.id, "facts", "Python")


def test_publish_failure_discards_staging(setup, monkeypatch):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, "Finance valuation")
    def conflict(*args, **kwargs):
        raise EvidenceError("CONFLICT", "revision changed", 409)
    monkeypatch.setattr(store, "publish_manifest", conflict)
    with pytest.raises(EvidenceError, match="revision"):
        service.build(profile.id, "facts")
    assert not dense.owned_names(profile.id)
    assert not list((service.index_root / profile.id).iterdir())


def test_canonical_unicode_slices_and_chunk_token_cap(setup):
    store, dense, service, profile = setup
    text = "café αβ 🚀 " * 90
    fact = store.confirm_fact(profile.id, text)
    chunks = service._chunk(fact)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text == text[chunk.start:chunk.end]
        assert len(service.models.tokenize_offsets(chunk.text)) <= 220
        assert chunk.text_sha256 == __import__("hashlib").sha256(chunk.text.encode()).hexdigest()


def test_bm25_query_uses_corpus_vocabulary_not_query_local_ids(setup):
    from copilot.retrieval import sparse
    store, dense, service, profile = setup
    tech = store.confirm_fact(profile.id, "Python programming software tests")
    finance = store.confirm_fact(profile.id, "Discounted cash flow valuation finance")
    manifest = service.build(profile.id, "facts")
    ids = service._ids(manifest)
    engine, meta = sparse.load(service.index_root / manifest.sparse_relpath, ids, service.models.fingerprint)
    mapping = {c.id: c for c in store.eligible_chunks(profile.id, "facts", ids, None)}
    result = sparse.query(engine, meta, "valuation finance", 30)
    assert mapping[result[0]].record_id == finance.id
    assert mapping[result[-1]].record_id == tech.id
    assert len(sparse.query(engine, meta, "the and", 30)) == 2


@pytest.mark.real_models
def test_explicit_http_chroma_roundtrip(tmp_path):
    import os
    import uuid
    from pathlib import Path

    from copilot.models import LocalModels
    from copilot.retrieval import sparse
    directory = os.environ.get("COPILOT_REAL_MODEL_DIR")
    port = os.environ.get("COPILOT_HTTP_SMOKE_PORT")
    if not directory or not port:
        pytest.skip("Explicit local models and owned Chroma HTTP port required")
    client = chromadb.HttpClient(host="127.0.0.1", port=int(port))
    from importlib.metadata import version
    assert version("chromadb") == "1.5.9"
    assert client.get_version()  # Rust protocol endpoint may report 1.0.0, not wheel release.
    assert client.heartbeat() > 0
    dense = DenseIndex(client)
    store = Store(tmp_path / "data")
    models = LocalModels(Path(directory))
    service = EvidenceService(store, tmp_path / "index", dense, models)
    profile = store.create_profile("Synthetic HTTP", ["tech", "finance"])
    fact = store.confirm_fact(profile.id, "Built Python API software tests.")
    store.confirm_fact(profile.id, "Studied discounted cash flow finance valuation.")
    snapshot = store.snapshot(profile.id, "facts", service._chunk)
    chunks = sorted(snapshot.chunks, key=lambda c: c.id)
    ids = [c.id for c in chunks]
    name = "http_smoke_" + uuid.uuid4().hex
    try:
        dense.create(name, chunks, models.embed([c.text for c in chunks]), models.fingerprint,
                     profile.id, "synthetic-http")
        dense.verify(name, ids, models.fingerprint)
        directory = tmp_path / "sparse"
        sparse.create(directory, chunks, models.fingerprint)
        engine, meta = sparse.load(directory, ids, models.fingerprint)
        dense_results = dense.query(name, models.embed(["Python API tests"])[0], 2)
        bm25_results = sparse.query(engine, meta, "Python API tests", 2)
        mapping = {c.id: c for c in chunks}
        assert mapping[dense_results[0]].record_id == fact.id
        assert mapping[bm25_results[0]].record_id == fact.id
        citation = store.citation_for_chunk(profile.id, mapping[dense_results[0]])
        assert store.resolve_citation(profile.id, citation)
        selected = dense.query(name, models.embed(["finance"])[0], 2, [fact.id])
        assert all(mapping[key].record_id == fact.id for key in selected)
    finally:
        dense.delete(name)
    assert name not in dense.names()


def test_cleanup_does_not_require_model_provisioning(setup, tmp_path):
    from copilot.models import LocalModels
    store, dense, service, profile = setup
    fact = store.confirm_fact(profile.id, "Python evidence")
    service.build(profile.id, "facts")
    ticket = store.revoke_fact(profile.id, fact.id)
    service.models = LocalModels(tmp_path / "missing-models")
    assert not service.models.ready
    service.cleanup(ticket)
    assert not store.pending_cleanup()
    assert not dense.owned_names(profile.id)


@pytest.mark.parametrize("dense_failure,sparse_failure", [(True, False), (False, True), (True, True)])
def test_failed_build_attempts_both_cleanups_and_recovers(setup, monkeypatch, dense_failure, sparse_failure):
    import shutil
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, "Synthetic staging cleanup evidence")
    attempts = []
    original_dense_delete = dense.delete
    original_rmtree = shutil.rmtree
    original_publish = store.publish_manifest

    def failed_publish(*args, **kwargs):
        raise EvidenceError("CONFLICT", "original build conflict", 409)

    def delete_dense(name):
        attempts.append("dense")
        if dense_failure:
            raise OSError("synthetic dense outage")
        original_dense_delete(name)

    def delete_sparse(path, *args, **kwargs):
        attempts.append("sparse")
        if sparse_failure:
            raise OSError("synthetic sparse removal outage")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(store, "publish_manifest", failed_publish)
    monkeypatch.setattr(dense, "delete", delete_dense)
    monkeypatch.setattr(shutil, "rmtree", delete_sparse)
    with pytest.raises(EvidenceError) as caught:
        service.build(profile.id, "facts")
    assert caught.value.code == "CLEANUP_PENDING"
    assert isinstance(caught.value.__cause__, EvidenceError)
    assert caught.value.__cause__.code == "CONFLICT"
    assert attempts == ["dense", "sparse"]
    assert store.active_manifest(profile.id, "facts") is None
    assert bool(dense.owned_names(profile.id)) == dense_failure
    assert bool(list((service.index_root / profile.id).iterdir())) == sparse_failure
    monkeypatch.setattr(dense, "delete", original_dense_delete)
    monkeypatch.setattr(shutil, "rmtree", original_rmtree)
    monkeypatch.setattr(store, "publish_manifest", original_publish)
    service.recover()
    assert not dense.owned_names(profile.id)
    assert not list((service.index_root / profile.id).iterdir())


def test_old_cleanup_ticket_preserves_unrelated_and_newer_generations(setup):
    store, dense, service, profile = setup
    fact = store.confirm_fact(profile.id, 'Python fact')
    source = store.add_source(profile.id, 'notes.txt', 'text/plain', b'finance document', ['finance document'], 'test')
    old = service.build(profile.id, 'facts')
    document = service.build(profile.id, 'documents')
    ticket = store.revoke_fact(profile.id, fact.id)
    store.confirm_fact(profile.id, 'New Python fact')
    newer = service.build(profile.id, 'facts')
    service.cleanup(ticket)
    assert old.dense_collection not in dense.names()
    assert newer.dense_collection in dense.names() and document.dense_collection in dense.names()
    assert service.search(profile.id, 'facts', 'Python')
    assert service.search(profile.id, 'documents', 'finance', [source.id])
