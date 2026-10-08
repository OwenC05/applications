"""Synthetic direct Responses transport; no key discovery or paid network calls."""
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_jobs import app
from test_jobs import setup as setup

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ConsentInput, DomainRepository
from copilot.provider import DefinitelyUnsent, Limits, Provider


class Output(c.Contract):
    answer: str


def ready(setup, transport=None, key='synthetic-private-key'):
    store, owner, jobs, _ = setup
    application = app(setup)
    DomainRepository(store).consent(owner, ConsentInput(expected_consent_revision=0,
        provider='openai', purposes=('drafting',), granted=True))
    jobs.enqueue(owner, 'draft', {}, 'one', application)
    leased = jobs.claim(str(uuid4()), lease_seconds=5)
    provider = Provider(jobs, lambda: key, transport)
    provider.configure_limits(owner, Limits(100000, 10, 50000, 5))
    return provider, leased


def good(payload, before_send):
    assert before_send() == 'synthetic-private-key'
    assert payload['store'] is False
    assert payload['text']['format']['type'] == 'json_schema'
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': json.dumps({'answer': 'Synthetic grounded output'})}]}],
        'usage': {'total_tokens': 20}}


def send(provider, job, stage='draft', **changes):
    options = {'model': 'configured-pinned-model', 'instructions': 'Use supplied evidence.',
               'input_value': {'evidence': 'Synthetic'}, 'output_model': Output,
               'reserved_tokens': 5000, 'max_output_tokens': 2000, **changes}
    return provider.send(job.id, job.lease_owner, job.fence, stage, **options)


def test_success_stage_cache_request_hash_and_content_free_usage(setup):
    calls = []
    def transport(payload, before_send):
        calls.append(True)
        return good(payload, before_send)
    provider, job = ready(setup, transport)
    assert send(provider, job).answer == 'Synthetic grounded output'
    assert send(provider, job).answer == 'Synthetic grounded output'
    assert calls == [True]
    with pytest.raises(EvidenceError):
        send(provider, job, input_value={'evidence': 'Changed'})
    usage = provider.usage(job.profile_id)
    assert usage['reserved_calls'] == 1 and usage['reported_tokens'] == 20
    assert usage['monetary_cost'] is None
    assert 'Synthetic' not in json.dumps(usage) and 'private-key' not in json.dumps(usage)


@pytest.mark.parametrize('condition', ['no_key', 'no_consent', 'wrong_purpose', 'no_budget'])
def test_missing_authority_produces_zero_calls(setup, condition):
    calls = []
    provider, job = ready(setup, lambda *_: calls.append(True))
    store, owner, _, _ = setup
    if condition == 'no_key':
        provider._key_supplier = lambda: ''
    elif condition == 'no_budget':
        with store._tx() as db:
            db.execute('DELETE FROM budget_limits WHERE owner=?', (owner,))
    else:
        purpose = ('research',) if condition == 'wrong_purpose' else ('drafting',)
        DomainRepository(store).consent(owner, ConsentInput(expected_consent_revision=1,
            provider='openai', purposes=purpose, granted=condition != 'no_consent'))
    with pytest.raises(EvidenceError):
        send(provider, job)
    assert calls == []


def test_atomic_reservations_never_exceed_budget(setup):
    provider, job = ready(setup, good)
    provider.configure_limits(job.profile_id, Limits(5000, 1, 5000, 1))
    def reserve(index):
        try:
            return provider.reserve(job.id, job.lease_owner, job.fence, str(index), 5000, 'model')
        except EvidenceError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(item is not None for item in results) == 1
    assert provider.usage(job.profile_id)['reserved_calls'] == 1


def test_transmission_failure_pauses_and_never_replays(setup):
    calls = []
    def failed(payload, before_send):
        before_send()
        calls.append(True)
        raise RuntimeError('Do not expose synthetic-private-key or personal input')
    provider, job = ready(setup, failed)
    with pytest.raises(EvidenceError) as exception:
        send(provider, job)
    assert exception.value.code == 'PROVIDER_UNKNOWN'
    assert 'private-key' not in str(exception.value)
    assert provider.jobs.get(job.profile_id, job.id).state == 'indeterminate'
    with pytest.raises(EvidenceError):
        send(provider, job)
    assert calls == [True]
    assert provider.usage(job.profile_id)['reserved_tokens'] == 5000


def test_prepared_crash_expiry_is_indeterminate_without_reclaim(setup):
    provider, job = ready(setup, good)
    provider.reserve(job.id, job.lease_owner, job.fence, 'prepared', 5000, 'model')
    setup[3].advance(6)
    assert provider.jobs.claim(str(uuid4())) is None
    assert provider.jobs.get(job.profile_id, job.id).state == 'indeterminate'
    assert provider.usage(job.profile_id)['unknown_attempts'] == 1


def test_proven_unsent_releases_reservation_but_does_not_auto_retry(setup):
    calls = []
    def unsent(*_):
        calls.append(True)
        raise DefinitelyUnsent()
    provider, job = ready(setup, unsent)
    with pytest.raises(EvidenceError) as exception:
        send(provider, job)
    assert exception.value.code == 'PROVIDER_UNSENT'
    assert provider.usage(job.profile_id)['reserved_calls'] == 0
    with pytest.raises(EvidenceError):
        send(provider, job)
    assert calls == [True]


def test_consent_revoked_during_connect_stops_before_body(setup):
    sent = []
    store, owner, _, _ = setup
    def revoke(payload, before_send):
        DomainRepository(store).consent(owner, ConsentInput(expected_consent_revision=1,
            provider='openai', purposes=('drafting',), granted=False))
        before_send()
        sent.append(True)
    provider, job = ready(setup, revoke)
    with pytest.raises(EvidenceError):
        send(provider, job)
    assert sent == []
    assert provider.jobs.get(owner, job.id).state == 'indeterminate'


@pytest.mark.parametrize('response', [
    {'status': 'incomplete', 'output': []},
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No'}]}]},
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"answer":123}'}]}]},
])
def test_invalid_refused_incomplete_fail_closed(setup, response):
    def transport(payload, before_send):
        before_send()
        return response
    provider, job = ready(setup, transport)
    with pytest.raises(EvidenceError) as exception:
        send(provider, job)
    assert exception.value.code == 'INVALID_PROVIDER_OUTPUT'
    assert provider.jobs.stage_result(job.profile_id, job.id, 'draft') is None
    assert provider.jobs.get(job.profile_id, job.id).state == 'indeterminate'


def test_tiny_reservation_cannot_request_large_output(setup):
    calls = []
    provider, job = ready(setup, lambda *_: calls.append(True))
    with pytest.raises(EvidenceError):
        send(provider, job, reserved_tokens=1)
    assert calls == [] and provider.usage(job.profile_id)['reserved_calls'] == 0


def test_success_then_revocation_prevents_next_stage_and_preserves_first_result(setup):
    calls = []
    def transport(payload, before_send):
        calls.append(True)
        return good(payload, before_send)
    provider, job = ready(setup, transport)
    assert send(provider, job, 'first')
    DomainRepository(setup[0]).consent(job.profile_id, ConsentInput(expected_consent_revision=1,
        provider='openai', purposes=('drafting',), granted=False))
    with pytest.raises(EvidenceError):
        send(provider, job, 'second')
    assert calls == [True]
    assert provider.usage(job.profile_id)['reserved_calls'] == 1


def test_cancel_during_transmission_discards_late_result_retains_reservation(setup):
    def transport(payload, before_send):
        before_send()
        provider.jobs.cancel(job.profile_id, job.id)
        return good(payload, lambda: 'synthetic-private-key')
    provider, job = ready(setup, transport)
    with pytest.raises(EvidenceError):
        send(provider, job)
    assert provider.jobs.get(job.profile_id, job.id).state == 'indeterminate'
    assert provider.usage(job.profile_id)['reserved_tokens'] == 5000


def test_strict_output_schema_requires_optional_defaults(setup):
    from copilot.provider import strict_output_schema
    schema = strict_output_schema(Output)
    assert set(schema['required']) == set(schema['properties'])
    assert schema['additionalProperties'] is False
    assert 'default' not in schema['properties']['schema_version']


def test_provider_unknown_after_dispatch_cannot_be_declared_unsent(setup):
    def transport(payload, before_send):
        before_send()
        raise DefinitelyUnsent()
    provider, job = ready(setup, transport)
    with pytest.raises(EvidenceError) as exception:
        send(provider, job)
    assert exception.value.code == 'PROVIDER_UNKNOWN'
    assert provider.usage(job.profile_id)['reserved_calls'] == 1


def test_midnight_admission_cannot_exceed_new_day_call_limit(setup):
    from datetime import datetime, timezone
    setup[3].now = datetime(2026, 10, 8, 23, 59, 59, tzinfo=timezone.utc)
    provider, job = ready(setup, good)
    provider.configure_limits(job.profile_id, Limits(10000, 1, 10000, 1))
    attempt = provider.reserve(job.id, job.lease_owner, job.fence, 'first', 5000, 'model')
    setup[3].now = datetime(2026, 10, 9, 0, 0, 1, tzinfo=timezone.utc)
    provider._before_send(attempt.id, job.id, job.lease_owner, job.fence)
    with provider.store._tx() as db:
        day = db.execute('SELECT day FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone()[0]
    assert day == '2026-10-09'
    another = provider.jobs.enqueue(job.profile_id, 'draft', {}, 'new-day', job.application_id)
    leased = provider.jobs.claim(str(uuid4()), job_id=another.id)
    with pytest.raises(EvidenceError):
        provider.reserve(leased.id, leased.lease_owner, leased.fence, 'second', 5000, 'model')


@pytest.mark.parametrize('daily_tokens,daily_calls', [(10000, 1), (5000, 2)])
def test_concurrent_midnight_admissions_respect_tokens_and_calls(setup, daily_tokens, daily_calls):
    from datetime import datetime, timezone
    setup[3].now = datetime(2026, 10, 8, 23, 59, 59, tzinfo=timezone.utc)
    provider, first = ready(setup, good)
    provider.configure_limits(first.profile_id, Limits(10000, 2, 5000, 1))
    other = provider.jobs.enqueue(first.profile_id, 'draft', {}, 'other-midnight', first.application_id)
    second = provider.jobs.claim(str(uuid4()), lease_seconds=5, job_id=other.id)
    attempts = [(job, provider.reserve(job.id, job.lease_owner, job.fence, 'call', 5000, 'model')) for job in (first, second)]
    provider.configure_limits(first.profile_id, Limits(daily_tokens, daily_calls, 5000, 1))
    setup[3].now = datetime(2026, 10, 9, 0, 0, 1, tzinfo=timezone.utc)
    def admit(item):
        job, attempt = item
        try:
            provider._before_send(attempt.id, job.id, job.lease_owner, job.fence)
            return True
        except EvidenceError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(admit, attempts)) == 1
    with provider.store._tx() as db:
        assert db.execute('SELECT count(*) FROM provider_attempts WHERE day=? AND dispatched=1', ('2026-10-09',)).fetchone()[0] == 1
