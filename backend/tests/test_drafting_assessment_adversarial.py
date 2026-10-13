"""Adversarial reassessment lifecycle with real Provider and synthetic transport."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from admission_legacy_helpers import preupgrade_copy
from test_drafting_assessment import capture_assessment_launch
from test_drafting_edits_adversarial import edit, edit_request
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ApplicationInput, ApplicationPatch, ConsentInput
from copilot.drafting.assessment_contracts import AssessmentPreviewRequest, AssessmentRequest
from copilot.drafting.service import DraftingService
from copilot.provider import Limits
from copilot.store import Store


def consent(service, scope):
    with service.store._read() as db:
        revision = service.domain.revisions(db, scope.profile_id).consent
    service.domain.consent(scope.profile_id, ConsentInput(expected_consent_revision=revision,
        provider='openai', purposes=('research', 'drafting', 'assessment'), granted=True))


@pytest.fixture
def assessment(drafting):  # noqa: F811
    service, scope, *_ = drafting
    consent(service, scope)
    parent, *_ = execute(drafting)
    return drafting, edit(service, scope, parent)


def request_for(service, scope, parent, key='assessment'):
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    preview = AssessmentPreviewRequest(expected_revisions=parent.revisions,
        parent_text_sha256=parent.text_sha256, parent_ledger_sha256=parent.ledger_sha256,
        parent_publication_sha256=c.canonical_hash(detail['publication']), model='synthetic-model')
    disclosure = service.assessment_preview(scope.profile_id, scope.application_id, parent.id, preview)
    return AssessmentRequest(**preview.model_dump(), idempotency_key=key,
        disclosure_sha256=disclosure['disclosure_sha256'], acknowledged=True)


def queued(assessment, key='assessment', *, launch=None):
    local, parent = assessment
    service, scope, *_ = local
    launch = launch if launch is not None else capture_assessment_launch(service, scope, request_for(service, scope, parent, key))
    request = launch.request
    job = service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, launch)
    worker = str(uuid4())
    leased = service.jobs.claim(worker, job_id=job.id)
    return job, worker, leased.fence, request


def assessed(assessment, key='assessment', *, launch=None):
    job, worker, fence, request = queued(assessment, key, launch=launch)
    return assessment[0][0].run_assess(job.id, worker, fence), job, request


def test_reassessment_exactly_one_call_ignores_rewrite_and_preserves_exact_text(assessment):
    local, parent = assessment
    service, scope, _, sent, settings = local
    settings['rewrite'] = True
    before = len(sent)
    child, job, _ = assessed(assessment)
    assert len(sent) == before + 1
    assert child.text_sha256 == parent.text_sha256
    assert child.answers == parent.answers and child.cover_letter == parent.cover_letter
    assert child.revisions.application_output == parent.revisions.application_output + 1
    assert child.assessment_state == 'assessed'
    detail = service.detail(scope.profile_id, scope.application_id, child.id)
    assert detail['current'] and not detail['review_eligible'] and not detail['browser_eligible']
    assert service.jobs.get(scope.profile_id, job.id).stage == 'assessment_published'


def test_adding_assessment_consent_after_generation_stales_old_edit_without_sends(drafting):  # noqa: F811
    service, scope, _, sent, _ = drafting
    generated, *_ = execute(drafting)
    parent = edit(service, scope, generated)
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    preview = AssessmentPreviewRequest(expected_revisions=parent.revisions,
        parent_text_sha256=parent.text_sha256, parent_ledger_sha256=parent.ledger_sha256,
        parent_publication_sha256=c.canonical_hash(detail['publication']), model='synthetic-model')
    consent(service, scope)
    with pytest.raises(EvidenceError):
        service.assessment_preview(scope.profile_id, scope.application_id, parent.id, preview)
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, edit_request(service, scope, parent, 'no-rebase'))
    assert not service.detail(scope.profile_id, scope.application_id, parent.id)['current']
    assert parent.answers[0].text in service.export(scope.profile_id, scope.application_id, parent.id)
    assert len(sent) == 3


def test_matching_key_digest_does_not_authorize_wrong_linked_retained_job(assessment):
    local, parent = assessment
    service, scope, _, sent, _ = local
    launch = capture_assessment_launch(service, scope, request_for(service, scope, parent))
    job, _, _, request = queued(assessment, launch=launch)
    wrong = service.jobs.enqueue(scope.profile_id, 'research', {}, 'wrong-link', scope.application_id)
    with service.store._tx() as db:
        db.execute('UPDATE job_keys SET job_id=? WHERE owner=? AND key=?',
            (wrong.id, scope.profile_id, request.idempotency_key))
    with pytest.raises(EvidenceError):
        service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, launch)
    assert len(sent) == 3 and service.jobs.get(scope.profile_id, job.id).state == 'running'


@pytest.mark.parametrize('state', ['prepared', 'indeterminate'])
def test_unknown_or_prepared_assessment_attempt_never_reentered(assessment, state):
    service, scope, _, sent, _ = assessment[0]
    parent = assessment[1]
    job, worker, fence, _ = queued(assessment)
    attempt = service.provider.reserve(job.id, worker, fence, 'reassess', 1000, 'synthetic-model')
    if state == 'indeterminate':
        service.provider._unknown(attempt.id, job.id)
    with pytest.raises(EvidenceError):
        service.run_assess(job.id, worker, fence)
    assert len(sent) == 3
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output


@pytest.mark.parametrize('crash_stage', ['assessment_stage_reassess', 'assessment_published'])
def test_completed_response_restart_uses_one_send_and_current_terminal_fence(assessment, monkeypatch, crash_stage):
    service, scope, _, sent, _ = assessment[0]
    job, worker, fence, _ = queued(assessment)
    commit = service.jobs.commit_stage

    def interrupted(job_id, worker_id, current_fence, stage, result, **kwargs):
        if stage == crash_stage:
            raise RuntimeError('synthetic process interruption')
        return commit(job_id, worker_id, current_fence, stage, result, **kwargs)

    monkeypatch.setattr(service.jobs, 'commit_stage', interrupted)
    with pytest.raises(RuntimeError):
        service.run_assess(job.id, worker, fence)
    assert len(sent) == 4
    with service.store._tx() as db:
        current, _, _ = service.jobs._job(db, job.id)
        service.jobs._save(db, current.model_copy(update={
            'lease_expires_at': service.jobs._now() - timedelta(seconds=1)}))
    monkeypatch.setattr(service.jobs, 'commit_stage', commit)
    worker2 = str(uuid4())
    reclaimed = service.jobs.claim(worker2, job_id=job.id)
    assert reclaimed.fence > fence
    child = service.run_assess(job.id, worker2, reclaimed.fence)
    assert len(sent) == 4
    detail = service.detail(scope.profile_id, scope.application_id, child.id)
    assert detail['current'] and detail['publication']['fence'] == reclaimed.fence
    with service.store._read() as db:
        intermediate = db.execute('SELECT fence FROM job_stages WHERE job_id=? AND stage=?',
            (job.id, 'assessment_stage_reassess')).fetchone()[0]
        terminal = db.execute('SELECT fence FROM job_stages WHERE job_id=? AND stage=?',
            (job.id, 'assessment_published')).fetchone()[0]
        attempt = json.loads(db.execute('SELECT data FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone()[0])
        intent = json.loads(db.execute('SELECT data FROM records WHERE id=?', (detail['publication']['intent_id'],)).fetchone()[0])
    assert intermediate == (fence if crash_stage == 'assessment_published' else reclaimed.fence)
    assert attempt['fence'] == fence
    assert terminal == intent['fence'] == reclaimed.fence and intent['state'] == 'committed'


@pytest.mark.parametrize('phase', ['admission', 'completion'])
@pytest.mark.parametrize('mutation', ['consent', 'cancel', 'parent_delete', 'research', 'generation', 'original'])
def test_transmission_and_completion_revalidate_dependencies(assessment, phase, mutation):
    local, parent = assessment
    service, scope, batch, sent, _ = local
    job, worker, fence, _ = queued(assessment)
    transport = service.provider.transport

    def change():
        if mutation == 'cancel':
            service.jobs.cancel(scope.profile_id, job.id)
        elif mutation == 'consent':
            consent(service, scope)
        elif mutation == 'original':
            with service.store._read() as db:
                _, values = service.store.capture_scoped_sources(db, batch.employer_generation.scope, service.jobs._now())
            service.store._blob(values[0]['source']['id']).write_bytes(b'corrupt original')
        else:
            with service.store._tx() as db:
                if mutation == 'parent_delete':
                    db.execute('DELETE FROM records WHERE id=?', (parent.id,))
                elif mutation == 'generation':
                    db.execute("UPDATE manifests SET state='retired' WHERE id=?", (batch.employer_generation.generation_id,))
                else:
                    run = service.domain._get(db, scope.profile_id, scope.research_run_id, 'research_run', c.ResearchRun)
                    service.domain._save(db, scope.profile_id, 'research_run', run.model_copy(update={
                        'acquired_at': service.jobs._now() - timedelta(days=1),
                        'expires_at': service.jobs._now() - timedelta(seconds=1)}))

    def guarded(payload, before_send):
        if phase == 'admission':
            change()
        result = transport(payload, before_send)
        if phase == 'completion':
            change()
        return result

    service.provider.transport = guarded
    with pytest.raises(EvidenceError):
        service.run_assess(job.id, worker, fence)
    assert len(sent) == (3 if phase == 'admission' else 4)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output
    with service.store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='workspace:draft'").fetchone()[0] == (1 if mutation == 'parent_delete' else 2)
        if mutation != 'parent_delete':
            assert service.domain._get(db, scope.profile_id, parent.id, 'draft', c.DraftRevision) == parent


@pytest.mark.parametrize('tamper', ['parent', 'depth', 'intent_fence', 'stage_zero_fence', 'provider_scope', 'payload_digest', 'text', 'context'])
def test_corrupt_assessment_history_fails_before_stale_io_bypass(assessment, monkeypatch, tamper):
    service, scope, *_ = assessment[0]
    child, job, _ = assessed(assessment)
    detail = service.detail(scope.profile_id, scope.application_id, child.id)
    publication = detail['publication']
    with service.store._tx() as db:
        app = service.domain._get(db, scope.profile_id, scope.application_id, 'application', c.ApplicationRecord)
        service.domain._save(db, scope.profile_id, 'application', app.model_copy(update={'input_revision': app.input_revision + 1}))
    monkeypatch.setattr(service.store, 'verify_scoped_sources', lambda *a: pytest.fail('stale history touched originals'))
    assert not service.detail(scope.profile_id, scope.application_id, child.id)['current']
    with service.store._tx() as db:
        if tamper in ('parent', 'depth'):
            publication['parent_draft_id' if tamper == 'parent' else 'edit_depth'] = child.id if tamper == 'parent' else 63
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(publication), publication['id']))
        elif tamper == 'intent_fence':
            raw = json.loads(db.execute('SELECT data FROM records WHERE id=?', (publication['intent_id'],)).fetchone()[0])
            raw['fence'] += 1
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), publication['intent_id']))
        elif tamper == 'stage_zero_fence':
            db.execute("UPDATE job_stages SET fence=0 WHERE job_id=? AND stage='assessment_stage_reassess'", (job.id,))
        elif tamper in ('provider_scope', 'payload_digest'):
            if tamper == 'payload_digest':
                db.execute('UPDATE provider_attempts SET request_sha256=? WHERE job_id=?', ('0' * 64, job.id))
            else:
                raw = json.loads(db.execute('SELECT data FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone()[0])
                raw['application_id'] = str(uuid4())
                db.execute('UPDATE provider_attempts SET data=? WHERE job_id=?', (json.dumps(raw), job.id))
        elif tamper == 'text':
            raw = json.loads(db.execute('SELECT data FROM records WHERE id=?', (child.id,)).fetchone()[0])
            raw['answers'][0]['text'] += 'forged'
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), child.id))
        else:
            raw = json.loads(db.execute('SELECT parameters FROM jobs WHERE id=?', (job.id,)).fetchone()[0])
            raw['context']['company'] = 'Unrelated employer'
            db.execute('UPDATE jobs SET parameters=? WHERE id=?', (json.dumps(raw), job.id))
    with pytest.raises(EvidenceError):
        DraftingService(service.store).detail(scope.profile_id, scope.application_id, child.id)


@pytest.mark.parametrize('mode', ['legacy_edits_then_assess', 'alternating'])
def test_mixed_lineage_bounds_remain_readable_and_reject_before_reserve(assessment, monkeypatch, mode):
    from copilot.drafting.admission_runtime import DraftAdmission
    from copilot.worker import Worker

    local, parent = assessment
    service, scope, _, sent, _ = local
    service.provider.configure_limits(scope.profile_id, Limits(100_000_000, 1000, 2_000_000, 5))
    worker = Worker(service.store, lambda: pytest.fail('Assessment initialized retrieval models'),
                    drafting_factory=lambda _s, _j: service)
    assert (worker.lease_seconds, worker.interval) == (30, 10)

    def worker_assessed(parent, key):
        request = request_for(service, scope, parent, key)
        job = service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id,
            capture_assessment_launch(service, scope, request))
        assert worker.run_once(job.id, recovery=False) == dict(job_id=job.id, state='completed')
        finished_at = worker.jobs._now()
        assert worker.last_outcome['heartbeat'] in ({'state': 'stopped'},
            {'state': 'authority_lost', 'code': 'AUTHORITY_LOST'})
        assert worker.last_outcome['recovery'] == {'state': 'not_requested'}
        with service.store._read() as db:
            terminal = service.jobs._job(db, job.id, scope.profile_id)[0]
            assert (terminal.id, terminal.profile_id, terminal.application_id, terminal.kind,
                    terminal.state, terminal.stage, terminal.lease_owner, terminal.fence,
                    terminal.attempt_count, terminal.cancellation_requested) == (
                job.id, scope.profile_id, scope.application_id, 'assess', 'completed',
                'assessment_published', worker.worker_id, job.fence + 1, 1, False)
            assert terminal.lease_expires_at is not None and terminal.lease_expires_at > finished_at
            certificate = DraftAdmission(service)._completed_publication_certificate(
                db, scope.profile_id, scope.application_id, job.id)
            assert (certificate.owner, certificate.application_id, certificate.job_id,
                    certificate.kind, certificate.fence) == (
                scope.profile_id, scope.application_id, job.id, 'assess', terminal.fence)
            child = service.domain._get(db, scope.profile_id, certificate.draft_id, 'draft', c.DraftRevision)
            assert child.revisions == terminal.revisions.model_copy(update={
                'application_output': terminal.revisions.application_output + 1})
            return child

    # Construct every link through the real public operations. No setup-only
    # cross-transaction memo: the default Worker genuinely renews each lease.
    if mode == 'legacy_edits_then_assess':
        for depth in range(2, 65):
            parent = edit(service, scope, parent, key=f'legacy-edit-{depth}')
    else:
        for depth in range(1, 64):
            parent = worker_assessed(parent, key=f'alternating-assess-{depth}')
            parent = edit(service, scope, parent, key=f'alternating-edit-{depth + 1}')
    # Last assessment admission, completion, publication and inspection are all
    # uncached, exercising the complete accepted 65-link or 128-link history.
    parent = worker_assessed(parent, key=f'{mode}-final-assess')
    expected_assessments = 1 if mode == 'legacy_edits_then_assess' else 64
    assert len(sent) == 3 + expected_assessments
    with service.store._read() as db:
        origins = dict(db.execute("SELECT json_extract(data,'$.origin'),COUNT(*) FROM records "
            "WHERE kind='workspace:draft_dependency' GROUP BY json_extract(data,'$.origin')").fetchall())
        assert origins == {None: 1, 'edit': 64, 'assessment': expected_assessments}
        assert db.execute("SELECT COUNT(*) FROM provider_attempts WHERE stage='reassess' AND state='completed'").fetchone()[0] == expected_assessments
    fresh = DraftingService(service.store, jobs=service.jobs, provider=service.provider)
    detail = fresh.detail(scope.profile_id, scope.application_id, parent.id)
    assert detail['current'] and detail['publication']['edit_depth'] == 64
    assert parent.answers[0].text in fresh.export(scope.profile_id, scope.application_id, parent.id)
    request = edit_request(fresh, scope, parent, key='beyond-bound')
    monkeypatch.setattr(service.provider, 'reserve', lambda *a, **k: pytest.fail('Bound rejection reserved provider budget'))
    with pytest.raises(EvidenceError):
        fresh.edit(scope.profile_id, scope.application_id, parent.id, request)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output


def test_orphan_assessment_intent_disclosure_is_forgotten_with_unknown_accounting(assessment, tmp_path):
    local, parent = assessment
    service, scope, *_ = local
    job, worker, fence, _ = queued(assessment)

    def unknown(payload, before_send):
        before_send()
        raise RuntimeError('synthetic ambiguous transport loss')

    service.provider.transport = unknown
    with pytest.raises(EvidenceError):
        service.run_assess(job.id, worker, fence)
    with service.store._read() as db:
        attempt = db.execute('SELECT id,data,state,day,dispatched,request_sha256 FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone()
        intent_ids = [r[0] for r in db.execute("SELECT id FROM records WHERE kind='workspace:draft_intent' AND json_extract(data,'$.job_id')=?", (job.id,))]
        assert intent_ids and attempt[2] == 'indeterminate'
    with service.store._read() as db:
        metadata = service.domain.revisions(db, scope.profile_id).metadata
    other = service.domain.create_application(scope.profile_id, ApplicationInput(
        expected_metadata_revision=metadata, company='Other employer', role='Intern', sector='finance',
        vacancy_url='https://other.example/job'))
    other_attempt_id, other_job_id, other_record_id = str(uuid4()), str(uuid4()), str(uuid4())
    other_attempt = json.loads(attempt[1]) | dict(id=other_attempt_id,
        application_id=other.application_id, job_id=other_job_id)
    other_record = dict(id=other_record_id, profile_id=scope.profile_id,
        application_id=other.application_id, private_text='OTHER-APP-PLAINTEXT-CANARY')
    original = service
    target = tmp_path / 'owned-orphan-assessment-preupgrade'
    with preupgrade_copy(original.store, target) as db:
        orphan_keys = db.execute('SELECT owner,key,job_id,request_sha256,application_id,kind FROM job_keys WHERE job_id=?', (job.id,)).fetchall()
        preserved_keys = db.execute('SELECT owner,key,job_id,request_sha256,application_id,kind FROM job_keys WHERE job_id!=? ORDER BY owner,key', (job.id,)).fetchall()
        assert orphan_keys
        # Marker-0 bootstrap rejects orphan keys. Omit only the exact rows for
        # the job removed below; restore them as a post-startup fault instead.
        db.execute('DELETE FROM job_keys WHERE job_id=?', (job.id,))
        db.execute('UPDATE provider_attempts SET response=? WHERE id=?', ('ORPHAN-APP-A-RESPONSE-CANARY', attempt[0]))
        db.execute('DELETE FROM jobs WHERE id=?', (job.id,))
        db.execute('DELETE FROM records WHERE id=?', (parent.id,))
        db.execute('INSERT INTO provider_attempts(id,owner,job_id,state,data,dispatched,day,response,stage,request_sha256) '
            'SELECT ?,owner,?,state,?,dispatched,day,?,stage,request_sha256 FROM provider_attempts WHERE id=?',
            (other_attempt_id, other_job_id, json.dumps(other_attempt), 'OTHER-APP-RESPONSE-CANARY', attempt[0]))
        db.execute('INSERT INTO records(id,owner,kind,data) VALUES(?,?,?,?)',
            (other_record_id, scope.profile_id, 'workspace:draft_intent', json.dumps(other_record)))
    service = DraftingService(Store(target))
    service.provider._key_supplier = lambda: pytest.fail('Copied privacy phase read a provider key')
    service.provider.transport = lambda *a, **k: pytest.fail('Copied privacy phase invoked provider transport')
    with service.store._read() as db:
        admission_before_restore = (db.execute('SELECT * FROM drafting_admission_heads ORDER BY owner,application_id').fetchall(),
            db.execute('SELECT * FROM drafting_admission_bindings ORDER BY job_id').fetchall(),
            db.execute('PRAGMA user_version').fetchone())
        assert db.execute('SELECT owner,key,job_id,request_sha256,application_id,kind FROM job_keys ORDER BY owner,key').fetchall() == preserved_keys
    # Explicit post-startup orphan-key corruption; this is not a claim that
    # legacy orphan keys can migrate, nor a reset of active admission metadata.
    with service.store._tx() as db:
        db.executemany('INSERT INTO job_keys(owner,key,job_id,request_sha256,application_id,kind) VALUES(?,?,?,?,?,?)', orphan_keys)
    with service.store._read() as db:
        assert db.execute('SELECT owner,key,job_id,request_sha256,application_id,kind FROM job_keys WHERE job_id=?', (job.id,)).fetchall() == orphan_keys
        assert (db.execute('SELECT * FROM drafting_admission_heads ORDER BY owner,application_id').fetchall(),
            db.execute('SELECT * FROM drafting_admission_bindings ORDER BY job_id').fetchall(),
            db.execute('PRAGMA user_version').fetchone()) == admission_before_restore
        assert db.execute('SELECT revision,last_job_id FROM drafting_admission_heads WHERE owner=? AND application_id=?',
            (scope.profile_id, scope.application_id)).fetchone() == (1, None)
        assert not db.execute("SELECT 1 FROM drafting_admission_bindings WHERE origin!='legacy'").fetchone()
        assert db.execute('SELECT 1 FROM job_keys WHERE job_id=?', (job.id,)).fetchone()
    with original.store._read() as db:
        assert db.execute('SELECT 1 FROM jobs WHERE id=?', (job.id,)).fetchone()
        assert db.execute('SELECT 1 FROM records WHERE id=?', (parent.id,)).fetchone()
    app = service.domain.application(scope.profile_id, scope.application_id)
    service.domain.delete_application(scope.profile_id, scope.application_id, ApplicationPatch(
        expected_input_revision=app.input_revision, expected_output_revision=app.output_revision))
    with service.store._read() as db:
        retained = db.execute('SELECT data,state,response,day,dispatched,request_sha256 FROM provider_attempts WHERE id=?', (attempt[0],)).fetchone()
        assert retained[0] == attempt[1] and retained[1] == 'indeterminate' and retained[2] is None
        assert retained[3:] == attempt[3:]
        assert not db.execute('SELECT 1 FROM job_keys WHERE job_id=?', (job.id,)).fetchone()
        assert not db.execute("SELECT 1 FROM records WHERE owner=? AND kind IN ('workspace:draft','workspace:draft_intent','workspace:draft_dependency') "
            "AND json_extract(data,'$.application_id')=?", (scope.profile_id, scope.application_id)).fetchone()
        assert all(db.execute('SELECT 1 FROM tombstones WHERE id=?', (rid,)).fetchone() for rid in intent_ids)
        assert json.loads(db.execute('SELECT data FROM records WHERE id=?', (other_record_id,)).fetchone()[0]) == other_record
        other_retained = db.execute('SELECT data,state,response FROM provider_attempts WHERE id=?', (other_attempt_id,)).fetchone()
        assert json.loads(other_retained[0]) == other_attempt
        assert other_retained[1:] == ('indeterminate', 'OTHER-APP-RESPONSE-CANARY')
