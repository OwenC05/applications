"""Trusted S5 transmission guards and shared byte estimates; synthetic transport only."""
import pytest
from test_jobs import setup  # noqa: F401
from test_provider import Output, good, ready, send

from copilot.contracts import EvidenceError
from copilot.jobs import encode
from copilot.provider import TransmissionGuard, build_payload, conservative_reservation


def options(**changes):
    return dict(model='configured-pinned-model', instructions='Use supplied evidence.',
                input_value={'evidence': 'Synthetic 😀'}, output_model=Output,
                max_output_tokens=2000) | changes


def test_estimator_is_exact_shared_payload_bytes_plus_output_ceiling():
    payload = build_payload(**options())
    assert conservative_reservation(**options()) == len(encode(payload).encode('utf-8')) + 2000
    assert payload['store'] is False and payload['text']['format']['strict'] is True


@pytest.mark.parametrize('changes', [dict(max_output_tokens=True), dict(max_output_tokens=8001),
    dict(instructions=''), dict(input_value={'large': 'x' * 131072})])
def test_estimator_preserves_payload_bounds(changes):
    with pytest.raises(EvidenceError):
        conservative_reservation(**options(**changes))


def guard_for(provider, events, state):
    def prepare():
        # Opening an independent writer proves prepare is outside the send writer.
        with provider.store._tx() as db:
            db.execute('SELECT 1')
        events.append('prepare')
        return state['version']
    def validate(db, certificate):
        assert db.in_transaction
        events.append('validate')
        if certificate != state['version'] or state.get('deny'):
            raise EvidenceError('CONFLICT', 'Synthetic scope changed', 409)
    return TransmissionGuard(prepare, validate)


def test_guard_wraps_admission_actual_before_send_completion_and_cache(setup):  # noqa: F811
    events, state, sent = [], {'version': 1}, []
    def transport(payload, before_send):
        events.append('connected')
        before_send()
        sent.append(True)
        events.append('sent')
        return good(payload, lambda: 'synthetic-private-key')
    provider, job = ready(setup, transport)
    guard = guard_for(provider, events, state)
    assert send(provider, job, guard=guard).answer
    assert events == ['prepare', 'validate', 'connected', 'prepare', 'validate', 'sent', 'prepare', 'validate']
    events.clear()
    assert send(provider, job, guard=guard).answer
    assert sent == [True] and events == ['prepare', 'validate', 'prepare', 'validate']


def test_mutation_after_connect_is_denied_before_any_body_and_keeps_uncertainty(setup):  # noqa: F811
    events, state, sent = [], {'version': 1}, []
    def transport(payload, before_send):
        state['deny'] = True
        before_send()
        sent.append(True)
    provider, job = ready(setup, transport)
    with pytest.raises(EvidenceError) as exc:
        send(provider, job, guard=guard_for(provider, events, state))
    assert exc.value.code == 'PROVIDER_UNKNOWN' and sent == []
    assert provider.jobs.get(job.profile_id, job.id).state == 'indeterminate'
    assert provider.usage(job.profile_id)['reserved_calls'] == 1


def test_scope_change_between_prepare_and_writer_is_denied(setup):  # noqa: F811
    sent, count = [], 0
    provider, job = ready(setup, lambda *_: sent.append(True))
    def prepare():
        return 1
    def validate(db, certificate):
        nonlocal count
        count += 1
        raise EvidenceError('CONFLICT', 'Synthetic canonical change', 409)
    with pytest.raises(EvidenceError) as exc:
        send(provider, job, guard=TransmissionGuard(prepare, validate))
    assert exc.value.code == 'CONFLICT' and count == 1 and not sent
    assert provider.usage(job.profile_id)['reserved_calls'] == 0


def test_guard_completion_change_cannot_commit_response(setup):  # noqa: F811
    events, state = [], {'version': 1}
    def transport(payload, before_send):
        result = good(payload, before_send)
        state['deny'] = True
        return result
    provider, job = ready(setup, transport)
    with pytest.raises(EvidenceError) as exc:
        send(provider, job, guard=guard_for(provider, events, state))
    assert exc.value.code == 'PROVIDER_UNKNOWN'
    assert provider.jobs.get(job.profile_id, job.id).state == 'indeterminate'
    with provider.store._read() as db:
        assert db.execute('SELECT response FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone() == (None,)


def test_guard_must_be_trusted_internal_protocol_before_any_attempt(setup):  # noqa: F811
    sent = []
    provider, job = ready(setup, lambda *_: sent.append(True))
    with pytest.raises(EvidenceError):
        send(provider, job, guard={'prepare': 'model-chosen-tool'})
    assert not sent and provider.usage(job.profile_id)['reserved_calls'] == 0
