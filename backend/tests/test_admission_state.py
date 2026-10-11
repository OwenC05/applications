"""Synthetic SQL-only coverage for dormant admission storage (not paid eligibility)."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from copilot import admission_state as a
from copilot.contracts import EvidenceError
from copilot.domain import contracts as c

OWNER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
APP = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
SECOND = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
JOB = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'


def application(owner=OWNER, app=APP):
    return c.ApplicationRecord(profile_id=owner, application_id=app, company='Synthetic',
        role='Engineer', sector='tech', vacancy_url='https://example.test/job',
        official_domains=('example.test',), questions=(), input_revision=0,
        created_at=datetime(2026, 10, 11, tzinfo=timezone.utc))


def add_app(db, owner=OWNER, app=APP):
    value = application(owner, app)
    db.execute('INSERT INTO records VALUES(?,?,?,?)',
               (app, owner, 'workspace:application', value.model_dump_json()))
    return value


def job_tables(db, annotated=True):
    # Actual current Jobs layouts, without constructing services or reading private data.
    for sql in (
        'CREATE TABLE jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, parameters TEXT NOT NULL, dependencies TEXT NOT NULL, UNIQUE(owner,id))',
        'CREATE TABLE job_keys(owner TEXT NOT NULL, key TEXT NOT NULL, job_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, PRIMARY KEY(owner,key))',
        'CREATE TABLE job_stages(job_id TEXT NOT NULL, stage TEXT NOT NULL, fence INTEGER NOT NULL, data TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(job_id,stage))',
        'CREATE TABLE provider_attempts(id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, dispatched INTEGER NOT NULL DEFAULT 0, day TEXT NOT NULL, response TEXT, stage TEXT NOT NULL, request_sha256 TEXT NOT NULL, UNIQUE(job_id,stage))',
    ):
        db.execute(sql)
    if annotated:
        db.execute('ALTER TABLE job_keys ADD COLUMN application_id TEXT')
        db.execute('ALTER TABLE job_keys ADD COLUMN kind TEXT')


def add_job(db, *, forgotten=False, kind='draft', owner=OWNER, app=APP):
    value = c.DurableJob(id=JOB, profile_id=owner, application_id=app, kind=kind,
        stage='queued', idempotency_key='forgotten' if forgotten else 'synthetic-key',
        state='cancelled' if forgotten else 'queued', revisions=c.RevisionVector(), fence=0)
    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
        (JOB, owner, value.state, value.model_dump_json(), '{}' if forgotten else '{"text":"fixture"}', '[]'))
    if not forgotten:
        columns = {r[1] for r in db.execute('PRAGMA table_info(job_keys)')}
        if 'kind' in columns:
            db.execute('INSERT INTO job_keys VALUES(?,?,?,?,?,?)',
                       (owner, value.idempotency_key, JOB, 'a' * 64, app, kind))
        else:
            db.execute('INSERT INTO job_keys VALUES(?,?,?,?)',
                       (owner, value.idempotency_key, JOB, 'a' * 64))
    return value


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    connection.execute('CREATE TABLE records(id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)')
    connection.execute('CREATE TABLE tombstones(id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL)')
    for owner in (OWNER, OTHER):
        connection.execute('INSERT INTO profiles VALUES(?,?)', (owner, json.dumps(
            {'id': owner, 'name': 'Synthetic', 'sectors': ['tech'], 'revision': 0})))
    connection.commit()
    connection.execute('BEGIN IMMEDIATE')
    yield connection
    connection.close()


def unavailable(call):
    with pytest.raises(EvidenceError) as raised:
        call()
    assert (raised.value.code, raised.value.http_status) == ('ADMISSION_UNAVAILABLE', 503)


def test_transaction_required_and_bootstrap_does_not_commit(db):
    db.rollback()
    unavailable(lambda: a.bootstrap_in_transaction(db))
    db.execute('BEGIN IMMEDIATE')
    add_app(db)
    a.bootstrap_in_transaction(db)
    assert db.in_transaction
    assert a.read_head(db, OWNER, APP).revision == 1
    db.rollback()
    assert db.execute('PRAGMA user_version').fetchone() == (0,)
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='drafting_admission_heads'").fetchone()


def test_first_insert_owned_by_helper_and_read_select_only(db):
    a.bootstrap_in_transaction(db)
    a.register_new_application(db, application())
    statements = []
    db.set_trace_callback(statements.append)
    assert a.read_head(db, OWNER, APP) == a.AdmissionHead(OWNER, APP, 0, None)
    db.set_trace_callback(None)
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA')) for s in statements)
    with pytest.raises(EvidenceError) as error:
        a.register_new_application(db, application())
    assert error.value.code == 'CONFLICT'
    db.rollback()
    assert not db.execute('SELECT 1 FROM records').fetchone()


def test_legacy_classification_preserves_all_existing_bytes_and_restart(db):
    add_app(db)
    job_tables(db)
    add_job(db)
    db.execute("INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
               (SECOND, OWNER, SECOND, 'prepared', '{malformed', 0, '2026-10-11', 'retained', 'send', 'b' * 64))
    before = {table: db.execute(f'SELECT * FROM {table}').fetchall()
              for table in ('jobs', 'job_keys', 'job_stages', 'provider_attempts')}
    a.bootstrap_in_transaction(db)
    assert db.execute('SELECT origin,captured_revision,request_sha256 FROM drafting_admission_bindings').fetchall() == [('legacy', None, None)]
    a.bootstrap_in_transaction(db)
    assert before == {table: db.execute(f'SELECT * FROM {table}').fetchall() for table in before}
    assert a.read_head(db, OWNER, APP).last_job_id is None


@pytest.mark.parametrize('annotated', [False, True])
def test_supported_key_layouts(db, annotated):
    add_app(db)
    job_tables(db, annotated)
    add_job(db)
    a.bootstrap_in_transaction(db)
    assert db.execute('SELECT count(*) FROM drafting_admission_bindings').fetchone() == (1,)


def test_partial_group_and_unknown_marker_fail_without_repair(db):
    db.execute('CREATE TABLE jobs(id TEXT)')
    unavailable(lambda: a.bootstrap_in_transaction(db))
    assert db.execute('PRAGMA user_version').fetchone() == (0,)
    db.execute('DROP TABLE jobs')
    db.execute('PRAGMA user_version=2')
    unavailable(lambda: a.bootstrap_in_transaction(db))


@pytest.mark.parametrize('corruption', ['owner', 'state', 'id', 'key', 'annotation'])
def test_legacy_contradictory_links_fail_closed(db, corruption):
    add_app(db)
    job_tables(db)
    add_job(db)
    if corruption in ('owner', 'state', 'id'):
        column, value = {'owner': ('owner', OTHER), 'state': ('state', 'completed'), 'id': ('id', SECOND)}[corruption]
        db.execute(f'UPDATE jobs SET {column}=?', (value,))
    elif corruption == 'key':
        db.execute("UPDATE job_keys SET key='different'")
    else:
        db.execute('UPDATE job_keys SET application_id=?', (SECOND,))
    unavailable(lambda: a.bootstrap_in_transaction(db))


@pytest.mark.parametrize('deleted', ['live', 'application', 'profile'])
def test_complete_forgotten_signature_and_deleted_tombstones(db, deleted):
    add_app(db)
    job_tables(db)
    add_job(db, forgotten=True)
    if deleted != 'live':
        db.execute('DELETE FROM records WHERE id=?', (APP,))
        key, kind = (APP, 'workspace:application') if deleted == 'application' else (OWNER, 'profile')
        db.execute('INSERT INTO tombstones VALUES(?,?,?)', (key, OWNER, kind))
        if deleted == 'profile':
            db.execute('DELETE FROM profiles WHERE id=?', (OWNER,))
    a.bootstrap_in_transaction(db)
    assert db.execute('SELECT kind FROM tombstones WHERE id=?', (JOB,)).fetchone() == ('drafting_job_forgotten',)
    a.bootstrap_in_transaction(db)
    if deleted != 'live':
        with pytest.raises(EvidenceError) as error:
            a.read_head(db, OWNER, APP)
        assert error.value.code == 'NOT_FOUND'


@pytest.mark.parametrize('partial', ['parameters', 'stage', 'response', 'foreign_marker', 'no_scope_tombstone'])
def test_partial_forgotten_signature_rejected(db, partial):
    add_app(db)
    job_tables(db)
    add_job(db, forgotten=True)
    if partial == 'parameters':
        db.execute("UPDATE jobs SET parameters='{\"retained\":true}'")
    elif partial == 'stage':
        db.execute('INSERT INTO job_stages VALUES(?,?,?,?,?)', (JOB, 'done', 0, '{}', 'a' * 64))
    elif partial == 'response':
        db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (SECOND, OWNER, JOB, 'completed', '{}', 1, '2026-10-11', '', 'send', 'a' * 64))
    elif partial == 'foreign_marker':
        db.execute('INSERT INTO tombstones VALUES(?,?,?)', (JOB, OTHER, 'drafting_job_forgotten'))
    else:
        db.execute('DELETE FROM records WHERE id=?', (APP,))
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_restart_never_grandfathers_new_unbound_paid_job(db):
    add_app(db)
    a.bootstrap_in_transaction(db)
    job_tables(db)
    a.bootstrap_in_transaction(db)  # Newly created, empty canonical group is legitimate.
    add_job(db)
    unavailable(lambda: a.bootstrap_in_transaction(db))
    assert not db.execute('SELECT 1 FROM drafting_admission_bindings').fetchone()


def test_missing_head_or_altered_schema_not_repaired(db):
    add_app(db)
    a.bootstrap_in_transaction(db)
    db.execute('DELETE FROM drafting_admission_heads')
    unavailable(lambda: a.read_head(db, OWNER, APP))
    unavailable(lambda: a.bootstrap_in_transaction(db))
    assert not db.execute('SELECT 1 FROM drafting_admission_heads').fetchone()
    db.execute('DROP TABLE drafting_admission_heads')
    db.execute('CREATE TABLE drafting_admission_heads(owner TEXT,application_id TEXT,revision INTEGER,last_job_id TEXT)')
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_monotonic_owner_isolation_max_and_deleted_retention(db):
    add_app(db)
    add_app(db, OWNER, SECOND)
    add_app(db, OTHER, JOB)
    a.bootstrap_in_transaction(db)
    db.execute('UPDATE drafting_admission_heads SET revision=? WHERE application_id=?', (a.MAX_REVISION, APP))
    a.invalidate_scope(db, OWNER)
    assert a.read_head(db, OWNER, APP).revision == a.MAX_REVISION
    assert a.read_head(db, OWNER, SECOND).revision == 2
    assert a.read_head(db, OTHER, JOB).revision == 1
    db.execute('INSERT INTO tombstones VALUES(?,?,?)', (SECOND, OWNER, 'workspace:application'))
    db.execute('DELETE FROM records WHERE id=?', (SECOND,))
    a.invalidate_scope(db, OWNER, SECOND)
    assert db.execute('SELECT revision FROM drafting_admission_heads WHERE application_id=?', (SECOND,)).fetchone() == (3,)
    a.bootstrap_in_transaction(db)


@pytest.mark.parametrize('identity', ['record', 'head', 'app_tombstone', 'profile_tombstone'])
def test_register_rejects_reused_identity(db, identity):
    a.bootstrap_in_transaction(db)
    if identity == 'record':
        db.execute('INSERT INTO records VALUES(?,?,?,?)', (APP, OTHER, 'different', '{}'))
    elif identity == 'head':
        db.execute('INSERT INTO drafting_admission_heads VALUES(?,?,?,?)', (OWNER, APP, 0, None))
    else:
        db.execute('INSERT INTO tombstones VALUES(?,?,?)',
                   (APP if identity == 'app_tombstone' else OWNER, OWNER,
                    'workspace:application' if identity == 'app_tombstone' else 'profile'))
    with pytest.raises(EvidenceError) as error:
        a.register_new_application(db, application())
    assert error.value.code == 'CONFLICT'
    assert not db.execute("SELECT 1 FROM records WHERE id=? AND kind='workspace:application'", (APP,)).fetchone()


def test_invalid_public_inputs_and_tampered_application(db):
    a.bootstrap_in_transaction(db)
    for bad in ('wrong', None, 1, OWNER.upper()):
        with pytest.raises(EvidenceError) as error:
            a.read_head(db, bad, APP)
        assert (error.value.code, error.value.http_status) == ('INVALID_INPUT', 422)
    with pytest.raises(EvidenceError) as error:
        a.register_new_application(db, application().model_copy(update={'input_revision': True}))
    assert error.value.code == 'INVALID_INPUT'
    with pytest.raises(EvidenceError) as error:
        a.read_head(db, OTHER, APP)
    assert error.value.code == 'NOT_FOUND'


def test_sql_constraints_and_loaded_validation_bypass(db):
    add_app(db)
    a.bootstrap_in_transaction(db)
    for value in (-1, a.MAX_REVISION + 1, 1.5, 'text'):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE drafting_admission_heads SET revision=?', (value,))
    db.execute('PRAGMA ignore_check_constraints=ON')
    db.execute('UPDATE drafting_admission_heads SET revision=-1')
    unavailable(lambda: a.read_head(db, OWNER, APP))


def test_bootstrap_failure_caller_rollback_restores_ddl_marker_and_classification(db):
    add_app(db)
    job_tables(db)
    add_job(db, forgotten=True)
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    def authorizer(action, first, second, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and first == 'user_version' and second == '1' else sqlite3.SQLITE_OK
    db.set_authorizer(authorizer)
    unavailable(lambda: a.bootstrap_in_transaction(db))
    assert db.in_transaction
    db.set_authorizer(None)
    db.rollback()
    assert db.execute('PRAGMA user_version').fetchone() == (0,)
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'drafting_admission_%'").fetchone()
    assert not db.execute('SELECT 1 FROM tombstones WHERE id=?', (JOB,)).fetchone()


def admitted_binding(db):
    db.execute('DELETE FROM drafting_admission_bindings')
    db.execute('INSERT INTO drafting_admission_bindings VALUES(?,?,?,?,?,?,?,?,?)',
               (JOB, OWNER, APP, 'draft', 'admitted', 1, 2, 'a' * 64, 'b' * 64))
    db.execute('UPDATE drafting_admission_heads SET revision=3,last_job_id=? WHERE application_id=?', (JOB, APP))


def test_admitted_metadata_restart_and_privacy_gaps_are_not_authority(db):
    add_app(db)
    job_tables(db)
    add_job(db)
    a.bootstrap_in_transaction(db)
    admitted_binding(db)
    a.bootstrap_in_transaction(db)
    assert a.read_head(db, OWNER, APP) == a.AdmissionHead(OWNER, APP, 3, JOB)
    a.invalidate_scope(db, OWNER, APP)
    assert a.read_head(db, OWNER, APP).revision == 4
    a.bootstrap_in_transaction(db)


@pytest.mark.parametrize('change', ['wrong_owner', 'missing_binding', 'future_consumption', 'legacy_head', 'wrong_kind'])
def test_head_binding_retained_job_association_independently_validated(db, change):
    add_app(db)
    job_tables(db)
    add_job(db)
    a.bootstrap_in_transaction(db)
    admitted_binding(db)
    db.execute('PRAGMA ignore_check_constraints=ON')
    if change == 'wrong_owner':
        db.execute('UPDATE drafting_admission_bindings SET owner=?', (OTHER,))
    elif change == 'missing_binding':
        db.execute('DELETE FROM drafting_admission_bindings')
    elif change == 'future_consumption':
        db.execute('UPDATE drafting_admission_heads SET revision=1')
    elif change == 'legacy_head':
        db.execute("UPDATE drafting_admission_bindings SET origin='legacy',captured_revision=NULL,consumed_revision=NULL,envelope_sha256=NULL")
    else:
        db.execute("UPDATE drafting_admission_bindings SET kind='assess'")
    unavailable(lambda: a.read_head(db, OWNER, APP))
    unavailable(lambda: a.bootstrap_in_transaction(db))


@pytest.mark.parametrize('values', [
    ('admitted', None, None, None, None),
    ('admitted', 1, 3, 'a' * 64, 'b' * 64),
    ('admitted', a.MAX_REVISION, a.MAX_REVISION + 1, 'a' * 64, 'b' * 64),
    ('admitted', 1, 2, 'A' * 64, 'b' * 64),
    ('legacy', 0, 1, None, None),
    ('legacy', None, None, None, 'b' * 64),
])
def test_binding_sql_shape_constraints(db, values):
    a.bootstrap_in_transaction(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO drafting_admission_bindings VALUES(?,?,?,?,?,?,?,?,?)',
                   (JOB, OWNER, APP, 'draft', *values))


def test_bypassed_binding_constraints_still_rejected_when_loaded(db):
    add_app(db)
    job_tables(db)
    add_job(db)
    a.bootstrap_in_transaction(db)
    admitted_binding(db)
    db.execute('PRAGMA ignore_check_constraints=ON')
    db.execute("UPDATE drafting_admission_bindings SET request_sha256='not-a-digest'")
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_registration_insert_failure_is_caller_atomic(db):
    a.bootstrap_in_transaction(db)
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    def deny_head(action, first, second, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and first == 'drafting_admission_heads' else sqlite3.SQLITE_OK
    db.set_authorizer(deny_head)
    unavailable(lambda: a.register_new_application(db, application()))
    assert db.in_transaction
    db.set_authorizer(None)
    db.rollback()
    assert not db.execute('SELECT 1 FROM records WHERE id=?', (APP,)).fetchone()
    assert not db.execute('SELECT 1 FROM drafting_admission_heads').fetchone()


def test_invalidation_validates_all_affected_before_writing_and_ignores_other_owner_corruption(db):
    add_app(db)
    add_app(db, OWNER, SECOND)
    add_app(db, OTHER, JOB)
    a.bootstrap_in_transaction(db)
    db.execute("UPDATE records SET data='corrupt' WHERE id=?", (JOB,))
    a.invalidate_scope(db, OWNER, APP)
    assert a.read_head(db, OWNER, APP).revision == 2
    db.execute('DELETE FROM drafting_admission_heads WHERE application_id=?', (SECOND,))
    unavailable(lambda: a.invalidate_scope(db, OWNER))
    assert a.read_head(db, OWNER, APP).revision == 2


def test_partial_scope_annotation_and_missing_unique_schema_rejected(db):
    job_tables(db, annotated=False)
    db.execute('ALTER TABLE job_keys ADD COLUMN kind TEXT')
    unavailable(lambda: a.bootstrap_in_transaction(db))
    db.execute('ALTER TABLE job_keys DROP COLUMN kind')
    db.execute('DROP TABLE jobs')
    db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, parameters TEXT NOT NULL, dependencies TEXT NOT NULL)')
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_unexplained_extra_head_and_orphan_binding_rejected(db):
    a.bootstrap_in_transaction(db)
    db.execute('INSERT INTO drafting_admission_heads VALUES(?,?,0,NULL)', (OWNER, APP))
    unavailable(lambda: a.bootstrap_in_transaction(db))
    db.execute('DELETE FROM drafting_admission_heads')
    db.execute('INSERT INTO drafting_admission_bindings VALUES(?,?,?,?,?,NULL,NULL,NULL,NULL)', (JOB, OWNER, APP, 'draft', 'legacy'))
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_nonpaid_job_has_no_binding_and_missing_marker_is_not_repaired(db):
    add_app(db)
    job_tables(db)
    add_job(db, kind='research')
    a.bootstrap_in_transaction(db)
    assert not db.execute('SELECT 1 FROM drafting_admission_bindings').fetchone()
    db.execute('PRAGMA user_version=0')
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_writer_helpers_never_begin_commit_or_rollback(db):
    traces = []
    db.set_trace_callback(traces.append)
    a.bootstrap_in_transaction(db)
    a.register_new_application(db, application())
    a.invalidate_scope(db, OWNER)
    db.set_trace_callback(None)
    assert not any(s.strip().upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE')) for s in traces)
    db.commit()
    unavailable(lambda: a.register_new_application(db, application(app=SECOND)))
    unavailable(lambda: a.invalidate_scope(db, OWNER))


@pytest.mark.parametrize('tamper', ['extra', 'missing', 'nested_extra', 'nested_scalar'])
def test_register_revalidates_retained_typed_attributes_without_serializing_them_away(db, tamper):
    a.bootstrap_in_transaction(db)
    value = application()
    if tamper == 'extra':
        value = value.model_copy(update={'unexpected': 'retained'})
    elif tamper == 'missing':
        del value.__dict__['company']
    else:
        question = c.Question(id='q', text='Why?', type='writing', constraint_origin='user')
        question = question.model_copy(update={'unexpected': 'retained'} if tamper == 'nested_extra' else {'required': 1})
        value = value.model_copy(update={'questions': (question,)})
    with pytest.raises(EvidenceError) as error:
        a.register_new_application(db, value)
    assert (error.value.code, error.value.http_status) == ('INVALID_INPUT', 422)
    assert not db.execute('SELECT 1 FROM records').fetchone()


def test_file_database_restart_is_read_only_and_durable(db, tmp_path):
    add_app(db)
    job_tables(db)
    add_job(db)
    a.bootstrap_in_transaction(db)
    db.commit()
    path = tmp_path / 'synthetic.sqlite3'
    with sqlite3.connect(path) as disk:
        db.backup(disk)
    with sqlite3.connect(path) as reopened:
        assert not reopened.in_transaction
        assert a.read_head(reopened, OWNER, APP).revision == 1
        reopened.execute('BEGIN IMMEDIATE')
        statements = []
        reopened.set_trace_callback(statements.append)
        a.bootstrap_in_transaction(reopened)
        reopened.set_trace_callback(None)
        assert all(s.strip().upper().startswith(('SELECT', 'PRAGMA')) for s in statements)
        assert reopened.in_transaction
        reopened.rollback()


def test_valid_orphan_attempt_and_malformed_attempt_are_preserved_not_eligibility(db):
    add_app(db)
    job_tables(db)
    attempt = c.ProviderAttempt(id=SECOND, profile_id=OWNER, application_id=APP,
        job_id=JOB, fence=1, reserved_tokens=100, provider='synthetic',
        model_version='synthetic', consent_revision=0, state='prepared',
        prepared_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
    db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
               (SECOND, OWNER, JOB, 'prepared', attempt.model_dump_json(), 0, '2026-10-11', None, 'send', 'a' * 64))
    db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
               (APP, OTHER, SECOND, 'contradictory', '{not-json', 0, '2026-10-11', None, 'send', 'b' * 64))
    before = db.execute('SELECT * FROM provider_attempts').fetchall()
    a.bootstrap_in_transaction(db)
    a.bootstrap_in_transaction(db)
    assert db.execute('SELECT * FROM provider_attempts').fetchall() == before
    assert not db.execute('SELECT 1 FROM drafting_admission_bindings').fetchone()


def test_literal_case_in_schema_checks_cannot_disguise_incompatible_constraints(db):
    a.bootstrap_in_transaction(db)
    db.execute('DROP TABLE drafting_admission_bindings')
    db.execute(a._BINDING_SQL.replace("'draft','assess'", "'DRAFT','ASSESS'"))
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_unverified_legacy_digest_is_not_accepted_as_verified_metadata(db):
    add_app(db)
    job_tables(db)
    add_job(db)
    a.bootstrap_in_transaction(db)
    db.execute("UPDATE drafting_admission_bindings SET request_sha256=?", ('a' * 64,))
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_public_helpers_reject_partial_job_schema_even_without_last_job(db):
    add_app(db)
    a.bootstrap_in_transaction(db)
    db.execute('CREATE TABLE jobs(id TEXT)')
    unavailable(lambda: a.read_head(db, OWNER, APP))
    unavailable(lambda: a.invalidate_scope(db, OWNER))
    unavailable(lambda: a.register_new_application(db, application(app=SECOND)))
    assert db.execute('SELECT revision FROM drafting_admission_heads').fetchone() == (1,)
    assert not db.execute('SELECT 1 FROM records WHERE id=?', (SECOND,)).fetchone()


def test_whitespace_in_quoted_constraint_literal_is_not_normalized_away(db):
    a.bootstrap_in_transaction(db)
    db.execute('DROP TABLE drafting_admission_bindings')
    db.execute(a._BINDING_SQL.replace("'draft','assess'", "'d r a f t','assess'"))
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_valid_profile_deletion_cannot_excuse_foreign_application_tombstone(db):
    job_tables(db)
    add_job(db, forgotten=True)
    db.execute('DELETE FROM profiles WHERE id=?', (OWNER,))
    db.execute('INSERT INTO tombstones VALUES(?,?,?)', (OWNER, OWNER, 'profile'))
    db.execute('INSERT INTO tombstones VALUES(?,?,?)', (APP, OTHER, 'workspace:application'))
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_incompatible_job_column_default_is_not_supported_schema(db):
    job_tables(db)
    db.execute('DROP TABLE provider_attempts')
    db.execute('CREATE TABLE provider_attempts(id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, dispatched INTEGER NOT NULL DEFAULT 1, day TEXT NOT NULL, response TEXT, stage TEXT NOT NULL, request_sha256 TEXT NOT NULL, UNIQUE(job_id,stage))')
    unavailable(lambda: a.bootstrap_in_transaction(db))


def test_profile_deletion_marker_cannot_excuse_still_live_canonical_profile(db):
    # Missing application + scrubbed job is not a deleted scope while its profile survives.
    job_tables(db)
    add_job(db, forgotten=True)
    db.execute('INSERT INTO tombstones VALUES(?,?,?)', (OWNER, OWNER, 'profile'))
    unavailable(lambda: a.bootstrap_in_transaction(db))
    assert db.execute('PRAGMA user_version').fetchone() == (0,)
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='drafting_admission_heads'").fetchone()


def test_restart_rejects_profile_resurrection_behind_retained_deletion_marker(db):
    job_tables(db)
    add_job(db, forgotten=True)
    serialized = db.execute('SELECT data FROM profiles WHERE id=?', (OWNER,)).fetchone()[0]
    db.execute('DELETE FROM profiles WHERE id=?', (OWNER,))
    db.execute('INSERT INTO tombstones VALUES(?,?,?)', (OWNER, OWNER, 'profile'))
    a.bootstrap_in_transaction(db)
    db.execute('INSERT INTO profiles VALUES(?,?)', (OWNER, serialized))
    before = db.execute('SELECT * FROM drafting_admission_bindings').fetchall()
    unavailable(lambda: a.bootstrap_in_transaction(db))
    assert db.execute('PRAGMA user_version').fetchone() == (1,)
    assert db.execute('SELECT * FROM drafting_admission_bindings').fetchall() == before
