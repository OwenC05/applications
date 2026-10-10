"""Real Chroma/BM25 with synthetic models/originals; no semantic-quality claim."""
import json
from datetime import timedelta
from uuid import uuid4

import chromadb
import pytest
from test_retrieval import FixtureModels
from test_scoped_generations import research, second_scope

from copilot.contracts import EvidenceError
from copilot.domain.contracts import Question
from copilot.retrieval import sparse
from copilot.retrieval.dense import DenseIndex
from copilot.retrieval.packet_contracts import CorpusScope
from copilot.retrieval.packets import compose_batch
from copilot.retrieval.service import EvidenceService


@pytest.fixture
def runtime(tmp_path):
    store, research_service, repo, app, scope = research(tmp_path)
    dense = DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma')))
    service = EvidenceService(store, tmp_path / 'indexes', dense, FixtureModels())
    return store, research_service, repo, app, scope, service


def build(service, scope, key=None):
    job = service.jobs.enqueue(scope.profile_id, 'index',
        {'corpus': 'employer', 'research_run_id': scope.research_run_id},
        key or str(uuid4()), scope.application_id)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    return service.run_index(job.id, worker, claimed.fence)


def test_real_scoped_branches_isolate_same_owner_company_before_ranking(runtime, monkeypatch):
    store, research_service, repo, _, a, service = runtime
    b = second_scope(store, research_service, repo, a.profile_id)
    ma, mb = build(service, a), build(service, b)
    assert ma.application_id != mb.application_id
    original_dense, original_sparse = service.dense.query, sparse.query
    observations = []
    def dense(name, vector, limit, source_ids=None, **kwargs):
        scope = kwargs['scope']
        metadata = service.dense.get(name).metadata
        assert metadata['application_id'] == scope.application_id
        assert metadata['research_run_id'] == scope.research_run_id
        assert metadata['generation_id'] == kwargs['generation_id']
        observations.append(('dense', scope.application_id))
        return original_dense(name, vector, limit, source_ids, **kwargs)
    def bm25(engine, meta, text, limit, source_ids=None):
        assert meta['application_id'] in {a.application_id, b.application_id}
        observations.append(('sparse', meta['application_id']))
        return original_sparse(engine, meta, text, limit, source_ids)
    monkeypatch.setattr(service.dense, 'query', dense)
    monkeypatch.setattr(sparse, 'query', bm25)
    for scope in (a, b):
        result = service.search_scoped(scope, ('Café Finance', 'not Python'))
        assert result.references and all(r.application_id == scope.application_id for r in result.references)
        assert result.binding.scope == scope
    assert observations == [(branch, scope.application_id) for scope in (a, b)
                            for _ in range(2) for branch in ('dense', 'sparse')]
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='chunk:employer'").fetchone()


@pytest.mark.parametrize('tamper', ['dense_scope', 'record_scope', 'record_id', 'dense_model', 'dense_ids', 'sparse_scope', 'sparse_ids', 'sparse_model', 'missing_dense', 'missing_sparse'])
def test_corrupt_branch_rejected_before_either_ranks(runtime, monkeypatch, tamper):
    _, _, _, _, scope, service = runtime
    manifest = build(service, scope)
    collection = service.dense.get(manifest.dense_collection)
    path = service.index_root / manifest.sparse_relpath / 'manifest.json'
    if tamper == 'dense_scope':
        collection.modify(metadata={**collection.metadata, 'application_id': str(uuid4())})
    elif tamper == 'record_scope':
        rows = collection.get(include=['metadatas'])
        collection.update(ids=[rows['ids'][0]], metadatas=[{**rows['metadatas'][0], 'research_run_id': str(uuid4())}])
    elif tamper == 'record_id':
        rows = collection.get(include=['metadatas'])
        collection.update(ids=[rows['ids'][0]], metadatas=[{**rows['metadatas'][0], 'record_id': str(uuid4())}])
    elif tamper == 'dense_model':
        collection.modify(metadata={**collection.metadata, 'fingerprint': 'f' * 64})
    elif tamper == 'dense_ids':
        collection.delete(ids=[collection.get(include=[])['ids'][0]])
    elif tamper.startswith('sparse_'):
        meta = json.loads(path.read_text())
        key = {'sparse_scope': 'application_id', 'sparse_ids': 'ids', 'sparse_model': 'fingerprint'}[tamper]
        meta[key] = [] if tamper == 'sparse_ids' else str(uuid4())
        path.write_text(json.dumps(meta))
    elif tamper == 'missing_dense':
        service.dense.delete(manifest.dense_collection)
    else:
        path.unlink()
    monkeypatch.setattr(service.dense, 'query', lambda *a, **k: pytest.fail('dense ranked corrupted scope'))
    monkeypatch.setattr(sparse, 'query', lambda *a, **k: pytest.fail('BM25 ranked corrupted scope'))
    with pytest.raises(EvidenceError):
        service.search_scoped(scope, ['Finance'])


@pytest.mark.parametrize('phase', ['embed', 'rerank'])
@pytest.mark.parametrize('change', ['input', 'head', 'expired', 'original', 'model', 'delete'])
def test_blocking_work_mutations_fail_closed(runtime, monkeypatch, phase, change):
    store, _, repo, app, scope, service = runtime
    manifest = build(service, scope)
    previous = getattr(service.models, phase)
    def race(*args):
        result = previous(*args)
        if change == 'input':
            with store._tx() as db:
                db.execute("UPDATE records SET data=json_set(data,'$.input_revision',1) WHERE id=?", (scope.application_id,))
        elif change == 'head':
            with store._tx() as db:
                db.execute('UPDATE research_heads SET run_id=? WHERE application_id=?', (str(uuid4()), scope.application_id))
        elif change == 'expired':
            now = service.jobs._now()
            service.jobs.clock = lambda: now + timedelta(days=8)
        elif change == 'original':
            with store._read() as db:
                _, values = store.capture_scoped_sources(db, scope, service.jobs._now())
            store._blob(values[0]['source']['id']).write_bytes(b'changed')
        elif change == 'model':
            service.models.fingerprint = 'f' * 64
        else:
            from copilot.domain.repository import ApplicationPatch
            repo.delete_application(scope.profile_id, scope.application_id,
                                    ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
        return result
    monkeypatch.setattr(service.models, phase, race)
    with pytest.raises(EvidenceError):
        service.search_scoped(scope, ['Finance'])
    assert manifest.generation_id


def test_variants_single_capture_binding_and_zero_results(runtime, monkeypatch):
    store, _, _, _, scope, service = runtime
    build(service, scope)
    variants = ['Finance', 'Café']
    original = service.models.embed
    seen = []
    def embed(texts):
        seen.extend(texts)
        variants[:] = ['changed']
        return original(texts)
    monkeypatch.setattr(service.models, 'embed', embed)
    first = service.search_scoped(scope, variants)
    assert seen == ['Finance', 'Café']
    assert first.binding.scope == scope
    with pytest.raises(EvidenceError):
        service.search_scoped(scope, ['Finance'], binding=first.binding.model_copy(update={'generation_id': str(uuid4())}))
    personal = CorpusScope(profile_id=scope.profile_id, corpus='facts')
    service.build(scope.profile_id, 'facts')
    result = service.search_scoped(personal, ['nothing'])
    assert result.binding.scope == personal and result.references == () and result.hits == ()


@pytest.mark.parametrize('queries', [[], ['x'] * 5, [' '], ['x ' * 129]])
def test_query_boundaries(runtime, queries):
    *_, scope, service = runtime
    with pytest.raises(EvidenceError):
        service.search_scoped(scope, queries)


def test_unicode_full_canonical_hash_and_crop_resolution(runtime):
    store, _, _, _, scope, service = runtime
    # Multiple chunks force selected chunk hashes to differ from full canonical hashes.
    text = 'café 😀 e\u0301 not Python ' * 100
    fact = store.confirm_fact(scope.profile_id, text)
    service.build(scope.profile_id, 'facts')
    personal = CorpusScope(profile_id=scope.profile_id, corpus='facts')
    result = service.search_scoped(personal, ['not Python'])
    reference = result.references[0]
    assert reference.span.text_sha256 == fact.text_sha256
    assert reference.span.text_sha256 != result.hits[0]['chunk'].text_sha256
    span = reference.span.model_copy(update={'end': reference.span.start + 5,
                                             'excerpt': text[reference.span.start:reference.span.start + 5]})
    with store._read() as db:
        resolved = store.resolve_evidence_reference(db, result.binding, reference.model_copy(update={'span': span}), service.jobs._now())
        assert resolved['canonical_text'] == text
        bad = span.model_copy(update={'text_sha256': result.hits[0]['chunk'].text_sha256})
        with pytest.raises(EvidenceError):
            store.resolve_evidence_reference(db, result.binding, reference.model_copy(update={'span': bad}), service.jobs._now())


def test_actual_compose_batch_consumes_scoped_service(runtime):
    store, _, repo, app, scope, service = runtime
    store.confirm_fact(scope.profile_id, 'Built synthetic Finance tooling, not Python leadership.')
    service.build(scope.profile_id, 'facts')
    build(service, scope)
    facts = service.search_scoped(CorpusScope(profile_id=scope.profile_id, corpus='facts'), ['Finance']).binding
    employer = service.search_scoped(scope, ['Finance']).binding
    application = repo.application(scope.profile_id, app.application_id).model_copy(update={
        'questions': (Question(id='why', text='Why Finance, not Python?', type='writing', max_words=100, constraint_origin='user'),)})
    with store._read() as db:
        revisions = service.jobs.capture(db, scope.profile_id, scope.application_id)
    batch = compose_batch(application=application, research_run_id=scope.research_run_id,
        job_id=str(uuid4()), fence=1, revisions=revisions, facts_generation=facts,
        employer_generation=employer, hiring_criteria=(), tokenize=service.models.tokenize_offsets,
        tokenizer_version='synthetic', tokenizer_fingerprint=service.models.fingerprint,
        search_scoped=service.search_scoped, currentness_guard=lambda: None,
        created_at=service.jobs._now(), per_packet_budget=24000, aggregate_budget=24000)
    assert batch.packets[0].facts and batch.packets[0].employer
    assert batch.facts_generation == facts and batch.employer_generation == employer


def test_enqueue_status_cancel_never_initialize_api_models_and_exact_replay(runtime, tmp_path):
    from fastapi.testclient import TestClient

    from copilot.api import create_app
    from copilot.config import Settings

    store, _, _, _, scope, _ = runtime
    app = create_app(Settings(store.root, tmp_path / 'models'),
                     service_factory=lambda *a: pytest.fail('API initialized retrieval models'))
    route = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/research/{scope.research_run_id}/indexes'
    body = {'schema_version': 1, 'idempotency_key': 'api-index', 'expected_input_revision': 0,
            'expected_research_revision': 1}
    with TestClient(app, base_url='http://127.0.0.1:3001') as client:
        headers = {'x-evidence-token': app.state.token, 'Origin': 'http://127.0.0.1:3001'}
        response = client.post(route, json=body, headers=headers)
        assert response.status_code == 202, response.text
        job = response.json()['job']
        prefix = f'/api/workspace/profiles/{scope.profile_id}/jobs'
        assert client.get(prefix, headers=headers).status_code == 200
        assert client.get(prefix + '/' + job['id'], headers=headers).status_code == 200
        assert client.post(prefix + '/' + job['id'] + '/cancel', json={'schema_version': 1}, headers=headers).status_code == 200
        with store._tx() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.input_revision',1) WHERE id=?", (scope.application_id,))
        replay = client.post(route, json=body, headers=headers)
        assert replay.status_code == 202 and replay.json()['job']['id'] == job['id']
        assert client.post(route, json={**body, 'expected_input_revision': 1}, headers=headers).status_code == 409
        assert client.post(route, json={**body, 'idempotency_key': 'new'}, headers=headers).status_code == 409
        assert client.post(route, json={**body, 'expected_research_revision': True}, headers=headers).status_code == 422
        assert app.state.service is None


def test_worker_dispatches_actual_employer_build(runtime):
    from copilot.worker import Worker

    store, _, _, _, scope, service = runtime
    job = service.jobs.enqueue(scope.profile_id, 'index',
        {'corpus': 'employer', 'research_run_id': scope.research_run_id}, 'worker-index', scope.application_id)
    worker = Worker(store, lambda: service)
    worker.run_once()
    assert service.jobs.get(scope.profile_id, job.id).state == 'completed'
    assert service.search_scoped(scope, ['Finance']).references


def test_unrelated_changes_preserve_live_search(runtime, monkeypatch):
    from copilot.domain.contracts import ProfilePatch
    from copilot.domain.repository import ConsentInput

    store, research_service, repo, _, scope, service = runtime
    build(service, scope)
    original = service.models.rerank
    mutated = False
    def race(*args):
        nonlocal mutated
        if not mutated:
            with store._read() as db:
                revisions = repo.revisions(db, scope.profile_id)
            repo.patch_profile(scope.profile_id, ProfilePatch(expected_metadata_revision=revisions.metadata, name='Renamed'))
            repo.consent(scope.profile_id, ConsentInput(expected_consent_revision=revisions.consent,
                provider='synthetic', purposes=('research',), granted=True))
            second_scope(store, research_service, repo, scope.profile_id)
            mutated = True
        return original(*args)
    monkeypatch.setattr(service.models, 'rerank', race)
    assert service.search_scoped(scope, ['Finance']).references


def test_cancel_build_preserves_other_scope_personal_and_previous_generation(runtime, monkeypatch):
    store, research_service, repo, _, a, service = runtime
    b = second_scope(store, research_service, repo, a.profile_id)
    previous, other = build(service, a), build(service, b)
    store.confirm_fact(a.profile_id, 'Personal evidence')
    personal = service.build(a.profile_id, 'facts')
    job = service.jobs.enqueue(a.profile_id, 'index', {'corpus': 'employer', 'research_run_id': a.research_run_id}, 'cancel', a.application_id)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    original = service.models.embed
    def cancel(texts):
        service.jobs.cancel(a.profile_id, job.id)
        return original(texts)
    monkeypatch.setattr(service.models, 'embed', cancel)
    with pytest.raises(EvidenceError):
        service.run_index(job.id, worker, claimed.fence)
    assert all(m.dense_collection in service.dense.names() for m in (previous, other, personal))
    with store._read() as db:
        assert store.active_scoped_manifest(db, a, service.jobs._now()) == previous
        assert store.active_scoped_manifest(db, b, service.jobs._now()) == other


def test_indeterminate_dense_build_remains_cleanup_pending(runtime, monkeypatch):
    _, _, _, _, scope, service = runtime
    original = service.dense.create
    def unknown(*args):
        original(*args)
        raise TimeoutError('synthetic unknown response')
    monkeypatch.setattr(service.dense, 'create', unknown)
    with pytest.raises(EvidenceError, match='cleanup is pending'):
        build(service, scope)
    intent = service.generations.list()[0]
    assert intent.state == 'cleanup_pending'
    assert service.generations.dense_pending(intent.generation_id)
    assert intent.dense_collection not in service.dense.names()
    with pytest.raises(EvidenceError):
        service.cleanup_generation(intent.generation_id)


def test_new_run_never_falls_back_to_prior_run_or_other_application(runtime, monkeypatch):
    from test_research_integration import execute

    from copilot.research.service import ResearchRequest

    store, research_service, repo, app, old_scope, service = runtime
    other = second_scope(store, research_service, repo, old_scope.profile_id)
    old, other_manifest = build(service, old_scope), build(service, other)
    execute(research_service, old_scope.profile_id, app, ResearchRequest(idempotency_key='new-run',
            expected_input_revision=0, expected_output_revision=1))
    current = old_scope.model_copy(update={'research_run_id': research_service.list(old_scope.profile_id, app.application_id)['current_run_id']})
    assert current.research_run_id != old_scope.research_run_id
    with pytest.raises(EvidenceError):
        service.search_scoped(old_scope, ['Finance'])
    with pytest.raises(EvidenceError):
        service.search_scoped(current, ['Finance'])
    new = build(service, current)
    assert new.generation_id != old.generation_id
    result = service.search_scoped(current, ['Finance'])
    assert all(r.research_run_id == current.research_run_id for r in result.references)
    assert service.search_scoped(other, ['Finance']).binding.generation_id == other_manifest.generation_id


@pytest.mark.parametrize('change', ['input', 'incomplete', 'expired', 'original', 'delete'])
def test_build_final_eligibility_rejects_changes_and_preserves_other_scope(runtime, monkeypatch, change):
    from copilot.domain.repository import ApplicationPatch

    store, research_service, repo, _, scope, service = runtime
    other = second_scope(store, research_service, repo, scope.profile_id)
    surviving = build(service, other)
    store.confirm_fact(scope.profile_id, 'Personal survives')
    personal = service.build(scope.profile_id, 'facts')
    original = service.models.embed
    def race(texts):
        result = original(texts)
        if change == 'input':
            with store._tx() as db:
                db.execute("UPDATE records SET data=json_set(data,'$.input_revision',1) WHERE id=?", (scope.application_id,))
        elif change == 'incomplete':
            with store._tx() as db:
                db.execute("UPDATE records SET data=json_set(data,'$.state','incomplete') WHERE id=?", (scope.research_run_id,))
        elif change == 'expired':
            now = service.jobs._now()
            service.jobs.clock = lambda: now + timedelta(days=8)
        elif change == 'original':
            with store._read() as db:
                _, values = store.capture_scoped_sources(db, scope, service.jobs._now())
            store._blob(values[0]['source']['id']).write_bytes(b'changed')
        else:
            repo.delete_application(scope.profile_id, scope.application_id,
                ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
        return result
    monkeypatch.setattr(service.models, 'embed', race)
    with pytest.raises(EvidenceError):
        build(service, scope)
    assert all(m.dense_collection in service.dense.names() for m in (surviving, personal))


def test_original_verifications_outside_writer_and_final_equality_recapture(runtime, monkeypatch):
    store, _, _, _, scope, service = runtime
    build(service, scope)
    original = store.verify_scoped_sources
    calls = 0
    def verify(scope, values):
        nonlocal calls
        calls += 1
        original(scope, values)
        # Successful independent writer is direct evidence no outer writer spans IO.
        with store._tx() as db:
            db.execute('UPDATE profiles SET data=data WHERE id=?', (scope.profile_id,))
            if calls == 3:
                db.execute('UPDATE app_revisions SET research=research+1 WHERE application_id=?', (scope.application_id,))
    monkeypatch.setattr(store, 'verify_scoped_sources', verify)
    with pytest.raises(EvidenceError):
        service.search_scoped(scope, ['Finance'])
    assert calls == 3


def test_variants_capture_one_generation_and_reject_publication_during_query(runtime, monkeypatch):
    _, _, _, _, scope, service = runtime
    old = build(service, scope)
    original_query = service.dense.query
    queried = []
    def query(name, *args, **kwargs):
        queried.append(name)
        result = original_query(name, *args, **kwargs)
        if len(queried) == 1:
            build(service, scope)
        return result
    monkeypatch.setattr(service.dense, 'query', query)
    with pytest.raises(EvidenceError):
        service.search_scoped(scope, ['Finance', 'not Python'])
    assert queried == [old.dense_collection] * 2


def test_exact_128_token_and_four_variant_boundary(runtime):
    *_, scope, service = runtime
    build(service, scope)
    result = service.search_scoped(scope, ['Finance ' * 128] * 4)
    assert result.references
    with pytest.raises(EvidenceError):
        service.search_scoped(scope, ['Finance'], limit=True)


def test_model_change_during_build_rejected(runtime, monkeypatch):
    *_, scope, service = runtime
    original = service.models.embed
    def race(texts):
        result = original(texts)
        service.models.fingerprint = 'f' * 64
        return result
    monkeypatch.setattr(service.models, 'embed', race)
    with pytest.raises(EvidenceError, match='Model changed'):
        build(service, scope)
    with service.store._read() as db:
        assert not db.execute('SELECT 1 FROM manifests').fetchone()
