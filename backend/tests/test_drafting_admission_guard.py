"""Admission enforcement tests use synthetic SQLite only."""
from uuid import uuid4

import pytest
from test_drafting_service import drafting  # noqa: F401
from test_jobs import setup as setup
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError


@pytest.mark.parametrize('kind', ['draft', 'assess'])
def test_generic_paid_insert_denied_without_side_effects(setup, kind):
    store, owner, jobs, _ = setup
    with pytest.raises(EvidenceError) as caught:
        jobs.enqueue(owner, kind, {}, 'unapproved', str(uuid4()))
    assert (caught.value.code, caught.value.http_status) == ('ADMISSION_REQUIRED', 409)
    with store._read() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone() == (0,)
        assert db.execute('SELECT count(*) FROM job_keys').fetchone() == (0,)


def test_guard_is_low_level_and_error_kind_explicit():
    from copilot.drafting.admission_guard import AdmissionGuardError
    exc = AdmissionGuardError('history')
    assert (exc.code, exc.http_status, exc.kind) == ('ADMISSION_UNAVAILABLE', 503, 'history')


def legacy_jobs(setup, tmp_path, *, parameters='not json'):
    from admission_legacy_helpers import preupgrade_copy
    from test_jobs import app

    from copilot.jobs import Jobs
    from copilot.store import Store
    store, owner, jobs, clock = setup
    application = app(setup)
    original = jobs.enqueue(owner, 'research', {}, 'legacy', application)
    path = tmp_path / 'legacy'
    with preupgrade_copy(store, path) as db:
        paid = original.model_copy(update={'kind': 'draft'})
        db.execute('UPDATE jobs SET data=?,parameters=? WHERE id=?', (paid.model_dump_json(), parameters, paid.id))
        db.execute("UPDATE job_keys SET kind='draft' WHERE job_id=?", (paid.id,))
    upgraded = Store(path)
    return upgraded, owner, Jobs(upgraded, clock), clock, paid


def test_legacy_invalid_parameters_skip_unchanged_get_and_cancel(setup, tmp_path):
    store, owner, jobs, _, paid = legacy_jobs(setup, tmp_path)
    from copilot.drafting.admission_guard import inspect_retained_paid_job
    with store._read() as db:
        before = db.execute('SELECT * FROM jobs WHERE id=?', (paid.id,)).fetchone()
        retained = inspect_retained_paid_job(db, owner, paid.id)
    assert retained.origin == 'legacy' and retained.parameters_json is None
    index = jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'unrelated')
    assert jobs.claim(str(uuid4())).id == index.id
    with store._read() as db:
        assert db.execute('SELECT * FROM jobs WHERE id=?', (paid.id,)).fetchone() == before
    assert jobs.get(owner, paid.id) == paid
    assert jobs.cancel(owner, paid.id).state == 'cancelled'


def test_legacy_cancel_strict_prepared_attempt_becomes_unknown(setup, tmp_path):
    from copilot.domain import contracts as c
    store, owner, jobs, clock, paid = legacy_jobs(setup, tmp_path)
    attempt = c.ProviderAttempt(id=str(uuid4()), profile_id=owner, application_id=paid.application_id,
        job_id=paid.id, fence=1, reserved_tokens=100, provider='openai', model_version='synthetic',
        consent_revision=0, state='prepared', prepared_at=clock())
    with store._tx() as db:
        db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
            (attempt.id, owner, paid.id, 'prepared', attempt.model_dump_json(), 0,
             clock().date().isoformat(), None, 'draft', 'a' * 64))
    assert jobs.cancel(owner, paid.id).state == 'indeterminate'
    with store._read() as db:
        assert db.execute('SELECT state FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone() == ('indeterminate',)


def test_cancel_validates_all_attempts_before_any_mutation(setup, tmp_path):
    from copilot.domain import contracts as c
    store, owner, jobs, clock, paid = legacy_jobs(setup, tmp_path)
    attempt = c.ProviderAttempt(id=str(uuid4()), profile_id=owner, application_id=paid.application_id,
        job_id=paid.id, fence=1, reserved_tokens=100, provider='openai', model_version='synthetic',
        consent_revision=0, state='prepared', prepared_at=clock())
    with store._tx() as db:
        db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
            (attempt.id, str(uuid4()), paid.id, 'prepared', attempt.model_dump_json(), 0,
             clock().date().isoformat(), None, 'draft', 'a' * 64))
    with pytest.raises(EvidenceError):
        jobs.cancel(owner, paid.id)
    assert jobs.get(owner, paid.id).state == 'queued'
    with store._read() as db:
        assert db.execute('SELECT state FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone() == ('prepared',)


def test_missing_binding_background_skip_is_unchanged_explicit_fails(setup, tmp_path):
    store, owner, jobs, _, paid = legacy_jobs(setup, tmp_path)
    with store._tx() as db:
        db.execute('DELETE FROM drafting_admission_bindings WHERE job_id=?', (paid.id,))
    index = jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'unrelated')
    assert jobs.claim(str(uuid4())).id == index.id
    with pytest.raises(EvidenceError) as caught:
        jobs.claim(str(uuid4()), job_id=paid.id)
    assert caught.value.code == 'ADMISSION_UNAVAILABLE'
    assert jobs.get(owner, paid.id) == paid


def test_fresh_internal_paid_retry_denied_before_insert(setup):
    from test_jobs import app

    from copilot.domain import contracts as c
    from copilot.retrycontracts import WARNING_VERSION, WarnedRetry
    store, owner, jobs, _ = setup
    application = app(setup)
    prior = jobs.enqueue(owner, 'research', {}, 'prior', application).model_copy(update={'kind': 'draft'})
    body = WarnedRetry(prior_attempt_id=str(uuid4()), idempotency_key='retry',
        acknowledge_duplicate_charge=True, warning_version=WARNING_VERSION)
    with store._tx() as db:
        with pytest.raises(EvidenceError) as caught:
            jobs._create_warned_retry(db, prior, {}, (), body, c.canonical_hash(body))
        assert caught.value.code == 'ADMISSION_REQUIRED'
        assert db.execute('SELECT count(*) FROM jobs').fetchone() == (1,)


def test_corrupt_canonical_legacy_job_fails_get_cancel(setup, tmp_path):
    store, owner, jobs, _, paid = legacy_jobs(setup, tmp_path)
    with store._tx() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (paid.id,))
    for action in (jobs.get, jobs.cancel):
        with pytest.raises(EvidenceError):
            action(owner, paid.id)


@pytest.fixture
def admitted(drafting):  # noqa: F811
    from test_drafting_service import enqueue
    service, scope, *_ = drafting
    job, _ = enqueue(drafting)
    return service, scope, job


@pytest.mark.parametrize('tamper', ['key', 'parameters', 'dependencies', 'head', 'head_rollback', 'binding', 'canonical'])
def test_admitted_tamper_denies_before_lease_and_continues_other_jobs(admitted, tamper):
    service, scope, job = admitted
    store, jobs = service.store, service.jobs
    with store._tx() as db:
        if tamper == 'key':
            db.execute("UPDATE job_keys SET request_sha256=? WHERE job_id=?", ('a' * 64, job.id))
        elif tamper == 'parameters':
            db.execute("UPDATE jobs SET parameters='{}' WHERE id=?", (job.id,))
        elif tamper == 'dependencies':
            db.execute("UPDATE jobs SET dependencies='[]' WHERE id=?", (job.id,))
        elif tamper == 'head':
            db.execute('UPDATE drafting_admission_heads SET revision=revision+1 WHERE application_id=?', (scope.application_id,))
        elif tamper == 'head_rollback':
            db.execute('UPDATE drafting_admission_heads SET revision=0 WHERE application_id=?', (scope.application_id,))
        elif tamper == 'binding':
            db.execute('DELETE FROM drafting_admission_bindings WHERE job_id=?', (job.id,))
        else:
            db.execute("UPDATE jobs SET data='{}' WHERE id=?", (job.id,))
    with store._read() as db:
        before = db.execute('SELECT * FROM jobs WHERE id=?', (job.id,)).fetchone()
    with pytest.raises(EvidenceError) as caught:
        jobs.claim(str(uuid4()), job_id=job.id)
    assert caught.value.code == 'ADMISSION_UNAVAILABLE'
    unrelated = jobs.enqueue(scope.profile_id, 'index', {'corpus': 'facts'}, 'other')
    assert jobs.claim(str(uuid4())).id == unrelated.id
    with store._read() as db:
        assert db.execute('SELECT * FROM jobs WHERE id=?', (job.id,)).fetchone() == before


def test_historical_association_survives_head_advance_but_dispatch_does_not(admitted):
    from copilot.drafting.admission_guard import (
        inspect_retained_paid_job,
        require_dispatch_admission,
    )
    service, scope, job = admitted
    with service.store._tx() as db:
        retained = inspect_retained_paid_job(db, scope.profile_id, job.id)
        assert retained.origin == 'admitted'
        db.execute('UPDATE drafting_admission_heads SET revision=revision+1 WHERE application_id=?', (scope.application_id,))
        assert inspect_retained_paid_job(db, scope.profile_id, job.id) == retained
        with pytest.raises(EvidenceError):
            require_dispatch_admission(db, scope.profile_id, job.id)


@pytest.mark.parametrize('cached', [False, True])
def test_head_revocation_blocks_key_send_cached_return_and_publication(admitted, cached):
    from test_provider import good, send
    service, scope, queued = admitted
    jobs, provider = service.jobs, service.provider
    job = jobs.claim(str(uuid4()), job_id=queued.id)
    provider.transport = good
    provider._key_supplier = lambda: 'synthetic-private-key'
    if cached:
        assert send(provider, job).answer
    with service.store._tx() as db:
        db.execute('UPDATE drafting_admission_heads SET revision=revision+1 WHERE application_id=?', (scope.application_id,))
    provider._key_supplier = lambda: pytest.fail('Revoked admission read provider key')
    provider.transport = lambda *_: pytest.fail('Revoked admission sent')
    for action in (lambda: send(provider, job),
                   lambda: jobs.commit_stage(job.id, job.lease_owner, job.fence, 'published', {},
                       lambda *_: pytest.fail('Revoked admission published'))):
        with pytest.raises(EvidenceError) as caught:
            action()
        assert caught.value.code == 'ADMISSION_UNAVAILABLE'


def test_head_metadata_and_canonical_history_have_distinct_errors(admitted):
    from copilot.drafting.admission_guard import (
        AdmissionGuardError,
        inspect_admission_head,
        inspect_retained_paid_job,
    )
    service, scope, job = admitted
    with service.store._tx() as db:
        db.execute("UPDATE jobs SET data='{}' WHERE id=?", (job.id,))
        assert inspect_admission_head(db, scope.profile_id, scope.application_id).last_job_id == job.id
        with pytest.raises(AdmissionGuardError) as caught:
            inspect_retained_paid_job(db, scope.profile_id, job.id)
        assert caught.value.kind == 'history'
        db.execute('DELETE FROM drafting_admission_bindings WHERE job_id=?', (job.id,))
        with pytest.raises(AdmissionGuardError) as caught:
            inspect_admission_head(db, scope.profile_id, scope.application_id)
        assert caught.value.kind == 'metadata'


@pytest.mark.parametrize('marker', [True, False])
def test_full_scrub_needs_exact_marker_and_never_dispatches(admitted, marker):
    from copilot.drafting.admission_guard import (
        AdmissionGuardError,
        inspect_retained_paid_job,
        require_dispatch_admission,
    )
    service, scope, job = admitted
    with service.store._tx() as db:
        scrubbed = job.model_copy(update={'idempotency_key': 'forgotten'})
        db.execute("UPDATE jobs SET data=?,parameters='{}' WHERE id=?", (scrubbed.model_dump_json(), job.id))
        db.execute('DELETE FROM job_keys WHERE job_id=?', (job.id,))
        if marker:
            db.execute('INSERT INTO tombstones VALUES(?,?,?)', (job.id, scope.profile_id, 'drafting_job_forgotten'))
            retained = inspect_retained_paid_job(db, scope.profile_id, job.id)
            assert retained.forgotten and retained.parameters_json is None
            assert retained.captured_revision == 0 and retained.consumed_revision == 1
            with pytest.raises(AdmissionGuardError):
                require_dispatch_admission(db, scope.profile_id, job.id)
        else:
            with pytest.raises(AdmissionGuardError) as caught:
                inspect_retained_paid_job(db, scope.profile_id, job.id)
            assert caught.value.kind == 'history'


def test_consumed_max_remains_dispatchable_until_content_privacy_guard(admitted):
    from copilot.admission_state import MAX_REVISION
    from copilot.drafting.admission_contracts import DraftLaunch, draft_launch_hash
    from copilot.drafting.admission_guard import require_dispatch_admission
    from copilot.drafting.runtime_contracts import DraftParameters
    service, scope, job = admitted
    with service.store._tx() as db:
        raw = db.execute('SELECT parameters FROM jobs WHERE id=?', (job.id,)).fetchone()[0]
        parameters = DraftParameters.model_validate_json(raw)
        envelope = draft_launch_hash(scope.profile_id, scope.application_id,
            DraftLaunch(admission_revision=MAX_REVISION - 1, request=parameters.request))
        db.execute('UPDATE drafting_admission_bindings SET captured_revision=?,consumed_revision=?,envelope_sha256=? WHERE job_id=?',
            (MAX_REVISION - 1, MAX_REVISION, envelope, job.id))
        db.execute('UPDATE job_keys SET request_sha256=? WHERE job_id=?', (envelope, job.id))
        db.execute('UPDATE drafting_admission_heads SET revision=? WHERE application_id=?', (MAX_REVISION, scope.application_id))
        assert require_dispatch_admission(db, scope.profile_id, job.id).consumed_revision == MAX_REVISION


@pytest.mark.parametrize('when', ['before_send', 'before_completion'])
def test_admission_revoked_during_provider_window_retains_honest_unknown(admitted, when):
    from test_provider import send
    service, scope, queued = admitted
    jobs, provider = service.jobs, service.provider
    job = jobs.claim(str(uuid4()), job_id=queued.id)
    key_reads = []
    provider._key_supplier = lambda: key_reads.append(True) or 'synthetic-private-key'

    def revoke():
        with service.store._tx() as db:
            db.execute('UPDATE drafting_admission_heads SET revision=revision+1 WHERE application_id=?', (scope.application_id,))

    def transport(payload, before_send):
        if when == 'before_completion':
            assert before_send() == 'synthetic-private-key'
        revoke()
        reads = len(key_reads)
        if when == 'before_send':
            with pytest.raises(EvidenceError):
                before_send()
            assert len(key_reads) == reads
            raise RuntimeError('Synthetic pre-send refusal; no transmission certainty assumed')
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': '{"answer":"synthetic"}'}]}], 'usage': {'total_tokens': 1}}

    provider.transport = transport
    with pytest.raises(EvidenceError) as caught:
        send(provider, job)
    assert caught.value.code == 'PROVIDER_UNKNOWN'
    assert jobs.get(scope.profile_id, job.id).state == 'indeterminate'
    with service.store._read() as db:
        assert db.execute('SELECT state,response FROM provider_attempts WHERE job_id=?', (job.id,)).fetchone() == ('indeterminate', None)


def test_guard_import_has_no_runtime_or_io_dependency():
    import subprocess
    import sys
    script = """
import socket, sqlite3
socket.socket = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
sqlite3.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('database'))
import copilot.drafting.admission_guard
import sys
assert not any(name in sys.modules for name in (
    'copilot.jobs', 'copilot.provider', 'copilot.drafting.service',
    'copilot.retrieval.service', 'chromadb', 'sentence_transformers'))
"""
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)


def test_dangling_metadata_without_job_tables_is_narrow_metadata_error(admitted):
    from copilot.drafting.admission_guard import (
        AdmissionGuardError,
        inspect_admission_head,
        inspect_retained_paid_job,
    )
    service, scope, job = admitted
    with service.store._tx() as db:
        for table in ('jobs', 'job_keys', 'job_stages', 'provider_attempts'):
            db.execute(f'DROP TABLE {table}')
        for inspect, identity in ((inspect_admission_head, scope.application_id), (inspect_retained_paid_job, job.id)):
            with pytest.raises(AdmissionGuardError) as caught:
                inspect(db, scope.profile_id, identity)
            assert caught.value.kind == 'metadata'


@pytest.mark.parametrize('tamper', ['sql_owner', 'serialized_owner', 'serialized_application'])
def test_malformed_stored_scope_skips_unchanged_background_and_explicit_503(
        setup, tmp_path, tamper, caplog):
    import json
    store, owner, jobs, _, paid = legacy_jobs(setup, tmp_path)
    private_invalid = 'not-a-uuid-private-corruption'
    with store._tx() as db:
        if tamper == 'sql_owner':
            db.execute('UPDATE jobs SET owner=? WHERE id=?', (private_invalid, paid.id))
        else:
            value = json.loads(db.execute('SELECT data FROM jobs WHERE id=?', (paid.id,)).fetchone()[0])
            value['profile_id' if tamper == 'serialized_owner' else 'application_id'] = private_invalid
            db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(value), paid.id))
    with store._read() as db:
        before = db.execute('SELECT * FROM jobs WHERE id=?', (paid.id,)).fetchone()
    unrelated = jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'malformed-scope-unrelated')
    assert jobs.claim(str(uuid4())).id == unrelated.id
    with pytest.raises(EvidenceError) as caught:
        jobs.claim(str(uuid4()), job_id=paid.id)
    assert (caught.value.code, caught.value.http_status) == ('ADMISSION_UNAVAILABLE', 503)
    with store._read() as db:
        assert db.execute('SELECT * FROM jobs WHERE id=?', (paid.id,)).fetchone() == before
    assert private_invalid not in caplog.text
    assert paid.id in caplog.text


def test_malformed_stored_job_id_not_logged_or_allowed_to_starve_queue(setup, tmp_path, caplog):
    store, owner, jobs, _, paid = legacy_jobs(setup, tmp_path)
    private_invalid = 'not-a-uuid-private-job-id'
    with store._tx() as db:
        db.execute('UPDATE jobs SET id=? WHERE id=?', (private_invalid, paid.id))
        db.execute('UPDATE drafting_admission_bindings SET job_id=? WHERE job_id=?', (private_invalid, paid.id))
    with store._read() as db:
        before = db.execute('SELECT * FROM jobs WHERE id=?', (private_invalid,)).fetchone()
    unrelated = jobs.enqueue(owner, 'index', {'corpus': 'facts'}, 'bad-id-unrelated')
    assert jobs.claim(str(uuid4())).id == unrelated.id
    with store._read() as db:
        assert db.execute('SELECT * FROM jobs WHERE id=?', (private_invalid,)).fetchone() == before
    assert 'ADMISSION_UNAVAILABLE' in caplog.text and private_invalid not in caplog.text
    with pytest.raises(EvidenceError) as caught:
        jobs.claim(str(uuid4()), job_id=private_invalid)
    # Public Jobs identifier rejection remains distinct from stored corruption.
    assert (caught.value.code, caught.value.http_status) == ('NOT_FOUND', 404)
