"""Real packet-bound cover-letter review acceptance; synthetic provider only.

CoverLetterTarget is a separate packet input, not ApplicationPatch state. These
cases do not manufacture canonical cover-letter mutations after publication.
"""
import json

import pytest
import test_drafting_service as generation
from fastapi.testclient import TestClient
from test_drafting_review_api import api_for, authorize, url_for
from test_drafting_review_ui_integration import IMPORTS, node
from test_drafting_reviews import preview_body, review_body
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.retrieval.packet_contracts import CoverLetterTarget


@pytest.fixture
def covering(packets, request, monkeypatch):  # noqa: F811
    store, _, repo, app, scope, *_ = packets
    options = getattr(request, 'param', {})
    # Canonical input is configured before any packet/draft exists. The separate
    # cover target is supplied to the actual packet request, never forged later.
    with store._tx() as db:
        repo._save(db, scope.profile_id, 'application', app.model_copy(update={
            'questions': tuple(q for q in app.questions
                if q.type == 'writing' or options.get('manual', False))}))
    cover = CoverLetterTarget(text='Write an honest cover letter.', constraint_origin='user',
        required=options.get('required', True), max_words=options.get('max_words', 400),
        max_chars=options.get('max_chars'))
    original_run = generation.run
    with monkeypatch.context() as local:
        local.setattr(generation, 'run', lambda value: original_run(value, cover_letter_target=cover))
        configured = generation.drafting.__wrapped__(packets)
    service, _, _, _, settings = configured
    transport = service.provider.transport

    def cover_transport(payload, before_send):
        response = transport(payload, before_send)
        if payload['text']['format']['name'] == 'DraftOutput' and 'cover_text' in settings:
            content = response['output'][0]['content'][0]
            output = json.loads(content['text'])
            for target in output['targets']:
                if target['target'] == 'cover_letter':
                    target['text'] = settings['cover_text']
            content['text'] = json.dumps(output)
        return response

    service.provider.transport = cover_transport
    return configured


def retained_state(service, scope):
    """Byte-identical DB rows, including every publication and accounting row."""
    with service.store._read() as db:
        return {table: tuple(db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall())
            for table in ('records', 'jobs', 'job_keys', 'job_stages', 'job_retries',
                          'provider_attempts', 'provider_retry_links', 'budget_limits',
                          'app_revisions', 'drafting_admission_heads', 'drafting_admission_bindings')}


def test_actual_cover_review_http_exact_ledger_snapshot_export_and_node_edit(covering, tmp_path):
    service, scope, batch, sent, settings = covering
    exact = '  Café e\u0301 😀\r\n\tHonest interest.\n '
    settings['text'] = exact
    parent, *_ = generation.execute(covering)
    assert batch.cover_letter_target is not None
    assert parent.cover_letter == exact
    assert [p.question.id for p in batch.packets] == ['writing', 'cover_letter']
    with TestClient(api_for(service, tmp_path), base_url='http://127.0.0.1:3001') as client:
        authorize(client)
        url = url_for(scope, parent)
        preview = client.post(url + '/preview',
            json=preview_body(service, scope, parent).model_dump(mode='json'))
        assert preview.status_code == 200, preview.text
        disclosure = preview.json()['disclosure']
        assert disclosure['cover_letter_target'] == batch.cover_letter_target.model_dump(mode='json')
        assert disclosure['cover_letter'] == exact
        assert disclosure['ledger'] == parent.model_dump(mode='json')['ledger']
        assert preview.json()['disclosure_sha256'] == c.canonical_hash(disclosure)
        request = dict(preview_body(service, scope, parent).model_dump(mode='json'),
            idempotency_key='cover-review', acknowledged=True,
            disclosure_sha256=preview.json()['disclosure_sha256'])
        response = client.post(url, json=request)
        assert response.status_code == 201, response.text
        review = response.json()
        assert review['draft']['cover_letter'] == exact
        assert review['draft']['answers'] == parent.model_dump(mode='json')['answers']
        assert review['draft']['ledger'] == parent.model_dump(mode='json')['ledger']
        assert review['text_sha256'] == parent.text_sha256
        assert review['ledger_sha256'] == parent.ledger_sha256
        assert review['reviewed_current'] and review['writing_review_ready']
        assert not review['review_eligible'] and not review['browser_eligible']
        snapshot = review['review_snapshot']
        assert snapshot['draft_id'] == review['draft']['id']
        assert snapshot['revisions'] == review['draft']['revisions']
        assert review['publication']['review_snapshot_sha256'] == c.canonical_hash(snapshot)
        base = url.rsplit('/', 2)[0]
        exported = client.get(base + '/' + review['draft']['id'] + '/export')
        assert exported.status_code == 200 and exact in exported.text
        edit = node(IMPORTS + """
const b=new DraftBuffer(input.review);
assert.deepEqual(b.targets(),[{target:'writing',text:input.exact},{target:'cover_letter',text:input.exact}]);
assert.equal(b.matchesSaved(),true);
const req=b.editRequest('cover-node-edit');
assert.deepEqual(req.draft.targets.map(x=>[x.target,x.text]),[['writing',input.exact],['cover_letter',input.exact]]);
assert.deepEqual(req.expected_revisions,input.review.draft.revisions);
console.log(JSON.stringify(req));
""", dict(review=review, exact=exact))
        edited_response = client.post(base + '/' + review['draft']['id'] + '/edits', json=edit)
        assert edited_response.status_code == 201, edited_response.text
        edited = edited_response.json()
        assert edited['draft']['cover_letter'] == exact
        assert edited['draft']['answers'] == review['draft']['answers']
        assert edited['draft']['ledger'] == [] and not edited['draft']['inventory_complete']
        assert edited['draft']['assessment_state'] == 'unassessed'
        assert edited['review_snapshot'] is None and not edited['reviewed_current']
        assert not edited['writing_review_ready'] and not edited['review_eligible']
        node(IMPORTS + """
assert.equal(new DraftBuffer(input.edited).targets()[1].text,input.exact);
console.log(JSON.stringify({verified:true}));
""", dict(edited=edited, exact=exact))
    assert len(sent) == 3


@pytest.mark.parametrize('covering,text,reason', [
    ({'required': True}, ' \r\n\t', 'REQUIRED_ANSWER_MISSING'),
    ({'max_words': 2}, 'one two three', 'OUTPUT_LIMIT_EXCEEDED'),
    ({'max_chars': 3}, '😀abcd', 'OUTPUT_LIMIT_EXCEEDED'),
    ({'manual': True}, 'Honest interest.', 'MANUAL_REQUIREMENTS_UNRESOLVED'),
], indirect=['covering'])
def test_cover_required_limits_and_manual_block_review_without_mutation(covering, text, reason):
    service, scope, _, sent, settings = covering
    settings['cover_text'] = text
    parent, *_ = generation.execute(covering)
    # The synthetic provider emits valid ordinary writing and independent exact
    # cover text. Production generation/assessment binds both normally.
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    assert parent.cover_letter == text and parent.answers[0].text == 'I am interested.'
    assert reason in detail['reason_codes']
    assert not detail['writing_review_ready'] and not detail['review_eligible']
    before = retained_state(service, scope)
    with pytest.raises(EvidenceError) as error:
        service.review_preview(scope.profile_id, scope.application_id, parent.id,
            preview_body(service, scope, parent))
    assert error.value.http_status == 409
    assert retained_state(service, scope) == before
    assert len(sent) == 3


@pytest.mark.parametrize('covering', [{'required': False}], indirect=True)
def test_optional_cover_target_is_truthfully_disclosed_and_reviewable(covering):
    service, scope, batch, sent, _ = covering
    parent, *_ = generation.execute(covering)
    preview = service.review_preview(scope.profile_id, scope.application_id, parent.id,
        preview_body(service, scope, parent))
    assert preview['disclosure']['cover_letter_target'] == batch.cover_letter_target.model_dump(mode='json')
    assert preview['disclosure']['cover_letter_target']['required'] is False
    child = service.review(scope.profile_id, scope.application_id, parent.id,
        review_body(service, scope, parent))
    assert child.cover_letter == parent.cover_letter
    assert service.detail(scope.profile_id, scope.application_id, child.id)['reviewed_current']
    assert len(sent) == 3


@pytest.mark.parametrize('field,value', [('required', False), ('max_words', 350), ('max_chars', 800)])
def test_real_changed_cover_successor_denies_old_ack_without_additional_writes(
        covering, packets, field, value):  # noqa: F811
    service, scope, original_batch, sent, settings = covering
    exact = '  Café e\u0301 😀\r\n\tHonest interest.\n '
    settings['text'] = exact
    parent, *_ = generation.execute(covering)
    old_preview = service.review_preview(scope.profile_id, scope.application_id, parent.id,
        preview_body(service, scope, parent))
    acknowledged = review_body(service, scope, parent, key='old-cover-ack')
    assert acknowledged.disclosure_sha256 == old_preview['disclosure_sha256']
    target = original_batch.cover_letter_target.model_copy(update={field: value})
    # Cover targets are immutable packet inputs, not global application settings.
    # Rebuilding a packet alone does not invalidate the previous packet: the real
    # successor generation below advances output and makes the old parent stale.
    successor_batch, *_ = generation.run(packets, key='successor-cover-packets',
        cover_letter_target=target)
    successor_fixture = service, scope, successor_batch, sent, settings
    successor, *_ = generation.execute(successor_fixture, key='successor-cover-draft')
    assert successor_batch.canonical_questions == original_batch.canonical_questions
    assert successor.cover_letter == parent.cover_letter == exact
    assert successor.answers[0].text == parent.answers[0].text == exact
    assert successor.revisions.application_output == parent.revisions.application_output + 1
    before_denial = retained_state(service, scope)
    with pytest.raises(EvidenceError) as error:
        service.review(scope.profile_id, scope.application_id, parent.id, acknowledged)
    assert error.value.code == 'CONFLICT' and error.value.http_status == 409
    assert retained_state(service, scope) == before_denial
    fresh = service.review_preview(scope.profile_id, scope.application_id, successor.id,
        preview_body(service, scope, successor))
    disclosure = fresh['disclosure']
    assert disclosure['cover_letter_target'] == target.model_dump(mode='json')
    assert disclosure['canonical_questions'] == old_preview['disclosure']['canonical_questions']
    assert disclosure['cover_letter'] == old_preview['disclosure']['cover_letter'] == exact
    assert disclosure['batch_id'] == successor_batch.id != original_batch.id
    assert disclosure['batch_sha256'] == successor_batch.batch_sha256 != original_batch.batch_sha256
    assert disclosure['pre_revisions'] == successor.revisions.model_dump(mode='json')
    assert disclosure['pre_revisions'] != old_preview['disclosure']['pre_revisions']
    assert fresh['disclosure_sha256'] == c.canonical_hash(disclosure)
    assert fresh['disclosure_sha256'] != acknowledged.disclosure_sha256
    # Rebinding every parent/vector/hash field to the current successor must not
    # make the old disclosure acknowledgment valid for the changed target.
    rebound = review_body(service, scope, successor, key='rebound-old-cover-disclosure')
    rebound = rebound.model_copy(update={'disclosure_sha256': acknowledged.disclosure_sha256})
    before_rebound_denial = retained_state(service, scope)
    with pytest.raises(EvidenceError, match='disclosure changed') as rebound_error:
        service.review(scope.profile_id, scope.application_id, successor.id, rebound)
    assert rebound_error.value.code == 'CONFLICT' and rebound_error.value.http_status == 409
    assert retained_state(service, scope) == before_rebound_denial
    historical = service.detail(scope.profile_id, scope.application_id, parent.id)
    assert not historical['current'] and historical['draft']['cover_letter'] == exact
    assert exact in service.export(scope.profile_id, scope.application_id, parent.id)
    assert len(sent) == 6
