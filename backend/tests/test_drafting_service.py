"""Synthetic Responses with real canonical packets; never paid/model network IO."""
import json
from uuid import uuid4

import pytest
from test_packet_service import packets, run  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain.repository import ConsentInput
from copilot.drafting.runtime_contracts import DraftPreviewRequest, DraftRequest
from copilot.drafting.service import DraftingService
from copilot.provider import Limits, Provider


@pytest.fixture
def drafting(packets):  # noqa: F811
    store, research, repo, app, scope, _, packet_service = packets
    batch, _, _ = run(packets)
    consent = repo._singleton
    with store._read() as db:
        from copilot.domain.contracts import ConsentRecord
        current = consent(db, scope.profile_id, 'consent', ConsentRecord)
    repo.consent(scope.profile_id, ConsentInput(expected_consent_revision=current.revision if current else 0,
        provider='openai', purposes=('research', 'drafting'), granted=True))
    sent = []
    settings = {'rewrite': False, 'unsupported': False, 'text': 'I am interested.'}

    def transport(payload, before_send):
        before_send()
        sent.append(payload)
        data = json.loads(payload['input'])
        name = payload['text']['format']['name']
        targets = data['context']['targets']
        if name == 'PlanOutput':
            output = dict(targets=[dict(target=t['question']['id'], outline=[], evidence_ids=[], gaps=[]) for t in targets])
        elif name == 'DraftOutput':
            output = dict(targets=[dict(target=t['question']['id'], text=settings['text']) for t in targets])
        else:
            assessments = []
            for item in data['draft']['targets']:
                span = dict(start=0, end=len(item['text']), excerpt=item['text'])
                assessments.append(dict(target=item['target'], inventory_complete=True,
                    claims=[dict(**span, kind='personal', assessment='unsupported', evidence_ids=[])] if settings['unsupported'] else [],
                    nonfactual_ranges=[] if settings['unsupported'] else [span]))
            output = dict(text_sha256=data['text_sha256'], targets=assessments, style_notes=[],
                          rewrite_required=settings['rewrite'])
        return dict(status='completed', output=[dict(type='message', content=[dict(type='output_text', text=json.dumps(output))])], usage=dict(total_tokens=20))

    provider = Provider(research.jobs, lambda: 'synthetic', transport)
    provider.configure_limits(scope.profile_id, Limits(10_000_000, 100, 2_000_000, 5))
    service = DraftingService(store, jobs=research.jobs, provider=provider)
    return service, scope, batch, sent, settings


def body(drafting, key='draft'):
    service, scope, batch, _, _ = drafting
    with service.store._read() as db:
        revisions = service.jobs.capture(db, scope.profile_id, scope.application_id)
    preview_request = DraftPreviewRequest(batch_id=batch.id, batch_sha256=batch.batch_sha256,
        expected_revisions=revisions, model='synthetic-model')
    preview = service.preview(scope.profile_id, scope.application_id, preview_request)
    return DraftRequest(**preview_request.model_dump(), idempotency_key=key,
                        disclosure_sha256=preview['disclosure_sha256'], acknowledged=True)


def enqueue(drafting, key='draft'):
    service, scope, *_ = drafting
    request = body(drafting, key)
    return service.enqueue(scope.profile_id, scope.application_id, request), request


def execute(drafting, key='draft'):
    service, *_ = drafting
    job, request = enqueue(drafting, key)
    worker = str(uuid4())
    lease = service.jobs.claim(worker, job_id=job.id)
    return service.run_draft(job.id, worker, lease.fence), job, request


@pytest.mark.parametrize('rewrite,count', [(False, 3), (True, 5)])
def test_real_provider_three_or_five_calls_atomic_publication_and_inspection(drafting, rewrite, count):
    service, scope, batch, sent, settings = drafting
    settings['rewrite'] = rewrite
    draft, job, request = execute(drafting)
    assert len(sent) == count
    assert draft.revisions.application_output == job.revisions.application_output + 1
    assert service.jobs.get(scope.profile_id, job.id).state == 'completed'
    detail = DraftingService(service.store).detail(scope.profile_id, scope.application_id, draft.id)
    assert detail['current'] and detail['draft']['answers'][0]['packet_id'] == batch.packets[0].id
    assert 'MANUAL_REQUIREMENTS_UNRESOLVED' in detail['reason_codes']
    assert not detail['review_eligible'] and not detail['browser_eligible']
    assert service.enqueue(scope.profile_id, scope.application_id, request).id == job.id
    assert len(service.list(scope.profile_id, scope.application_id)['drafts']) == 1


def test_preview_exact_selected_disclosure_no_key_or_send(drafting):
    service, scope, batch, sent, _ = drafting
    service.provider._key_supplier = lambda: pytest.fail('Preview read a key')
    request = body(drafting)
    assert not sent
    with service.store._read() as db:
        assert not db.execute("SELECT 1 FROM provider_attempts WHERE job_id IN (SELECT id FROM jobs WHERE json_extract(data,'$.kind')='draft')").fetchone()
    altered = request.model_copy(update={'model': 'different-model'})
    with pytest.raises(EvidenceError, match='disclosure'):
        service.enqueue(scope.profile_id, scope.application_id, altered)


@pytest.mark.parametrize('unsupported,limit', [(True, False), (False, True)])
def test_well_formed_blocked_text_preserved(drafting, unsupported, limit):
    service, scope, _, _, settings = drafting
    settings['unsupported'] = unsupported
    settings['text'] = 'word ' * 201 if limit else 'I led every project.'
    draft, *_ = execute(drafting)
    assert draft.answers[0].text == settings['text']
    detail = service.detail(scope.profile_id, scope.application_id, draft.id)
    assert ('CLAIM_UNSUPPORTED' if unsupported else 'OUTPUT_LIMIT_EXCEEDED') in detail['reason_codes']


def test_no_budget_cannot_queue_or_send(drafting):
    service, scope, _, sent, _ = drafting
    with service.store._tx() as db:
        db.execute('DELETE FROM budget_limits WHERE owner=?', (scope.profile_id,))
    request = body(drafting)
    with pytest.raises(EvidenceError) as caught:
        service.enqueue(scope.profile_id, scope.application_id, request)
    assert caught.value.code == 'BUDGET_REQUIRED'
    assert not sent


def test_disclosure_exact_selected_excerpts_and_full_questions_only(drafting):
    service, scope, batch, sent, _ = drafting
    request = body(drafting)
    preview_request = DraftPreviewRequest.model_validate_json(request.model_dump_json(exclude={
        'idempotency_key', 'disclosure_sha256', 'acknowledged'}))
    result = service.preview(scope.profile_id, scope.application_id, preview_request)
    selected = result['disclosure']['selected_context']
    assert len(selected['targets']) == len(batch.packets)
    for disclosed, packet in zip(selected['targets'], batch.packets, strict=True):
        assert disclosed['question'] == packet.question.model_dump(mode='json')
        assert [item['excerpt'] for item in disclosed['employer']] == [ref.span.excerpt for ref in packet.employer]
        assert [item['excerpt'] for item in disclosed['facts']] == [ref.span.excerpt for ref in packet.facts]
        assert all(set(item) == {'id', 'kind', 'excerpt'} for item in disclosed['facts'] + disclosed['employer'])
    serialized = json.dumps(selected)
    assert 'original_sha256' not in serialized and 'research_run_id' not in serialized
    assert 'profile_id' not in serialized and 'generation_id' not in serialized
    assert 'Upload a document' not in serialized
    assert result['disclosure']['conservative_reservation_units'] == 5 * (131072 + request.max_output_tokens)
    assert not sent


@pytest.mark.parametrize('change', [dict(expected_revisions={'facts': 0}), dict(model='https://example.com'),
    dict(max_output_tokens=8001), dict(max_output_tokens=True), dict(acknowledged=1), dict(acknowledged=False)])
def test_request_wire_contract_is_strict(drafting, change):
    from pydantic import ValidationError
    request = body(drafting)
    with pytest.raises(ValidationError):
        DraftRequest.model_validate_json(json.dumps(request.model_dump(mode='json') | change))
