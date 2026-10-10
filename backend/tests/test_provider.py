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


def uncertain(setup):
    def transport(payload, before_send):
        before_send()
        raise RuntimeError('synthetic transport loss')
    provider, job = ready(setup, transport)
    with pytest.raises(EvidenceError):
        send(provider, job)
    with provider.store._read() as db:
        attempt = c.ProviderAttempt.model_validate_json(db.execute('SELECT data FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone()[0])
    return provider, job, attempt


def retry_request(attempt, key='warned-retry', **changes):
    from copilot.retrycontracts import WarnedRetry
    return WarnedRetry(prior_attempt_id=attempt.id, idempotency_key=key,
                       acknowledge_duplicate_charge=True, warning_version='duplicate_charge_possible_v1', **changes)


def test_explicit_warned_retry_distinct_lineage_and_original_uncertainty_unchanged(setup):
    provider, original, attempt = uncertain(setup)
    retry = provider.request_retry(original.profile_id, original.id, retry_request(attempt))
    assert retry.id != original.id and retry.revisions == original.revisions
    assert provider.request_retry(original.profile_id, original.id, retry_request(attempt)).id == retry.id
    with pytest.raises(EvidenceError):
        provider.request_retry(original.profile_id, original.id, retry_request(attempt).model_copy(update={'prior_attempt_id': str(uuid4())}))
    leased = provider.jobs.claim(str(uuid4()), job_id=retry.id)
    provider.transport = good
    assert send(provider, leased).answer == 'Synthetic grounded output'
    assert provider.jobs.get(original.profile_id, original.id).state == 'indeterminate'
    with provider.store._read() as db:
        rows = db.execute('SELECT id,state FROM provider_attempts ORDER BY rowid').fetchall()
        assert len(rows) == 2 and rows[0] == (attempt.id, 'indeterminate') and rows[1][0] != attempt.id
        assert db.execute('SELECT prior_attempt_id FROM provider_retry_links WHERE attempt_id=?', (rows[1][0],)).fetchone() == (attempt.id,)
    assert provider.usage(original.profile_id)['reserved_calls'] == 2


@pytest.mark.parametrize('condition', ['missing_ack', 'cross_owner', 'stale', 'revoked', 'no_budget', 'exhausted'])
def test_warned_retry_rejects_without_new_jobs_or_reservations(setup, condition):
    provider, original, attempt = uncertain(setup)
    store, owner, jobs, _ = setup
    body = retry_request(attempt)
    request_owner = owner
    if condition == 'missing_ack':
        body = body.model_copy(update={'acknowledge_duplicate_charge': False})
    elif condition == 'cross_owner':
        request_owner = store.create_profile('Other', ['tech']).id
    elif condition == 'stale':
        store.confirm_fact(owner, 'Changed selected evidence')
    elif condition == 'revoked':
        DomainRepository(store).consent(owner, ConsentInput(expected_consent_revision=1, provider='openai', purposes=('drafting',), granted=False))
    elif condition == 'no_budget':
        with store._tx() as db:
            db.execute('DELETE FROM budget_limits WHERE owner=?', (owner,))
    else:
        provider.configure_limits(owner, Limits(5000, 1, 5000, 1))
    with pytest.raises((EvidenceError, ValueError)):
        provider.request_retry(request_owner, original.id, body)
    assert len(jobs.list(owner)) == 1
    assert provider.usage(owner)['reserved_calls'] == 1


def test_retry_without_key_queues_but_cannot_reserve_or_transmit(setup):
    provider, original, attempt = uncertain(setup)
    provider._key_supplier = lambda: pytest.fail('Queue operation must not access key')
    retry = provider.request_retry(original.profile_id, original.id, retry_request(attempt))
    job = provider.jobs.claim(str(uuid4()), job_id=retry.id)
    provider._key_supplier = lambda: ''
    provider.transport = lambda *_: pytest.fail('No key must mean no paid transmission')
    with pytest.raises(EvidenceError) as error:
        send(provider, job)
    assert error.value.code == 'PROVIDER_UNCONFIGURED'
    assert provider.usage(original.profile_id)['reserved_calls'] == 1


def test_retry_queue_api_is_strict_scoped_and_honest_about_unavailable_execution(setup):
    from fastapi.testclient import TestClient

    from copilot.api import create_app
    from copilot.config import Settings
    provider, original, attempt = uncertain(setup)
    def forbidden(*args):
        pytest.fail('API retry cannot initialize external services')
    api = create_app(Settings(provider.store.root, provider.store.root / 'models'), forbidden)
    # Inject the synthetic trusted clock only, not a key/transport.
    api.state.jobs.clock = provider.jobs.clock
    base = f'/api/workspace/profiles/{original.profile_id}/jobs/{original.id}'
    request = retry_request(attempt).model_dump(mode='json')
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/evidence/status').json()['token']
        attempts = client.get(base + '/provider-attempts')
        assert attempts.status_code == 200
        assert attempts.json()['attempts'][0]['id'] == attempt.id
        assert 'synthetic-private-key' not in attempts.text and 'response' not in attempts.text
        result = client.post(base + '/retry', json=request)
        assert result.status_code == 202
        assert result.json()['original_outcome'] == 'indeterminate'
        assert result.json()['execution_available'] is False
        assert result.json()['retry_of']['prior_attempt_id'] == attempt.id
        assert client.post(base + '/retry', json=request).json()['job']['id'] == result.json()['job']['id']
        for invalid in ({'acknowledge_duplicate_charge': False}, {'acknowledge_duplicate_charge': 1},
                        {'warning_version': 'unknown'}, {'parameters': {'untrusted': True}}):
            assert client.post(base + '/retry', json=request | invalid).status_code == 422
        assert client.post(base + '/retry', content='{}', headers={'content-type': 'text/plain'}).status_code == 415
        assert client.post(base + '/retry', json=request, headers={'x-evidence-token': 'bad'}).status_code == 403
        assert client.get(base + '/provider-attempts', headers={'host': 'evil.example'}).status_code == 403


def test_atomic_retry_family_budget_includes_original_and_sibling_attempts(setup):
    provider, original, attempt = uncertain(setup)
    provider.configure_limits(original.profile_id, Limits(100000, 10, 10000, 2))
    first = provider.request_retry(original.profile_id, original.id, retry_request(attempt, 'first'))
    second = provider.request_retry(original.profile_id, original.id, retry_request(attempt, 'second'))
    leases = [provider.jobs.claim(str(uuid4()), job_id=job.id) for job in (first, second)]
    def reserve(job):
        try:
            return provider.reserve(job.id, job.lease_owner, job.fence, 'draft', 5000, 'model')
        except EvidenceError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, leases))
    assert sum(attempt is not None for attempt in results) == 1
    assert provider.usage(original.profile_id)['reserved_calls'] == 2
    assert provider.jobs.get(original.profile_id, original.id).state == 'indeterminate'


@pytest.mark.parametrize('kind,purpose', [('research', 'research'), ('draft', 'drafting'), ('assess', 'assessment')])
def test_warned_retry_known_paid_operation_preserves_exact_original_capture(setup, kind, purpose):
    store, owner, jobs, _ = setup
    application = app(setup)
    DomainRepository(store).consent(owner, ConsentInput(expected_consent_revision=0,
        provider='openai', purposes=(purpose,), granted=True))
    queued = jobs.enqueue(owner, kind, {'server_selected': ['synthetic-span']}, 'operation', application)
    leased = jobs.claim(str(uuid4()), job_id=queued.id)
    provider = Provider(jobs, lambda: 'synthetic-key')
    provider.configure_limits(owner, Limits(100000, 10, 100000, 10))
    attempt = provider.reserve(leased.id, leased.lease_owner, leased.fence, 'stage', 5000, 'model')
    provider._unknown(attempt.id, leased.id)
    retry = provider.request_retry(owner, leased.id, retry_request(attempt))
    with store._read() as db:
        original_data = jobs._job(db, leased.id)[1:]
        retried_data = jobs._job(db, retry.id)[1:]
    assert original_data == retried_data
    assert retry.revisions == leased.revisions and retry.kind == kind


def test_retry_prior_attempt_must_belong_to_exact_owned_job(setup):
    provider, original, attempt = uncertain(setup)
    queued = provider.jobs.enqueue(original.profile_id, 'draft', {}, 'other-job', original.application_id)
    leased = provider.jobs.claim(str(uuid4()), job_id=queued.id)
    other = provider.reserve(leased.id, leased.lease_owner, leased.fence, 'draft', 5000, 'model')
    provider._unknown(other.id, leased.id)
    with pytest.raises(EvidenceError) as error:
        provider.request_retry(original.profile_id, original.id, retry_request(other))
    assert error.value.code == 'NOT_FOUND'
    assert len(provider.jobs.list(original.profile_id)) == 2
