"""Draft handler/failure wiring without models, keys or provider traffic."""
from uuid import uuid4

import pytest
from test_jobs import (
    app,
    setup,  # noqa: F401
)

from copilot.contracts import EvidenceError
from copilot.worker import Worker


def queued(setup, kind='draft'):  # noqa: F811
    _, owner, jobs, _ = setup
    application = app(setup)
    return jobs.enqueue(owner, kind, {}, 'synthetic-' + kind, application)


@pytest.mark.parametrize('kind', ['draft', 'assess'])
def test_definite_failure_records_exact_authority_despite_stale_inputs(setup, kind):  # noqa: F811
    store, owner, jobs, _ = setup
    job = queued(setup, kind)
    worker = str(uuid4())
    leased = jobs.claim(worker, job_id=job.id)
    store.confirm_fact(owner, 'Synthetic fact changed after job capture')
    failed = jobs.fail_drafting(job.id, worker, leased.fence)
    assert failed.state == 'failed' and failed.stage == 'drafting_failed'
    assert jobs.claim(str(uuid4()), job_id=job.id) is None


@pytest.mark.parametrize('loss', ['worker', 'fence', 'expiry', 'cancel', 'reclaim', 'kind'])
def test_failure_never_grants_publication_or_overwrites_lost_authority(setup, loss):  # noqa: F811
    _, owner, jobs, clock = setup
    job = (jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'index')
           if loss == 'kind' else queued(setup))
    worker = str(uuid4())
    leased = jobs.claim(worker, job_id=job.id, lease_seconds=5)
    fence = leased.fence
    if loss == 'worker':
        worker = str(uuid4())
    elif loss == 'fence':
        fence += 1
    elif loss in ('expiry', 'reclaim'):
        clock.advance(6)
        if loss == 'reclaim':
            assert jobs.claim(str(uuid4()), job_id=job.id)
    elif loss == 'cancel':
        jobs.cancel(owner, job.id)
    before = jobs.get(owner, job.id)
    with pytest.raises(EvidenceError):
        jobs.fail_drafting(job.id, worker, fence)
    assert jobs.get(owner, job.id) == before


@pytest.mark.parametrize('state', ['prepared', 'indeterminate'])
def test_uncertain_paid_attempt_is_never_failed_or_released(setup, state):  # noqa: F811
    store, owner, jobs, _ = setup
    job = queued(setup)
    worker = str(uuid4())
    leased = jobs.claim(worker, job_id=job.id)
    with store._tx() as db:
        db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
            (str(uuid4()), owner, job.id, state, '{"synthetic_reservation":5000}',
             1, '2026-10-10', None, 'plan', 'a' * 64))
        before = db.execute('SELECT * FROM provider_attempts WHERE job_id=?', (job.id,)).fetchall()
    with pytest.raises(EvidenceError):
        jobs.fail_drafting(job.id, worker, leased.fence)
    assert jobs.get(owner, job.id).state == 'running'
    with store._read() as db:
        assert db.execute('SELECT * FROM provider_attempts WHERE job_id=?', (job.id,)).fetchall() == before


def test_worker_dispatches_draft_without_loading_retrieval_or_key(setup):  # noqa: F811
    store, _, jobs, clock = setup
    job = queued(setup)
    calls = []
    class Handler:
        def run_draft(self, job_id, worker, fence):
            calls.append(job_id)
            jobs.finish(job_id, worker, fence)
    worker = Worker(store, lambda: pytest.fail('Retrieval models must not load'),
                    drafting_factory=lambda s, j: Handler())
    worker.jobs.clock = clock
    assert worker.run_once(job.id, recovery=False) == {'job_id': job.id, 'state': 'completed'}
    assert calls == [job.id]


def test_worker_reports_paid_unknown_honestly_without_failing_it(setup):  # noqa: F811
    store, owner, jobs, clock = setup
    job = queued(setup)
    class Handler:
        def run_draft(self, job_id, worker, fence):
            with store._tx() as db:
                current, _, _ = jobs._job(db, job_id)
                jobs._save(db, current.model_copy(update={'state': 'indeterminate', 'stage': 'provider_unknown'}))
            raise EvidenceError('PROVIDER_UNKNOWN', 'Unknown synthetic outcome', 502)
    worker = Worker(store, lambda: pytest.fail('Retrieval models must not load'),
                    drafting_factory=lambda s, j: Handler())
    worker.jobs.clock = clock
    assert worker.run_once(job.id, recovery=False)['error'] == 'PROVIDER_UNKNOWN'
    assert jobs.get(owner, job.id).state == 'indeterminate'


def test_forgetting_scrubs_orphan_draft_plaintext_by_scope(setup):  # noqa: F811
    store, owner, jobs, _ = setup
    application = app(setup)
    other_app = str(uuid4())
    forgotten, retained = [], []
    with store._tx() as db:
        for kind in ('draft', 'draft_intent', 'draft_dependency', 'draft_review'):
            for app_id, ids in ((application, forgotten), (other_app, retained)):
                record_id = str(uuid4())
                ids.append(record_id)
                jobs.domain._save(db, owner, kind, {'id': record_id, 'profile_id': owner,
                    'application_id': app_id, 'plaintext': 'Synthetic generated prose'}, record_id)
        store.forget_jobs(db, owner, application_id=application)
    with store._read() as db:
        assert all(not db.execute('SELECT 1 FROM records WHERE id=?', (rid,)).fetchone() for rid in forgotten)
        assert all(db.execute('SELECT 1 FROM tombstones WHERE id=?', (rid,)).fetchone() for rid in forgotten)
        assert all(db.execute('SELECT 1 FROM records WHERE id=?', (rid,)).fetchone() for rid in retained)
    with store._tx() as db:
        store.forget_jobs(db, owner, affected=('facts',))
    with store._read() as db:
        assert all(not db.execute('SELECT 1 FROM records WHERE id=?', (rid,)).fetchone() for rid in retained)


@pytest.mark.parametrize('linkage', ['missing_job', 'other_application_job', 'retained_points_to_forgotten_job'])
def test_orphan_attempt_response_scrub_uses_attempt_scope_not_job_link(setup, linkage):  # noqa: F811
    from test_provider import good, ready, send

    from copilot.domain.repository import ApplicationInput

    provider, job = ready(setup, good)
    assert send(provider, job).answer
    store, owner, jobs, _ = setup
    with store._read() as db:
        revision = jobs.domain.revisions(db, owner).metadata
    other = jobs.domain.create_application(owner, ApplicationInput(expected_metadata_revision=revision,
        company='Other', role='Analyst', sector='finance', vacancy_url='https://other.test/job'))
    other_job = jobs.enqueue(owner, 'draft', {}, 'other-application', other.application_id)
    other_lease = jobs.claim(str(uuid4()), job_id=other_job.id)
    assert send(provider, other_lease, stage='other').answer
    with store._tx() as db:
        first_id = db.execute('SELECT id FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone()[0]
        if linkage == 'missing_job':
            db.execute('DELETE FROM jobs WHERE id=?', (job.id,))
        elif linkage == 'other_application_job':
            db.execute("UPDATE provider_attempts SET job_id=?,data=json_set(data,'$.job_id',?) WHERE id=?",
                       (other_job.id, other_job.id, first_id))
        else:
            db.execute("UPDATE provider_attempts SET job_id=?,data=json_set(data,'$.job_id',?) WHERE stage='other' AND owner=?",
                       (job.id, job.id, owner))
        before = db.execute('SELECT state,data FROM provider_attempts WHERE id=?', (first_id,)).fetchone()
        store.forget_jobs(db, owner, application_id=job.application_id)
    with store._read() as db:
        assert db.execute('SELECT response FROM provider_attempts WHERE id=?', (first_id,)).fetchone() == (None,)
        assert db.execute('SELECT state,data FROM provider_attempts WHERE id=?', (first_id,)).fetchone() == before
        retained = db.execute("SELECT response FROM provider_attempts WHERE owner=? AND json_extract(data,'$.application_id')=?",
                              (owner, other.application_id)).fetchone()
        assert retained and 'Synthetic grounded output' in retained[0]


def test_owner_fact_forgetting_scrubs_orphan_prepared_attempt_retains_accounting(setup):  # noqa: F811
    from test_provider import ready

    provider, job = ready(setup)
    attempt = provider.reserve(job.id, job.lease_owner, job.fence, 'orphan', 5000, 'synthetic-model')
    with provider.store._tx() as db:
        db.execute('UPDATE provider_attempts SET response=? WHERE id=?', ('synthetic-orphan-plaintext', attempt.id))
        db.execute('DELETE FROM jobs WHERE id=?', (job.id,))
        provider.store.forget_jobs(db, job.profile_id, affected=('facts',))
    with provider.store._read() as db:
        row = db.execute('SELECT state,data,response FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone()
    from copilot.domain.contracts import ProviderAttempt
    retained = ProviderAttempt.model_validate_json(row[1])
    assert row[0] == 'indeterminate' and row[2] is None
    assert retained.state == 'indeterminate' and retained.reserved_tokens == 5000


def test_orphan_keys_are_erased_by_owner_and_exact_application_scope(setup):  # noqa: F811
    from copilot.domain.repository import ApplicationInput

    store, owner, jobs, _ = setup
    application = app(setup)
    with store._read() as db:
        revision = jobs.domain.revisions(db, owner).metadata
    other = jobs.domain.create_application(owner, ApplicationInput(expected_metadata_revision=revision,
        company='Other', role='Analyst', sector='finance', vacancy_url='https://other.test/job'))
    first = jobs.enqueue(owner, 'draft', {}, 'private-canary-A', application)
    second = jobs.enqueue(owner, 'draft', {}, 'private-canary-B', other.application_id)
    with store._tx() as db:
        db.execute('DELETE FROM jobs WHERE id IN (?,?)', (first.id, second.id))
        store.forget_jobs(db, owner, application_id=application)
    with store._read() as db:
        assert db.execute('SELECT key FROM job_keys WHERE owner=?', (owner,)).fetchall() == [('private-canary-B',)]
    with store._tx() as db:
        store.forget_jobs(db, owner, profile=True)
    with store._read() as db:
        assert not db.execute('SELECT 1 FROM job_keys WHERE owner=?', (owner,)).fetchone()


def test_unknown_legacy_key_scope_blocks_app_deletion_without_cross_app_loss(setup):  # noqa: F811
    from copilot.domain.repository import ApplicationPatch

    store, owner, jobs, _ = setup
    application = app(setup)
    with store._tx() as db:
        db.execute('DROP TABLE job_keys')
        db.execute('CREATE TABLE job_keys(owner TEXT,key TEXT,job_id TEXT,request_sha256 TEXT,PRIMARY KEY(owner,key))')
        db.execute('INSERT INTO job_keys VALUES(?,?,?,?)', (owner, 'private-legacy-unknown', str(uuid4()), 'a' * 64))
    from copilot.jobs import Jobs
    Jobs(store)
    before = jobs.domain.application(owner, application)
    with pytest.raises(EvidenceError) as exc:
        jobs.domain.delete_application(owner, application, ApplicationPatch(
            expected_input_revision=before.input_revision, expected_output_revision=before.output_revision))
    assert exc.value.code == 'CONFLICT'
    assert jobs.domain.application(owner, application) == before
    with store._read() as db:
        assert db.execute('SELECT key FROM job_keys WHERE owner=?', (owner,)).fetchone() == ('private-legacy-unknown',)
    with store._tx() as db:
        store.forget_jobs(db, owner, profile=True)
    with store._read() as db:
        assert not db.execute('SELECT 1 FROM job_keys WHERE owner=?', (owner,)).fetchone()


@pytest.mark.parametrize('tamper', ['none', 'missing_job', 'sql_owner', 'serialized_owner', 'serialized_id'])
def test_legacy_key_scope_migration_backfills_only_exact_retained_job(setup, tamper):  # noqa: F811
    from copilot.jobs import Jobs

    store, owner, jobs, _ = setup
    job = queued(setup)
    with store._tx() as db:
        rows = db.execute('SELECT owner,key,job_id,request_sha256 FROM job_keys').fetchall()
        db.execute('DROP TABLE job_keys')
        db.execute('CREATE TABLE job_keys(owner TEXT,key TEXT,job_id TEXT,request_sha256 TEXT,PRIMARY KEY(owner,key))')
        db.executemany('INSERT INTO job_keys VALUES(?,?,?,?)', rows)
        if tamper == 'missing_job':
            db.execute('DELETE FROM jobs WHERE id=?', (job.id,))
        elif tamper == 'sql_owner':
            db.execute('UPDATE jobs SET owner=? WHERE id=?', (str(uuid4()), job.id))
        elif tamper == 'serialized_owner':
            db.execute("UPDATE jobs SET data=json_set(data,'$.profile_id',?) WHERE id=?", (str(uuid4()), job.id))
        elif tamper == 'serialized_id':
            db.execute("UPDATE jobs SET data=json_set(data,'$.id',?) WHERE id=?", (str(uuid4()), job.id))
    Jobs(store)
    with store._read() as db:
        scope = db.execute('SELECT application_id,kind FROM job_keys WHERE owner=?', (owner,)).fetchone()
    assert scope == ((job.application_id, 'draft') if tamper == 'none' else (None, None))
