"""Local review HTTP contracts; synthetic originals, no model/key/network IO."""
import asyncio
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_drafting_reviews import preview_body, review_body, reviewing  # noqa: F401
from test_drafting_service import execute
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.api import create_app
from copilot.config import Settings


def api_for(service, tmp_path):
    api = create_app(Settings(service.store.root, tmp_path / 'absent-models'),
                     lambda *_: pytest.fail('Local review loaded retrieval models'))
    api.state.drafting.provider._key_supplier = lambda: pytest.fail('Local review read provider key')
    api.state.drafting.provider.transport = lambda *a, **k: pytest.fail('Local review sent provider request')
    return api


def authorize(client):
    client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']


def url_for(scope, draft):
    return f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{draft.id}/review'


def test_http_exact_preview_review_and_restart_replay_without_jobs(reviewing, tmp_path):  # noqa: F811
    service, scope, _, sent, settings = reviewing
    settings['text'] = '  Café e\u0301 😀\n\tA\r\n '
    parent, *_ = execute(reviewing)
    request = preview_body(service, scope, parent).model_dump(mode='json')
    url = url_for(scope, parent)
    api = api_for(service, tmp_path)
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        authorize(client)
        preview = client.post(url + '/preview', json=request)
        assert preview.status_code == 200, preview.text
        assert set(preview.json()) == {'schema_version', 'disclosure', 'disclosure_sha256'}
        disclosure = preview.json()['disclosure']
        assert disclosure['answers'][0]['text'] == settings['text']
        assert disclosure['warnings'][-1] == 'NO_BROWSER_AUTHORIZATION'
        body = dict(request, idempotency_key='http-review', acknowledged=True,
                    disclosure_sha256=preview.json()['disclosure_sha256'])
        response = client.post(url, json=body)
        assert response.status_code == 201, response.text
        value = response.json()
        child = value['draft']['id']
        assert value['publication']['origin'] == 'review'
        assert value['reviewed_current'] and value['writing_review_ready']
        assert not value['review_eligible'] and not value['browser_eligible']
        assert value['review_snapshot']['draft_id'] == child
        assert value['review_snapshot']['revisions'] == value['draft']['revisions']
        assert value['draft']['answers'] == parent.model_dump(mode='json')['answers']
        exported = client.get(url.rsplit('/', 2)[0] + '/' + child + '/export')
        assert exported.status_code == 200 and settings['text'] in exported.text
        assert client.post(url, json=body).json()['draft']['id'] == child
    with TestClient(api_for(service, tmp_path), base_url='http://127.0.0.1:3001') as restarted:
        authorize(restarted)
        assert restarted.post(url, json=body).json()['draft']['id'] == child
    with service.store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.kind')='draft'").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='workspace:draft_review'").fetchone() == (1,)
    assert len(sent) == 3


@pytest.mark.parametrize('change', ['partial_vector', 'ack_one', 'extra_text', 'wrong_hash'])
def test_http_review_strict_body_or_disclosure_never_mutates(reviewing, tmp_path, change):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    request = preview_body(service, scope, parent).model_dump(mode='json')
    with TestClient(api_for(service, tmp_path), base_url='http://127.0.0.1:3001') as client:
        authorize(client)
        url = url_for(scope, parent)
        preview = client.post(url + '/preview', json=request)
        assert preview.status_code == 200, preview.text
        body = dict(request, idempotency_key='invalid-review', acknowledged=True,
                    disclosure_sha256=preview.json()['disclosure_sha256'])
        expected = 422
        if change == 'partial_vector':
            del body['expected_revisions']['facts']
        elif change == 'ack_one':
            body['acknowledged'] = 1
        elif change == 'extra_text':
            body['cover_letter'] = 'Caller supplied writing'
        else:
            body['disclosure_sha256'] = '0' * 64
            expected = 409
        response = client.post(url, json=body)
        assert response.status_code == expected, response.text
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output


def test_http_postcommit_response_failure_requires_exact_replay(reviewing, tmp_path, monkeypatch):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    api = api_for(service, tmp_path)
    original = api.state.drafting.detail
    calls = []

    def lost(*args):
        calls.append(args[-1])
        if len(calls) == 1:
            raise RuntimeError('Synthetic response failure after local commit')
        return original(*args)

    monkeypatch.setattr(api.state.drafting, 'detail', lost)
    with TestClient(api, base_url='http://127.0.0.1:3001', raise_server_exceptions=False) as client:
        authorize(client)
        url = url_for(scope, parent)
        request = preview_body(service, scope, parent).model_dump(mode='json')
        preview = client.post(url + '/preview', json=request)
        assert preview.status_code == 200, preview.text
        body = dict(request, idempotency_key='lost-review', acknowledged=True,
                    disclosure_sha256=preview.json()['disclosure_sha256'])
        assert client.post(url, json=body).status_code == 500
        assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output + 1
        replay = client.post(url, json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()['draft']['id'] == calls[0] == calls[1]
        assert replay.json()['reviewed_current']


def test_http_review_csrf_strict_scope_and_parent_ownership(reviewing, tmp_path):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = preview_body(service, scope, parent).model_dump(mode='json')
    url = url_for(scope, parent) + '/preview'
    with TestClient(api_for(service, tmp_path), base_url='http://127.0.0.1:3001') as client:
        assert client.post(url, json=body).status_code == 403
        authorize(client)
        for identity in (scope.profile_id, scope.application_id, parent.id):
            assert client.post(url.replace(identity, str(uuid4())), json=body).status_code == 404
        assert client.post(url.replace(parent.id, 'alias'), json=body).status_code == 422


@pytest.mark.parametrize('operation', ['review_preview', 'review'])
def test_local_review_work_is_offloaded_and_health_remains_responsive(reviewing, tmp_path, monkeypatch, operation):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    api = api_for(service, tmp_path)
    request = (preview_body(service, scope, parent) if operation == 'review_preview'
               else review_body(service, scope, parent))
    original = getattr(api.state.drafting, operation)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def blocked(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(api.state.drafting, operation, blocked)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                     base_url='http://127.0.0.1:3001') as client:
            token = (await client.get('/api/workspace/status')).json()['boot_token']
            client.headers['x-evidence-token'] = token
            url = url_for(scope, parent) + ('/preview' if operation == 'review_preview' else '')
            pending = asyncio.create_task(client.post(url, json=request.model_dump(mode='json')))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                health = await asyncio.wait_for(client.get('/health'), timeout=2)
                assert health.status_code == 200
            finally:
                release.set()
            response = await pending
            assert response.status_code == (200 if operation == 'review_preview' else 201), response.text

    asyncio.run(exercise())
