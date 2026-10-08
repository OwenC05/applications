"""Synthetic Chroma/BM25 races; no real models or paid provider calls."""
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import chromadb
import pytest
from test_retrieval import FixtureModels

from copilot.contracts import EvidenceError
from copilot.generations import Generations
from copilot.retrieval.dense import DenseIndex
from copilot.retrieval.service import EvidenceService
from copilot.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'data')
    dense = DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma')))
    service = EvidenceService(store, tmp_path / 'indexes', dense, FixtureModels())
    profile = store.create_profile('Synthetic', ['tech', 'finance'])
    return store, dense, service, profile


def run_background(call):
    errors = []
    def target():
        try:
            call()
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=target)
    thread.start()
    return thread, errors


def test_registration_precedes_tokenizer_and_tokenizer_holds_no_sqlite_lock(setup):
    store, _, service, profile = setup
    store.confirm_fact(profile.id, 'Python tooling')
    entered, release = threading.Event(), threading.Event()
    original = service.models.tokenize_offsets
    def blocked(text):
        entered.set()
        assert release.wait(5)
        return original(text)
    service.models.tokenize_offsets = blocked
    thread, errors = run_background(lambda: service.build(profile.id, 'facts'))
    assert entered.wait(5)
    try:
        intents = service.generations.list()
        assert len(intents) == 1 and intents[0].state == 'registered'
        job = service.jobs.get(profile.id, intents[0].job_id)
        service.jobs.cancel(profile.id, job.id)
        store.confirm_fact(profile.id, 'Changed while tokenizer blocked')
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors and errors[0].code == 'CONFLICT'
    assert store.active_manifest(profile.id, 'facts') is None
    assert service.generations.list()[0].state == 'cleaned'


def test_changed_chunker_failed_rebuild_preserves_prior_canonical_chunks(setup, monkeypatch):
    store, _, service, profile = setup
    store.confirm_fact(profile.id, 'Python tests ' * 240)
    previous = service.build(profile.id, 'facts')
    ids = service._ids(previous)
    before = store.eligible_chunks(profile.id, 'facts', ids, generation_id=previous.generation_id)
    import copilot.retrieval.service as module
    monkeypatch.setattr(module, 'CHUNKER_VERSION', 'synthetic-next-chunker')
    def fail(_texts):
        raise EvidenceError('MODEL_NOT_READY', 'Synthetic failed rebuild', 503)
    monkeypatch.setattr(service.models, 'embed', fail)
    with pytest.raises(EvidenceError, match='failed rebuild'):
        service.build(profile.id, 'facts')
    assert store.active_manifest(profile.id, 'facts') == previous
    assert store.eligible_chunks(profile.id, 'facts', ids, generation_id=previous.generation_id) == before
    monkeypatch.undo()
    assert service.search(profile.id, 'facts', 'Python')
    store.confirm_fact(profile.id, 'Actually changed facts')
    with pytest.raises(EvidenceError):
        service.search(profile.id, 'facts', 'Python')


def test_live_staged_generation_survives_new_service_recovery(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Python evidence')
    entered, release = threading.Event(), threading.Event()
    original = dense.create
    def blocked(*args):
        original(*args)
        entered.set()
        assert release.wait(5)
    dense.create = blocked
    thread, errors = run_background(lambda: service.build(profile.id, 'facts'))
    assert entered.wait(5)
    try:
        from copilot.retrieval.service import EvidenceService
        restarted = EvidenceService(store, service.index_root, dense, service.models)
        assert restarted.recover()['cleanup_pending'] == 0
        intent = restarted.generations.list()[0]
        assert intent.dense_collection in dense.names()
        assert intent.state == 'building'
    finally:
        release.set()
        thread.join(5)
    assert not errors and store.active_manifest(profile.id, 'facts')


def test_expired_old_producer_late_dense_write_cannot_publish_or_delete_new_generation(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Python evidence')
    now = [datetime(2026, 10, 8, tzinfo=timezone.utc)]
    service.jobs.clock = lambda: now[0]
    job = service.jobs.enqueue(profile.id, 'index', {'corpus': 'facts'}, 'race')
    old_worker = str(uuid4())
    old = service.jobs.claim(old_worker, lease_seconds=5, job_id=job.id)
    entered, release = threading.Event(), threading.Event()
    original = dense.create
    first = [True]
    def blocked(*args):
        if first[0]:
            first[0] = False
            entered.set()
            assert release.wait(5)
        original(*args)
    dense.create = blocked
    thread, errors = run_background(lambda: service.run_index(job.id, old_worker, old.fence))
    assert entered.wait(5)
    try:
        old_intent = service.generations.list()[0]
        now[0] += timedelta(seconds=6)
        new_worker = str(uuid4())
        new = service.jobs.claim(new_worker, job_id=job.id)
        assert new.fence > old.fence
        newest = service.run_index(job.id, new_worker, new.fence)
        assert service.recover()['cleanup_pending'] == 1  # Old producer still owns its lock.
        assert newest.dense_collection in dense.names()
        assert old_intent.state == 'building'
    finally:
        release.set()
        thread.join(5)
    assert errors and errors[0].code == 'CONFLICT'
    service.recover()
    assert old_intent.dense_collection not in dense.names()
    assert newest.dense_collection in dense.names()
    assert service.search(profile.id, 'facts', 'Python')
    assert service.jobs.get(profile.id, job.id).state == 'completed'


def test_profile_delete_while_producer_live_retains_pending_ticket_until_quiescence(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Python private synthetic text')
    entered, release = threading.Event(), threading.Event()
    original = dense.create
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    dense.create = blocked
    thread, errors = run_background(lambda: service.build(profile.id, 'facts'))
    assert entered.wait(5)
    try:
        intent = service.generations.list()[0]
        ticket = store.delete_profile(profile.id)
        assert intent.generation_id in ticket.generation_ids
        with store._tx() as db:
            assert db.execute('SELECT captured FROM generation_intents').fetchone()[0] is None
            assert not db.execute('SELECT 1 FROM generation_chunks').fetchone()
        with pytest.raises(EvidenceError, match='quiesced'):
            service.cleanup(ticket)
        assert store.pending_cleanup()
    finally:
        release.set()
        thread.join(5)
    assert errors and errors[0].code in ('CONFLICT', 'NOT_FOUND')
    service.recover()
    assert not store.pending_cleanup()
    assert not dense.names()
    assert not list(service.index_root.iterdir())


def test_crash_after_artifacts_recovers_only_after_lease_expiry(setup, monkeypatch):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Python evidence')
    now = [datetime(2026, 10, 8, tzinfo=timezone.utc)]
    service.jobs.clock = lambda: now[0]
    job = service.jobs.enqueue(profile.id, 'index', {'corpus': 'facts'}, 'crash')
    worker = str(uuid4())
    lease = service.jobs.claim(worker, lease_seconds=5, job_id=job.id)
    def crash(*_args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(service.generations, 'publish', crash)
    with pytest.raises(KeyboardInterrupt):
        service.run_index(job.id, worker, lease.fence)
    intent = service.generations.list()[0]
    assert intent.dense_collection in dense.names()
    assert (service.index_root / intent.sparse_relpath).exists()
    service.recover()
    assert intent.dense_collection in dense.names()
    now[0] += timedelta(seconds=6)
    service.recover()
    assert intent.dense_collection not in dense.names()
    assert not (service.index_root / intent.sparse_relpath).exists()
    assert service.generations.list()[0].state == 'cleaned'


def test_unknown_artifacts_are_preserved_and_reported(setup):
    _, dense, service, profile = setup
    path = service.index_root / 'unregistered'
    path.mkdir()
    (path / 'canary').write_text('Synthetic unknown')
    name = 'evidence_' + uuid4().hex
    dense.client.create_collection(name, embedding_function=None)
    report = service.recover()
    assert report['unknown_sparse'] == report['unknown_dense'] == 1
    assert path.exists() and name in dense.names()
    assert service.generations.list() == []


def test_snapshot_tokenizer_is_nonmutating_and_outside_writer_transaction(setup):
    store, _, service, profile = setup
    store.confirm_fact(profile.id, 'Python evidence')
    manifest = service.build(profile.id, 'facts')
    before = store.eligible_chunks(profile.id, 'facts', service._ids(manifest))
    def chunk(record):
        store.create_profile('Independent during tokenization', ['finance'])
        return service._chunk(record)
    store.snapshot(profile.id, 'facts', chunk)
    assert store.eligible_chunks(profile.id, 'facts', service._ids(manifest)) == before


def test_stage_refuses_tampered_chunk_and_stale_capture(setup):
    from dataclasses import replace
    store, _, service, profile = setup
    store.confirm_fact(profile.id, 'Python evidence')
    job = service.jobs.enqueue(profile.id, 'index', {'corpus': 'facts'}, 'staging')
    worker = str(uuid4())
    lease = service.jobs.claim(worker, job_id=job.id)
    generation_id = str(uuid4())
    intent, values = service.generations.register(job.id, worker, lease.fence, generation_id,
                                                service.models.fingerprint, 'synthetic')
    snapshot = store.chunk_capture(profile.id, 'facts', intent.revisions.facts, values, service._chunk)
    tampered = replace(snapshot, chunks=(replace(snapshot.chunks[0], text='not canonical'),))
    with pytest.raises(EvidenceError):
        service.generations.stage(generation_id, worker, lease.fence, tampered)
    service.jobs.cancel(profile.id, job.id)
    with pytest.raises(EvidenceError):
        service.generations.stage(generation_id, worker, lease.fence, snapshot)
    assert Generations(store).list()[0].state == 'registered'


def test_unfenced_manifest_publication_is_rejected(setup):
    store, _, service, profile = setup
    manifest = service.build(profile.id, 'facts')
    with pytest.raises(EvidenceError, match='fenced'):
        store.publish_manifest(manifest, manifest.revision)


@pytest.mark.parametrize('point', ['after_dense', 'after_sparse', 'after_publish'])
def test_process_interrupt_never_publishes_mixed_generations(setup, monkeypatch, point):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Python evidence')
    previous = service.build(profile.id, 'facts')
    now = [datetime(2026, 10, 8, tzinfo=timezone.utc)]
    service.jobs.clock = lambda: now[0]
    from copilot.retrieval import sparse
    if point == 'after_dense':
        def interrupted(*_args):
            raise KeyboardInterrupt()
        monkeypatch.setattr(sparse, 'create', interrupted)
    elif point == 'after_sparse':
        original = sparse.create
        def interrupted(*args):
            original(*args)
            raise KeyboardInterrupt()
        monkeypatch.setattr(sparse, 'create', interrupted)
    else:
        original = service.generations.publish
        def interrupted(*args):
            original(*args)
            raise KeyboardInterrupt()
        monkeypatch.setattr(service.generations, 'publish', interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.build(profile.id, 'facts')
    active = store.active_manifest(profile.id, 'facts')
    intents = service.generations.list()
    newest = intents[-1]
    assert active.generation_id == (newest.generation_id if point == 'after_publish' else previous.generation_id)
    monkeypatch.undo()
    assert service.search(profile.id, 'facts', 'Python')
    now[0] += timedelta(seconds=301)
    service.recover()
    assert active.dense_collection in dense.names()
    assert (service.index_root / active.sparse_relpath).exists()
    if point != 'after_publish':
        assert newest.dense_collection not in dense.names()


def test_honest_legacy_nested_manifest_survives_recovery_and_deletes_by_captured_ticket(setup):
    import json
    from dataclasses import asdict

    from copilot.contracts import Manifest
    from copilot.retrieval import sparse
    from copilot.retrieval.dense import ids_checksum
    from copilot.retrieval.service import CHUNKER_VERSION
    store, dense, service, profile = setup
    fact = store.confirm_fact(profile.id, 'Python legacy evidence')
    snapshot = store.snapshot(profile.id, 'facts', service._chunk)
    generation_id = str(uuid4())
    collection = 'evidence_' + generation_id.replace('-', '')
    path = profile.id + '/' + generation_id
    chunks = list(snapshot.chunks)
    dense.create(collection, chunks, service.models.embed([c.text for c in chunks]),
                 service.models.fingerprint, profile.id, generation_id)
    sparse.create(service.index_root / path, chunks, service.models.fingerprint)
    legacy = Manifest(generation_id, profile.id, 'facts', snapshot.revision, service.models.fingerprint,
                      CHUNKER_VERSION, ids_checksum([c.id for c in chunks]), len(chunks), collection, path)
    # Synthetic pre-S2 database fixture; no fabricated job/lease for a legacy generation.
    with store._tx() as db:
        for chunk in chunks:
            store._put(db, profile.id, 'chunk:facts', chunk)
        db.execute('INSERT INTO manifests VALUES(?,?,?,?,?)',
                   (generation_id, profile.id, 'facts', 'active', json.dumps(asdict(legacy))))
    service.recover()
    assert not service.generations.list()
    assert service.search(profile.id, 'facts', 'Python')
    ticket = store.revoke_fact(profile.id, fact.id)
    service.cleanup(ticket)
    assert collection not in dense.names()
    assert not (service.index_root / path).exists()
    assert not store.pending_cleanup()


def test_deleted_source_scrubs_only_its_corpus_generations_and_preserves_fact_index(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Standalone Python fact')
    fact_index = service.build(profile.id, 'facts')
    source = store.add_source(profile.id, 'notes.txt', 'text/plain', b'Finance document', ['Finance document'], 'test')
    document_index = service.build(profile.id, 'documents')
    ticket = store.delete_source(profile.id, source.id)
    assert ticket.generation_ids == [document_index.generation_id]
    service.cleanup(ticket)
    assert fact_index.dense_collection in dense.names()
    assert document_index.dense_collection not in dense.names()
    assert service.search(profile.id, 'facts', 'Python')


def test_direct_transaction_is_not_a_substitute_for_job_authority(setup):
    store, _, service, profile = setup
    manifest = service.build(profile.id, 'facts')
    with store._tx() as db, pytest.raises(EvidenceError, match='fenced'):
        store.publish_manifest(manifest, manifest.revision, transaction=db)


def test_actual_manifest_and_chunk_publication_roll_back_atomically(setup, monkeypatch):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Python original evidence')
    original = service.build(profile.id, 'facts')
    original_chunks = store.eligible_chunks(profile.id, 'facts', service._ids(original))
    publish = store.publish_manifest
    def interrupted(*args, **kwargs):
        publish(*args, **kwargs)
        raise EvidenceError('CONFLICT', 'Synthetic fault after SQL publication', 409)
    monkeypatch.setattr(store, 'publish_manifest', interrupted)
    with pytest.raises(EvidenceError, match='after SQL'):
        service.build(profile.id, 'facts')
    assert store.active_manifest(profile.id, 'facts') == original
    assert store.eligible_chunks(profile.id, 'facts', service._ids(original)) == original_chunks
    assert service.jobs.list(profile.id)[-1].state == 'failed'
    assert service.generations.list()[-1].state == 'cleaned'
    assert dense.names() == [original.dense_collection]
    assert service.search(profile.id, 'facts', 'Python')


def test_empty_manifest_generation_identity_is_verified(setup):
    _, _, service, profile = setup
    manifest = service.build(profile.id, 'facts')
    (service.index_root / manifest.sparse_relpath / 'empty.json').write_text('{}')
    with pytest.raises(EvidenceError, match='mismatched'):
        service.search(profile.id, 'facts', 'Anything')


def test_legacy_cleanup_rejects_symlink_index_root_before_any_removal(setup, tmp_path):
    import json
    from dataclasses import asdict

    from copilot.contracts import Manifest
    from copilot.retrieval.dense import ids_checksum
    from copilot.retrieval.service import CHUNKER_VERSION
    store, dense, service, profile = setup
    generation_id = str(uuid4())
    collection = 'evidence_' + generation_id.replace('-', '')
    relative = profile.id + '/' + generation_id
    outside = tmp_path / 'outside-storage'
    directory = outside / relative
    directory.mkdir(parents=True)
    canary = directory / 'canary'
    canary.write_bytes(b'Outside bytes must survive')
    dense.client.create_collection(collection, embedding_function=None,
        metadata={'profile_id': profile.id, 'generation_id': generation_id})
    legacy = Manifest(generation_id, profile.id, 'facts', 0, service.models.fingerprint,
                      CHUNKER_VERSION, ids_checksum([]), 0, collection, relative)
    with store._tx() as db:
        db.execute('INSERT INTO manifests VALUES(?,?,?,?,?)',
                   (generation_id, profile.id, 'facts', 'active', json.dumps(asdict(legacy))))
    service.index_root.rmdir()
    service.index_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(EvidenceError, match='unsafe'):
        service._manifest_path(legacy)
    ticket = store.delete_profile(profile.id)
    with pytest.raises(EvidenceError) as caught:
        service.cleanup(ticket)
    assert caught.value.code == 'CLEANUP_PENDING'
    assert canary.read_bytes() == b'Outside bytes must survive'
    assert collection in dense.names()
    assert [pending.id for pending in store.pending_cleanup()] == [ticket.id]


def test_accepted_remote_write_outlives_local_producer_and_never_false_completes_cleanup(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Synthetic delayed Python vector')
    release = threading.Event()
    receivers, remote_errors = [], []
    original = dense.create
    def disconnected(*args):
        def receiver():
            try:
                assert release.wait(5)
                original(*args)
            except BaseException as exc:
                remote_errors.append(exc)
        receiver_thread = threading.Thread(target=receiver)
        receivers.append(receiver_thread)
        receiver_thread.start()
        raise OSError('Synthetic disconnect after remote acceptance')
    dense.create = disconnected
    try:
        with pytest.raises(EvidenceError) as caught:
            service.build(profile.id, 'facts')
        assert caught.value.code == 'CLEANUP_PENDING'
        intent = service.generations.list()[0]
        assert intent.state == 'cleanup_pending'
        assert intent.dense_collection not in dense.names()
        ticket = store.delete_profile(profile.id)
        with pytest.raises(EvidenceError) as caught:
            service.cleanup(ticket)
        assert caught.value.code == 'CLEANUP_PENDING'
        assert store.pending_cleanup() and service.generations.list()[0].state != 'cleaned'
    finally:
        release.set()
        for receiver in receivers:
            receiver.join(5)
    assert not remote_errors
    assert intent.dense_collection in dense.names()
    service.recover()
    assert intent.dense_collection not in dense.names()
    assert store.pending_cleanup() and service.generations.list()[0].state == 'cleanup_pending'


@pytest.mark.parametrize('after_partial_write', [False, True])
def test_failed_dense_call_cannot_infer_unsent_from_absence(setup, after_partial_write):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Synthetic Python evidence')
    original = dense.create
    def failed(*args):
        with store._tx() as db:
            assert db.execute('SELECT state FROM dense_mutations').fetchone()[0] == 'inflight'
        if after_partial_write:
            original(*args)
        raise OSError('Synthetic opaque adapter failure')
    dense.create = failed
    with pytest.raises(EvidenceError) as caught:
        service.build(profile.id, 'facts')
    assert caught.value.code == 'CLEANUP_PENDING'
    intent = service.generations.list()[0]
    assert intent.state == 'cleanup_pending'
    assert not dense.names()
    assert service.jobs.get(profile.id, intent.job_id).state == 'failed'
    with store._tx() as db:
        assert db.execute('SELECT state FROM dense_mutations').fetchone()[0] == 'indeterminate'
    assert service.recover()['cleanup_pending'] == 1
    assert service.generations.list()[0].state == 'cleanup_pending'


def test_failure_before_dense_envelope_has_no_remote_ambiguity(setup):
    store, _, service, profile = setup
    store.confirm_fact(profile.id, 'Synthetic Python evidence')
    def failed(_texts):
        raise EvidenceError('MODEL_NOT_READY', 'Synthetic embedding failure', 503)
    service.models.embed = failed
    with pytest.raises(EvidenceError, match='embedding'):
        service.build(profile.id, 'facts')
    assert service.generations.list()[0].state == 'cleaned'
    with store._tx() as db:
        assert db.execute('SELECT state FROM dense_mutations').fetchone()[0] == 'not_attempted'
    assert service.recover()['cleanup_pending'] == 0


def test_cleanup_only_late_acknowledgement_preserves_newer_generation_and_never_revives_job(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Synthetic Python evidence')
    original_create = dense.create
    def disconnected(*_args):
        raise OSError('Synthetic unresolved remote write')
    dense.create = disconnected
    with pytest.raises(EvidenceError):
        service.build(profile.id, 'facts')
    old = service.generations.list()[0]
    dense.create = original_create
    newer = service.build(profile.id, 'facts')
    with pytest.raises(EvidenceError, match='scope mismatch'):
        service.generations.dense_result(old.generation_id, old.fence + 1, True)
    # Inject a trusted complete-envelope receipt, not an absence-based reconciliation.
    service.generations.dense_result(old.generation_id, old.fence, True)
    assert service.jobs.get(profile.id, old.job_id).state == 'failed'
    assert service.generations.list()[0].state == 'cleanup_pending'
    assert store.active_manifest(profile.id, 'facts') == newer
    assert service.recover()['cleanup_pending'] == 0
    assert service.generations.list()[0].state == 'cleaned'
    assert newer.dense_collection in dense.names()
    assert service.search(profile.id, 'facts', 'Python')
    with pytest.raises(EvidenceError, match='unresolved envelope'):
        service.generations.dense_result(old.generation_id, old.fence, True)


def test_successful_original_envelope_can_ack_after_profile_forget_without_restoring_content(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Synthetic private Python evidence')
    entered, release = threading.Event(), threading.Event()
    original = dense.create
    def held(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    dense.create = held
    thread, errors = run_background(lambda: service.build(profile.id, 'facts'))
    assert entered.wait(5)
    try:
        intent = service.generations.list()[0]
        with store._tx() as db:
            assert db.execute('SELECT state FROM dense_mutations').fetchone()[0] == 'inflight'
        ticket = store.delete_profile(profile.id)
        with pytest.raises(EvidenceError):
            service.cleanup(ticket)
    finally:
        release.set()
        thread.join(5)
    assert errors and not thread.is_alive()
    with store._tx() as db:
        assert db.execute('SELECT fence,state FROM dense_mutations').fetchone() == (intent.fence, 'acknowledged')
        assert db.execute('SELECT captured FROM generation_intents').fetchone()[0] is None
        assert not db.execute('SELECT 1 FROM generation_chunks').fetchone()
        assert not db.execute('SELECT 1 FROM profiles').fetchone()
    service.recover()
    assert not store.pending_cleanup() and not dense.names()
    assert service.generations.list()[0].state == 'cleaned'


def test_crash_during_dense_envelope_preserves_ambiguity_after_restart(setup):
    store, dense, service, profile = setup
    store.confirm_fact(profile.id, 'Synthetic Python evidence')
    def interrupted(*_args):
        raise KeyboardInterrupt()
    dense.create = interrupted
    with pytest.raises(KeyboardInterrupt):
        service.build(profile.id, 'facts')
    intent = service.generations.list()[0]
    # A process kill would leave inflight; exception handling makes uncertainty explicit.
    with store._tx() as db:
        db.execute("UPDATE dense_mutations SET state='inflight' WHERE generation_id=?", (intent.generation_id,))
    service.jobs.cancel(profile.id, intent.job_id)
    restarted_store = Store(store.root)
    restarted = EvidenceService(restarted_store, service.index_root, dense, service.models)
    assert restarted.recover()['cleanup_pending'] == 1
    assert restarted.generations.list()[0].state == 'cleanup_pending'
    assert not dense.names()
