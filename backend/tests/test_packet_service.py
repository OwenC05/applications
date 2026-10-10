"""Durable unpaid packets with synthetic originals/models, real local indexes."""
import json
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_scoped_retrieval import build, runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain.contracts import Question
from copilot.retrieval.packet_runtime_contracts import PacketRequest, PacketSelection
from copilot.retrieval.packet_service import PacketService
from copilot.worker import Worker


@pytest.fixture
def packets(runtime):  # noqa: F811
    store, research, repo, app, scope, evidence = runtime
    # Canonical questions belong to captured input, not a client packet request.
    with store._tx() as db:
        app = app.model_copy(update={'questions': (
            Question(id='writing', text='Explain finance experience; do not invent Python.', type='writing', max_words=200, constraint_origin='user'),
            Question(id='manual', text='Upload a document', type='file', constraint_origin='user'),)})
        repo._save(db, scope.profile_id, 'application', app)
    evidence.build(scope.profile_id, 'facts')  # Registered empty facts generation.
    build(evidence, scope)
    service = PacketService(store, evidence, jobs=research.jobs)
    return store, research, repo, app, scope, evidence, service


def request(service, scope, key='packets', **changes):
    with service.store._read() as db:
        revisions = service.jobs.capture(db, scope.profile_id, scope.application_id)
    return PacketRequest(idempotency_key=key, expected_facts_revision=revisions.facts,
        expected_input_revision=revisions.application_input, expected_research_revision=revisions.research,
        **changes)


def queued(packets, **changes):
    _, _, _, _, scope, _, service = packets
    body = request(service, scope, **changes)
    job = service.enqueue(scope.profile_id, scope.application_id, scope.research_run_id, body)
    return job, body


def run(packets, **changes):
    *_, service = packets
    job, body = queued(packets, **changes)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    return service.run_packets(job.id, worker, claimed.fence), job, body


def test_durable_empty_facts_bindings_exact_questions_and_restart(packets):
    store, _, repo, app, scope, _, service = packets
    before = repo.application(scope.profile_id, app.application_id)
    batch, job, body = run(packets)
    assert batch.facts_generation.generation_id != batch.employer_generation.generation_id
    assert batch.packets[0].question == app.questions[0]
    assert batch.packets[0].facts == ()
    assert batch.manual_requirements[0].question_id == 'manual'
    assert repo.application(scope.profile_id, app.application_id) == before
    inspection = PacketService(store).detail(scope.profile_id, app.application_id, batch.id)
    assert inspection['current'] and not inspection['review_eligible'] and not inspection['browser_eligible']
    assert inspection['semantic_support'] == 'not_assessed'
    assert service.jobs.get(scope.profile_id, job.id).state == 'completed'
    assert service.enqueue(scope.profile_id, app.application_id, scope.research_run_id, body).id == job.id
    assert PacketService(store).question(scope.profile_id, app.application_id, batch.id, 'manual')['manual_requirement']
    with pytest.raises(EvidenceError):
        PacketService(store).question(scope.profile_id, app.application_id, batch.id, 'foreign')


@pytest.mark.parametrize('change', [dict(per_packet_budget=6001), dict(aggregate_budget=24001),
    dict(expected_facts_revision=True), dict(selections=[dict(source_id=str(uuid4()), unit_id=str(uuid4()), start=0, end=1, text='injected')]),
    dict(generation_id=str(uuid4())), dict(criteria=['injected']), dict(per_packet_budget=0),
    dict(cover_letter_target=dict(text='x' * 2001, constraint_origin='user'))])
def test_request_strictness(change):
    payload = dict(schema_version=1, idempotency_key='strict', expected_facts_revision=0,
                   expected_input_revision=0, expected_research_revision=1)
    with pytest.raises(ValidationError):
        PacketRequest.model_validate_json(json.dumps(payload | change))


def test_intent_precedes_tokenization_and_no_writer_held(packets, monkeypatch):
    store, _, _, _, scope, evidence, service = packets
    tokenize = evidence.models.tokenize_offsets
    calls = []
    def checked(text):
        with store._tx() as db:
            row = db.execute("SELECT data FROM records WHERE kind='workspace:packet_intent'").fetchone()
            assert row and json.loads(row[0])['state'] == 'registered'
        calls.append(text)
        return tokenize(text)
    monkeypatch.setattr(evidence.models, 'tokenize_offsets', checked)
    batch, _, _ = run(packets)
    assert calls and batch.research_run_id == scope.research_run_id


@pytest.mark.parametrize('phase', ['tokenize', 'search', 'final_fingerprint', 'original'])
def test_cancel_or_original_change_blocks_publication(packets, monkeypatch, phase):
    store, _, _, _, scope, evidence, service = packets
    job, _ = queued(packets)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    if phase in ('tokenize', 'search'):
        target, attr = (evidence.models, 'tokenize_offsets') if phase == 'tokenize' else (evidence, 'search_scoped')
        original = getattr(target, attr)
        def cancelled(*args, **kwargs):
            service.jobs.cancel(scope.profile_id, job.id)
            return original(*args, **kwargs)
        monkeypatch.setattr(target, attr, cancelled)
    elif phase == 'final_fingerprint':
        original = evidence.search_scoped
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            evidence.models.fingerprint = 'f' * 64
            return result
        monkeypatch.setattr(evidence, 'search_scoped', changed)
    else:
        original = store.verify_scoped_sources
        def changed(binding_scope, values):
            if binding_scope.corpus == 'employer':
                store._blob(values[0]['source']['id']).write_bytes(b'tampered-original')
            return original(binding_scope, values)
        monkeypatch.setattr(store, 'verify_scoped_sources', changed)
    with pytest.raises(EvidenceError):
        service.run_packets(job.id, worker, claimed.fence)
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:packet_batch'").fetchone()
        assert not db.execute('SELECT 1 FROM job_stages WHERE job_id=?', (job.id,)).fetchone()
        intent = json.loads(db.execute("SELECT data FROM records WHERE kind='workspace:packet_intent'").fetchone()[0])
        assert intent['state'] == 'failed'


def test_atomic_rollback(packets, monkeypatch):
    store, _, _, _, scope, _, service = packets
    save = service._save_new
    def fail(db, owner, kind, record):
        save(db, owner, kind, record)
        if kind == 'packet_dependency':
            raise RuntimeError('injected publication rollback')
    monkeypatch.setattr(service, '_save_new', fail)
    with pytest.raises(RuntimeError):
        run(packets)
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind IN ('workspace:packet_batch','workspace:packet_dependency')").fetchone()
        assert not db.execute("SELECT 1 FROM jobs WHERE state='completed' AND json_extract(data,'$.kind')='packets'").fetchone()


@pytest.mark.parametrize('change', ['facts', 'head', 'expiry', 'generation'])
def test_known_stale_history_is_inspectable(packets, change):
    store, _, _, app, scope, _, service = packets
    batch, _, _ = run(packets)
    if change == 'facts':
        store.confirm_fact(scope.profile_id, 'New confirmed fact')
    elif change == 'expiry':
        service.jobs.clock = lambda: batch.created_at + timedelta(days=8)
    else:
        with store._tx() as db:
            if change == 'head':
                db.execute('UPDATE research_heads SET run_id=?', (str(uuid4()),))
            else:
                db.execute("UPDATE manifests SET state='retired' WHERE corpus='facts'")
    detail = service.detail(scope.profile_id, app.application_id, batch.id)
    assert not detail['current'] and detail['batch']['id'] == batch.id


@pytest.mark.parametrize('change', ['batch', 'report', 'stage', 'intent', 'job'])
def test_corrupt_immutable_association_never_stale_success(packets, change):
    store, _, _, app, scope, _, service = packets
    batch, job, _ = run(packets)
    with store._tx() as db:
        if change == 'batch':
            db.execute("UPDATE records SET data=json_set(data,'$.tokenizer_version','tampered') WHERE id=?", (batch.id,))
        elif change == 'report':
            db.execute("UPDATE records SET data=json_set(data,'$.report.compiler_gaps',json('[\"no_sources\"]')) WHERE kind='workspace:packet_dependency'")
        elif change == 'stage':
            db.execute("UPDATE job_stages SET data='{}' WHERE job_id=?", (job.id,))
        elif change == 'intent':
            db.execute("UPDATE records SET data=json_set(data,'$.state','failed') WHERE kind='workspace:packet_intent'")
        else:
            db.execute("UPDATE jobs SET data=json_set(data,'$.state','cancelled') WHERE id=?", (job.id,))
    with pytest.raises(EvidenceError):
        service.detail(scope.profile_id, app.application_id, batch.id)


def test_revocation_scrubs_all_packet_derivatives(packets):
    store, _, _, _, scope, _, service = packets
    # New fact before enqueue changes facts index; rebuild it.
    fact = store.confirm_fact(scope.profile_id, 'PERSONAL-REVOCATION-CANARY')
    service.evidence.build(scope.profile_id, 'facts')
    _, completed, _ = run(packets)
    queued_job, _ = queued(packets, key='queued')
    store.revoke_fact(scope.profile_id, fact.id)
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind IN ('workspace:packet_batch','workspace:packet_dependency','workspace:packet_intent')").fetchone()
        for job in (completed, queued_job):
            assert db.execute('SELECT parameters,dependencies FROM jobs WHERE id=?', (job.id,)).fetchone() == ('{}', '[]')
            assert not db.execute('SELECT 1 FROM job_stages WHERE job_id=?', (job.id,)).fetchone()
            assert not db.execute('SELECT 1 FROM job_keys WHERE job_id=?', (job.id,)).fetchone()
    assert service.jobs.get(scope.profile_id, queued_job.id).state == 'cancelled'


def test_injected_worker_blocked_local_work_keeps_cancel_responsive(packets):
    _, _, _, _, scope, evidence, service = packets
    job, _ = queued(packets)
    entered, release = threading.Event(), threading.Event()
    original = evidence.search_scoped
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    evidence.search_scoped = blocked
    worker = Worker(service.store, lambda: pytest.fail('unwanted service initialization'),
                    packet_factory=lambda store, jobs: PacketService(store, evidence, jobs=jobs))
    thread = threading.Thread(target=lambda: worker.run_once(job.id, recovery=False))
    thread.start()
    try:
        assert entered.wait(10)
        assert service.jobs.get(scope.profile_id, job.id).state == 'running'
        assert service.jobs.cancel(scope.profile_id, job.id).state == 'cancelled'
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert worker.last_outcome['job']['error'] == 'CONFLICT'


@pytest.mark.parametrize('phase', ['before', 'tokenize', 'search'])
def test_irrelevant_revisions_never_rebase_or_invalidate_packet_capture(packets, monkeypatch, phase):
    store, _, _, app, scope, evidence, service = packets
    job, _ = queued(packets)
    changed = False
    def edit():
        nonlocal changed
        if changed:
            return
        changed = True
        with store._tx() as db:
            db.execute('UPDATE profile_revisions SET metadata=metadata+1,documents=documents+1,consent=consent+1 WHERE owner=?', (scope.profile_id,))
            db.execute("UPDATE records SET data=json_set(data,'$.output_revision',99) WHERE id=?", (app.application_id,))
    if phase == 'before':
        edit()
    else:
        target, name = (evidence.models, 'tokenize_offsets') if phase == 'tokenize' else (evidence, 'search_scoped')
        original = getattr(target, name)
        def callback(*args, **kwargs):
            edit()
            return original(*args, **kwargs)
        monkeypatch.setattr(target, name, callback)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    batch = service.run_packets(job.id, worker, claimed.fence)
    assert batch.revisions == job.revisions
    assert service.detail(scope.profile_id, app.application_id, batch.id)['current']
    with store._read() as db:
        assert service.jobs.capture(db, scope.profile_id, app.application_id).application_output == 99


@pytest.mark.parametrize('phase', ['before', 'tokenize', 'search', 'final_save'])
def test_same_revision_generation_replacement_never_rebases(packets, monkeypatch, phase):
    store, _, _, _, scope, evidence, service = packets
    job, _ = queued(packets)
    replaced = False
    def replace():
        nonlocal replaced
        if not replaced:
            replaced = True
            evidence.build(scope.profile_id, 'facts')
    if phase == 'before':
        replace()
    elif phase in ('tokenize', 'search'):
        target, name = (evidence.models, 'tokenize_offsets') if phase == 'tokenize' else (evidence, 'search_scoped')
        original = getattr(target, name)
        def callback(*args, **kwargs):
            replace()
            return original(*args, **kwargs)
        monkeypatch.setattr(target, name, callback)
    else:
        original = service.jobs.commit_stage
        def commit(*args, **kwargs):
            if args[3] == 'packets_published':
                replace()
            return original(*args, **kwargs)
        monkeypatch.setattr(service.jobs, 'commit_stage', commit)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    with pytest.raises(EvidenceError):
        service.run_packets(job.id, worker, claimed.fence)
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:packet_batch'").fetchone()


@pytest.mark.parametrize('change', ['expiry', 'input', 'head', 'reclaim', 'application_delete', 'profile_delete'])
def test_guarded_authority_losses_during_composition(packets, monkeypatch, change):
    store, _, repo, app, scope, evidence, service = packets
    job, _ = queued(packets)
    original = evidence.models.tokenize_offsets
    changed = False
    def callback(text):
        nonlocal changed
        if not changed:
            changed = True
            if change == 'expiry':
                now = service.jobs._now()
                service.jobs.clock = lambda: now + timedelta(days=8)
            elif change == 'reclaim':
                now = service.jobs._now()
                service.jobs.clock = lambda: now + timedelta(seconds=61)
                assert service.jobs.claim(str(uuid4()), job_id=job.id).fence == 2
            elif change == 'application_delete':
                from copilot.domain.repository import ApplicationPatch
                repo.delete_application(scope.profile_id, app.application_id, ApplicationPatch(
                    expected_input_revision=0, expected_output_revision=app.output_revision))
            elif change == 'profile_delete':
                store.delete_profile(scope.profile_id)
            else:
                with store._tx() as db:
                    if change == 'input':
                        db.execute("UPDATE records SET data=json_set(data,'$.input_revision',1) WHERE id=?", (app.application_id,))
                    else:
                        db.execute('UPDATE research_heads SET run_id=?', (str(uuid4()),))
        return original(text)
    monkeypatch.setattr(evidence.models, 'tokenize_offsets', callback)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    with pytest.raises(EvidenceError) as failure:
        service.run_packets(job.id, worker, claimed.fence)
    assert failure.value.code in ('CONFLICT', 'NOT_FOUND')
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:packet_batch'").fetchone()


def test_server_bound_exact_quotes_complete_compiler_omissions_and_cover(packets):
    from copilot.retrieval.packet_contracts import CoverLetterTarget
    store, _, _, app, scope, _, service = packets
    with store._read() as db:
        _, values = store.capture_scoped_sources(db, scope, service.jobs._now())
    item = values[0]
    selection = PacketSelection(source_id=item['source']['id'], unit_id=item['unit']['id'],
                                start=0, end=len(item['unit']['text']))
    cover = CoverLetterTarget(text='Write a cover letter without inventing experience.', constraint_origin='user')
    batch, _, _ = run(packets, selections=(selection, selection,
        PacketSelection(source_id=str(uuid4()), unit_id=str(uuid4()), start=0, end=1)), cover_letter_target=cover)
    assert batch.hiring_criteria[0].text == item['unit']['text']
    assert batch.hiring_criteria[0].inferred is False
    assert batch.hiring_criteria[0].evidence[0].generation_id == batch.employer_generation.generation_id
    assert batch.cover_letter_target.max_words == 400
    assert [p.question.id for p in batch.packets] == ['writing', 'cover_letter']
    detail = service.detail(scope.profile_id, app.application_id, batch.id)
    report = detail['publication']['report']
    assert [o['reason'] for o in report['compiler_omissions']] == ['duplicate_span', 'unknown_source']


def test_binder_exposes_cross_chunk_omission_never_crops(packets):
    from dataclasses import asdict, replace

    from copilot.contracts import decode_chunk
    from copilot.retrieval.packet_runtime_contracts import PacketParameters
    from copilot.store import digest
    store, _, _, _, scope, _, service = packets
    with store._read() as db:
        _, values = store.capture_scoped_sources(db, scope, service.jobs._now())
    item = values[0]
    selection = PacketSelection(source_id=item['source']['id'], unit_id=item['unit']['id'],
                                start=0, end=len(item['unit']['text']))
    job, _ = queued(packets, selections=(selection,))
    with store._tx() as db:
        raw = service.jobs._job(db, job.id)[1]
        parameters = PacketParameters.model_validate_json(json.dumps(raw))
        generation = parameters.employer_generation.generation_id
        rows = db.execute('SELECT id,data FROM generation_chunks WHERE generation_id=?', (generation,)).fetchall()
        for id, data in rows:
            chunk = decode_chunk(json.loads(data))
            if chunk.unit_id != selection.unit_id:
                continue
            db.execute('DELETE FROM generation_chunks WHERE generation_id=? AND id=?', (generation, id))
            midpoint = len(chunk.text) // 2
            for start, end in ((0, midpoint), (midpoint, len(chunk.text))):
                text = chunk.text[start:end]
                part = replace(chunk, id=str(uuid4()), start=start, end=end, text=text, text_sha256=digest(text))
                db.execute('INSERT INTO generation_chunks VALUES(?,?,?,?)', (generation, part.id, scope.profile_id, json.dumps(asdict(part))))
        criteria, report = service._criteria(db, parameters, values)
    assert criteria == ()
    assert report.binder_omissions[0].selection_indices == (0,)
    assert report.binder_omissions[0].reason == 'not_in_generation_chunk'


def test_api_get_enqueue_no_model_initialization_and_scope_isolation(packets, tmp_path):
    from fastapi.testclient import TestClient

    from copilot.api import create_app
    from copilot.config import Settings
    store, _, _, app, scope, _, service = packets
    batch, job, body = run(packets)
    def forbidden(*args):
        pytest.fail('Inspection/enqueue cannot initialize model, Chroma, query or provider')
    api = create_app(Settings(store.root, tmp_path / 'models'), forbidden)
    route = f'/api/workspace/profiles/{scope.profile_id}/applications/{app.application_id}'
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/evidence/status').json()['token']
        assert client.get(route + '/packets').json()['batches'][0]['current']
        assert client.get(route + '/packets/' + batch.id).json()['batch_sha256'] == batch.batch_sha256
        assert client.get(route + '/packets/' + batch.id + '/questions/manual').json()['question'] == app.questions[1].model_dump(mode='json')
        assert client.get(route + '/packets/' + batch.id + '/questions/foreign').status_code == 404
        foreign = route.replace(app.application_id, str(uuid4()))
        assert client.get(foreign + '/packets/' + batch.id).status_code == 404
        response = client.post(route + f'/research/{scope.research_run_id}/packets', json=body.model_dump(mode='json'))
        assert response.status_code == 202 and response.json()['job']['id'] == job.id
        assert client.post(route + f'/research/{scope.research_run_id}/packets', json=body.model_dump(mode='json') | {'criteria': ['injected']}).status_code == 422
        store.confirm_fact(scope.profile_id, 'Change revision but allow exact replay')
        assert client.post(route + f'/research/{scope.research_run_id}/packets', json=body.model_dump(mode='json')).json()['job']['id'] == job.id
        assert client.post(route + f'/research/{scope.research_run_id}/packets', json=body.model_dump(mode='json') | {'aggregate_budget': 23000}).status_code == 409
    assert api.state.service is None


def test_actual_http_status_cancel_and_get_responsive_during_blocked_local_retrieval(packets, tmp_path):
    import socket
    import time

    import httpx
    import uvicorn

    from copilot.api import create_app
    from copilot.config import Settings
    store, _, _, app, scope, evidence, service = packets
    # Real loopback server, not an in-process ASGI shortcut.
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    def forbidden(*args):
        pytest.fail('Control API must not initialize retrieval/model/provider')
    api = create_app(Settings(store.root, tmp_path / 'models', port=port), forbidden)
    entered, release = threading.Event(), threading.Event()
    original = evidence.search_scoped
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    evidence.search_scoped = blocked
    worker = Worker(store, forbidden, lease_seconds=5, heartbeat_interval=0.1,
                    packet_factory=lambda s, jobs: PacketService(s, evidence, jobs=jobs))
    server = uvicorn.Server(uvicorn.Config(api, host='127.0.0.1', port=port,
                                         access_log=False, log_level='critical'))
    server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    server_thread.start()
    worker_thread = None
    base = f'/api/workspace/profiles/{scope.profile_id}'
    app_base = base + f'/applications/{app.application_id}'
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False) as client:
            client.headers['x-evidence-token'] = client.get('/api/evidence/status').json()['token']
            response = client.post(app_base + f'/research/{scope.research_run_id}/packets',
                                   json=request(service, scope).model_dump(mode='json'))
            assert response.status_code == 202
            job_id = response.json()['job']['id']
            worker_thread = threading.Thread(target=lambda: worker.run_once(job_id, recovery=False))
            worker_thread.start()
            assert entered.wait(5)
            started = time.monotonic()
            assert client.get('/api/evidence/status').status_code == 200
            assert client.get(app_base + '/packets').json()['batches'] == []
            assert client.get(base + '/jobs/' + job_id).json()['job']['state'] == 'running'
            assert client.post(base + '/jobs/' + job_id + '/cancel', json={}).json()['job']['state'] == 'cancelled'
            assert time.monotonic() - started < 1.5
    finally:
        release.set()
        if worker_thread:
            worker_thread.join(10)
        server.should_exit = True
        server_thread.join(5)
        listener.close()
    assert not server_thread.is_alive() and not worker_thread.is_alive()
    assert worker.last_outcome['job']['error'] == 'CONFLICT'
    assert api.state.service is None


@pytest.mark.parametrize('mode', ['source', 'application', 'profile'])
def test_orphan_packet_record_forgetting_uses_owner_app_scope_not_job_graph(packets, mode):
    from copilot.domain.repository import ApplicationPatch
    store, _, repo, app, scope, _, service = packets
    other_owner = store.create_profile('Unrelated owner', ['tech']).id
    other_app = str(uuid4())
    kinds = ('workspace:packet_batch', 'workspace:packet_dependency', 'workspace:packet_intent')
    scopes = [(scope.profile_id, app.application_id), (scope.profile_id, other_app), (other_owner, str(uuid4()))]
    ids = {}
    with store._tx() as db:
        for owner, application_id in scopes:
            for kind in kinds:
                id = str(uuid4())
                # Missing/malformed job association intentionally cannot authorize
                # inspection, but does not exempt retained plaintext from privacy.
                value = dict(id=id, profile_id=owner, application_id=application_id,
                             job_id={'malformed': 'not-an-id'}, text='ORPHAN-PRIVACY-CANARY')
                db.execute('INSERT INTO records VALUES(?,?,?,?)', (id, owner, kind, json.dumps(value)))
                ids[id] = (owner, application_id)
    if mode == 'source':
        source = store.add_source(scope.profile_id, 'fixture.txt', 'text/plain', b'synthetic', ['synthetic'], 'fixture')
        store.delete_source(scope.profile_id, source.id)
    elif mode == 'application':
        repo.delete_application(scope.profile_id, app.application_id, ApplicationPatch(
            expected_input_revision=0, expected_output_revision=app.output_revision))
    else:
        store.delete_profile(scope.profile_id)
    with store._read() as db:
        for id, (owner, application_id) in ids.items():
            erased = owner == scope.profile_id and (mode != 'application' or application_id == app.application_id)
            assert bool(db.execute('SELECT 1 FROM records WHERE id=?', (id,)).fetchone()) is not erased
            if erased:
                assert db.execute('SELECT 1 FROM tombstones WHERE id=?', (id,)).fetchone()
    # No personal source/fact/profile from another owner is removed.
    assert other_owner in {profile.id for profile in store.list_profiles()}


def test_forged_retrieval_reference_fails_closed(packets, monkeypatch):
    from dataclasses import replace
    _, _, _, _, _, evidence, _ = packets
    original = evidence.search_scoped
    def forged(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.references:
            reference = result.references[0].model_copy(update={'generation_id': str(uuid4())})
            return replace(result, references=(reference,))
        return result
    monkeypatch.setattr(evidence, 'search_scoped', forged)
    with pytest.raises(ValueError, match='generation'):
        run(packets)


def test_budget_manual_coverage_preserves_full_questions(packets):
    _, _, _, app, _, _, _ = packets
    batch, _, _ = run(packets, per_packet_budget=1, aggregate_budget=1)
    assert batch.canonical_questions == app.questions
    assert not batch.packets
    assert {m.question_id for m in batch.manual_requirements} == {'writing', 'manual'}
    assert batch.token_count == 0
