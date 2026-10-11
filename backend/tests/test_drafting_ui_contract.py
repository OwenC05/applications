"""Authoritative inspection inputs for the local editor; synthetic provider only."""
import pytest
from fastapi.testclient import TestClient
from test_drafting_assessment import assess, assessment  # noqa: F401
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets, run  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.api import create_app
from copilot.config import Settings
from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.drafting.edit_contracts import EditRequest
from copilot.drafting.schemas import DraftOutput, GeneratedText
from copilot.retrieval.packet_contracts import CoverLetterTarget


@pytest.mark.parametrize('origin', ['generation', 'edit', 'assessment'])
def test_inspection_exposes_frozen_target_bindings_and_publication_hash_without_calls(assessment, origin, tmp_path):  # noqa: F811
    service, scope, edited, sent, _ = assessment
    edit_detail = service.detail(scope.profile_id, scope.application_id, edited.id)
    draft_id = edited.id
    if origin == 'generation':
        draft_id = edit_detail['publication']['parent_draft_id']
    elif origin == 'assessment':
        draft_id = assess(assessment)[0].id
    calls = len(sent)
    service.provider._key_supplier = lambda: pytest.fail('Inspection must not read a key')
    service.provider._transport = lambda *_: pytest.fail('Inspection must not transmit')
    detail = service.detail(scope.profile_id, scope.application_id, draft_id)
    with service.store._read() as db:
        _, publication, parameters, _ = service._association(db, scope.profile_id, scope.application_id, draft_id)
    assert detail['publication_sha256'] == publication.publication_sha256 == c.canonical_hash(detail['publication'])
    assert detail['writing_targets'] == [dict(question=t.question.model_dump(mode='json'), packet_id=t.packet_id)
                                         for t in parameters.context.targets]
    assert [t['question']['id'] for t in detail['writing_targets']] == ['writing']
    assert detail['writing_targets'][0]['packet_id'] == detail['draft']['answers'][0]['packet_id']
    listed = service.list(scope.profile_id, scope.application_id)['drafts']
    assert next(item for item in listed if item['draft']['id'] == draft_id) == detail
    assert len(sent) == calls
    assert not detail['review_eligible'] and not detail['browser_eligible']
    api = create_app(Settings(service.store.root, tmp_path / 'absent-models'),
                     lambda *_: pytest.fail('Inspection must not initialize retrieval models'))
    api.state.drafting.provider._key_supplier = lambda: pytest.fail('HTTP inspection must not read a key')
    root = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts'
    with TestClient(api, base_url='http://127.0.0.1:3001') as http:
        response = http.get(root + '/' + draft_id)
        assert response.status_code == 200, response.text
        assert response.json()['publication_sha256'] == detail['publication_sha256']
        assert response.json()['writing_targets'] == detail['writing_targets']
        assert next(item for item in http.get(root).json()['drafts'] if item['draft']['id'] == draft_id)['writing_targets'] == detail['writing_targets']
        saved_export = http.get(root + '/' + draft_id + '/export')
        assert saved_export.status_code == 200, saved_export.text
        assert saved_export.headers['content-type'].startswith('text/plain')
        assert saved_export.content == service.export(scope.profile_id, scope.application_id, draft_id).encode('utf-8')
    assert not (tmp_path / 'absent-models').exists() and len(sent) == calls


def test_explicit_empty_cover_letter_is_a_frozen_last_target_not_inferred_from_text(drafting, packets):  # noqa: F811
    service, scope, _, sent, settings = drafting
    batch, _, _ = run(packets, key='cover-packets', cover_letter_target=CoverLetterTarget(
        text='Optional cover letter', required=False, constraint_origin='user'))
    generated, _, _ = execute((service, scope, batch, sent, settings), 'with-cover')
    detail = service.detail(scope.profile_id, scope.application_id, generated.id)
    assert [t['question']['id'] for t in detail['writing_targets']] == ['writing', 'cover_letter']
    assert [q['id'] for q in detail['canonical_questions']] == ['writing', 'manual']
    request = EditRequest(expected_revisions=generated.revisions,
        parent_text_sha256=generated.text_sha256, parent_ledger_sha256=generated.ledger_sha256,
        parent_publication_sha256=detail['publication_sha256'], idempotency_key='empty-cover',
        draft=DraftOutput(targets=(GeneratedText(target='writing', text=generated.answers[0].text),
                                  GeneratedText(target='cover_letter', text=''))))
    edited = service.edit(scope.profile_id, scope.application_id, generated.id, request)
    inspected = service.detail(scope.profile_id, scope.application_id, edited.id)
    assert inspected['draft']['cover_letter'] == ''
    assert inspected['writing_targets'] == detail['writing_targets']
    assert inspected['publication_sha256'] == c.canonical_hash(inspected['publication'])
    assert len(sent) == 3


@pytest.mark.parametrize('status', [409, 500])
def test_http_edit_can_commit_before_error_and_exact_operation_replay_recovers_one_child(drafting, tmp_path, monkeypatch, status):  # noqa: F811
    from test_drafting_edit_api import api_for, edit_payload

    service, scope, _, sent, _ = drafting
    parent, *_ = execute(drafting)
    payload = edit_payload(service, scope, parent, 'Captured café 😀\r\n', key='postcommit')
    api = api_for(service, tmp_path)
    original = api.state.drafting.detail
    committed = []

    def failed_inspection(owner, application_id, draft_id):
        # Real edit publication has already committed before this API read fails.
        with service.store._read() as db:
            child = service.domain._get(db, owner, draft_id, 'draft', c.DraftRevision)
            assert child.revisions.application_output == parent.revisions.application_output + 1
            assert service.jobs.capture(db, owner, application_id) == child.revisions
        committed.append(draft_id)
        raise EvidenceError('CONFLICT' if status == 409 else 'UNAVAILABLE', 'Synthetic post-commit inspection failure', status)

    monkeypatch.setattr(api.state.drafting, 'detail', failed_inspection)
    route = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{parent.id}/edits'
    with TestClient(api, base_url='http://127.0.0.1:3001') as http:
        http.headers['x-evidence-token'] = http.get('/api/workspace/status').json()['boot_token']
        failed = http.post(route, json=payload)
        assert failed.status_code == status and len(committed) == 1
        monkeypatch.setattr(api.state.drafting, 'detail', original)
        replay = http.post(route, json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()['draft']['id'] == committed[0]
        assert replay.json()['draft']['answers'][0]['text'] == 'Captured café 😀\r\n'
        assert replay.json()['publication']['request']['idempotency_key'] == 'postcommit'
        with service.store._read() as db:
            assert service.jobs.capture(db, scope.profile_id, scope.application_id).application_output == parent.revisions.application_output + 1
            assert db.execute("SELECT COUNT(*) FROM records WHERE owner=? AND kind='workspace:draft'", (scope.profile_id,)).fetchone()[0] == 2
    assert len(sent) == 3
