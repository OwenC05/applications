"""Elapsed-time authority boundaries; real storage, synthetic clock/transport only."""
from uuid import uuid4

import pytest
from test_drafting_service import drafting, enqueue  # noqa: F401
from test_jobs import Clock, setup  # noqa: F401
from test_packet_service import packets, queued  # noqa: F401
from test_provider import Output, good, send
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain.repository import ApplicationInput, ConsentInput, DomainRepository
from copilot.provider import Limits, Provider, TransmissionGuard


@pytest.fixture
def boundary(setup):  # noqa: F811
    store, owner, jobs, clock = setup
    domain = DomainRepository(store)
    app = domain.create_application(owner, ApplicationInput(expected_metadata_revision=0,
        company='Synthetic', role='Engineer', sector='tech', vacancy_url='https://example.test/job'))
    domain.consent(owner, ConsentInput(expected_consent_revision=0, provider='openai',
        purposes=('research',), granted=True))
    queued_job = jobs.enqueue(owner, 'research', {}, 'late-authority', app.application_id)
    job = jobs.claim(str(uuid4()), lease_seconds=30, job_id=queued_job.id)
    assert (job.lease_expires_at - job.heartbeat_at).total_seconds() == 30
    events = {'bodies': 0, 'keys': 0}

    def key():
        events['keys'] += 1
        return 'synthetic-private-key'

    def transport(payload, before_send):
        before_send()
        events['bodies'] += 1
        return good(payload, lambda: 'synthetic-private-key')

    provider = Provider(jobs, key, transport)
    provider.configure_limits(owner, Limits(100000, 10, 50000, 5))
    return provider, job, clock, events


def retained(provider, job):
    with provider.store._read() as db:
        rows = db.execute('SELECT state,dispatched,response,data FROM provider_attempts WHERE job_id=?',
                          (job.id,)).fetchall()
    current = provider.jobs.get(job.profile_id, job.id)
    assert current.fence == job.fence and current.lease_owner == job.lease_owner
    assert current.lease_expires_at == job.lease_expires_at and current.attempt_count == 1
    return rows, current


def assert_unknown(provider, job, events, bodies):
    rows, current = retained(provider, job)
    assert len(rows) == 1
    assert rows[0][:3] == ('indeterminate', int(bodies > 0), None)
    assert current.state == 'indeterminate' and current.stage == 'provider_unknown'
    usage = provider.usage(job.profile_id)
    assert usage['reserved_calls'] == 1 and usage['reserved_tokens'] == 5000
    assert usage['unknown_attempts'] == 1 and usage['reported_tokens'] == 0
    assert events['bodies'] == bodies


def accepting_guard(provider, job, clock, expire_at, seconds=31):
    calls = []

    def validate(db, certificate):
        assert db.in_transaction and certificate is True
        provider.jobs.assert_current(db, job.id, job.lease_owner, job.fence)
        calls.append(True)
        if len(calls) == expire_at:
            clock.advance(seconds)

    return TransmissionGuard(lambda: True, validate)


@pytest.mark.parametrize('seconds', [30, 31])
def test_accepting_dispatch_guard_cannot_admit_expired_body(boundary, seconds):
    provider, job, clock, events = boundary
    guard = accepting_guard(provider, job, clock, 2, seconds)
    with pytest.raises(EvidenceError) as error:
        send(provider, job, guard=guard)
    assert error.value.code == 'PROVIDER_UNKNOWN'
    assert_unknown(provider, job, events, 0)


def test_accepting_completion_guard_cannot_persist_expired_response(boundary):
    provider, job, clock, events = boundary
    with pytest.raises(EvidenceError) as error:
        send(provider, job, guard=accepting_guard(provider, job, clock, 3))
    assert error.value.code == 'PROVIDER_UNKNOWN'
    assert_unknown(provider, job, events, 1)


def test_accepting_cache_guard_cannot_return_expired_completed_output(boundary):
    provider, job, clock, events = boundary
    guard = accepting_guard(provider, job, clock, 5)
    assert send(provider, job, guard=guard).answer == 'Synthetic grounded output'
    before = retained(provider, job)
    with pytest.raises(EvidenceError) as error:
        send(provider, job, guard=guard)
    assert error.value.code == 'CONFLICT'
    assert retained(provider, job) == before
    assert events['bodies'] == 1
    assert provider.usage(job.profile_id)['reported_tokens'] == 20


def test_family_budget_work_cannot_admit_body_after_lease_expiry(boundary, monkeypatch):
    provider, job, clock, events = boundary
    original = provider._family_attempts

    def delayed(db, job_id, exclude=None):
        result = original(db, job_id, exclude)
        if exclude is not None:  # Transmission budget check, not initial reservation.
            clock.advance(31)
        return result

    monkeypatch.setattr(provider, '_family_attempts', delayed)
    with pytest.raises(EvidenceError) as error:
        send(provider, job)
    assert error.value.code == 'PROVIDER_UNKNOWN'
    assert_unknown(provider, job, events, 0)


@pytest.mark.parametrize('seconds', [30, 31])
def test_jobs_capture_finishing_at_or_after_deadline_denies_authority(boundary, monkeypatch, seconds):
    provider, job, clock, events = boundary
    original = provider.jobs.capture

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.advance(seconds)
        return result

    monkeypatch.setattr(provider.jobs, 'capture', delayed)
    with provider.store._tx() as db, pytest.raises(EvidenceError) as error:
        provider.jobs.assert_current(db, job.id, job.lease_owner, job.fence)
    assert error.value.code == 'CONFLICT'
    rows, current = retained(provider, job)
    assert rows == [] and current.state == 'running'
    assert events == {'bodies': 0, 'keys': 0}


@pytest.mark.parametrize('operation', ['dispatch', 'completion'])
def test_existing_purpose_consent_work_cannot_outlive_lease(boundary, monkeypatch, operation):
    provider, job, clock, events = boundary
    original = provider._purpose_consent
    calls = []

    def delayed(db, current):
        result = original(db, current)
        calls.append(True)
        # Detect observable transport phase, not a future helper's call count.
        if (operation == 'dispatch' and events['keys'] == 1
                or operation == 'completion' and events['bodies'] == 1):
            clock.advance(31)
        return result

    monkeypatch.setattr(provider, '_purpose_consent', delayed)
    with pytest.raises(EvidenceError) as error:
        send(provider, job)
    assert calls and error.value.code == 'PROVIDER_UNKNOWN'
    assert_unknown(provider, job, events, int(operation == 'completion'))


def test_completed_response_serialization_cannot_persist_after_expiry(boundary, monkeypatch):
    provider, job, clock, events = boundary
    original = Output.model_dump_json

    def delayed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        clock.advance(31)
        return result

    monkeypatch.setattr(Output, 'model_dump_json', delayed)
    with pytest.raises(EvidenceError) as error:
        send(provider, job)
    assert error.value.code == 'PROVIDER_UNKNOWN'
    assert_unknown(provider, job, events, 1)


def test_cached_output_parsing_cannot_return_after_expiry(boundary, monkeypatch):
    provider, job, clock, events = boundary
    assert send(provider, job).answer
    before = retained(provider, job)
    original = Output.model_validate_json

    def delayed(cls, *args, **kwargs):
        result = original(*args, **kwargs)
        clock.advance(31)
        return result

    monkeypatch.setattr(Output, 'model_validate_json', classmethod(delayed))
    with pytest.raises(EvidenceError) as error:
        send(provider, job)
    assert error.value.code == 'CONFLICT'
    assert retained(provider, job) == before and events['bodies'] == 1


@pytest.mark.parametrize('seconds', [30, 31])
def test_already_expired_entry_denies_without_reservation_or_key(boundary, seconds):
    provider, job, clock, events = boundary
    clock.advance(seconds)
    with pytest.raises(EvidenceError) as error:
        send(provider, job)
    assert error.value.code == 'CONFLICT'
    assert retained(provider, job)[0] == []
    assert events == {'bodies': 0, 'keys': 0}
    assert provider.usage(job.profile_id)['reserved_calls'] == 0


def test_live_boundary_preserves_single_send_cache_fence_and_key_reads(boundary):
    provider, job, _, events = boundary
    assert send(provider, job).answer == 'Synthetic grounded output'
    keys = events['keys']
    assert send(provider, job).answer == 'Synthetic grounded output'
    rows, current = retained(provider, job)
    assert len(rows) == 1 and rows[0][0:2] == ('completed', 1) and rows[0][2]
    assert current.state == 'running' and events['bodies'] == 1
    assert keys == 3 and events['keys'] == 4  # Final effect checks must not load keys.
    assert provider.usage(job.profile_id)['unknown_attempts'] == 0


def test_explicit_guard_rejection_remains_unsent_and_unreserved(boundary):
    provider, job, _, events = boundary

    def reject(db, certificate):
        raise EvidenceError('CONFLICT', 'Synthetic explicit rejection', 409)

    with pytest.raises(EvidenceError) as error:
        send(provider, job, guard=TransmissionGuard(lambda: True, reject))
    assert error.value.code == 'CONFLICT'
    assert retained(provider, job)[0] == [] and events['bodies'] == 0
    assert provider.usage(job.profile_id)['reserved_calls'] == 0


def test_packet_currentness_work_cannot_return_expired_authority(packets, monkeypatch):  # noqa: F811
    *_, service = packets
    queued_job, _ = queued(packets)
    clock = Clock()
    # Keep research freshness valid; only this claimed lease advances synthetically.
    clock.now = service.jobs._now()
    service.jobs.clock = clock
    job = service.jobs.claim(str(uuid4()), lease_seconds=30, job_id=queued_job.id)
    original = service.jobs._packet_current
    calls = []

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        clock.advance(31)
        return result

    monkeypatch.setattr(service.jobs, '_packet_current', delayed)
    with service.store._tx() as db, pytest.raises(EvidenceError) as error:
        service.jobs.assert_current(db, job.id, job.lease_owner, job.fence)
    assert calls == [True] and error.value.code == 'CONFLICT'
    assert service.jobs.get(job.profile_id, job.id) == job


def test_admission_work_cannot_return_expired_draft_authority(drafting, monkeypatch):  # noqa: F811
    import copilot.jobs as jobs_module

    service, _, _, sent, _ = drafting
    queued_job, _ = enqueue(drafting)
    clock = Clock()
    clock.now = service.jobs._now()
    service.jobs.clock = clock
    job = service.jobs.claim(str(uuid4()), lease_seconds=30, job_id=queued_job.id)
    original = jobs_module.require_dispatch_admission
    calls = []

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        clock.advance(31)
        return result

    monkeypatch.setattr(jobs_module, 'require_dispatch_admission', delayed)
    with service.store._tx() as db, pytest.raises(EvidenceError) as error:
        service.jobs.assert_current(db, job.id, job.lease_owner, job.fence)
    assert calls == [True] and error.value.code == 'CONFLICT'
    assert service.jobs.get(job.profile_id, job.id) == job and sent == []


@pytest.mark.parametrize('phase,guard_call', [('dispatch', 2), ('completion', 3), ('cache', 5)])
def test_final_effect_consent_work_is_followed_by_live_lease_check(boundary, monkeypatch, phase, guard_call):
    """Placement control: post-guard consent must exist and may itself consume time."""
    provider, job, clock, events = boundary
    original = provider._purpose_consent
    state = {'guards': 0, 'armed': False, 'final_consent': 0}

    def validate(db, certificate):
        assert certificate is True and db.in_transaction
        provider.jobs.assert_current(db, job.id, job.lease_owner, job.fence)
        state['guards'] += 1
        state['armed'] = state['guards'] == guard_call

    def delayed(db, current):
        result = original(db, current)
        if state['armed'] and events['bodies'] == int(phase != 'dispatch'):
            state['final_consent'] += 1
            clock.advance(31)
        return result

    monkeypatch.setattr(provider, '_purpose_consent', delayed)
    guard = TransmissionGuard(lambda: True, validate)
    if phase == 'cache':
        assert send(provider, job, guard=guard).answer
        before = retained(provider, job)
    with pytest.raises(EvidenceError) as error:
        send(provider, job, guard=guard)
    assert state['final_consent'] == 1
    if phase == 'cache':
        assert error.value.code == 'CONFLICT'
        assert retained(provider, job) == before and events['bodies'] == 1
    else:
        assert error.value.code == 'PROVIDER_UNKNOWN'
        assert_unknown(provider, job, events, int(phase == 'completion'))
