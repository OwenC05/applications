"""HTTP queue/disclosure/budget integration: synthetic data, no keys or paid calls."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_drafting_service import body, drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401
from test_workspace import client, create  # noqa: F401

from copilot.api import create_app
from copilot.config import Settings


def test_budget_configuration_is_explicit_and_does_not_grant_consent(client):  # noqa: F811
    owner = create(client)
    url = f'/api/workspace/profiles/{owner}/budget'
    initial = client.get(url)
    assert initial.status_code == 200 and initial.json()['configured_limits'] is None
    configured = dict(daily_tokens=1000000, daily_calls=10, job_tokens=700000, job_calls=5, acknowledged=True)
    response = client.post(url, json=configured)
    assert response.status_code == 200, response.text
    assert response.json()['configured_limits']['job_calls'] == 5
    assert response.json()['monetary_cost'] is None and not response.json()['grants_cloud_consent']
    assert client.get(f'/api/workspace/profiles/{owner}').json()['consent'] is None
    assert client.get(f'/api/workspace/profiles/{owner}/usage').json()['usage']['reserved_calls'] == 0


@pytest.mark.parametrize('changes', [dict(acknowledged=False), dict(acknowledged=1),
    dict(acknowledged='true'), dict(daily_tokens=True), dict(job_calls=0),
    dict(job_calls=11), dict(job_tokens=1000001), dict(provider_key='forbidden')])
def test_budget_bounds_ack_and_secret_input_fail_closed(client, changes):  # noqa: F811
    owner = create(client)
    payload = dict(daily_tokens=1000000, daily_calls=10, job_tokens=700000, job_calls=5, acknowledged=True) | changes
    response = client.post(f'/api/workspace/profiles/{owner}/budget', json=payload)
    assert response.status_code == 422
    assert client.get(f'/api/workspace/profiles/{owner}/budget').json()['configured_limits'] is None


def test_budget_owner_and_csrf_boundaries(client):  # noqa: F811
    payload = dict(daily_tokens=1000000, daily_calls=10, job_tokens=700000, job_calls=5, acknowledged=True)
    assert client.post(f'/api/workspace/profiles/{uuid4()}/budget', json=payload).status_code == 404
    owner = create(client)
    del client.headers['x-evidence-token']
    assert client.post(f'/api/workspace/profiles/{owner}/budget', json=payload).status_code == 403


def test_http_preview_queue_and_model_free_inspection(drafting, tmp_path):  # noqa: F811
    service, scope, batch, sent, _ = drafting
    request = body(drafting)
    api = create_app(Settings(service.store.root, tmp_path / 'unused-models'),
                     lambda *_: pytest.fail('HTTP drafting must not initialize retrieval models'))
    def forbidden_key():
        pytest.fail('HTTP preview/queue/read must not discover a cloud key')
    api.state.drafting.provider._key_supplier = forbidden_key
    root = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
    with TestClient(api, base_url='http://127.0.0.1:3001') as http:
        http.headers['x-evidence-token'] = http.get('/api/workspace/status').json()['boot_token']
        preview = http.post(root + '/preview', json=request.model_dump(mode='json', exclude={
            'idempotency_key', 'disclosure_sha256', 'acknowledged'}))
        assert preview.status_code == 200, preview.text
        assert preview.json()['disclosure_sha256'] == request.disclosure_sha256
        queued = http.post(root, json=request.model_dump(mode='json'))
        assert queued.status_code == 202, queued.text
        job = queued.json()['job']
        assert job['kind'] == 'draft' and job['state'] == 'queued'
        assert not sent and 'selected_context' not in job and 'disclosure_sha256' not in job
        assert http.post(root, json=request.model_dump(mode='json')).json()['job']['id'] == job['id']
        assert http.get(root).json()['drafts'] == []
        worker = str(uuid4())
        claimed = service.jobs.claim(worker, job_id=job['id'])
        draft = service.run_draft(job['id'], worker, claimed.fence)
        detail = http.get(root + '/' + draft.id)
        assert detail.status_code == 200, detail.text
        assert detail.json()['current'] and not detail.json()['review_eligible']
        assert not detail.json()['browser_eligible'] and len(sent) == 3
        assert len(http.get(root).json()['drafts']) == 1
        assert http.get(root + '/' + str(uuid4())).status_code == 404
        assert http.get(f'/api/workspace/profiles/{uuid4()}/applications/{scope.application_id}/drafts/{draft.id}').status_code == 404
        assert http.get(f'/api/workspace/profiles/{scope.profile_id}/jobs/{job["id"]}').json()['job']['stage'] == 'draft_published'
        assert not (tmp_path / 'unused-models').exists()


@pytest.mark.parametrize('rewrite,calls', [(False, 3), (True, 5)])
def test_actual_worker_dispatches_guarded_durable_pipeline_without_retrieval(drafting, rewrite, calls):  # noqa: F811
    from copilot.worker import Worker

    service, scope, _, sent, settings = drafting
    settings['rewrite'] = rewrite
    request = body(drafting, key='worker-flow')
    job = service.enqueue(scope.profile_id, scope.application_id, request)
    worker = Worker(service.store, lambda: pytest.fail('Drafting must not initialize retrieval models'),
                    drafting_factory=lambda store, jobs: service)
    result = worker.run_once(job.id, recovery=False)
    assert result == {'job_id': job.id, 'state': 'completed'}
    assert len(sent) == calls
    inspection = service.list(scope.profile_id, scope.application_id)
    assert len(inspection['drafts']) == 1 and inspection['drafts'][0]['current']
    assert not inspection['drafts'][0]['review_eligible']


@pytest.mark.parametrize('operation', ['preview', 'enqueue'])
def test_slow_drafting_capture_runs_off_event_loop_and_health_stays_responsive(drafting, tmp_path, monkeypatch, operation):  # noqa: F811
    import asyncio
    import threading

    import httpx

    service, scope, _, sent, _ = drafting
    request = body(drafting, key='scheduling-' + operation)
    api = create_app(Settings(service.store.root, tmp_path / 'unused-models'),
                     lambda *_: pytest.fail('No retrieval initialization'))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_observed = []
    original = getattr(api.state.drafting, operation)
    def blocked(*args):
        try:
            asyncio.get_running_loop()
            loop_observed.append(True)
        except RuntimeError:
            pass
        entered.set()
        try:
            if not release.wait(2):
                raise RuntimeError('Synthetic capture release deadline')
            return original(*args)
        finally:
            finished.set()
    monkeypatch.setattr(api.state.drafting, operation, blocked)
    root = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
    payload = request.model_dump(mode='json')
    if operation == 'preview':
        payload = {k: v for k, v in payload.items() if k not in {'idempotency_key', 'disclosure_sha256', 'acknowledged'}}
    async def probe():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://127.0.0.1:3001') as http:
            http.headers['x-evidence-token'] = (await http.get('/api/workspace/status')).json()['boot_token']
            pending = asyncio.create_task(http.post(root + ('/preview' if operation == 'preview' else ''), json=payload))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert not loop_observed, 'Synchronous source verification ran on the API event loop'
                health = await asyncio.wait_for(http.get('/health'), 1)
                assert health.status_code == 200 and not finished.is_set()
            finally:
                release.set()
                response = await pending
            assert response.status_code == (200 if operation == 'preview' else 202), response.text
    asyncio.run(probe())
    assert not sent and not (tmp_path / 'unused-models').exists()
