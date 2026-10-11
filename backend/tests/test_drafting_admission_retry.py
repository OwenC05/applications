"""Legacy retry replay is read-only, never a new paid admission."""
from uuid import uuid4

import pytest
from admission_legacy_helpers import preupgrade_copy
from test_jobs import setup as setup
from test_provider import retry_request, uncertain

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.jobs import Jobs
from copilot.provider import Provider
from copilot.store import Store


def retained_retry(setup, tmp_path):
    provider, original, attempt = uncertain(setup)
    request = retry_request(attempt)
    retry = provider.request_retry(original.profile_id, original.id, request)
    target = tmp_path / 'legacy-retry'
    with preupgrade_copy(provider.store, target) as db:
        for job_id in (original.id, retry.id):
            row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
            job = c.DurableJob.model_validate_json(row[0]).model_copy(update={'kind': 'draft'})
            db.execute('UPDATE jobs SET data=? WHERE id=?', (job.model_dump_json(), job_id))
            db.execute("UPDATE job_keys SET kind='draft' WHERE job_id=?", (job_id,))
    store = Store(target)
    return Provider(Jobs(store, setup[3]), lambda: pytest.fail('No key read')), original, retry, request


def test_exact_legacy_warned_retry_replay_read_only(setup, tmp_path):
    provider, original, retry, request = retained_retry(setup, tmp_path)
    with provider.store._read() as db:
        before = tuple(db.iterdump())
    replayed = provider.request_retry(original.profile_id, original.id, request)
    assert replayed.id == retry.id and replayed.kind == 'draft'
    with provider.store._read() as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('change', ['new_key', 'other_attempt', 'lineage'])
def test_legacy_retry_changes_do_not_insert_or_send(setup, tmp_path, change):
    provider, original, retry, request = retained_retry(setup, tmp_path)
    if change == 'new_key':
        request = request.model_copy(update={'idempotency_key': 'fresh-paid-retry'})
    elif change == 'other_attempt':
        request = request.model_copy(update={'prior_attempt_id': str(uuid4())})
    else:
        with provider.store._tx() as db:
            db.execute('UPDATE job_retries SET prior_attempt_id=? WHERE new_job_id=?', (str(uuid4()), retry.id))
    with provider.store._read() as db:
        before = tuple(db.iterdump())
    with pytest.raises(EvidenceError) as caught:
        provider.request_retry(original.profile_id, original.id, request)
    assert caught.value.code == ('ADMISSION_REQUIRED' if change == 'new_key' else 'CONFLICT')
    with provider.store._read() as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('prior_scalar,retry_scalar', [(True, 1), (1, 1.0)])
@pytest.mark.parametrize('column', ['parameters', 'dependencies'])
def test_legacy_retry_rejects_python_equal_but_distinct_json_scalars(
        setup, tmp_path, prior_scalar, retry_scalar, column):
    import json
    provider, original, retry, request = retained_retry(setup, tmp_path)
    with provider.store._tx() as db:
        for job, scalar in ((original, prior_scalar), (retry, retry_scalar)):
            value = {'nested': [{'version': scalar}]} if column == 'parameters' else [scalar]
            db.execute(f'UPDATE jobs SET {column}=? WHERE id=?', (json.dumps(value), job.id))
    with provider.store._read() as db:
        before = tuple(db.iterdump())
    with pytest.raises(EvidenceError) as caught:
        provider.request_retry(original.profile_id, original.id, request)
    assert caught.value.code == 'CONFLICT'
    with provider.store._read() as db:
        assert tuple(db.iterdump()) == before


def test_legacy_retry_dependency_tuple_projection_does_not_hide_container_change(setup, tmp_path):
    provider, original, retry, request = retained_retry(setup, tmp_path)
    with provider.store._tx() as db:
        db.execute('UPDATE jobs SET dependencies=? WHERE id=?', ('{"facts":1}', original.id))
        db.execute('UPDATE jobs SET dependencies=? WHERE id=?', ('["facts"]', retry.id))
    with pytest.raises(EvidenceError) as caught:
        provider.request_retry(original.profile_id, original.id, request)
    assert caught.value.code == 'CONFLICT'
