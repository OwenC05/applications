"""Local immutable edit/export HTTP boundaries; synthetic data and no cloud IO."""
import asyncio
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.api import create_app
from copilot.config import Settings


def edit_payload(service, scope, parent, text='  Exact Unicode: Zoë 🧠\n\n', key='http-edit'):
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    with service.store._read() as db:
        _, _, parameters, _ = service._association(db, scope.profile_id, scope.application_id, parent.id)
    return dict(expected_revisions=parent.revisions.model_dump(mode='json'),
                parent_text_sha256=detail['text_sha256'], parent_ledger_sha256=detail['ledger_sha256'],
                parent_publication_sha256=service_publication_hash(service, scope, parent.id),
                draft=dict(targets=[dict(target=t.question.id, text=text) for t in parameters.context.targets]),
                idempotency_key=key)


def service_publication_hash(service, scope, draft_id):
    with service.store._read() as db:
        return service._association(db, scope.profile_id, scope.application_id, draft_id)[1].publication_sha256


def api_for(service, tmp_path):
    api = create_app(Settings(service.store.root, tmp_path / 'absent-models'),
                     lambda *_: pytest.fail('Local edit/export loaded retrieval models'))
    api.state.drafting.provider._key_supplier = lambda: pytest.fail('Local edit/export read a provider key')
    return api


def test_http_immutable_edit_replay_and_clean_current_stale_export(drafting, tmp_path):  # noqa: F811
    service, scope, _, sent, _ = drafting
    parent, *_ = execute(drafting)
    text = '  Exact Unicode: Zoë 🧠\n\n'
    payload = edit_payload(service, scope, parent, text)
    api = api_for(service, tmp_path)
    base = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']
        response = client.post(f'{base}/{parent.id}/edits', json=payload)
        assert response.status_code == 201, response.text
        edited = response.json()
        child = edited['draft']
        assert edited['current'] and child['answers'][0]['text'] == text
        assert child['ledger'] == [] and child['assessment_state'] == 'unassessed'
        assert not child['inventory_complete'] and not edited['review_eligible'] and not edited['browser_eligible']
        assert edited['capabilities'] == dict(generation=True, edit=True, reassess=False, review=False)
        assert 'ASSESSMENT_REQUIRED' in edited['reason_codes']
        assert child['revisions']['application_output'] == parent.revisions.application_output + 1
        replay = client.post(f'{base}/{parent.id}/edits', json=payload)
        assert replay.status_code == 201 and replay.json()['draft']['id'] == child['id']
        assert len(client.get(base).json()['drafts']) == 2
        assert not client.get(f'{base}/{parent.id}').json()['current']
        for draft_id in (parent.id, child['id']):
            exported = client.get(f'{base}/{draft_id}/export')
            assert exported.status_code == 200, exported.text
            assert exported.headers['content-type'] == 'text/plain; charset=utf-8'
            assert exported.headers['content-disposition'] == 'attachment; filename="application-draft.txt"'
            assert exported.headers['cache-control'] == 'no-store'
            assert exported.headers['x-content-type-options'] == 'nosniff'
            assert 'export is not review or submission approval' in exported.text
            assert 'publication_sha256' not in exported.text and 'provider_attempts' not in exported.text
            if draft_id == child['id']:
                assert text in exported.text
                assert edited['canonical_questions'][0]['text'] in exported.text
        changed = dict(payload, idempotency_key='http-edit')
        changed['draft'] = dict(targets=[dict(target=t['target'], text='different') for t in payload['draft']['targets']])
        assert client.post(f'{base}/{parent.id}/edits', json=changed).status_code == 409
    assert len(sent) == 3


@pytest.mark.parametrize('change', ['missing_vector', 'extra_ledger', 'unknown_field', 'bool_revision'])
def test_http_edit_strict_request_rejected_before_mutation(drafting, tmp_path, change):  # noqa: F811
    service, scope, _, sent, _ = drafting
    parent, *_ = execute(drafting)
    payload = edit_payload(service, scope, parent)
    if change == 'missing_vector':
        del payload['expected_revisions']['facts']
    elif change == 'bool_revision':
        payload['expected_revisions']['facts'] = True
    elif change == 'extra_ledger':
        payload['ledger'] = []
    else:
        payload['provider_key'] = 'not-an-accepted-field'
    api = api_for(service, tmp_path)
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']
        base = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
        assert client.post(f'{base}/{parent.id}/edits', json=payload).status_code == 422
        assert len(client.get(base).json()['drafts']) == 1
    assert len(sent) == 3


def test_http_edit_owner_csrf_and_corrupt_export_fail_closed(drafting, tmp_path):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    payload = edit_payload(service, scope, parent)
    api = api_for(service, tmp_path)
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        base = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
        assert client.post(f'{base}/{parent.id}/edits', json=payload).status_code == 403
        client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']
        wrong = f'/api/workspace/profiles/{uuid4()}/applications/{scope.application_id}/drafts/{parent.id}'
        assert client.post(wrong + '/edits', json=payload).status_code == 404
        assert client.get(wrong + '/export').status_code == 404
        with service.store._tx() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cover_letter','forged') WHERE id=?", (parent.id,))
        assert client.get(f'{base}/{parent.id}/export').status_code == 409


def test_http_edit_offloads_original_verification_and_keeps_health_responsive(drafting, tmp_path, monkeypatch):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    payload = edit_payload(service, scope, parent)
    api = api_for(service, tmp_path)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def blocked_edit(*_):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3), 'Test failed to release owned edit worker'
        return parent

    monkeypatch.setattr(api.state.drafting, 'edit', blocked_edit)

    async def scenario():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:3001') as client:
            client.headers['x-evidence-token'] = (await client.get('/api/workspace/status')).json()['boot_token']
            url = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{parent.id}/edits'
            pending = asyncio.create_task(client.post(url, json=payload))
            try:
                assert await asyncio.to_thread(entered.wait, 2), 'Edit work never entered its thread'
                health = await asyncio.wait_for(client.get('/health'), timeout=.5)
                assert health.status_code == 200
            finally:
                release.set()
            response = await pending
            assert response.status_code == 201, response.text

    asyncio.run(scenario())
