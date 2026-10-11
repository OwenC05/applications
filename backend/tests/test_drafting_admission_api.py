"""Admission wire boundaries and real HTTP lost-response/restart recovery."""
import copy
import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from test_drafting_service import body, capture_draft_launch, drafting  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401
from test_workspace import application, client, create  # noqa: F401

from copilot import admission_state as a
from copilot.api import create_app
from copilot.config import Settings
from copilot.contracts import EvidenceError
from copilot.domain.repository import ApplicationPatch, ProposalDelete, ProposalInput


def test_admission_get_is_exact_read_only_and_does_not_initialize_runtime(client, monkeypatch):  # noqa: F811
    owner = create(client)
    app = application(client, owner)
    api = client.app
    store = api.state.store
    statements = []
    original = store._read
    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(store, '_read', traced)
    monkeypatch.setattr(store, '_tx', lambda: pytest.fail('Admission GET opened writer'))
    monkeypatch.setattr(api.state.jobs, 'get', lambda *_: pytest.fail('Admission GET used job writer'))
    monkeypatch.setattr(api.state.jobs, 'list', lambda *_: pytest.fail('Admission GET used job list'))
    api.state.drafting.provider._key_supplier = lambda: pytest.fail('Admission GET read key')
    url = f'/api/workspace/profiles/{owner}/applications/{app}/drafting-admission'
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json() == dict(schema_version=1, profile_id=owner, application_id=app,
        admission_revision=0, eligible=True, reason_codes=[], head_job=None)
    assert statements and all(s.lstrip().split()[0].upper() in {'BEGIN', 'SELECT', 'PRAGMA', 'COMMIT', 'ROLLBACK'}
                              for s in statements)
    assert client.get(url.replace(owner, str(uuid4()))).status_code == 404
    assert client.get(url.replace(app, str(uuid4()))).status_code == 404
    assert client.get(url.replace(app, 'invalid')).status_code == 422


def test_http_strict_wrapper_cannot_downgrade_or_queue_raw_fresh(drafting, tmp_path):  # noqa: F811
    service, scope, _, sent, _ = drafting
    raw = body(drafting)
    launch = capture_draft_launch(drafting, raw).model_dump(mode='json')
    api = create_app(Settings(service.store.root, tmp_path / 'absent-models'),
                     lambda *_: pytest.fail('Wire checks initialized retrieval'))
    root = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
    cases = []
    for field, value in [('admission_revision', True), ('admission_revision', '0'),
                         ('schema_version', True), ('extra', 'forbidden')]:
        cases.append(dict(launch, **{field: value}))
    missing = copy.deepcopy(launch)
    del missing['request']['expected_revisions']['facts']
    cases.extend([missing, dict(request=raw.model_dump(mode='json')),
                  dict(raw.model_dump(mode='json'), admission_revision=0)])
    with TestClient(api, base_url='http://127.0.0.1:3001') as http:
        http.headers['x-evidence-token'] = http.get('/api/workspace/status').json()['boot_token']
        for malformed in cases:
            response = http.post(root, json=malformed)
            assert response.status_code == 422, response.text
        response = http.post(root, json=raw.model_dump(mode='json'))
        assert response.status_code == 409 and response.json()['error']['code'] == 'ADMISSION_REQUIRED'
        assert http.post(root, content='[', headers={'content-type': 'application/json'}).status_code == 422
        assert http.post(root, content='{}', headers={'content-type': 'text/plain'}).status_code == 415
    with service.store._read() as db:
        assert not db.execute("SELECT 1 FROM jobs WHERE json_extract(data,'$.kind')='draft'").fetchone()
    assert not sent


@contextmanager
def actual_http(api, port):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(api, log_level='critical'))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]))
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('Owned HTTP startup failed')
            time.sleep(.01)
        yield
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive(), 'Owned HTTP helper did not stop'


def test_actual_http_postcommit_failure_restart_original_capture_replays_once(drafting, tmp_path, monkeypatch):  # noqa: F811
    service, scope, _, sent, _ = drafting
    launch = capture_draft_launch(drafting, body(drafting, key='synthetic-lost-response'))
    with socket.socket() as port_probe:
        port_probe.bind(('127.0.0.1', 0))
        port = port_probe.getsockname()[1]
    settings = Settings(service.store.root, tmp_path / 'absent-models', port=port)
    def api_instance():
        api = create_app(settings, lambda *_: pytest.fail('Actual HTTP initialized retrieval'))
        api.state.drafting.provider._key_supplier = lambda: pytest.fail('Actual HTTP read a cloud key')
        return api
    api = api_instance()
    base = f'http://127.0.0.1:{port}'
    root = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
    calls = []
    original = api.state.store._tx
    @contextmanager
    def fail_summary_writer():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError('synthetic summary failure after committed admission')
        with original() as db:
            yield db
    with actual_http(api, port), httpx.Client(base_url=base, timeout=10) as http:
        http.headers['x-evidence-token'] = http.get('/api/workspace/status').json()['boot_token']
        with monkeypatch.context() as patch:
            patch.setattr(api.state.store, '_tx', fail_summary_writer)
            failed = http.post(root, json=launch.model_dump(mode='json'))
        assert failed.status_code == 500 and len(calls) == 2
    with service.store._read() as db:
        jobs = db.execute("SELECT id FROM jobs WHERE json_extract(data,'$.kind')='draft'").fetchall()
        assert len(jobs) == 1
        job_id = jobs[0][0]
    restarted = api_instance()
    with actual_http(restarted, port), httpx.Client(base_url=base, timeout=10) as http:
        http.headers['x-evidence-token'] = http.get('/api/workspace/status').json()['boot_token']
        replay = http.post(root, json=launch.model_dump(mode='json'))
        assert replay.status_code == 202 and replay.json()['job']['id'] == job_id
        changed = launch.model_copy(update={'request': launch.request.model_copy(update={'idempotency_key': 'different-key'})})
        assert http.post(root, json=changed.model_dump(mode='json')).status_code == 409
    with service.store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.kind')='draft'").fetchone() == (1,)
    assert not sent and not (tmp_path / 'absent-models').exists()


@pytest.mark.parametrize('action', ['fact', 'source', 'proposal', 'application', 'profile'])
@pytest.mark.parametrize('prepared', [False, True])
def test_consumed_max_privacy_revokes_without_reset_or_uncertainty_refund(drafting, action, prepared):  # noqa: F811
    service, scope, _, sent, _ = drafting
    store, owner, app = service.store, scope.profile_id, scope.application_id
    proposal = source = None
    with store._read() as db:
        metadata = service.domain.revisions(db, owner).metadata
    if action == 'proposal':
        proposal = service.domain.propose(owner, ProposalInput(
            expected_metadata_revision=metadata, text='Pending synthetic fact'))
    if action == 'source':
        source = store.add_source(owner, 'synthetic.txt', 'text/plain', b'Synthetic', ['Synthetic'], 'test')
    with store._tx() as db:
        db.execute(f'UPDATE {a.HEADS} SET revision=? WHERE owner=? AND application_id=?',
                   (a.MAX_REVISION - 1, owner, app))
    launch = capture_draft_launch(drafting, body(drafting, key='synthetic-max'))
    job = service.enqueue(owner, app, launch)
    worker = str(uuid4())
    lease = service.jobs.claim(worker, job_id=job.id)
    attempt = service.provider.reserve(job.id, worker, lease.fence, 'reserved', 5000, 'synthetic-model') if prepared else None
    service.provider._key_supplier = lambda: pytest.fail('Revoked MAX job reached a key')
    if action == 'fact':
        fact = store.confirm_fact(owner, 'Synthetic fact after capture')
        store.revoke_fact(owner, fact.id)
    elif action == 'source':
        store.delete_source(owner, source.id)
    elif action == 'proposal':
        with store._read() as db:
            revisions = service.domain.revisions(db, owner)
        service.domain.delete_proposal(owner, proposal.id, ProposalDelete(
            expected_metadata_revision=revisions.metadata, expected_facts_revision=revisions.facts))
    elif action == 'application':
        service.domain.delete_application(owner, app, ApplicationPatch(
            expected_input_revision=job.revisions.application_input,
            expected_output_revision=job.revisions.application_output))
    else:
        store.delete_profile(owner)
    with store._read() as db:
        assert db.execute(f'SELECT revision FROM {a.HEADS} WHERE owner=? AND application_id=?', (owner, app)).fetchone() == (a.MAX_REVISION,)
        assert db.execute(f'SELECT captured_revision,consumed_revision FROM {a.BINDINGS} WHERE job_id=?', (job.id,)).fetchone() == (a.MAX_REVISION - 1, a.MAX_REVISION)
        if action == 'proposal':
            assert not db.execute('SELECT 1 FROM tombstones WHERE id=?', (job.id,)).fetchone()
            assert db.execute('SELECT 1 FROM job_keys WHERE job_id=?', (job.id,)).fetchone()
        else:
            assert db.execute('SELECT owner,kind FROM tombstones WHERE id=?', (job.id,)).fetchone() == (owner, 'drafting_job_forgotten')
        if attempt:
            row = db.execute('SELECT state,data,response FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone()
            from copilot.domain.contracts import ProviderAttempt
            retained = ProviderAttempt.model_validate_json(row[1])
            assert row[0] in ('prepared', 'indeterminate') and row[2] is None
            assert retained.reserved_tokens == attempt.reserved_tokens and retained.reserved_calls == attempt.reserved_calls
    with pytest.raises(EvidenceError):
        service.provider.reserve(job.id, worker, lease.fence, 'after-forgetting', 100, 'synthetic-model')
    with pytest.raises(EvidenceError):
        service.jobs.commit_stage(job.id, worker, lease.fence, 'forbidden', {'text': 'never published'})
    if action == 'proposal':
        # Head-only revocation does not erase intact historical replay. It
        # returns the old job, never a fresh admission or dispatch authority.
        assert service.enqueue(owner, app, launch).id == job.id
    else:
        with pytest.raises(EvidenceError):
            service.enqueue(owner, app, launch)
    assert not sent
