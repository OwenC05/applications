"""Draft lifecycle races, restart cache, and immutable historical integrity."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_drafting_service import drafting, enqueue, execute  # noqa: F401
from test_jobs import setup as job_setup  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ConsentInput
from copilot.drafting.service import DraftingService


def lease(drafting):  # noqa: F811
    service, *_ = drafting
    job, request = enqueue(drafting)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    return job, worker, claimed.fence, request


@pytest.mark.parametrize('condition', ['key', 'consent'])
def test_missing_key_or_consent_zero_transmitted_bodies(drafting, condition):  # noqa: F811
    service, scope, _, sent, _ = drafting
    if condition == 'key':
        service.provider._key_supplier = lambda: ''
    else:
        service.domain.consent(scope.profile_id, ConsentInput(expected_consent_revision=1,
            provider='openai', purposes=('research',), granted=True))
    job, worker, fence, _ = lease(drafting)
    with pytest.raises(EvidenceError):
        service.run_draft(job.id, worker, fence)
    assert not sent
    assert not service.list(scope.profile_id, scope.application_id)['drafts']


def test_completed_stage_reclaim_reuses_exact_payload_new_intent(drafting, monkeypatch):  # noqa: F811
    service, scope, _, sent, _ = drafting
    job, worker, fence, _ = lease(drafting)
    original = service.provider.send
    def interrupt(*args, **kwargs):
        output = original(*args, **kwargs)
        if args[3] == 'plan':
            raise RuntimeError('simulated process loss after completed attempt')
        return output
    monkeypatch.setattr(service.provider, 'send', interrupt)
    with pytest.raises(RuntimeError):
        service.run_draft(job.id, worker, fence)
    with service.store._tx() as db:
        current, _, _ = service.jobs._job(db, job.id)
        service.jobs._save(db, current.model_copy(update={'lease_expires_at': service.jobs._now() - timedelta(seconds=1)}))
    second_worker = str(uuid4())
    reclaimed = service.jobs.claim(second_worker, job_id=job.id)
    assert reclaimed.fence > fence
    monkeypatch.setattr(service.provider, 'send', original)
    draft = service.run_draft(job.id, second_worker, reclaimed.fence)
    assert len(sent) == 3
    assert service.detail(scope.profile_id, scope.application_id, draft.id)['current']
    with service.store._read() as db:
        intents = db.execute("SELECT data FROM records WHERE kind='workspace:draft_intent'").fetchall()
    assert len(intents) == 2
    assert {json.loads(row[0])['state'] for row in intents} == {'registered', 'committed'}


@pytest.mark.parametrize('state', ['prepared', 'indeterminate'])
def test_uncertain_attempt_never_replayed(drafting, state):  # noqa: F811
    service, scope, _, sent, _ = drafting
    job, worker, fence, _ = lease(drafting)
    # A real reservation establishes ambiguity without any fabricated provider response.
    attempt = service.provider.reserve(job.id, worker, fence, 'plan', 1000, 'synthetic-model')
    if state == 'indeterminate':
        service.provider._unknown(attempt.id, job.id)
    with pytest.raises(EvidenceError):
        service.run_draft(job.id, worker, fence)
    assert not sent
    assert not service.list(scope.profile_id, scope.application_id)['drafts']


@pytest.mark.parametrize('mutation', ['cancel', 'expire', 'generation', 'original'])
def test_actual_transport_admission_rechecks_dependencies(drafting, mutation):  # noqa: F811
    service, scope, batch, sent, _ = drafting
    job, worker, fence, _ = lease(drafting)
    original_transport = service.provider.transport
    def transport(payload, before_send):
        if mutation == 'cancel':
            service.jobs.cancel(scope.profile_id, job.id)
        elif mutation == 'expire':
            with service.store._tx() as db:
                run = service.domain._get(db, scope.profile_id, scope.research_run_id, 'research_run', c.ResearchRun)
                service.domain._save(db, scope.profile_id, 'research_run', run.model_copy(update={'expires_at': service.jobs._now() - timedelta(seconds=1)}))
        elif mutation == 'generation':
            with service.store._tx() as db:
                db.execute("UPDATE manifests SET state='retired' WHERE id=?", (batch.facts_generation.generation_id,))
        else:
            with service.store._read() as db:
                _, values = service.store.capture_scoped_sources(db, batch.employer_generation.scope, service.jobs._now())
            # Corrupt the original without changing canonical metadata/revisions.
            source = values[0]['source']
            path = service.store._blob(source['id'])
            path.write_bytes(b'tampered original')
        return original_transport(payload, before_send)
    service.provider.transport = transport
    with pytest.raises(EvidenceError):
        service.run_draft(job.id, worker, fence)
    assert not sent
    assert not service.list(scope.profile_id, scope.application_id)['drafts']


def test_failure_preserves_previous_draft_and_stale_reads_need_no_originals(drafting, monkeypatch):  # noqa: F811
    service, scope, _, sent, _ = drafting
    draft, _, _ = execute(drafting)
    job, request = enqueue(drafting, 'next')
    worker = str(uuid4())
    current = service.jobs.claim(worker, job_id=job.id)
    service.provider.transport = lambda *args: (_ for _ in ()).throw(RuntimeError())
    with pytest.raises(EvidenceError):
        service.run_draft(job.id, worker, current.fence)
    assert service.detail(scope.profile_id, scope.application_id, draft.id)['draft']['answers'][0]['text'] == draft.answers[0].text
    with service.store._tx() as db:
        app = service.domain._get(db, scope.profile_id, scope.application_id, 'application', c.ApplicationRecord)
        service.domain._save(db, scope.profile_id, 'application', app.model_copy(update={'input_revision': app.input_revision + 1}))
    monkeypatch.setattr(service.store, 'verify_scoped_sources', lambda *a: pytest.fail('stale historical read touched originals'))
    result = DraftingService(service.store).detail(scope.profile_id, scope.application_id, draft.id)
    assert not result['current']
    assert result['draft']['answers'][0]['text'] == draft.answers[0].text
    # Corrupt history must fail BEFORE the known-staleness bypass.
    with service.store._tx() as db:
        db.execute("DELETE FROM job_stages WHERE job_id=? AND stage='draft_published'", (result['publication']['job_id'],))
    with pytest.raises(EvidenceError):
        service.detail(scope.profile_id, scope.application_id, draft.id)


def test_original_verification_never_holds_sqlite_writer(drafting, monkeypatch):  # noqa: F811
    service, _, _, _, _ = drafting
    original = service.store.verify_scoped_sources
    checked = []
    def verify(scope, values):
        with service.store._tx() as db:
            db.execute('SELECT 1').fetchone()
        checked.append(scope.corpus)
        return original(scope, values)
    monkeypatch.setattr(service.store, 'verify_scoped_sources', verify)
    execute(drafting)
    assert checked.count('employer') >= 5


def test_current_history_detects_original_tamper_but_stale_history_remains_exportable(drafting):  # noqa: F811
    service, scope, batch, _, _ = drafting
    draft, _, _ = execute(drafting)
    with service.store._read() as db:
        _, values = service.store.capture_scoped_sources(db, batch.employer_generation.scope, service.jobs._now())
    service.store._blob(values[0]['source']['id']).write_bytes(b'tamper')
    with pytest.raises(EvidenceError):
        service.detail(scope.profile_id, scope.application_id, draft.id)
    with service.store._tx() as db:
        app = service.domain._get(db, scope.profile_id, scope.application_id, 'application', c.ApplicationRecord)
        service.domain._save(db, scope.profile_id, 'application', app.model_copy(update={'input_revision': app.input_revision + 1}))
    assert not service.detail(scope.profile_id, scope.application_id, draft.id)['current']


def test_final_publication_cancellation_wins_without_output_increment(drafting, monkeypatch):  # noqa: F811
    service, scope, _, sent, _ = drafting
    job, worker, fence, _ = lease(drafting)
    original = service.jobs.commit_stage
    def commit(job_id, worker_id, current_fence, stage, result, **kwargs):
        if stage == 'draft_published':
            service.jobs.cancel(scope.profile_id, job_id)
        return original(job_id, worker_id, current_fence, stage, result, **kwargs)
    monkeypatch.setattr(service.jobs, 'commit_stage', commit)
    with pytest.raises(EvidenceError):
        service.run_draft(job.id, worker, fence)
    assert len(sent) == 3
    assert not service.list(scope.profile_id, scope.application_id)['drafts']
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == job.revisions.application_output


def test_same_revision_active_generation_identity_replacement_denies_body(drafting):  # noqa: F811
    service, scope, batch, sent, _ = drafting
    job, worker, fence, _ = lease(drafting)
    original_transport = service.provider.transport
    def transport(payload, before_send):
        with service.store._tx() as db:
            row = db.execute('SELECT data FROM manifests WHERE id=?', (batch.facts_generation.generation_id,)).fetchone()
            data = json.loads(row[0])
            data['generation_id'] = str(uuid4())
            db.execute('UPDATE manifests SET data=? WHERE id=?', (json.dumps(data), batch.facts_generation.generation_id))
        return original_transport(payload, before_send)
    service.provider.transport = transport
    with pytest.raises(EvidenceError):
        service.run_draft(job.id, worker, fence)
    assert not sent
    assert not service.list(scope.profile_id, scope.application_id)['drafts']


def test_legacy_same_owner_wrong_link_keeps_unknown_scope_and_rolls_back_app_forget(job_setup):  # noqa: F811
    from copilot.domain.repository import ApplicationInput, ApplicationPatch
    from copilot.jobs import Jobs

    store, owner, jobs, _ = job_setup
    first_app = jobs.domain.create_application(owner, ApplicationInput(
        expected_metadata_revision=0, company='A', role='Analyst', sector='finance',
        vacancy_url='https://a.example.test/job'))
    with store._read() as db:
        revision = jobs.domain.revisions(db, owner).metadata
    second_app = jobs.domain.create_application(owner, ApplicationInput(
        expected_metadata_revision=revision, company='B', role='Engineer', sector='tech',
        vacancy_url='https://b.example.test/job'))
    first = jobs.enqueue(owner, 'draft', {}, 'private-legacy-A-canary', first_app.application_id)
    second = jobs.enqueue(owner, 'draft', {}, 'private-legacy-B-canary', second_app.application_id)
    with store._tx() as db:
        rows = db.execute('SELECT owner,key,job_id,request_sha256 FROM job_keys').fetchall()
        db.execute('DROP TABLE job_keys')
        db.execute('CREATE TABLE job_keys(owner TEXT,key TEXT,job_id TEXT,request_sha256 TEXT,PRIMARY KEY(owner,key))')
        db.executemany('INSERT INTO job_keys VALUES(?,?,?,?)', rows)
        # Retained same-owner B is not proof that A's plaintext key belongs to B.
        db.execute('UPDATE job_keys SET job_id=? WHERE owner=? AND key=?',
                   (second.id, owner, first.idempotency_key))
    Jobs(store)
    with store._read() as db:
        assert db.execute('SELECT application_id,kind FROM job_keys WHERE owner=? AND key=?',
                          (owner, first.idempotency_key)).fetchone() == (None, None)
        assert db.execute('SELECT application_id,kind FROM job_keys WHERE owner=? AND key=?',
                          (owner, second.idempotency_key)).fetchone() == (second_app.application_id, 'draft')
        before = tuple(db.execute('SELECT * FROM records ORDER BY id').fetchall())
        jobs_before = tuple(db.execute('SELECT * FROM jobs ORDER BY id').fetchall())
        keys_before = tuple(db.execute('SELECT * FROM job_keys ORDER BY key').fetchall())
    with pytest.raises(EvidenceError) as caught:
        jobs.domain.delete_application(owner, first_app.application_id, ApplicationPatch(
            expected_input_revision=first_app.input_revision,
            expected_output_revision=first_app.output_revision))
    assert caught.value.code == 'CONFLICT' and caught.value.http_status == 409
    assert jobs.domain.application(owner, first_app.application_id) == first_app
    assert jobs.domain.application(owner, second_app.application_id) == second_app
    with store._read() as db:
        assert tuple(db.execute('SELECT * FROM records ORDER BY id').fetchall()) == before
        assert tuple(db.execute('SELECT * FROM jobs ORDER BY id').fetchall()) == jobs_before
        assert tuple(db.execute('SELECT * FROM job_keys ORDER BY key').fetchall()) == keys_before
    with store._tx() as db:
        store.forget_jobs(db, owner, profile=True)
    with store._read() as db:
        assert not db.execute('SELECT 1 FROM job_keys WHERE owner=?', (owner,)).fetchone()
    assert jobs.domain.application(owner, second_app.application_id) == second_app
