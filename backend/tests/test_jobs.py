"""Durable authority and stage publication tests, not scheduling throughput claims."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ApplicationInput, DomainRepository
from copilot.jobs import Jobs
from copilot.store import Store


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    clock = Clock()
    jobs = Jobs(store, clock)
    return store, owner, jobs, clock


def app(setup):
    store, owner, _, _ = setup
    return DomainRepository(store).create_application(owner, ApplicationInput(
        expected_metadata_revision=0, company='Example', role='Engineer', sector='tech',
        vacancy_url='https://example.test/job')).application_id


def test_atomic_claim_idempotent_request_and_ownership(setup):
    _, owner, jobs, _ = setup
    queued = jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one')
    assert jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one') == queued
    with pytest.raises(EvidenceError):
        jobs.enqueue(owner, 'index', {'corpus': 'documents'}, 'one')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: jobs.claim(str(uuid4())), range(2)))
    assert sum(r is not None for r in results) == 1
    with pytest.raises(EvidenceError):
        jobs.get(str(uuid4()), queued.id)


def test_expiry_fence_blocks_old_worker_stage_and_heartbeat(setup):
    _, owner, jobs, clock = setup
    jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one')
    first = jobs.claim(str(uuid4()), lease_seconds=5)
    clock.advance(6)
    second = jobs.claim(str(uuid4()), lease_seconds=5)
    assert second.fence == first.fence + 1
    for call in (lambda: jobs.commit_stage(first.id, first.lease_owner, first.fence, 'built', {'safe': True}),
                 lambda: jobs.heartbeat(first.id, first.lease_owner, first.fence)):
        with pytest.raises(EvidenceError):
            call()
    jobs.commit_stage(second.id, second.lease_owner, second.fence, 'built', {'safe': True})
    assert jobs.stage_result(owner, second.id, 'built') == {'safe': True}


def test_stages_immutable_reused_without_repeat_callback(setup):
    _, owner, jobs, _ = setup
    jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one')
    current = jobs.claim(str(uuid4()))
    calls = []
    def publish(*_args):
        calls.append(True)
    for _ in range(2):
        jobs.commit_stage(current.id, current.lease_owner, current.fence, 'stage', {'result': 1}, publish)
    assert calls == [True]
    with pytest.raises(EvidenceError):
        jobs.commit_stage(current.id, current.lease_owner, current.fence, 'stage', {'result': 2})
    assert jobs.finish(current.id, current.lease_owner, current.fence).state == 'completed'


def test_index_ignores_metadata_but_blocks_changed_corpus(setup):
    store, owner, jobs, _ = setup
    jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one')
    leased = jobs.claim(str(uuid4()))
    DomainRepository(store).patch_profile(owner, c.ProfilePatch(expected_metadata_revision=0, name='Renamed'))
    jobs.commit_stage(leased.id, leased.lease_owner, leased.fence, 'metadata_safe', {'ok': True})
    store.confirm_fact(owner, 'Changed fact')
    with pytest.raises(EvidenceError):
        jobs.commit_stage(leased.id, leased.lease_owner, leased.fence, 'stale', {'ok': True})


def test_cancel_and_profile_tombstone_block_staging(setup):
    store, owner, jobs, _ = setup
    queued = jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one')
    assert jobs.cancel(owner, queued.id).state == 'cancelled'
    assert jobs.claim(str(uuid4())) is None
    jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'two')
    running = jobs.claim(str(uuid4()))
    store.delete_profile(owner)
    with pytest.raises(EvidenceError):
        jobs.commit_stage(running.id, running.lease_owner, running.fence, 'late', {'ok': True})


def test_application_output_guard_captured_server_side(setup):
    store, owner, jobs, _ = setup
    application = app(setup)
    jobs.enqueue(owner, 'draft', {}, 'one', application)
    leased = jobs.claim(str(uuid4()))
    with store._tx() as db:
        raw = store._record(db, owner, application, 'workspace:application')
        raw['output_revision'] += 1
        import json
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), application))
    with pytest.raises(EvidenceError):
        jobs.commit_stage(leased.id, leased.lease_owner, leased.fence, 'late', {'ok': True})


def test_five_reclaims_bound_unpaid_restart(setup):
    _, owner, jobs, clock = setup
    queued = jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one')
    for _ in range(5):
        assert jobs.claim(str(uuid4()), lease_seconds=5)
        clock.advance(6)
    assert jobs.claim(str(uuid4()), lease_seconds=5) is None
    assert jobs.get(owner, queued.id).state == 'failed'


def test_final_publication_and_terminal_transition_are_atomic(setup):
    store, owner, jobs, _ = setup
    application = app(setup)
    queued = jobs.enqueue(owner, 'draft', {}, 'terminal', application)
    leased = jobs.claim(str(uuid4()))
    def publish(db, _job, _result):
        raw = store._record(db, owner, application, 'workspace:application')
        raw['output_revision'] += 1
        import json
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), application))
    jobs.commit_stage(leased.id, leased.lease_owner, leased.fence, 'published', {'ok': True}, publish, terminal=True)
    final = jobs.get(owner, queued.id)
    assert final.state == 'completed' and final.revisions.application_output == 0
    assert DomainRepository(store).application(owner, application).output_revision == 1


def test_publication_callback_failure_rolls_back_counter_and_stage(setup):
    store, owner, jobs, _ = setup
    application = app(setup)
    jobs.enqueue(owner, 'draft', {}, 'terminal', application)
    leased = jobs.claim(str(uuid4()))
    def publish(db, _job, _result):
        raw = store._record(db, owner, application, 'workspace:application')
        raw['output_revision'] += 1
        import json
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), application))
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        jobs.commit_stage(leased.id, leased.lease_owner, leased.fence, 'published', {'ok': True}, publish, terminal=True)
    assert DomainRepository(store).application(owner, application).output_revision == 0
    assert jobs.get(owner, leased.id).state == 'running'
    assert jobs.stage_result(owner, leased.id, 'published') is None


def test_deleted_application_does_not_starve_other_queued_owner(setup):
    store, owner, jobs, _ = setup
    application = app(setup)
    stale = jobs.enqueue(owner, 'draft', {}, 'stale', application)
    with store._tx() as db:
        db.execute('DELETE FROM records WHERE id=?', (application,))
    other = store.create_profile('Other', ['finance']).id
    runnable = jobs.enqueue(other, 'index', {'corpus': 'facts'}, 'runnable')
    leased = jobs.claim(str(uuid4()))
    assert leased.id == runnable.id
    assert jobs.get(owner, stale.id).state == 'failed'


def test_nonterminal_dependency_changing_callback_is_rejected_and_rolled_back(setup):
    store, owner, jobs, _ = setup
    application = app(setup)
    jobs.enqueue(owner, 'draft', {}, 'one', application)
    leased = jobs.claim(str(uuid4()))
    def publish(db, _job, _result):
        db.execute('INSERT INTO app_revisions VALUES(?,?,?)', (owner, application, 1))
    with pytest.raises(EvidenceError):
        jobs.commit_stage(leased.id, leased.lease_owner, leased.fence, 'research', {'ok': True}, publish)
    with store._tx() as db:
        assert db.execute('SELECT count(*) FROM app_revisions').fetchone()[0] == 0
    assert jobs.stage_result(owner, leased.id, 'research') is None


def test_terminal_publication_rejects_stale_fence_before_callback(setup):
    _, owner, jobs, clock = setup
    jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'one')
    old = jobs.claim(str(uuid4()), lease_seconds=5)
    clock.advance(6)
    assert jobs.claim(str(uuid4()))
    callbacks = []
    with pytest.raises(EvidenceError):
        jobs.commit_stage(old.id, old.lease_owner, old.fence, 'final', {'ok': True},
                          lambda *_: callbacks.append(True), terminal=True)
    assert callbacks == []


def test_disappeared_profile_preserves_paid_ambiguity_and_does_not_starve_queue(setup):
    from test_provider import ready
    store, owner, jobs, _ = setup
    provider, leased = ready(setup)
    attempt = provider.reserve(leased.id, leased.lease_owner, leased.fence, 'prepared', 5000, 'model')
    store.delete_profile(owner)
    other = store.create_profile('Other', ['finance']).id
    queued = jobs.enqueue(other, 'index', {'corpus': 'facts'}, 'other')
    assert jobs.claim(str(uuid4())).id == queued.id
    with store._tx() as db:
        assert db.execute('SELECT state FROM jobs WHERE id=?', (leased.id,)).fetchone()[0] == 'indeterminate'
        assert db.execute('SELECT state FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone()[0] == 'indeterminate'


def test_warned_retry_request_is_atomically_idempotent_without_original_revival(setup):
    from test_provider import retry_request, uncertain
    provider, original, attempt = uncertain(setup)
    def retry(_):
        return provider.request_retry(original.profile_id, original.id, retry_request(attempt))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(retry, range(4)))
    assert len({job.id for job in results}) == 1
    assert len(provider.jobs.list(original.profile_id)) == 2
    assert provider.jobs.get(original.profile_id, original.id).state == 'indeterminate'
    assert provider.jobs.retry_lineage(original.profile_id, results[0].id)['prior_attempt_id'] == attempt.id


@pytest.mark.parametrize('kind', ['index', 'cleanup'])
def test_warned_retry_has_no_local_index_or_cleanup_capability(setup, kind):
    from copilot.provider import Provider
    from copilot.retrycontracts import WarnedRetry
    _, owner, jobs, _ = setup
    job = jobs.enqueue(owner, kind, {'corpus': 'facts'} if kind == 'index' else {}, 'local')
    with jobs.store._tx() as db:
        jobs._save(db, job.model_copy(update={'state': 'indeterminate'}))
    with pytest.raises(EvidenceError):
        Provider(jobs, lambda: pytest.fail('No paid key access')).request_retry(owner, job.id,
            WarnedRetry(prior_attempt_id=str(uuid4()), idempotency_key='retry',
                        acknowledge_duplicate_charge=True, warning_version='duplicate_charge_possible_v1'))
    assert len(jobs.list(owner)) == 1
