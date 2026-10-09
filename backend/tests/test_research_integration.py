"""Research integration uses only synthetic acquired capsules, never network."""
import hashlib
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from copilot.contracts import EvidenceError
from copilot.domain.repository import ApplicationInput, ApplicationPatch, DomainRepository
from copilot.research.fetch import AcquiredSource
from copilot.research.service import ResearchRequest, ResearchService
from copilot.store import Store


def setup(tmp_path):
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['finance']).id
    repo = DomainRepository(store)
    app = repo.create_application(owner, ApplicationInput(expected_metadata_revision=0,
        company='Café', role='Finance Intern', sector='finance', company_url='https://example.com/company',
        vacancy_url='https://example.com/jobs/42', vacancy_id='42', official_domains=('example.com',),
        job_description='PRIVATE-JD-CANARY'))
    return store, owner, repo, app


def acquire(url, hosts, budget):
    text = 'Company: Café' if url.endswith('/company') else 'Employer: Café\nRole: Finance Intern\nVacancy ID: 42\nApplications: open'
    raw = text.encode()
    return AcquiredSource(url, url, datetime.now(UTC).isoformat(), 'text/plain', 'identity', 'plain-v1',
                          raw, hashlib.sha256(raw).hexdigest(), text, hashlib.sha256(raw).hexdigest())


def body(key='research', **changes):
    return ResearchRequest(idempotency_key=key, expected_input_revision=0, expected_output_revision=0, **changes)


def execute(service, owner, app, request=None):
    job = service.enqueue(owner, app.application_id, request or body())
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    service.run_research(job.id, worker, claimed.fence)
    return job


def test_research_public_capture_atomic_publication_and_success_retry(tmp_path):
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    job = execute(service, owner, app)
    listing = service.list(owner, app.application_id)
    result = service.detail(owner, app.application_id, listing['current_run_id'])
    assert result['eligibility']['eligible'] and result['run']['state'] == 'complete'
    assert result['run']['revisions']['research'] == 0
    assert repo.application(owner, app.application_id).output_revision == 1
    assert service.enqueue(owner, app.application_id, body()).id == job.id
    with pytest.raises(EvidenceError, match='Idempotency'):
        service.enqueue(owner, app.application_id, body(supporting_urls=('https://example.com/extra',)))
    with store._read() as db:
        params = db.execute('SELECT parameters FROM jobs WHERE id=?', (job.id,)).fetchone()[0]
        assert 'PRIVATE-JD-CANARY' not in params
        assert service.jobs.capture(db, owner, app.application_id).research == 1
        assert service.jobs.capture(db, owner).documents == 0
    for source in result['sources']:
        unit = service.source(owner, app.application_id, result['run']['id'], source['id'])
        for span in unit['identity_spans']:
            assert unit['unit']['text'][span['start']:span['end']] == span['excerpt']
        assert service.original(owner, app.application_id, result['run']['id'], source['id']) == unit['unit']['text'].encode()


def test_recovery_cannot_remove_staging_blobs_before_publication(tmp_path):
    store, owner, _, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    original = store._write_blob
    def write(path, raw):
        original(path, raw)
        report = store.reconcile_blobs()
        assert path.exists() and report['active_uploads'] >= 1
    store._write_blob = write
    execute(service, owner, app)
    assert len(list(store.blobs.iterdir())) == 2


def test_delete_during_acquisition_cannot_publish(tmp_path):
    store, owner, repo, app = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return acquire(*args)
    service = ResearchService(store, acquire_source=blocked)
    job = service.enqueue(owner, app.application_id, body())
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    errors = []
    def run():
        try:
            service.run_research(job.id, worker, claimed.fence)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    repo.delete_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=0))
    release.set()
    thread.join(5)
    assert errors and not thread.is_alive()
    assert not list(store.blobs.iterdir())
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:research_run'").fetchone()


def test_incomplete_refresh_replaces_complete_head_without_fallback(tmp_path):
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    old_id = service.list(owner, app.application_id)['current_run_id']
    def unsupported(url, hosts, budget):
        item = acquire(url, hosts, budget)
        from dataclasses import replace
        raw = b'Unsupported ordinary careers layout'
        return replace(item, original_bytes=raw, original_sha256=hashlib.sha256(raw).hexdigest(),
                       canonical_text=raw.decode(), canonical_sha256=hashlib.sha256(raw).hexdigest())
    service.acquire = unsupported
    execute(service, owner, app, ResearchRequest(idempotency_key='refresh', expected_input_revision=0, expected_output_revision=1))
    head = service.list(owner, app.application_id)['current_run_id']
    assert head != old_id
    assert service.detail(owner, app.application_id, head)['run']['state'] == 'incomplete'
    assert not service.detail(owner, app.application_id, head)['eligibility']['eligible']
    assert not service.detail(owner, app.application_id, old_id)['eligibility']['eligible']
    assert repo.application(owner, app.application_id).output_revision == 2


def test_application_forget_cleans_employer_only_preserves_personal_and_other_app(tmp_path):
    store, owner, repo, app = setup(tmp_path)
    personal = store.add_source(owner, 'personal.txt', 'text/plain', b'private', ['private'], 'plain-v1')
    other = repo.create_application(owner, ApplicationInput(expected_metadata_revision=1,
        company='Café', role='Finance Intern', sector='finance', company_url='https://example.com/company',
        vacancy_url='https://example.com/jobs/42', vacancy_id='42', official_domains=('example.com',)))
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    execute(service, owner, other, body('other'))
    run = service.list(owner, app.application_id)['current_run_id']
    ids = [x['id'] for x in service.detail(owner, app.application_id, run)['sources']]
    deleted = repo.delete_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    assert deleted.cleanup_pending
    for ticket in store.pending_cleanup():
        store.complete_cleanup(ticket.id)
    assert not any(store._blob(sid).exists() for sid in ids)
    assert store.read_source(owner, personal.id) == b'private'
    other_run = service.list(owner, other.application_id)['current_run_id']
    assert service.detail(owner, other.application_id, other_run)['eligibility']['eligible']
    assert len(list(store.blobs.iterdir())) == 3
    with pytest.raises(EvidenceError):
        service.detail(owner, other.application_id, run)


def test_profile_forget_removes_all_employer_originals(tmp_path):
    store, owner, _, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    store.delete_profile(owner)
    for ticket in store.pending_cleanup():
        store.complete_cleanup(ticket.id)
    assert not list(store.blobs.iterdir())


@pytest.mark.parametrize('failure', ['cancel', 'delete', 'reclaim', 'rollback'])
def test_late_written_producer_cannot_publish_after_authority_change(tmp_path, failure):
    from datetime import timedelta
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    job = service.enqueue(owner, app.application_id, body())
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    original = store._write_blob
    called = []
    def write(path, raw):
        original(path, raw)
        if not called:
            called.append(True)
            if failure == 'cancel':
                service.jobs.cancel(owner, job.id)
            elif failure == 'delete':
                repo.delete_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=0))
            elif failure == 'reclaim':
                service.jobs.clock = lambda: claimed.lease_expires_at + timedelta(seconds=1)
                assert service.jobs.claim(str(uuid4()), job_id=job.id).fence > claimed.fence
    store._write_blob = write
    if failure == 'rollback':
        original_save = service.domain._save
        def broken(db, *args, **kwargs):
            original_save(db, *args, **kwargs)
            if args[1] == 'application':
                raise RuntimeError('synthetic commit failure')
        service.domain._save = broken
    with pytest.raises(Exception):
        service.run_research(job.id, worker, claimed.fence)
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind IN ('workspace:research_run','workspace:employer_source')").fetchone()
        assert not db.execute('SELECT 1 FROM research_heads').fetchone()
    store.reconcile_blobs()
    assert not list(store.blobs.iterdir())


def test_original_hash_tamper_and_source_scope_denied(tmp_path):
    store, owner, _, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    run = service.list(owner, app.application_id)['current_run_id']
    source = service.detail(owner, app.application_id, run)['sources'][0]
    store._blob(source['id']).write_bytes(b'tamper')
    with pytest.raises(EvidenceError, match='integrity'):
        service.original(owner, app.application_id, run, source['id'])
    other_owner = store.create_profile('Other', ['tech']).id
    with pytest.raises(EvidenceError):
        service.source(other_owner, app.application_id, run, source['id'])


def test_worker_research_and_http_routes_never_initialize_models(tmp_path):
    from fastapi.testclient import TestClient

    from copilot.api import create_app
    from copilot.config import Settings
    from copilot.worker import Worker
    def forbidden(*args):
        pytest.fail('Research/control paths cannot initialize inference or Chroma')
    api = create_app(Settings(tmp_path / 'api', tmp_path / 'models'), forbidden)
    store = api.state.store
    owner = store.create_profile('Synthetic', ['finance']).id
    repo = DomainRepository(store)
    app = repo.create_application(owner, ApplicationInput(expected_metadata_revision=0,
        company='Café', role='Finance Intern', sector='finance', company_url='https://example.com/company',
        vacancy_url='https://example.com/jobs/42', vacancy_id='42', official_domains=('example.com',)))
    route = f'/api/workspace/profiles/{owner}/applications/{app.application_id}/research'
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/evidence/status').json()['token']
        payload = body().model_dump(mode='json')
        response = client.post(route, json=payload)
        assert response.status_code == 202
        assert 'idempotency_key' not in response.text and 'parameters' not in response.text
        assert client.post(route, content='{}', headers={'content-type': 'text/plain'}).status_code == 415
        assert client.post(route, json={**payload, 'job_description': 'PRIVATE'}).status_code == 422
        assert client.post(route, json=payload, headers={'x-evidence-token': 'bad'}).status_code == 403
        worker = Worker(store, forbidden, research_factory=lambda s, jobs: ResearchService(s, jobs=jobs, acquire_source=acquire))
        assert worker.run_once(response.json()['job']['id'])['state'] == 'completed'
        run = client.get(route).json()['current_run_id']
        detail = client.get(route + '/' + run).json()
        source_id = detail['sources'][0]['id']
        original = client.get(route + '/' + run + '/sources/' + source_id + '/original')
        assert original.status_code == 200 and original.headers['x-content-type-options'] == 'nosniff'
        assert original.headers['content-disposition'].startswith('attachment;')
        assert 'research-original.bin' in original.headers['content-disposition']
        assert client.get(route + '/' + run + '/sources/' + source_id).status_code == 200
        assert client.post(route, json=payload).json()['job']['id'] == response.json()['job']['id']


def test_actual_http_control_responsive_while_research_acquisition_blocks(tmp_path):
    import socket
    import time

    import httpx
    import uvicorn

    from copilot.api import create_app
    from copilot.config import Settings
    from copilot.worker import Worker
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    def forbidden(*args):
        pytest.fail('Research must not initialize inference')
    api = create_app(Settings(tmp_path / 'api', tmp_path / 'models', port=port), forbidden)
    owner = api.state.store.create_profile('Synthetic', ['finance']).id
    application = api.state.repository.create_application(owner, ApplicationInput(expected_metadata_revision=0,
        company='Café', role='Finance Intern', sector='finance', company_url='https://example.com/company',
        vacancy_url='https://example.com/jobs/42', vacancy_id='42', official_domains=('example.com',)))
    entered, release = threading.Event(), threading.Event()
    def blocked(*args):
        entered.set()
        assert release.wait(8)
        return acquire(*args)
    worker = Worker(api.state.store, forbidden, lease_seconds=5, heartbeat_interval=0.1,
                    research_factory=lambda s, jobs: ResearchService(s, jobs=jobs, acquire_source=blocked))
    server = uvicorn.Server(uvicorn.Config(api, host='127.0.0.1', port=port, access_log=False, log_level='critical'))
    server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    server_thread.start()
    worker_thread = None
    base = f'/api/workspace/profiles/{owner}'
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False) as client:
            client.headers['x-evidence-token'] = client.get('/api/evidence/status').json()['token']
            job = client.post(base + f'/applications/{application.application_id}/research', json=body().model_dump(mode='json')).json()['job']
            worker_thread = threading.Thread(target=worker.run_once)
            worker_thread.start()
            assert entered.wait(5)
            started = time.monotonic()
            assert client.get('/api/evidence/status').status_code == 200
            assert client.get(base).status_code == 200
            assert client.get(base + '/jobs/' + job['id']).json()['job']['state'] == 'running'
            assert client.post(base + '/jobs/' + job['id'] + '/cancel', json={}).json()['job']['state'] == 'cancelled'
            assert client.request('DELETE', base + '/applications/' + application.application_id,
                                  json={'expected_input_revision': 0, 'expected_output_revision': 0}).status_code == 200
            assert time.monotonic() - started < 1.5
    finally:
        release.set()
        if worker_thread:
            worker_thread.join(5)
        server.should_exit = True
        server_thread.join(5)
        listener.close()
    assert not server_thread.is_alive() and not worker_thread.is_alive()
    assert not list(api.state.store.blobs.iterdir())


@pytest.mark.parametrize('corruption', ['original_hash', 'canonical_hash', 'span'])
def test_integrity_validation_precedes_any_blob_creation(tmp_path, monkeypatch, corruption):
    from dataclasses import replace

    from copilot.research import service as module
    from copilot.research.broker import research
    store, owner, _, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    def corrupted(inputs, **kwargs):
        result = research(inputs, **kwargs)
        item = result.sources[0]
        if corruption == 'span':
            span = item.identity_spans[0].model_copy(update={'end': 999999})
            item = replace(item, identity_spans=(span,))
        else:
            field = 'original_sha256' if corruption == 'original_hash' else 'canonical_sha256'
            item = replace(item, source=item.source.model_copy(update={field: '0' * 64}))
        return replace(result, sources=(item, *result.sources[1:]))
    monkeypatch.setattr(module, 'research', corrupted)
    with pytest.raises(EvidenceError):
        execute(service, owner, app)
    assert not list(store.blobs.iterdir())


@pytest.mark.parametrize('state', ['not_owned', 'missing_receipt', 'replaced_inode'])
def test_employer_forget_never_deletes_unproven_or_replaced_bytes(tmp_path, state):
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    run = service.list(owner, app.application_id)['current_run_id']
    sid = service.detail(owner, app.application_id, run)['sources'][0]['id']
    path = store._blob(sid)
    if state == 'replaced_inode':
        replacement = path.with_name('replacement')
        replacement.write_bytes(b'outside-canary')
        replacement.replace(path)
    else:
        with store._tx() as db:
            if state == 'not_owned':
                db.execute("UPDATE upload_intents SET state='not_owned' WHERE id=?", (sid,))
            db.execute('DELETE FROM upload_receipts WHERE id=?', (sid,))
    before = path.read_bytes()
    repo.delete_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    store.reconcile_blobs()
    assert path.read_bytes() == before
    if state != 'not_owned':
        with pytest.raises(EvidenceError, match='ownership|identity|cleanup remains pending'):
            store.complete_cleanup(store.pending_cleanup()[0].id)
    else:
        store.complete_cleanup(store.pending_cleanup()[0].id)


@pytest.mark.parametrize('change', ['company', 'company_url', 'official_domains'])
def test_invalid_public_capture_rejected_without_enqueue(tmp_path, change):
    store, owner, repo, app = setup(tmp_path)
    value = {'company': 'x' * 201, 'company_url': None, 'official_domains': ()}[change]
    repo.patch_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0,
                           expected_output_revision=0, **{change: value}))
    service = ResearchService(store, acquire_source=lambda *args: pytest.fail('Invalid capture cannot acquire'))
    with pytest.raises(EvidenceError) as error:
        service.enqueue(owner, app.application_id, ResearchRequest(idempotency_key='bad', expected_input_revision=1, expected_output_revision=1))
    assert error.value.code == 'INVALID_INPUT'
    assert not service.jobs.list(owner)


def test_input_change_and_expiry_block_current_eligibility_not_self_output_bump(tmp_path):
    from datetime import timedelta
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    run = service.list(owner, app.application_id)['current_run_id']
    assert service.detail(owner, app.application_id, run)['eligibility']['eligible']
    clock = service.jobs.clock
    service.jobs.clock = lambda: clock() + timedelta(days=8)
    assert not service.detail(owner, app.application_id, run)['eligibility']['eligible']
    service.jobs.clock = clock
    repo.patch_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1, role='Different Role'))
    assert not service.detail(owner, app.application_id, run)['eligibility']['eligible']


def test_worker_restart_cleans_abandoned_originals_without_touching_published_history(tmp_path):
    store, owner, _, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    old = service.list(owner, app.application_id)['current_run_id']
    job = service.enqueue(owner, app.application_id, ResearchRequest(idempotency_key='crash', expected_input_revision=0, expected_output_revision=1))
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    original = store._write_blob
    def crash(path, raw):
        original(path, raw)
        raise RuntimeError('synthetic process death')
    store._write_blob = crash
    with pytest.raises(RuntimeError):
        service.run_research(job.id, worker, claimed.fence)
    service.jobs.cancel(owner, job.id)
    restarted = Store(store.root)
    restored = ResearchService(restarted, acquire_source=acquire)
    restored.recover()
    restarted.reconcile_blobs()
    restored.recover()
    assert len(list(restarted.blobs.iterdir())) == 2
    assert restored.list(owner, app.application_id)['current_run_id'] == old
    assert restored.detail(owner, app.application_id, old)['eligibility']['eligible']
    with restarted._read() as db:
        assert db.execute('SELECT state FROM research_intents WHERE job_id=?', (job.id,)).fetchone() == ('cleaned',)


def test_revoked_consent_revision_stops_next_acquisition_and_no_fact_promotion(tmp_path):
    store, owner, repo, app = setup(tmp_path)
    calls = []
    def revoke(*args):
        calls.append(args[0])
        with store._tx() as db:
            db.execute('UPDATE profile_revisions SET consent=consent+1 WHERE owner=?', (owner,))
        return acquire(*args)
    service = ResearchService(store, acquire_source=revoke)
    with pytest.raises(Exception):
        execute(service, owner, app)
    assert len(calls) == 1
    assert not list(store.blobs.iterdir())
    with store._read() as db:
        assert service.jobs.capture(db, owner).facts == 0
        assert not db.execute("SELECT 1 FROM records WHERE kind IN ('fact','workspace:proposal')").fetchone()


def test_large_canonical_sources_never_enter_job_stage_receipt(tmp_path):
    from dataclasses import replace
    store, owner, _, app = setup(tmp_path)
    def large(*args):
        item = acquire(*args)
        text = item.canonical_text + '\n' + 'z' * 600_000
        raw = text.encode()
        return replace(item, original_bytes=raw, original_sha256=hashlib.sha256(raw).hexdigest(),
                       canonical_text=text, canonical_sha256=hashlib.sha256(raw).hexdigest())
    service = ResearchService(store, acquire_source=large)
    job = execute(service, owner, app)
    with store._read() as db:
        stage = db.execute('SELECT data FROM job_stages WHERE job_id=?', (job.id,)).fetchone()[0]
        assert len(stage) < 2048 and 'zzzzzz' not in stage
    assert len(list(store.blobs.iterdir())) == 2


def test_research_exclusive_creation_conflict_preserved_immediate_and_restart(tmp_path):
    store, owner, _, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    original = store._write_blob
    paths = []
    def collide(path, raw):
        path.write_bytes(b'unknown-file-canary')
        paths.append(path)
        original(path, raw)
    store._write_blob = collide
    with pytest.raises(FileExistsError):
        execute(service, owner, app)
    store.reconcile_blobs()
    restarted = Store(store.root)
    restarted.reconcile_blobs()
    assert paths[0].read_bytes() == b'unknown-file-canary'
    store.delete_profile(owner)
    for ticket in store.pending_cleanup():
        store.complete_cleanup(ticket.id)
    assert paths[0].read_bytes() == b'unknown-file-canary'


@pytest.mark.parametrize('link', ['missing_intent', 'intent_owner', 'intent_application', 'intent_run',
                                 'intent_job', 'intent_fence', 'missing_scope', 'missing_job', 'missing_origin',
                                 'scope_owner', 'scope_application', 'scope_run', 'origin_job', 'origin_fence'])
@pytest.mark.parametrize('forget', ['application', 'profile'])
def test_broken_original_research_ownership_stays_pending_without_removing_bytes(tmp_path, link, forget):
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    job = execute(service, owner, app)
    run_id = service.list(owner, app.application_id)['current_run_id']
    sid = service.detail(owner, app.application_id, run_id)['sources'][0]['id']
    path = store._blob(sid)
    original = path.read_bytes()
    with store._tx() as db:
        intent_id = db.execute('SELECT intent_id FROM research_blob_scopes WHERE id=?', (sid,)).fetchone()[0]
        if link == 'missing_intent':
            db.execute('UPDATE research_blob_scopes SET intent_id=? WHERE id=?', (str(uuid4()), sid))
        elif link == 'missing_scope':
            db.execute('DELETE FROM research_blob_scopes WHERE id=?', (sid,))
        elif link == 'missing_job':
            db.execute('DELETE FROM jobs WHERE id=?', (job.id,))
        elif link == 'missing_origin':
            db.execute('DELETE FROM research_blob_origins WHERE id=?', (sid,))
        elif link.startswith('scope_'):
            column = {'scope_owner': 'owner', 'scope_application': 'application_id', 'scope_run': 'run_id'}[link]
            db.execute(f'UPDATE research_blob_scopes SET {column}=? WHERE id=?', (str(uuid4()), sid))
        elif link.startswith('origin_'):
            column = {'origin_job': 'job_id', 'origin_fence': 'fence'}[link]
            db.execute(f'UPDATE research_blob_origins SET {column}=? WHERE id=?',
                       (999 if column == 'fence' else str(uuid4()), sid))
        else:
            column = {'intent_owner': 'owner', 'intent_application': 'application_id', 'intent_run': 'run_id',
                      'intent_job': 'job_id', 'intent_fence': 'fence'}[link]
            db.execute(f'UPDATE research_intents SET {column}=? WHERE id=?',
                       (999 if column == 'fence' else str(uuid4()), intent_id))
    assert store.reconcile_blobs()['cleanup_pending'] >= 1
    if forget == 'application':
        repo.delete_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    else:
        store.delete_profile(owner)
    restarted = Store(store.root)
    assert restarted.reconcile_blobs()['cleanup_pending'] >= 1
    assert path.read_bytes() == original
    tickets = restarted.pending_cleanup()
    assert tickets
    for ticket in tickets:
        with pytest.raises(EvidenceError) as error:
            restarted.complete_cleanup(ticket.id)
        assert error.value.code == 'CLEANUP_PENDING'
    assert path.read_bytes() == original


def test_cleanup_cannot_certify_complete_with_source_linked_employer_unit(tmp_path):
    import json
    store, owner, repo, app = setup(tmp_path)
    service = ResearchService(store, acquire_source=acquire)
    execute(service, owner, app)
    run = service.list(owner, app.application_id)['current_run_id']
    sid = service.detail(owner, app.application_id, run)['sources'][0]['id']
    unit = service.source(owner, app.application_id, run, sid)['unit']
    with store._tx() as db:
        unit['application_id'] = str(uuid4())
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(unit), unit['id']))
    repo.delete_application(owner, app.application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=1))
    ticket = store.pending_cleanup()[0]
    with pytest.raises(EvidenceError) as error:
        store.complete_cleanup(ticket.id)
    assert error.value.code == 'CLEANUP_PENDING'
    assert store.pending_cleanup()
