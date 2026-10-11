"""Synthetic one-call exact-text assessments, with real durable Provider guards."""
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_drafting_edits import request as edit_request
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ConsentInput
from copilot.drafting.admission_contracts import AssessmentLaunch
from copilot.drafting.assessment_contracts import AssessmentPreviewRequest, AssessmentRequest


@pytest.fixture
def assessment(drafting):  # noqa: F811
    service, scope, _, sent, settings = drafting
    with service.store._read() as db:
        current = service.domain._singleton(db, scope.profile_id, 'consent', c.ConsentRecord)
    service.domain.consent(scope.profile_id, ConsentInput(expected_consent_revision=current.revision,
        provider='openai', purposes=('research', 'drafting', 'assessment'), granted=True))
    generated, _, _ = execute(drafting)
    exact = '  Café e\u0301 😀\n\tA\r\n '
    parent = service.edit(scope.profile_id, scope.application_id, generated.id,
        edit_request(service, scope, generated, exact))
    return service, scope, parent, sent, settings


def assessment_body(assessment, key='assessment'):
    service, scope, parent, *_ = assessment
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    request = AssessmentPreviewRequest(expected_revisions=parent.revisions, parent_text_sha256=parent.text_sha256,
        parent_ledger_sha256=parent.ledger_sha256, parent_publication_sha256=c.canonical_hash(detail['publication']), model='synthetic-model')
    preview = service.assessment_preview(scope.profile_id, scope.application_id, parent.id, request)
    return AssessmentRequest(**request.model_dump(), idempotency_key=key,
        disclosure_sha256=preview['disclosure_sha256'], acknowledged=True)


def capture_assessment_launch(service, scope, request):
    state = service.admission_state(scope.profile_id, scope.application_id)
    return AssessmentLaunch(admission_revision=state['admission_revision'], request=request)


def assess(assessment, *, launch=None):
    service, scope, parent, *_ = assessment
    launch = launch if launch is not None else capture_assessment_launch(service, scope, assessment_body(assessment))
    job = service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, launch)
    worker = str(uuid4())
    lease = service.jobs.claim(worker, job_id=job.id)
    return service.run_assess(job.id, worker, lease.fence), job, launch.request


@pytest.mark.parametrize('rewrite', [False, True])
def test_one_call_exact_unicode_fresh_ledger_no_rewrite(assessment, rewrite):
    service, scope, parent, sent, settings = assessment
    settings['rewrite'] = rewrite
    settings['unsupported'] = True
    launch = capture_assessment_launch(service, scope, assessment_body(assessment))
    draft, job, body = assess(assessment, launch=launch)
    assert len(sent) == 4
    assert draft.answers == parent.answers and draft.cover_letter == parent.cover_letter
    assert draft.text_sha256 == parent.text_sha256
    assert draft.assessment_state == 'assessed' and draft.ledger and not parent.ledger
    assert draft.revisions.application_output == parent.revisions.application_output + 1
    detail = service.detail(scope.profile_id, scope.application_id, draft.id)
    assert detail['current'] and detail['publication']['stage_names'] == ['reassess']
    assert detail['publication']['edit_depth'] == 1
    assert 'CLAIM_UNSUPPORTED' in detail['reason_codes']
    assert not detail['review_eligible'] and not detail['browser_eligible']
    assert service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, launch).id == job.id
    assert len(sent) == 4


def test_preview_discloses_exact_text_no_key_or_send(assessment):
    service, scope, parent, sent, _ = assessment
    service.provider._key_supplier = lambda: pytest.fail('preview must not read a key')
    body = assessment_body(assessment)
    request = AssessmentPreviewRequest.model_validate_json(body.model_dump_json(exclude={'idempotency_key','disclosure_sha256','acknowledged'}))
    preview = service.assessment_preview(scope.profile_id, scope.application_id, parent.id, request)
    assert preview['disclosure']['input_draft']['targets'][0]['text'] == parent.answers[0].text
    assert preview['disclosure']['maximum_calls'] == 1
    assert preview['disclosure']['conservative_reservation_units'] == 131072 + body.max_output_tokens
    assert len(sent) == 3
    with pytest.raises(EvidenceError):
        service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, capture_assessment_launch(service, scope, body.model_copy(update={'model': 'other'})))


@pytest.mark.parametrize('change', [dict(acknowledged=1),dict(max_output_tokens=True),dict(expected_revisions={'facts':0}),dict(batch_id=None),dict(model='https://bad'),dict(context={})])
def test_strict_wire(assessment, change):
    body = assessment_body(assessment)
    with pytest.raises(ValidationError):
        AssessmentRequest.model_validate_json(json.dumps(body.model_dump(mode='json') | change))


@pytest.mark.parametrize('failure', ['budget','key','consent'])
def test_authority_failure_zero_assessment_sends(assessment, failure):
    service, scope, parent, sent, _ = assessment
    if failure == 'budget':
        with service.store._tx() as db:
            db.execute('DELETE FROM budget_limits WHERE owner=?', (scope.profile_id,))
        with pytest.raises(EvidenceError):
            assess(assessment)
    elif failure == 'key':
        service.provider._key_supplier = lambda: ''
        with pytest.raises(EvidenceError):
            assess(assessment)
    else:
        body = assessment_body(assessment)
        with service.store._read() as db:
            current = service.domain._singleton(db, scope.profile_id, 'consent', c.ConsentRecord)
        service.domain.consent(scope.profile_id, ConsentInput(expected_consent_revision=current.revision,
            provider='openai', purposes=('research','drafting'), granted=True))
        with pytest.raises(EvidenceError):
            service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, body)
    assert len(sent) == 3
    assert service.detail(scope.profile_id, scope.application_id, parent.id)['draft']['answers'] == [a.model_dump(mode='json') for a in parent.answers]


@pytest.mark.parametrize('change', [dict(acknowledged=1), dict(max_output_tokens=True), dict(model='bad/url')])
def test_unvalidated_copy_rejected_before_transmission(assessment, change):
    service, scope, parent, sent, _ = assessment
    body = assessment_body(assessment)
    with pytest.raises(EvidenceError):
        service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, body.model_copy(update=change))
    assert len(sent) == 3


def test_assessed_generation_is_not_reassessment_parent(assessment):
    service, scope, parent, sent, _ = assessment
    with service.store._read() as db:
        rows = db.execute("SELECT data FROM records WHERE owner=? AND kind='workspace:draft_dependency' AND json_extract(data,'$.origin')='edit'", (scope.profile_id,)).fetchall()
        ancestor = json.loads(rows[0][0])['parent_draft_id']
    body = assessment_body(assessment)
    with pytest.raises(EvidenceError):
        service.assessment_preview(scope.profile_id, scope.application_id, ancestor,
            AssessmentPreviewRequest.model_validate_json(body.model_dump_json(exclude={'idempotency_key','disclosure_sha256','acknowledged'})))
    assert len(sent) == 3


@pytest.mark.parametrize('tamper', ['owner', 'application', 'attempt_id', 'stage_fence'])
def test_completed_reuse_checks_retained_authority_before_publication(assessment, monkeypatch, tamper):
    from datetime import timedelta
    service, scope, parent, sent, _ = assessment
    body = assessment_body(assessment)
    job = service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, capture_assessment_launch(service, scope, body))
    worker = str(uuid4())
    lease = service.jobs.claim(worker, job_id=job.id)
    commit = service.jobs.commit_stage

    def interrupted(job_id, worker_id, fence, stage, result, **kwargs):
        if stage == 'assessment_published':
            raise RuntimeError('synthetic process interruption')
        return commit(job_id, worker_id, fence, stage, result, **kwargs)

    monkeypatch.setattr(service.jobs, 'commit_stage', interrupted)
    with pytest.raises(RuntimeError):
        service.run_assess(job.id, worker, lease.fence)
    with service.store._tx() as db:
        current, _, _ = service.jobs._job(db, job.id)
        service.jobs._save(db, current.model_copy(update={'lease_expires_at': service.jobs._now() - timedelta(seconds=1)}))
        if tamper == 'owner':
            db.execute('UPDATE provider_attempts SET owner=? WHERE job_id=?', (str(uuid4()), job.id))
        elif tamper == 'stage_fence':
            db.execute("UPDATE job_stages SET fence=1.5 WHERE job_id=? AND stage='assessment_stage_reassess'", (job.id,))
        else:
            raw = json.loads(db.execute('SELECT data FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone()[0])
            raw['application_id' if tamper == 'application' else 'id'] = str(uuid4())
            db.execute('UPDATE provider_attempts SET data=? WHERE job_id=?', (json.dumps(raw), job.id))
    monkeypatch.setattr(service.jobs, 'commit_stage', commit)
    worker2 = str(uuid4())
    with pytest.raises(EvidenceError):
        reclaimed = service.jobs.claim(worker2, job_id=job.id)
        service.run_assess(job.id, worker2, reclaimed.fence)
    assert len(sent) == 4
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output


def test_rehashed_history_cannot_rebind_captured_parent_before_stale_bypass(assessment, monkeypatch):
    from copilot.drafting.assessment_contracts import AssessmentPublication, assessment_request_hash
    from copilot.jobs import encode

    service, scope, _, sent, _ = assessment
    child, job, _ = assess(assessment)
    detail = service.detail(scope.profile_id, scope.application_id, child.id)
    with service.store._tx() as db:
        app = service.domain._get(db, scope.profile_id, scope.application_id, 'application', c.ApplicationRecord)
        service.domain._save(db, scope.profile_id, 'application', app.model_copy(update={'input_revision': app.input_revision + 1}))
    monkeypatch.setattr(service.store, 'verify_scoped_sources', lambda *a: pytest.fail('Stale history touched original files'))
    assert not service.detail(scope.profile_id, scope.application_id, child.id)['current']

    with service.store._tx() as db:
        raw = json.loads(db.execute('SELECT parameters FROM jobs WHERE id=?', (job.id,)).fetchone()[0])
        wrong_parent = str(uuid4())
        request = AssessmentRequest.model_validate_json(encode(raw['request']))
        digest = assessment_request_hash(scope.profile_id, scope.application_id, wrong_parent, request)
        raw.update(parent_draft_id=wrong_parent, request_sha256=digest)
        db.execute('UPDATE jobs SET parameters=? WHERE id=?', (encode(raw), job.id))
        db.execute('UPDATE job_keys SET request_sha256=? WHERE owner=? AND key=?',
            (digest, scope.profile_id, request.idempotency_key))
        publication = detail['publication'] | {'request_sha256': digest}
        intent = json.loads(db.execute('SELECT data FROM records WHERE id=?', (publication['intent_id'],)).fetchone()[0])
        intent.update(parent_draft_id=wrong_parent, request_sha256=digest)
        db.execute('UPDATE records SET data=? WHERE id=?', (encode(intent), publication['intent_id']))
        db.execute('UPDATE records SET data=? WHERE id=?', (encode(publication), publication['id']))
        summary = service._summary(child, AssessmentPublication.model_validate_json(encode(publication)))
        db.execute("UPDATE job_stages SET data=?,sha256=? WHERE job_id=? AND stage='assessment_published'",
            (encode(summary), c.text_hash(encode(summary)), job.id))

    with pytest.raises(EvidenceError):
        service.detail(scope.profile_id, scope.application_id, child.id)
    assert len(sent) == 4
