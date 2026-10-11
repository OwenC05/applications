"""One-call assessment HTTP/worker integration with synthetic Provider transport."""
import asyncio
import json
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_drafting_edit_api import edit_payload
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.api import create_app
from copilot.config import Settings
from copilot.domain.contracts import ConsentRecord, canonical_hash
from copilot.domain.repository import ConsentInput
from copilot.drafting.edit_contracts import EditRequest
from copilot.worker import Worker


def setup_edit(fixture_values):
    service, scope, _, _, _ = fixture_values
    with service.store._read() as db:
        consent = service.domain._singleton(db, scope.profile_id, 'consent', ConsentRecord)
    service.domain.consent(scope.profile_id, ConsentInput(expected_consent_revision=consent.revision,
        provider='openai', purposes=('research', 'drafting', 'assessment'), granted=True))
    parent, *_ = execute(fixture_values)
    payload = edit_payload(service, scope, parent, text='  Exact unchanged Café 🧠\r\n ')
    edited = service.edit(scope.profile_id, scope.application_id, parent.id,
                         EditRequest.model_validate_json(json.dumps(payload)))
    detail = service.detail(scope.profile_id, scope.application_id, edited.id)
    request = dict(expected_revisions=edited.revisions.model_dump(mode='json'),
        parent_text_sha256=edited.text_sha256, parent_ledger_sha256=edited.ledger_sha256,
        parent_publication_sha256=canonical_hash(detail['publication']),
        model='synthetic-model', max_output_tokens=2000)
    return edited, request


def api_for(service, tmp_path):
    api = create_app(Settings(service.store.root, tmp_path / 'absent-models'),
                     lambda *_: pytest.fail('Assessment HTTP loaded retrieval models'))
    api.state.drafting.provider._key_supplier = lambda: pytest.fail('Assessment HTTP read a key')
    return api


def test_http_assessment_preview_queue_actual_worker_and_immutable_export(drafting, tmp_path):  # noqa: F811
    service, scope, _, sent, settings = drafting
    edited, request = setup_edit(drafting)
    settings['rewrite'] = True  # Advisory only: one assessment must never rewrite.
    api = api_for(service, tmp_path)
    base = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']
        url = f'{base}/{edited.id}/assessment'
        preview = client.post(url + '/preview', json=request)
        assert preview.status_code == 200, preview.text
        disclosure = preview.json()['disclosure']
        assert disclosure['maximum_calls'] == 1 and disclosure['monetary_cost'] is None
        assert json.dumps(edited.answers[0].text, ensure_ascii=False) in json.dumps(disclosure, ensure_ascii=False)
        body = dict(request, idempotency_key='http-assess', acknowledged=True,
                    disclosure_sha256=preview.json()['disclosure_sha256'])
        queued = client.post(url, json=body)
        assert queued.status_code == 202, queued.text
        job = queued.json()['job']
        assert job['kind'] == 'assess' and len(sent) == 3
        assert client.post(url, json=body).json()['job']['id'] == job['id']
        worker = Worker(service.store, lambda: pytest.fail('Assessment worker loaded retrieval models'),
                        drafting_factory=lambda _s, _j: service)
        assert worker.run_once(job['id'], recovery=False) == dict(job_id=job['id'], state='completed')
        assert len(sent) == 4
        completed = client.get(f'/api/workspace/profiles/{scope.profile_id}/jobs/{job["id"]}')
        assert completed.status_code == 200, completed.text
        assert completed.json()['job']['stage'] == 'assessment_published'
        all_drafts = client.get(base).json()['drafts']
        assessed = next(item for item in all_drafts if item['publication'].get('origin') == 'assessment')
        assert assessed['current'] and assessed['draft']['assessment_state'] == 'assessed'
        assert assessed['draft']['answers'] == edited.model_dump(mode='json')['answers']
        assert assessed['text_sha256'] == edited.text_sha256
        assert 'ASSESSMENT_REQUIRED' not in assessed['reason_codes']
        assert not assessed['review_eligible'] and not assessed['browser_eligible']
        assert assessed['draft']['revisions']['application_output'] == edited.revisions.application_output + 1
        replay = client.post(url, json=body)
        assert replay.status_code == 202 and replay.json()['job']['id'] == job['id']
        assert len(sent) == 4
        exported = client.get(f'{base}/{assessed["draft"]["id"]}/export')
        assert exported.status_code == 200 and edited.answers[0].text in exported.text


@pytest.mark.parametrize('change', ['partial_vector', 'ack_one', 'extra_text', 'wrong_hash'])
def test_http_assessment_bad_body_or_disclosure_never_queues(drafting, tmp_path, change):  # noqa: F811
    service, scope, _, sent, _ = drafting
    edited, request = setup_edit(drafting)
    api = api_for(service, tmp_path)
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']
        url = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{edited.id}/assessment'
        preview = client.post(url + '/preview', json=request)
        assert preview.status_code == 200, preview.text
        body = dict(request, idempotency_key='bad-assess', acknowledged=True,
                    disclosure_sha256=preview.json()['disclosure_sha256'])
        expected = 422
        if change == 'partial_vector':
            del body['expected_revisions']['facts']
        elif change == 'ack_one':
            body['acknowledged'] = 1
        elif change == 'extra_text':
            body['draft'] = {'targets': []}
        else:
            body['disclosure_sha256'] = '0' * 64
            expected = 409
        response = client.post(url, json=body)
        assert response.status_code == expected, response.text
    with service.store._read() as db:
        assert not db.execute("SELECT 1 FROM jobs WHERE json_extract(data,'$.kind')='assess'").fetchone()
    assert len(sent) == 3


def test_http_assessment_owner_csrf_and_unassessed_parent_boundaries(drafting, tmp_path):  # noqa: F811
    service, scope, *_ = drafting
    edited, request = setup_edit(drafting)
    api = api_for(service, tmp_path)
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        url = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{edited.id}/assessment/preview'
        assert client.post(url, json=request).status_code == 403
        client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']
        assert client.post(url.replace(scope.profile_id, str(uuid4())), json=request).status_code == 404
        assert client.post(url.replace(edited.id, str(uuid4())), json=request).status_code == 404


@pytest.mark.parametrize('operation', ['preview', 'enqueue'])
def test_http_assessment_capture_offloaded_and_health_responsive(drafting, tmp_path, monkeypatch, operation):  # noqa: F811
    service, scope, *_ = drafting
    edited, request = setup_edit(drafting)
    api = api_for(service, tmp_path)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    with service.store._read() as db:
        job_id = db.execute("SELECT id FROM jobs WHERE owner=? AND json_extract(data,'$.kind')='draft'",
                            (scope.profile_id,)).fetchone()[0]
        serialization_fixture = service.jobs._job(db, job_id, scope.profile_id)[0]

    def blocked(*_):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return {'synthetic': True} if operation == 'preview' else serialization_fixture

    monkeypatch.setattr(api.state.drafting, 'assessment_preview' if operation == 'preview' else 'enqueue_assessment', blocked)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://127.0.0.1:3001') as client:
            client.headers['x-evidence-token'] = (await client.get('/api/workspace/status')).json()['boot_token']
            base = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{edited.id}/assessment'
            payload = request if operation == 'preview' else dict(request,
                idempotency_key='thread-test', acknowledged=True, disclosure_sha256='0' * 64)
            pending = asyncio.create_task(client.post(base + ('/preview' if operation == 'preview' else ''), json=payload))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert (await asyncio.wait_for(client.get('/health'), timeout=.5)).status_code == 200
            finally:
                release.set()
            assert (await pending).status_code == (200 if operation == 'preview' else 202)

    asyncio.run(scenario())
