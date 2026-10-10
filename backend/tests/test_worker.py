"""Durable worker/API integration, synthetic data only."""
import threading
import time
from uuid import uuid4

import chromadb
import pytest
from fastapi.testclient import TestClient
from test_retrieval import FixtureModels

from copilot.api import create_app
from copilot.config import Settings
from copilot.domain.repository import ApplicationInput
from copilot.retrieval.dense import DenseIndex
from copilot.retrieval.service import EvidenceService


def test_strict_scoped_async_index_and_content_free_jobs_api(tmp_path):
    def no_network(*_args):
        pytest.fail('Enqueue/read/cancel must not initialize external/model service')
    app = create_app(Settings(tmp_path / 'data', tmp_path / 'models'), no_network)
    with TestClient(app, base_url='http://127.0.0.1:3001') as client:
        token = client.get('/api/evidence/status').json()['token']
        client.headers['x-evidence-token'] = token
        owner = client.post('/api/evidence/profiles', json={'name': 'Synthetic', 'sectors': ['tech']}).json()['id']
        other = client.post('/api/evidence/profiles', json={'name': 'Other', 'sectors': ['finance']}).json()['id']
        route = f'/api/workspace/profiles/{owner}/indexes'
        body = {'schema_version': 1, 'corpus': 'facts', 'idempotency_key': 'private-canary-key'}
        response = client.post(route, json=body)
        assert response.status_code == 202
        job = response.json()['job']
        assert job['state'] == 'queued' and job['kind'] == 'index'
        assert 'private-canary-key' not in response.text
        assert client.post(route, json=body).json()['job']['id'] == job['id']
        base = f'/api/workspace/profiles/{owner}/jobs'
        assert client.get(base).json()['jobs'][0]['id'] == job['id']
        assert client.get(base + '/' + job['id']).json()['job'] == job
        assert client.get(f'/api/workspace/profiles/{other}/jobs/{job["id"]}').status_code == 404
        assert client.post(route, json={**body, 'kind': 'research'}).status_code == 422
        assert client.post(route, json={**body, 'schema_version': True}).status_code == 422
        assert client.post(route, content='{}', headers={'content-type': 'text/plain'}).status_code == 415
        assert client.post(route, json=body, headers={'x-evidence-token': 'invalid'}).status_code == 403
        assert client.get(base, headers={'host': 'evil.example'}).status_code == 403
        canceled = client.post(base + '/' + job['id'] + '/cancel', json={'schema_version': 1})
        assert canceled.json()['job']['state'] == 'cancelled'
        usage = client.get(f'/api/workspace/profiles/{owner}/usage').json()['usage']
        assert usage['reserved_calls'] == 0 and usage['monetary_cost'] is None
        assert usage['pricing_status'] == 'unknown'


def test_worker_heartbeat_keeps_blocked_job_current_and_cancel_stops_publication(tmp_path):
    from copilot.store import Store
    from copilot.worker import Worker
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    store.confirm_fact(owner, 'Python synthetic evidence')
    entered, release = threading.Event(), threading.Event()
    models = FixtureModels()
    original = models.tokenize_offsets
    def blocked(text):
        entered.set()
        assert release.wait(8)
        return original(text)
    models.tokenize_offsets = blocked
    service = EvidenceService(store, tmp_path / 'index', DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma'))), models)
    worker = Worker(store, lambda: service, lease_seconds=5, heartbeat_interval=0.1)
    job = worker.jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'heartbeat')
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert entered.wait(5)
    try:
        first = worker.jobs.get(owner, job.id)
        time.sleep(0.3)
        current = worker.jobs.get(owner, job.id)
        assert current.lease_expires_at > first.lease_expires_at
        worker.jobs.cancel(owner, job.id)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert worker.jobs.get(owner, job.id).state == 'cancelled'
    assert store.active_manifest(owner, 'facts') is None


@pytest.mark.parametrize('stage', ['tokenizer', 'dense'])
def test_actual_http_reads_mutations_and_cancel_remain_responsive_during_index_barrier(tmp_path, stage):
    import socket

    import httpx
    import uvicorn

    from copilot.worker import Worker
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    settings = Settings(tmp_path / 'data', tmp_path / 'models', port=port)
    app = create_app(settings, lambda *_args: pytest.fail('HTTP control routes cannot initialize inference'))
    models = FixtureModels()
    dense = DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma')))
    service = EvidenceService(app.state.store, tmp_path / 'indexes', dense, models)
    entered, release = threading.Event(), threading.Event()
    if stage == 'tokenizer':
        original = models.tokenize_offsets
        def blocked(value):
            entered.set()
            assert release.wait(8)
            return original(value)
        models.tokenize_offsets = blocked
    else:
        original = dense.create
        def blocked(*args):
            entered.set()
            assert release.wait(8)
            return original(*args)
        dense.create = blocked
    worker = Worker(app.state.store, lambda: service, lease_seconds=5, heartbeat_interval=0.1)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='critical'))
    server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    server_thread.start()
    worker_thread = None
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False) as client:
            client.headers['x-evidence-token'] = client.get('/api/evidence/status').json()['token']
            owner = client.post('/api/evidence/profiles', json={'name': 'Synthetic', 'sectors': ['tech']}).json()['id']
            app.state.store.confirm_fact(owner, 'Synthetic Python evidence')
            job = client.post(f'/api/workspace/profiles/{owner}/indexes', json={
                'schema_version': 1, 'corpus': 'facts', 'idempotency_key': 'actual-http'}).json()['job']
            worker_thread = threading.Thread(target=worker.run_once)
            worker_thread.start()
            assert entered.wait(5)
            started = time.monotonic()
            assert client.get('/api/evidence/status').status_code == 200
            assert client.get(f'/api/workspace/profiles/{owner}').status_code == 200
            assert client.get(f'/api/workspace/profiles/{owner}/jobs/{job["id"]}').json()['job']['state'] == 'running'
            assert client.get(f'/api/workspace/profiles/{owner}/usage').status_code == 200
            assert client.post('/api/evidence/profiles', json={'name': 'Other during barrier', 'sectors': ['finance']}).status_code == 200
            assert client.post(f'/api/workspace/profiles/{owner}/jobs/{job["id"]}/cancel', json={'schema_version': 1}).json()['job']['state'] == 'cancelled'
            assert time.monotonic() - started < 1.5
    finally:
        release.set()
        if worker_thread:
            worker_thread.join(5)
        server.should_exit = True
        server_thread.join(5)
        listener.close()
    assert not server_thread.is_alive() and not worker_thread.is_alive()
    assert app.state.store.active_manifest(owner, 'facts') is None


def test_competing_workers_claim_one_job_and_unavailable_handlers_do_not_run(tmp_path):
    from copilot.store import Store
    from copilot.worker import Worker
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    first = Worker(store, lambda: pytest.fail('Unavailable handler must not initialize service'))
    second = Worker(store, lambda: pytest.fail('Unavailable handler must not initialize service'))
    application = first.jobs.domain.create_application(owner, ApplicationInput.model_validate_json(
        '{"expected_metadata_revision":0,"company":"Company","role":"Role","sector":"tech","vacancy_url":"https://example.com/job","questions":[]}'))
    job = first.jobs.enqueue(owner, 'draft', {}, 'not-enabled', application_id=application.application_id)
    results = []
    threads = [threading.Thread(target=lambda w=w: results.append(w.run_once(job.id, recovery=False))) for w in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sum(result is not None for result in results) == 1
    assert first.jobs.get(owner, job.id).state == 'failed'


def test_application_forget_is_exact_scope_and_retains_unknown_reservations(tmp_path):
    import json

    from copilot.domain import contracts as c
    from copilot.domain.repository import ApplicationPatch, ConsentInput
    from copilot.provider import Limits, Provider
    from copilot.store import Store
    from copilot.worker import Worker
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    worker = Worker(store, lambda: pytest.fail('No provider/model calls required'))
    domain = worker.jobs.domain
    apps = []
    for company in ('First', 'Second'):
        apps.append(domain.create_application(owner, ApplicationInput(expected_metadata_revision=domain.detail(owner).profile.revisions.metadata,
            company=company, role='Role', sector='tech', vacancy_url='https://example.com/job')))
    domain.consent(owner, ConsentInput(expected_consent_revision=0, provider='openai', purposes=('drafting',), granted=True))
    provider = Provider(worker.jobs, lambda: 'injected-synthetic-key')
    provider.configure_limits(owner, Limits(100000, 10, 50000, 5))
    first = worker.jobs.enqueue(owner, 'draft', {'private': 'FIRST-JOB-CANARY'}, 'FIRST-KEY-CANARY', apps[0].application_id)
    second = worker.jobs.enqueue(owner, 'draft', {'private': 'SECOND-JOB-CANARY'}, 'second', apps[1].application_id)
    lease = worker.jobs.claim(worker.worker_id, job_id=first.id)
    attempt = provider.reserve(first.id, worker.worker_id, lease.fence, 'draft', 5000, 'synthetic-pinned')
    with store._tx() as db:
        db.execute('INSERT INTO job_stages VALUES(?,?,?,?,?)', (first.id, 'stage', lease.fence, '{"private":"FIRST-STAGE-CANARY"}', 'synthetic'))
        db.execute('UPDATE provider_attempts SET response=? WHERE id=?', ('FIRST-RESPONSE-CANARY', attempt.id))
    domain.delete_application(owner, apps[0].application_id, ApplicationPatch(expected_input_revision=0, expected_output_revision=0))
    assert worker.jobs.get(owner, first.id).state == 'indeterminate'
    assert worker.jobs.get(owner, second.id).state == 'queued'
    with store._tx() as db:
        assert db.execute('SELECT parameters FROM jobs WHERE id=?', (first.id,)).fetchone()[0] == '{}'
        assert 'SECOND-JOB-CANARY' in db.execute('SELECT parameters FROM jobs WHERE id=?', (second.id,)).fetchone()[0]
        assert not db.execute('SELECT 1 FROM job_stages WHERE job_id=?', (first.id,)).fetchone()
        serialized, response = db.execute('SELECT data,response FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone()
        retained = c.ProviderAttempt.model_validate_json(serialized)
        assert retained.state == 'indeterminate' and retained.reserved_tokens == 5000
        assert response is None
        rows = json.dumps(db.execute('SELECT data,parameters FROM jobs WHERE id=?', (first.id,)).fetchall())
        assert 'FIRST-' not in rows
    assert provider.usage(owner)['reserved_calls'] == 1
    assert worker.jobs.claim(str(uuid4()), job_id=first.id) is None


def test_actual_http_parser_barrier_is_responsive_and_deleted_owner_cannot_publish(tmp_path, monkeypatch):
    import socket

    import httpx
    import uvicorn

    import copilot.api as api
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    app = create_app(Settings(tmp_path / 'data', tmp_path / 'models', port=port),
                     lambda *_args: pytest.fail('Parser/control routes cannot initialize indexes'))
    entered, release = threading.Event(), threading.Event()
    original = api.ingest
    def blocked(*args):
        entered.set()
        assert release.wait(8)
        return original(*args)
    monkeypatch.setattr(api, 'ingest', blocked)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='critical'))
    server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    server_thread.start()
    upload_thread = None
    responses, errors = [], []
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        base = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base, timeout=2, trust_env=False) as client:
            token = client.get('/api/evidence/status').json()['token']
            client.headers['x-evidence-token'] = token
            owner = client.post('/api/evidence/profiles', json={'name': 'Synthetic', 'sectors': ['tech']}).json()['id']
            job = client.post(f'/api/workspace/profiles/{owner}/indexes', json={
                'schema_version': 1, 'corpus': 'facts', 'idempotency_key': 'parser-barrier'}).json()['job']
            def upload():
                try:
                    with httpx.Client(base_url=base, timeout=8, trust_env=False) as uploader:
                        responses.append(uploader.post(f'/api/evidence/profiles/{owner}/sources',
                            headers={'x-evidence-token': token}, files={'file': ('notes.txt', b'Synthetic private', 'text/plain')}))
                except BaseException as exc:
                    errors.append(exc)
            upload_thread = threading.Thread(target=upload)
            upload_thread.start()
            assert entered.wait(5)
            started = time.monotonic()
            assert client.get('/api/evidence/status').status_code == 200
            assert client.get(f'/api/workspace/profiles/{owner}').status_code == 200
            assert client.post(f'/api/workspace/profiles/{owner}/jobs/{job["id"]}/cancel', json={'schema_version': 1}).status_code == 200
            assert client.delete(f'/api/evidence/profiles/{owner}').status_code == 202
            assert time.monotonic() - started < 1.5
    finally:
        release.set()
        if upload_thread:
            upload_thread.join(5)
        server.should_exit = True
        server_thread.join(5)
        listener.close()
    assert not errors and responses[0].status_code == 404
    assert not list(app.state.store.blobs.iterdir())
    with app.state.store._tx() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='source'").fetchone()
        assert not db.execute('SELECT 1 FROM upload_intents').fetchone()


def test_cli_index_uses_real_durable_worker_and_published_stage(tmp_path, monkeypatch, capsys):
    import json
    import sys

    import copilot.api as api
    from copilot.__main__ import main
    from copilot.store import Store
    settings = Settings(tmp_path / 'data', tmp_path / 'models')
    store = Store(settings.data_dir)
    owner = store.create_profile('Synthetic', ['tech']).id
    store.confirm_fact(owner, 'Python evidence')
    dense = DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma')))
    monkeypatch.setattr(Settings, 'from_env', classmethod(lambda _cls: settings))
    monkeypatch.setattr(api, 'make_service', lambda selected, _settings:
        EvidenceService(selected, tmp_path / 'index', dense, FixtureModels()))
    monkeypatch.setattr(sys, 'argv', ['copilot', 'index', '--profile', owner, '--corpus', 'facts'])
    main()
    manifest = json.loads(capsys.readouterr().out)
    with store._tx() as db:
        data = json.loads(db.execute('SELECT data FROM jobs').fetchone()[0])
        assert data['state'] == 'completed' and data['fence'] == 1
        assert db.execute('SELECT stage FROM job_stages').fetchone()[0] == 'index_published'
    assert store.active_manifest(owner, 'facts').generation_id == manifest['generation_id']


def test_worker_singleton_does_not_block_api_or_allow_another_runtime(tmp_path):
    from copilot.config import worker_lock
    from copilot.contracts import EvidenceError
    with worker_lock(tmp_path):
        with pytest.raises(EvidenceError, match='already running'):
            with worker_lock(tmp_path):
                pass
        app = create_app(Settings(tmp_path, tmp_path / 'models'))
        with TestClient(app, base_url='http://127.0.0.1:3001') as client:
            assert client.get('/health').status_code == 200
            assert client.get('/api/evidence/status').status_code == 200


def test_queued_profile_forget_scrubs_job_payload_before_any_generation(tmp_path):
    import json

    from copilot.store import Store
    from copilot.worker import Worker
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    worker = Worker(store, lambda: pytest.fail('Deleted queued job cannot initialize inference'))
    job = worker.jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'PRIVATE-KEY-CANARY')
    ticket = store.delete_profile(owner)
    assert not ticket.generation_ids
    assert worker.run_once(recovery=False) is None
    with store._tx() as db:
        serialized, parameters = db.execute('SELECT data,parameters FROM jobs WHERE id=?', (job.id,)).fetchone()
        assert json.loads(serialized)['state'] == 'cancelled'
        assert 'PRIVATE-KEY-CANARY' not in serialized and parameters == '{}'
        assert not db.execute('SELECT 1 FROM job_keys').fetchone()


def test_synchronous_cli_runner_joins_already_claimed_worker_without_duplicate_generation(tmp_path):
    from copilot.store import Store
    from copilot.worker import Worker
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    store.confirm_fact(owner, 'Python evidence')
    entered, release = threading.Event(), threading.Event()
    models = FixtureModels()
    original = models.tokenize_offsets
    def blocked(text):
        entered.set()
        assert release.wait(5)
        return original(text)
    models.tokenize_offsets = blocked
    service = EvidenceService(store, tmp_path / 'indexes',
        DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma'))), models)
    runtime = Worker(store, lambda: service)
    cli = Worker(store, lambda: pytest.fail('Already claimed job must not build again'))
    job = cli.jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'join')
    runtime_thread = threading.Thread(target=lambda: runtime.run_once(recovery=False))
    runtime_thread.start()
    assert entered.wait(5)
    results = []
    cli_thread = threading.Thread(target=lambda: results.append(cli.run_job(job.id)))
    cli_thread.start()
    try:
        time.sleep(0.1)
        assert cli_thread.is_alive()
    finally:
        release.set()
        runtime_thread.join(5)
        cli_thread.join(5)
    assert results == [{'job_id': job.id, 'state': 'completed'}]
    assert cli.jobs.get(owner, job.id).attempt_count == 1
    assert len(service.generations.list()) == 1


def test_api_diagnostics_expose_registered_ambiguity_without_model_or_external_recovery(tmp_path):
    from copilot.worker import Worker
    app = create_app(Settings(tmp_path / 'data', tmp_path / 'models'),
                     lambda *_args: pytest.fail('Diagnostics cannot initialize external services'))
    dense = DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma')))
    service = EvidenceService(app.state.store, tmp_path / 'indexes', dense, FixtureModels())
    def disconnected(*_args):
        raise OSError('Synthetic opaque disconnect')
    dense.create = disconnected
    worker = Worker(app.state.store, lambda: service)
    owner = app.state.store.create_profile('Synthetic', ['tech']).id
    app.state.store.confirm_fact(owner, 'Python evidence')
    job = worker.jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'ambiguity')
    worker.run_once(recovery=False)
    with TestClient(app, base_url='http://127.0.0.1:3001') as client:
        status = client.get('/api/evidence/status').json()
        assert status['pending_cleanup'] == 0 and status['pending_generation_cleanup'] == 1
        summary = client.get(f'/api/workspace/profiles/{owner}/jobs/{job.id}').json()['job']
        assert summary['state'] == 'failed' and summary['cleanup_pending'] is True


def test_worker_once_reports_recovery_failure_nonzero_without_private_exception(tmp_path, monkeypatch, capsys):
    import sys

    from copilot.__main__ import main
    from copilot.store import Store
    settings = Settings(tmp_path / 'data', tmp_path / 'models')
    monkeypatch.setattr(Settings, 'from_env', classmethod(lambda _cls: settings))
    monkeypatch.setattr(sys, 'argv', ['copilot', 'worker', '--once'])
    def failed(_self):
        raise OSError('PRIVATE-RECOVERY-EXCEPTION-CANARY')
    monkeypatch.setattr(Store, 'reconcile_blobs', failed)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code != 0
    output = capsys.readouterr()
    assert 'PRIVATE-' not in output.out + output.err
    assert 'UNAVAILABLE' in output.out and 'failed' in output.out


def test_heartbeat_infrastructure_failure_is_observable_without_raw_details(tmp_path, monkeypatch, capsys):
    from copilot.store import Store
    from copilot.worker import Worker
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    store.confirm_fact(owner, 'Python evidence')
    models = FixtureModels()
    original = models.tokenize_offsets
    def delayed(text):
        time.sleep(0.15)
        return original(text)
    models.tokenize_offsets = delayed
    service = EvidenceService(store, tmp_path / 'indexes',
        DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma'))), models)
    worker = Worker(store, lambda: service, lease_seconds=5, heartbeat_interval=0.02)
    worker.jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'heartbeat-failure')
    def failed(*_args):
        raise OSError('PRIVATE-HEARTBEAT-KEY-CANARY')
    monkeypatch.setattr(worker.jobs, 'heartbeat', failed)
    worker.run_once(recovery=False)
    assert worker.last_outcome['heartbeat'] == {'state': 'failed', 'code': 'UNAVAILABLE'}
    output = capsys.readouterr()
    assert 'PRIVATE-' not in output.out + output.err
    assert 'UNAVAILABLE' in output.err


def test_expected_heartbeat_authority_loss_is_distinct_from_infrastructure_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from copilot.contracts import EvidenceError
    from copilot.store import Store
    from copilot.worker import Worker
    worker = Worker(Store(tmp_path), lambda: None, lease_seconds=5, heartbeat_interval=0.01)
    def revoked(*_args):
        raise EvidenceError('CONFLICT', 'Synthetic revocation', 409)
    monkeypatch.setattr(worker.jobs, 'heartbeat', revoked)
    with worker.heartbeat(SimpleNamespace(id=str(uuid4()), fence=1)) as outcome:
        deadline = time.monotonic() + 2
        while outcome['state'] == 'active' and time.monotonic() < deadline:
            time.sleep(0.01)
    assert outcome == {'state': 'authority_lost', 'code': 'AUTHORITY_LOST'}


def test_warned_paid_research_retry_cannot_dispatch_static_handler(tmp_path):
    from copilot.domain.repository import ConsentInput
    from copilot.provider import Limits, Provider
    from copilot.retrycontracts import WarnedRetry
    from copilot.store import Store
    from copilot.worker import Worker
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    worker = Worker(store, lambda: pytest.fail('Paid retry cannot initialize inference'),
                    research_factory=lambda *_: pytest.fail('Paid retry cannot dispatch static research'))
    application = worker.jobs.domain.create_application(owner, ApplicationInput(expected_metadata_revision=0,
        company='Company', role='Role', sector='tech', vacancy_url='https://example.com/job'))
    worker.jobs.domain.consent(owner, ConsentInput(expected_consent_revision=0,
        provider='openai', purposes=('research',), granted=True))
    queued = worker.jobs.enqueue(owner, 'research', {}, 'uncertain', application.application_id)
    leased = worker.jobs.claim(str(uuid4()), job_id=queued.id)
    provider = Provider(worker.jobs, lambda: 'synthetic-key')
    provider.configure_limits(owner, Limits(100000, 10, 100000, 10))
    attempt = provider.reserve(leased.id, leased.lease_owner, leased.fence, 'research', 5000, 'configured-model')
    provider._unknown(attempt.id, leased.id)
    retry = provider.request_retry(owner, leased.id, WarnedRetry(prior_attempt_id=attempt.id,
        idempotency_key='warned', acknowledge_duplicate_charge=True, warning_version='duplicate_charge_possible_v1'))
    assert worker.run_once(retry.id, recovery=False)['error'] == 'HANDLER_UNAVAILABLE'
    assert worker.jobs.get(owner, leased.id).state == 'indeterminate'
    assert provider.usage(owner)['reserved_calls'] == 1
