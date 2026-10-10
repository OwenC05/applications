"""S4A1 canonical scoped lifecycle; synthetic originals, no retrieval/model IO."""
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_research_integration import acquire, body, execute, setup

from copilot.contracts import EmployerChunk, EmployerManifest, EvidenceError, decode_manifest
from copilot.generations import Generations
from copilot.research.service import ResearchService
from copilot.retrieval.packet_contracts import CorpusScope
from copilot.store import digest


def research(tmp_path):
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    run = service.list(owner, app.application_id)['current_run_id']
    scope = CorpusScope(profile_id=owner, corpus='employer', application_id=app.application_id, research_run_id=run)
    return store, service, repo, app, scope


def test_scoped_capture_uses_published_counter_and_original_proof(tmp_path):
    store, service, _, _, scope = research(tmp_path)
    with store._read() as db:
        revision, values = store.capture_scoped_sources(db, scope, service.jobs._now())
    assert revision == 1
    assert len(values) == 2
    store.verify_scoped_sources(scope, values)
    with store._tx() as db:
        db.execute('DELETE FROM research_blob_origins WHERE id=?', (values[0]['source']['id'],))
    with store._read() as db, pytest.raises(EvidenceError):
        store.capture_scoped_sources(db, scope, service.jobs._now())


def test_employer_manifest_decode_never_personal_fallback():
    with pytest.raises((ValueError, TypeError)):
        decode_manifest(dict(generation_id=str(uuid4()), profile_id=str(uuid4()), corpus='employer', revision=1,
            model_fingerprint='a' * 64, chunker_version='v1', chunk_ids_sha256='b' * 64,
            chunk_count=0, dense_collection='evidence_x', sparse_relpath='x'))


def test_scoped_registration_staging_and_publication(tmp_path):
    store, service, _, app, scope = research(tmp_path)
    jobs = service.jobs
    generations = Generations(store, jobs)
    job = jobs.enqueue(scope.profile_id, 'index', {'corpus': 'employer', 'research_run_id': scope.research_run_id}, 'index', app.application_id)
    worker = str(uuid4())
    job = jobs.claim(worker, job_id=job.id)
    intent, values = generations.register(job.id, worker, job.fence, str(uuid4()), 'a' * 64, 'v1')
    assert intent.application_id == app.application_id and intent.revisions.research == 1
    def chunks(unit):
        return [EmployerChunk(str(uuid4()), scope.profile_id, 'employer', unit.source_id, unit.id,
            0, len(unit.text), unit.text, digest(unit.text), application_id=scope.application_id,
            research_run_id=scope.research_run_id)]
    snapshot = store.chunk_capture(scope.profile_id, 'employer', 1, values, chunks)
    generations.stage(intent.generation_id, worker, job.fence, snapshot)
    generations.begin_dense(intent.generation_id, worker, job.fence)
    generations.dense_result(intent.generation_id, job.fence, True)
    manifest = EmployerManifest(intent.generation_id, scope.profile_id, 'employer', 1, 'a' * 64, 'v1',
        digest('\n'.join(c.id for c in snapshot.chunks)), len(snapshot.chunks), intent.dense_collection,
        intent.sparse_relpath, application_id=scope.application_id, research_run_id=scope.research_run_id)
    generations.publish(intent.generation_id, worker, job.fence, manifest)
    with store._read() as db:
        assert store.active_scoped_manifest(db, scope, jobs._now()) == manifest
        assert not db.execute("SELECT 1 FROM records WHERE kind='chunk:employer'").fetchone()
    assert decode_manifest(asdict(manifest)) == manifest


def registered(store, service, scope, key='index'):
    generations = Generations(store, service.jobs)
    job = service.jobs.enqueue(scope.profile_id, 'index', {'corpus': 'employer', 'research_run_id': scope.research_run_id},
                               key, scope.application_id)
    worker = str(uuid4())
    job = service.jobs.claim(worker, job_id=job.id)
    intent, values = generations.register(job.id, worker, job.fence, str(uuid4()), 'a' * 64, 'v1')
    return generations, job, worker, intent, values


def staged(store, service, scope, key='index'):
    generations, job, worker, intent, values = registered(store, service, scope, key)
    def chunks(unit):
        return [EmployerChunk(str(uuid4()), scope.profile_id, 'employer', unit.source_id, unit.id, 0,
                              len(unit.text), unit.text, digest(unit.text), application_id=scope.application_id,
                              research_run_id=scope.research_run_id)]
    snapshot = store.chunk_capture(scope.profile_id, 'employer', intent.revisions.research, values, chunks)
    generations.stage(intent.generation_id, worker, job.fence, snapshot)
    manifest = EmployerManifest(intent.generation_id, scope.profile_id, 'employer', intent.revisions.research,
        'a' * 64, 'v1', digest('\n'.join(c.id for c in snapshot.chunks)), len(snapshot.chunks),
        intent.dense_collection, intent.sparse_relpath, application_id=scope.application_id,
        research_run_id=scope.research_run_id)
    return generations, job, worker, intent, snapshot, manifest


def published(store, service, scope, key='index'):
    result = staged(store, service, scope, key)
    generations, job, worker, intent, _, manifest = result
    generations.begin_dense(intent.generation_id, worker, job.fence)
    generations.dense_result(intent.generation_id, job.fence, True)
    generations.publish(intent.generation_id, worker, job.fence, manifest)
    return result


@pytest.mark.parametrize('change', ['head', 'input', 'counter', 'incomplete', 'expired', 'closed', 'scope', 'hash', 'span', 'receipt', 'coverage'])
def test_canonical_eligibility_fails_closed(tmp_path, change):
    import json
    from datetime import timedelta
    store, service, _, _, scope = research(tmp_path)
    now = service.jobs._now()
    with store._tx() as db:
        _, _, values = store.require_current_research(db, scope, now)
        unit = values[0]['unit']
        source = values[0]['source']
        if change == 'head':
            db.execute('UPDATE research_heads SET run_id=?', (str(uuid4()),))
        elif change == 'counter':
            db.execute('UPDATE app_revisions SET research=research+1')
        elif change == 'input':
            db.execute("UPDATE records SET data=json_set(data,'$.input_revision',1) WHERE id=?", (scope.application_id,))
        elif change == 'incomplete':
            db.execute("UPDATE records SET data=json_set(data,'$.state','incomplete') WHERE id=?", (scope.research_run_id,))
        elif change == 'expired':
            now += timedelta(days=8)
        elif change == 'closed':
            db.execute("UPDATE records SET data=json_set(data,'$.role_state','closed') WHERE kind='workspace:employer_source' AND json_extract(data,'$.purpose')='exact_role'")
        elif change == 'scope':
            db.execute("UPDATE records SET data=json_set(data,'$.application_id',?) WHERE id=?", (str(uuid4()), source['id']))
        elif change == 'hash':
            db.execute("UPDATE records SET data=json_set(data,'$.text','changed') WHERE id=?", (unit['id'],))
        elif change == 'span':
            unit['identity_spans'][0]['excerpt'] = 'changed'
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(unit), unit['id']))
        elif change == 'receipt':
            db.execute('DELETE FROM upload_receipts WHERE id=?', (source['id'],))
        elif change == 'coverage':
            db.execute('DELETE FROM records WHERE id=?', (unit['id'],))
    with store._read() as db, pytest.raises(EvidenceError):
        store.capture_scoped_sources(db, scope, now)


def test_original_verification_is_outside_writer_and_canonical_recapture_is_mandatory(tmp_path, monkeypatch):
    store, service, _, _, scope = research(tmp_path)
    original = store.verify_scoped_sources
    def verify(scope, values):
        # A second writer succeeds while byte verification runs.
        original(scope, values)
        with store._tx() as db:
            db.execute('UPDATE app_revisions SET research=research+1 WHERE application_id=?', (scope.application_id,))
    monkeypatch.setattr(store, 'verify_scoped_sources', verify)
    with pytest.raises(EvidenceError):
        registered(store, service, scope)
    assert not Generations(store).list()


@pytest.mark.parametrize('mutation', ['bytes', 'identity', 'missing'])
def test_original_bytes_and_file_identity_required(tmp_path, mutation):
    store, service, _, _, scope = research(tmp_path)
    with store._read() as db:
        _, values = store.capture_scoped_sources(db, scope, service.jobs._now())
    path = store._blob(values[0]['source']['id'])
    raw = path.read_bytes()
    if mutation == 'bytes':
        path.write_bytes(b'changed')
    elif mutation == 'identity':
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(raw)
        replacement.replace(path)
    else:
        path.unlink()
    with pytest.raises(EvidenceError):
        store.verify_scoped_sources(scope, values)


def test_expiry_between_stage_and_publication_keeps_old_generation(tmp_path):
    from datetime import timedelta
    store, service, _, _, scope = research(tmp_path)
    previous = published(store, service, scope)[-1]
    generations, job, worker, intent, _, manifest = staged(store, service, scope, 'rebuild')
    generations.begin_dense(intent.generation_id, worker, job.fence)
    generations.dense_result(intent.generation_id, job.fence, True)
    now = service.jobs._now()
    service.jobs.clock = lambda: now + timedelta(days=8)
    with pytest.raises(EvidenceError):
        generations.publish(intent.generation_id, worker, job.fence, manifest)
    with store._read() as db:
        assert db.execute("SELECT state FROM manifests WHERE id=?", (previous.generation_id,)).fetchone() == ('active',)
    assert generations.list()[-1].state == 'building'


def test_generation_binding_reference_resolves_canonical_hash_and_cropped_span(tmp_path):
    from copilot.domain.contracts import EvidenceReference, SourceSpan
    from copilot.retrieval.packet_contracts import GenerationBinding
    store, service, _, _, scope = research(tmp_path)
    _, _, _, intent, snapshot, manifest = published(store, service, scope)
    chunk = snapshot.chunks[0]
    binding = GenerationBinding(generation_id=intent.generation_id, scope=scope, revision=1,
        model_fingerprint='a' * 64, chunker_version='v1', chunk_checksum=manifest.chunk_ids_sha256)
    reference = EvidenceReference(profile_id=scope.profile_id, kind='employer', generation_id=intent.generation_id,
        application_id=scope.application_id, research_run_id=scope.research_run_id,
        span=SourceSpan(record_id=chunk.record_id, unit_id=chunk.unit_id, start=0, end=3,
                        text_sha256=digest(chunk.text), excerpt=chunk.text[:3]))
    with store._read() as db:
        result = store.resolve_evidence_reference(db, binding, reference, service.jobs._now())
        assert result['canonical_text'] == chunk.text and result['semantic_support'] == 'not_assessed'
        with pytest.raises(EvidenceError):
            store.resolve_evidence_reference(db, binding, reference.model_copy(update={'span': reference.span.model_copy(update={'text_sha256': digest(chunk.text[:3])})}), service.jobs._now())
        with pytest.raises(EvidenceError):
            store.resolve_evidence_reference(db, binding, reference.model_copy(update={'research_run_id': str(uuid4())}), service.jobs._now())
    with store._tx() as db:
        db.execute('DELETE FROM generation_intents WHERE id=?', (intent.generation_id,))
    with store._read() as db, pytest.raises(EvidenceError):
        store.resolve_evidence_reference(db, binding, reference, service.jobs._now())


def second_scope(store, service, repo, owner):
    from copilot.domain.repository import ApplicationInput
    with store._read() as db:
        revision = repo.revisions(db, owner).metadata
    app = repo.create_application(owner, ApplicationInput(expected_metadata_revision=revision,
        company='Café', role='Finance Intern', sector='finance', company_url='https://example.com/company',
        vacancy_url='https://example.com/jobs/42', vacancy_id='42', official_domains=('example.com',)))
    execute(service, owner, app, body(key=str(uuid4())))
    return CorpusScope(profile_id=owner, corpus='employer', application_id=app.application_id,
                       research_run_id=service.list(owner, app.application_id)['current_run_id'])


def test_publication_retires_only_exact_application_and_run(tmp_path):
    store, service, repo, _, scope_a = research(tmp_path)
    scope_b = second_scope(store, service, repo, scope_a.profile_id)
    first = published(store, service, scope_a)[-1]
    other = published(store, service, scope_b, 'b-index')[-1]
    newer = published(store, service, scope_a, 'a-newer')[-1]
    with store._read() as db:
        assert store.active_scoped_manifest(db, scope_a, service.jobs._now()) == newer
        assert store.active_scoped_manifest(db, scope_b, service.jobs._now()) == other
        assert db.execute('SELECT state FROM manifests WHERE id=?', (first.generation_id,)).fetchone() == ('retired',)


def test_delete_captures_published_and_staging_only_exact_employer_scope(tmp_path):
    from copilot.domain.repository import ApplicationPatch
    store, service, repo, _, scope_a = research(tmp_path)
    scope_b = second_scope(store, service, repo, scope_a.profile_id)
    published_a = published(store, service, scope_a)[-1]
    published_b = published(store, service, scope_b, 'b')[-1]
    generations, job, worker, intent, _, _ = staged(store, service, scope_a, 'staging')
    generations.begin_dense(intent.generation_id, worker, job.fence)
    generations.dense_result(intent.generation_id, job.fence, False)
    fact = store.confirm_fact(scope_a.profile_id, 'Personal fact survives')
    result = repo.delete_application(scope_a.profile_id, scope_a.application_id,
                                    ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    assert result.cleanup_pending
    ticket = next(t for t in store.pending_cleanup() if intent.generation_id in t.generation_ids)
    assert set(ticket.generation_ids) == {published_a.generation_id, intent.generation_id}
    assert generations.dense_pending(intent.generation_id)
    with pytest.raises(EvidenceError):
        generations.cleaned(intent.generation_id)
    with pytest.raises(EvidenceError):
        generations.publish(intent.generation_id, worker, job.fence, published_a)
    with store._read() as db:
        assert store.active_scoped_manifest(db, scope_b, service.jobs._now()) == published_b
        assert store._record(db, scope_a.profile_id, fact.id, 'fact')['text'] == fact.text
        assert not db.execute('SELECT 1 FROM generation_chunks WHERE generation_id=?', (intent.generation_id,)).fetchone()
    # Original envelope acknowledgement is cleanup-only after deletion.
    generations.dense_result(intent.generation_id, job.fence, True)
    generations.cleaned(intent.generation_id)
    assert not generations.dense_pending(intent.generation_id)


def test_legacy_personal_wire_and_binding_stay_honest(tmp_path):
    import json

    from copilot.contracts import Chunk, Manifest, Snapshot
    from copilot.domain.contracts import EvidenceReference, SourceSpan
    from copilot.retrieval.packet_contracts import GenerationBinding
    store, service, _, _, scope = research(tmp_path)
    fact = store.confirm_fact(scope.profile_id, 'Personal café')
    personal = CorpusScope(profile_id=scope.profile_id, corpus='facts')
    chunk = Chunk(str(uuid4()), scope.profile_id, 'facts', fact.id, None, 0, len(fact.text), fact.text, fact.text_sha256)
    with store._tx() as db:
        revision = store.scope_revision(db, personal, service.jobs._now())
        manifest = Manifest(str(uuid4()), scope.profile_id, 'facts', revision, 'a' * 64, 'old', digest(chunk.id), 1, 'evidence_legacy', 'legacy')
        store._put(db, scope.profile_id, 'chunk:facts', chunk)
        db.execute('INSERT INTO manifests VALUES(?,?,?,?,?)', (manifest.generation_id, scope.profile_id, 'facts', 'active', json.dumps(asdict(manifest))))
    assert 'application_id' not in asdict(Snapshot(scope.profile_id, 'facts', revision, (chunk,)))
    assert decode_manifest(asdict(manifest)) == manifest
    binding = GenerationBinding(generation_id=manifest.generation_id, scope=personal, revision=revision,
        model_fingerprint='a' * 64, chunker_version='old', chunk_checksum=digest(chunk.id))
    reference = EvidenceReference(profile_id=scope.profile_id, kind='confirmed_fact', generation_id=manifest.generation_id,
        span=SourceSpan(record_id=fact.id, unit_id=None, start=0, end=len(fact.text), text_sha256=fact.text_sha256, excerpt=fact.text))
    with store._read() as db:
        assert store.resolve_evidence_reference(db, binding, reference, service.jobs._now())['integrity'] == 'verified'


def test_unrelated_metadata_consent_and_other_application_do_not_stale_capture(tmp_path):
    from copilot.domain.contracts import ProfilePatch
    from copilot.domain.repository import ConsentInput
    store, service, repo, _, scope = research(tmp_path)
    generations, job, worker, intent, _ = registered(store, service, scope)
    with store._read() as db:
        revisions = repo.revisions(db, scope.profile_id)
    repo.patch_profile(scope.profile_id, ProfilePatch(expected_metadata_revision=revisions.metadata, name='Renamed'))
    repo.consent(scope.profile_id, ConsentInput(expected_consent_revision=revisions.consent, provider='synthetic',
                                              purposes=('research',), granted=True))
    second_scope(store, service, repo, scope.profile_id)
    assert generations.guard(intent.generation_id, worker, job.fence).revisions == intent.revisions


def test_original_changed_after_staging_prevents_publication(tmp_path):
    store, service, _, _, scope = research(tmp_path)
    generations, job, worker, intent, snapshot, manifest = staged(store, service, scope)
    generations.begin_dense(intent.generation_id, worker, job.fence)
    generations.dense_result(intent.generation_id, job.fence, True)
    store._blob(snapshot.chunks[0].record_id).write_bytes(b'changed outside canonical graph')
    with pytest.raises(EvidenceError):
        generations.publish(intent.generation_id, worker, job.fence, manifest)
    with store._read() as db:
        assert not db.execute('SELECT 1 FROM manifests WHERE id=?', (intent.generation_id,)).fetchone()


def test_current_generation_cannot_accept_other_application_snapshot(tmp_path):
    from dataclasses import replace
    store, service, repo, _, scope = research(tmp_path)
    generations, job, worker, intent, values = registered(store, service, scope)
    other = second_scope(store, service, repo, scope.profile_id)
    snapshot = store.chunk_capture(scope.profile_id, 'employer', 1, values, lambda unit: [])
    with pytest.raises(EvidenceError):
        generations.stage(intent.generation_id, worker, job.fence, replace(snapshot, application_id=other.application_id))
    assert generations.list()[0].state == 'registered'


@pytest.mark.parametrize('missing', ['intent', 'mutation'])
def test_cleanup_never_receipts_missing_employer_graph(tmp_path, missing):
    from copilot.domain.repository import ApplicationPatch
    store, service, repo, _, scope = research(tmp_path)
    generations, _, _, intent, _, _ = published(store, service, scope)
    repo.delete_application(scope.profile_id, scope.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    ticket = next(t for t in store.pending_cleanup() if intent.generation_id in t.generation_ids)
    generations.cleaned(intent.generation_id)
    with store._tx() as db:
        if missing == 'intent':
            db.execute('DELETE FROM generation_intents WHERE id=?', (intent.generation_id,))
        else:
            db.execute('DELETE FROM dense_mutations WHERE generation_id=?', (intent.generation_id,))
    with pytest.raises(EvidenceError):
        store.complete_cleanup(ticket.id)
    assert ticket.id in {t.id for t in store.pending_cleanup()}


def test_cancellation_and_reclaim_never_rebase_registered_capture(tmp_path):
    from datetime import timedelta
    store, service, _, _, scope = research(tmp_path)
    generations, job, worker, intent, _ = registered(store, service, scope)
    now = service.jobs._now()
    service.jobs.clock = lambda: now + timedelta(seconds=61)
    next_worker = str(uuid4())
    reclaimed = service.jobs.claim(next_worker, job_id=job.id)
    assert reclaimed.fence == job.fence + 1 and reclaimed.revisions == job.revisions
    with pytest.raises(EvidenceError):
        generations.guard(intent.generation_id, worker, job.fence)
    with pytest.raises(EvidenceError):
        generations.guard(intent.generation_id, next_worker, reclaimed.fence)
    service.jobs.cancel(scope.profile_id, job.id)
    with pytest.raises(EvidenceError):
        generations.register(job.id, next_worker, reclaimed.fence, str(uuid4()), 'a' * 64, 'v1')


def test_employer_job_requires_owned_application_and_exact_parameters(tmp_path):
    store, service, _, _, scope = research(tmp_path)
    for parameters, application in [({'corpus': 'employer'}, scope.application_id),
                                    ({'corpus': 'employer', 'research_run_id': scope.research_run_id}, None),
                                    ({'corpus': 'facts'}, scope.application_id)]:
        with pytest.raises(EvidenceError):
            service.jobs.enqueue(scope.profile_id, 'index', parameters, str(uuid4()), application)
    stranger = store.create_profile('Other', ['finance'])
    with pytest.raises(EvidenceError):
        service.jobs.enqueue(stranger.id, 'index', {'corpus': 'employer', 'research_run_id': scope.research_run_id},
                             str(uuid4()), scope.application_id)


class CleanupDense:
    """Local acknowledgement double for lifecycle tests, not hybrid-index proof."""
    def __init__(self):
        self.collections = {}

    def create(self, name, chunks, vectors, fingerprint, owner, generation_id):
        from types import SimpleNamespace
        self.collections[name] = SimpleNamespace(metadata={'profile_id': owner, 'generation_id': generation_id},
                                                  ids=[c.id for c in chunks], fingerprint=fingerprint)

    def names(self):
        return list(self.collections)

    def get(self, name):
        return self.collections[name]

    def delete(self, name):
        self.collections.pop(name, None)

    def verify(self, name, ids, fingerprint):
        assert self.get(name).ids == ids and self.get(name).fingerprint == fingerprint


def test_existing_recovery_consumes_scoped_lifecycle_without_legacy_fallback(tmp_path):
    from test_retrieval import FixtureModels

    from copilot.domain.repository import ApplicationPatch
    from copilot.retrieval.service import EvidenceService
    store, service, repo, _, scope_a = research(tmp_path)
    scope_b = second_scope(store, service, repo, scope_a.profile_id)
    dense = CleanupDense()
    retrieval = EvidenceService(store, tmp_path / 'indexes', dense, FixtureModels())
    store.confirm_fact(scope_a.profile_id, 'Personal python fact')
    store.add_source(scope_a.profile_id, 'personal.txt', 'text/plain', b'Personal document', ['Personal document'], 'synthetic')
    facts = retrieval.build(scope_a.profile_id, 'facts')
    documents = retrieval.build(scope_a.profile_id, 'documents')
    a = published(store, service, scope_a, 'a-published')[-1]
    b = published(store, service, scope_b, 'b-published')[-1]
    generations, job, worker, uncertain, _, _ = staged(store, service, scope_a, 'a-uncertain')
    generations.begin_dense(uncertain.generation_id, worker, job.fence)
    generations.dense_result(uncertain.generation_id, job.fence, False)
    _, _, _, live_b, _ = registered(store, service, scope_b, 'b-live')
    # Materialize lifecycle-owned artifacts; these are not a claim of real employer ranking.
    for manifest in (a, b):
        dense.create(manifest.dense_collection, [], [], 'a' * 64, scope_a.profile_id, manifest.generation_id)
        (retrieval.index_root / manifest.sparse_relpath).mkdir()
    (retrieval.index_root / uncertain.sparse_relpath).mkdir()
    (retrieval.index_root / live_b.sparse_relpath).mkdir()
    assert retrieval.recover()['cleanup_pending'] == 0
    repo.delete_application(scope_a.profile_id, scope_a.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    assert retrieval.recover()['cleanup_pending'] >= 1
    assert a.dense_collection not in dense.names()
    assert not (retrieval.index_root / a.sparse_relpath).exists()
    assert b.dense_collection in dense.names()
    assert (retrieval.index_root / live_b.sparse_relpath).exists()
    assert facts.dense_collection in dense.names() and documents.dense_collection in dense.names()
    assert generations.dense_pending(uncertain.generation_id)
    generations.dense_result(uncertain.generation_id, job.fence, True)
    assert retrieval.recover()['cleanup_pending'] == 0
    assert not store.pending_cleanup()
    assert b.dense_collection in dense.names() and facts.dense_collection in dense.names()


def test_existing_cleanup_never_treats_unregistered_employer_as_legacy_personal(tmp_path):
    from test_retrieval import FixtureModels

    from copilot.domain.repository import ApplicationPatch
    from copilot.retrieval.service import EvidenceService
    store, service, repo, _, scope = research(tmp_path)
    manifest = published(store, service, scope)[-1]
    dense = CleanupDense()
    retrieval = EvidenceService(store, tmp_path / 'indexes', dense, FixtureModels())
    dense.create(manifest.dense_collection, [], [], 'a' * 64, scope.profile_id, manifest.generation_id)
    directory = retrieval.index_root / manifest.sparse_relpath
    directory.mkdir()
    repo.delete_application(scope.profile_id, scope.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    with store._tx() as db:
        db.execute('DELETE FROM generation_intents WHERE id=?', (manifest.generation_id,))
    assert retrieval.recover()['cleanup_pending'] >= 1
    assert manifest.dense_collection in dense.names() and directory.exists()
    assert store.pending_cleanup()


def test_restart_never_reconstructs_employer_dense_acknowledgement(tmp_path):
    from test_retrieval import FixtureModels

    from copilot.domain.repository import ApplicationPatch
    from copilot.retrieval.service import EvidenceService
    from copilot.store import Store
    store, service, repo, _, scope = research(tmp_path)
    manifest = published(store, service, scope)[-1]
    dense = CleanupDense()
    retrieval = EvidenceService(store, tmp_path / 'indexes', dense, FixtureModels())
    dense.create(manifest.dense_collection, [], [], 'a' * 64, scope.profile_id, manifest.generation_id)
    (retrieval.index_root / manifest.sparse_relpath).mkdir()
    repo.delete_application(scope.profile_id, scope.application_id,
                           ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    with store._tx() as db:
        db.execute('DELETE FROM dense_mutations WHERE generation_id=?', (manifest.generation_id,))
    assert retrieval.recover()['cleanup_pending'] >= 1
    assert store.pending_cleanup()
    restarted = Store(store.root)
    with restarted._read() as db:
        receipt = db.execute('SELECT state FROM dense_mutations WHERE generation_id=?',
                             (manifest.generation_id,)).fetchone()
    assert receipt is None or receipt[0] == 'indeterminate'
    after_restart = EvidenceService(restarted, retrieval.index_root, dense, FixtureModels())
    assert after_restart.recover()['cleanup_pending'] >= 1
    assert restarted.pending_cleanup()


def test_paired_supporting_source_unit_loss_rejects_full_graph(tmp_path):
    store, owner, _, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app, body(supporting_urls=('https://example.com/support',)))
    scope = CorpusScope(profile_id=owner, corpus='employer', application_id=app.application_id,
                        research_run_id=service.list(owner, app.application_id)['current_run_id'])
    with store._read() as db:
        _, values = store.capture_scoped_sources(db, scope, service.jobs._now())
    supporting = next(value for value in values if value['source']['purpose'] == 'supporting')
    with store._tx() as db:
        db.execute('DELETE FROM records WHERE id IN (?,?)',
                   (supporting['source']['id'], supporting['unit']['id']))
    with store._read() as db, pytest.raises(EvidenceError, match='graph'):
        store.require_current_research(db, scope, service.jobs._now())
    with pytest.raises(EvidenceError):
        registered(store, service, scope)
