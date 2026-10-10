"""Local edit behavior with synthetic generation; no new paid/model capability."""
import json

import pytest
from pydantic import ValidationError
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.drafting.edit_contracts import (
    EditPublication,
    EditRequest,
    edit_reason_codes,
    edit_request_hash,
)
from copilot.drafting.schemas import DraftOutput, GeneratedText


def request(service, scope, parent, text, key='edit'):
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    return EditRequest(expected_revisions=parent.revisions,
        parent_text_sha256=parent.text_sha256, parent_ledger_sha256=parent.ledger_sha256,
        parent_publication_sha256=c.canonical_hash(detail['publication']),
        draft=DraftOutput(targets=tuple(GeneratedText(target=a.question_id, text=text) for a in parent.answers)),
        idempotency_key=key)


def test_generation_edit_edit_exact_text_immutable_parent_no_paid_io(drafting):  # noqa: F811
    service, scope, _, sent, settings = drafting
    settings['unsupported'] = True
    parent, _, _ = execute(drafting)
    assert parent.ledger
    first_request = request(service, scope, parent, parent.answers[0].text)
    service.provider._key_supplier = lambda: pytest.fail('local edit read a key')
    service.provider.transport = lambda *a: pytest.fail('local edit sent a provider request')
    first = service.edit(scope.profile_id, scope.application_id, parent.id, first_request)
    assert first.answers == parent.answers
    assert first.ledger == () and first.assessment_state == 'unassessed' and not first.inventory_complete
    assert not first.support_ready
    assert first.revisions.application_output == parent.revisions.application_output + 1
    exact = '  Café e\u0301 😀\n\tA\r\n '
    second_request = request(service, scope, first, exact, 'second')
    second = service.edit(scope.profile_id, scope.application_id, first.id, second_request)
    assert second.answers[0].text == exact
    assert second.revisions.application_output == first.revisions.application_output + 1
    detail = service.detail(scope.profile_id, scope.application_id, second.id)
    assert detail['current'] and detail['publication']['edit_depth'] == 2
    assert detail['publication']['pre_revisions'] == first.revisions.model_dump(mode='json')
    assert not detail['review_eligible'] and not detail['browser_eligible']
    assert 'ASSESSMENT_REQUIRED' in detail['reason_codes']
    assert 'MANUAL_REQUIREMENTS_UNRESOLVED' in detail['reason_codes']
    assert detail['capabilities'] == dict(generation=True, edit=True, reassess=False, review=False)
    assert service.detail(scope.profile_id, scope.application_id, parent.id)['draft'] == parent.model_dump(mode='json')
    assert len(sent) == 3
    with service.store._read() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE json_extract(data,'$.kind')='draft'").fetchone() == (1,)


def test_exact_replay_precedes_staleness_and_conflicting_request_rejected(drafting):  # noqa: F811
    service, scope, *_ = drafting
    parent, _, _ = execute(drafting)
    body = request(service, scope, parent, 'first')
    first = service.edit(scope.profile_id, scope.application_id, parent.id, body)
    second = service.edit(scope.profile_id, scope.application_id, first.id, request(service, scope, first, 'later', 'later'))
    assert service.edit(scope.profile_id, scope.application_id, parent.id, body) == first
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == second.revisions.application_output
    changed = body.model_copy(update={'draft': DraftOutput(targets=(GeneratedText(target='writing', text='changed'),))})
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, changed)


@pytest.mark.parametrize('text,reason', [('', 'REQUIRED_ANSWER_MISSING'), (' \n\t', 'REQUIRED_ANSWER_MISSING'),
    ('word ' * 201, 'OUTPUT_LIMIT_EXCEEDED')])
def test_invalid_limits_publish_exact_unassessed_edit(drafting, text, reason):  # noqa: F811
    service, scope, *_ = drafting
    parent, _, _ = execute(drafting)
    child = service.edit(scope.profile_id, scope.application_id, parent.id, request(service, scope, parent, text))
    assert child.answers[0].text == text
    assert reason in service.detail(scope.profile_id, scope.application_id, child.id)['reason_codes']


@pytest.mark.parametrize('changes', [dict(expected_revisions={'facts': 0}), dict(idempotency_key=''),
    dict(ledger=[]), dict(assessment_state='assessed'), dict(parent_text_sha256='x'),
    dict(draft=dict(targets=[dict(target='writing', text='\ud800')]))])
def test_edit_request_wire_strict(drafting, changes):  # noqa: F811
    service, scope, *_ = drafting
    parent, _, _ = execute(drafting)
    body = request(service, scope, parent, 'exact').model_dump(mode='json') | changes
    with pytest.raises(ValidationError):
        EditRequest.model_validate_json(json.dumps(body))


@pytest.mark.parametrize('targets', [(), ('extra',), ('writing', 'writing'), ('other', 'writing')])
def test_edit_exact_target_inventory_required(drafting, targets):  # noqa: F811
    service, scope, *_ = drafting
    parent, _, _ = execute(drafting)
    body = request(service, scope, parent, 'exact')
    # model_copy deliberately bypasses validation: service must validate again.
    forged = body.model_copy(update={'draft': body.draft.model_copy(update={
        'targets': tuple(GeneratedText(target=target, text='x') for target in targets)})})
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, forged)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output


def test_reason_helper_optional_manual_and_full_request_hash(drafting):  # noqa: F811
    service, scope, *_ = drafting
    parent, _, _ = execute(drafting)
    body = request(service, scope, parent, 'exact')
    with service.store._read() as db:
        _, _, parameters, _ = service._association(db, scope.profile_id, scope.application_id, parent.id)
    context = parameters.context.model_copy(update={'required_manual_question_ids': ()})
    assert edit_reason_codes(context, body.draft) == ('ASSESSMENT_REQUIRED',)
    assert edit_request_hash(scope.profile_id, scope.application_id, parent.id, body) == c.canonical_hash(dict(
        operation='local_draft_edit', profile_id=scope.profile_id, application_id=scope.application_id,
        parent_draft_id=parent.id, request=body.model_dump(mode='json')))
    child = service.edit(scope.profile_id, scope.application_id, parent.id, body)
    detail = service.detail(scope.profile_id, scope.application_id, child.id)
    publication = EditPublication.model_validate_json(json.dumps(detail['publication']))
    assert publication.publication_sha256 == c.canonical_hash(detail['publication'])


@pytest.mark.parametrize('changes', [dict(expected_revisions={'facts': 0}),
    dict(draft={'targets': [{'target': 'writing', 'text': 'x', 'ledger': []}]}),
    dict(parent_publication_sha256='not-a-digest')])
def test_copied_request_revalidated_at_service_boundary(drafting, changes):  # noqa: F811
    service, scope, *_ = drafting
    parent, _, _ = execute(drafting)
    body = request(service, scope, parent, 'exact').model_copy(update=changes)
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, body)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output
