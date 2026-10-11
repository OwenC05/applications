"""Dormant, caller-transaction SQL storage; these values confer no launch authority.

Writers require the caller's BEGIN IMMEDIATE and rollback on *any* failure. This
module never starts, commits or rolls back transactions and is not runtime-wired.
"""
import json
import re
import sqlite3
from dataclasses import dataclass
from functools import wraps

from pydantic import BaseModel, TypeAdapter

from .contracts import EvidenceError
from .domain import contracts as c

MAX_REVISION = 9_007_199_254_740_991
_IDENTIFIER = TypeAdapter(c.Identifier)
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
HEADS = 'drafting_admission_heads'
BINDINGS = 'drafting_admission_bindings'
_HEAD_SQL = f'''CREATE TABLE {HEADS}(
    owner TEXT NOT NULL, application_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision BETWEEN 0 AND {MAX_REVISION}),
    last_job_id TEXT, PRIMARY KEY(owner,application_id),
    CHECK(typeof(owner)='text' AND typeof(application_id)='text'),
    CHECK(last_job_id IS NULL OR typeof(last_job_id)='text'))'''
_BINDING_SQL = f'''CREATE TABLE {BINDINGS}(
    job_id TEXT NOT NULL PRIMARY KEY, owner TEXT NOT NULL, application_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('draft','assess')),
    origin TEXT NOT NULL CHECK(origin IN ('admitted','legacy')),
    captured_revision INTEGER, consumed_revision INTEGER,
    request_sha256 TEXT, envelope_sha256 TEXT,
    CHECK(typeof(job_id)='text' AND typeof(owner)='text' AND typeof(application_id)='text'),
    CHECK(request_sha256 IS NULL OR (typeof(request_sha256)='text' AND length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(envelope_sha256 IS NULL OR (typeof(envelope_sha256)='text' AND length(envelope_sha256)=64 AND envelope_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK((origin='legacy' AND captured_revision IS NULL AND consumed_revision IS NULL AND envelope_sha256 IS NULL)
       OR (origin='admitted' AND typeof(captured_revision)='integer' AND captured_revision BETWEEN 0 AND {MAX_REVISION - 1}
           AND typeof(consumed_revision)='integer' AND consumed_revision=captured_revision+1
           AND request_sha256 IS NOT NULL AND envelope_sha256 IS NOT NULL)),
    UNIQUE(owner,application_id,captured_revision))'''

# name, declared type, NOT NULL, primary-key position, in actual canonical order.
_LAYOUTS = {
    'profiles': [('id', 'TEXT', 0, 1), ('data', 'TEXT', 1, 0)],
    'records': [('id', 'TEXT', 0, 1), ('owner', 'TEXT', 1, 0), ('kind', 'TEXT', 1, 0), ('data', 'TEXT', 1, 0)],
    'tombstones': [('id', 'TEXT', 0, 1), ('owner', 'TEXT', 1, 0), ('kind', 'TEXT', 1, 0)],
    'jobs': [('id', 'TEXT', 0, 1), ('owner', 'TEXT', 1, 0), ('state', 'TEXT', 1, 0), ('data', 'TEXT', 1, 0), ('parameters', 'TEXT', 1, 0), ('dependencies', 'TEXT', 1, 0)],
    'job_keys': [('owner', 'TEXT', 1, 1), ('key', 'TEXT', 1, 2), ('job_id', 'TEXT', 1, 0), ('request_sha256', 'TEXT', 1, 0)],
    'job_stages': [('job_id', 'TEXT', 1, 1), ('stage', 'TEXT', 1, 2), ('fence', 'INTEGER', 1, 0), ('data', 'TEXT', 1, 0), ('sha256', 'TEXT', 1, 0)],
    'provider_attempts': [('id', 'TEXT', 0, 1), ('owner', 'TEXT', 1, 0), ('job_id', 'TEXT', 1, 0), ('state', 'TEXT', 1, 0), ('data', 'TEXT', 1, 0), ('dispatched', 'INTEGER', 1, 0), ('day', 'TEXT', 1, 0), ('response', 'TEXT', 0, 0), ('stage', 'TEXT', 1, 0), ('request_sha256', 'TEXT', 1, 0)],
}


@dataclass(frozen=True)
class AdmissionHead:
    owner: str
    application_id: str
    revision: int
    last_job_id: str | None


def _fail(code='ADMISSION_UNAVAILABLE', status=503):
    messages = {'ADMISSION_UNAVAILABLE': 'Drafting admission storage is unavailable',
                'INVALID_INPUT': 'Invalid admission storage input',
                'NOT_FOUND': 'Application not found',
                'CONFLICT': 'Application identity cannot be reused'}
    raise EvidenceError(code, messages[code], status)


def _safe_sql(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except sqlite3.Error:
            _fail()
    return guarded


def _application_input(value):
    """Preserve forged missing/extra/scalar values, including nested questions."""
    if isinstance(value, BaseModel):
        values = dict(value.__dict__)
        values.update(value.__pydantic_extra__ or {})
        return {key: _application_input(item) for key, item in values.items()}
    if isinstance(value, dict):
        return {key: _application_input(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_application_input(item) for item in value)
    if isinstance(value, list):
        return [_application_input(item) for item in value]
    return value


def _identifier(value, *, public=False):
    try:
        return _IDENTIFIER.validate_python(value, strict=True)
    except ValueError:
        _fail('INVALID_INPUT', 422) if public else _fail()


def _writer(db):
    if not db.in_transaction:
        _fail()


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _layout(db, table, expected):
    columns = db.execute(f'PRAGMA table_info({table})').fetchall()
    actual = [(r[1], r[2], r[3], r[5]) for r in columns]
    defaults = {r[1]: r[4] for r in columns if r[4] is not None}
    expected_defaults = {'dispatched': '0'} if table == 'provider_attempts' else {}
    if actual != expected or defaults != expected_defaults:
        _fail()


def _unique(db, table, columns):
    for row in db.execute(f'PRAGMA index_list({table})').fetchall():
        if row[2] and not row[4]:
            name = row[1].replace('"', '""')
            if tuple(r[2] for r in db.execute(f'PRAGMA index_info("{name}")')) == columns:
                return
    _fail()


def _base_schema(db):
    for table in ('profiles', 'records', 'tombstones'):
        _layout(db, table, _LAYOUTS[table])


def _job_schema(db):
    present = _tables(db) & {'jobs', 'job_keys', 'job_stages', 'provider_attempts'}
    if not present:
        return False
    if len(present) != 4:
        _fail()
    for table in present:
        expected = _LAYOUTS[table]
        if table == 'job_keys':
            names = {r[1] for r in db.execute('PRAGMA table_info(job_keys)')}
            annotations = names & {'application_id', 'kind'}
            if len(annotations) == 1:
                _fail()
            if annotations:
                expected = expected + [('application_id', 'TEXT', 0, 0), ('kind', 'TEXT', 0, 0)]
        _layout(db, table, expected)
    for table, columns in (('jobs', ('owner', 'id')), ('provider_attempts', ('job_id', 'stage'))):
        _unique(db, table, columns)
    return True


def _admission_schema(db):
    if db.execute('PRAGMA user_version').fetchone() != (1,):
        _fail()
    for table, expected in ((HEADS, _HEAD_SQL), (BINDINGS, _BINDING_SQL)):
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        # These tables are created only by the frozen DDL below. Do not normalize
        # quoted literals: whitespace/case changes can change CHECK semantics.
        if not row or row[0] != expected:
            _fail()
    _job_schema(db)


def _tombstone(db, identity):
    return db.execute('SELECT owner,kind FROM tombstones WHERE id=?', (identity,)).fetchone()


def _live_owner(db, owner):
    row = db.execute('SELECT data FROM profiles WHERE id=?', (owner,)).fetchone()
    if not row:
        return False
    try:
        value = json.loads(row[0])
        if not isinstance(value, dict) or value.get('id') != owner or _tombstone(db, owner):
            _fail()
    except (ValueError, TypeError):
        _fail()
    return True


def _application(db, owner, application_id):
    row = db.execute('SELECT owner,kind,data FROM records WHERE id=?', (application_id,)).fetchone()
    if not row:
        return None
    if row[0] != owner or row[1] != 'workspace:application':
        return None
    try:
        value = c.ApplicationRecord.model_validate_json(row[2])
    except ValueError:
        _fail()
    if (value.profile_id, value.application_id) != (owner, application_id) or _tombstone(db, application_id) or not _live_owner(db, owner):
        _fail()
    return value


def _deleted_scope(db, owner, app):
    application_marker, profile_marker = _tombstone(db, app), _tombstone(db, owner)
    if (application_marker not in (None, (owner, 'workspace:application'))
            or profile_marker not in (None, (owner, 'profile'))):
        _fail()
    # A deletion receipt cannot override a contradictory surviving canonical row.
    if profile_marker is not None and db.execute('SELECT 1 FROM profiles WHERE id=?', (owner,)).fetchone():
        _fail()
    return application_marker is not None or profile_marker is not None


def _scope(db, owner, app):
    _identifier(owner)
    _identifier(app)
    if _application(db, owner, app) is not None:
        return
    # A conflicting retained record is not a deleted scope.
    if db.execute('SELECT 1 FROM records WHERE id=?', (app,)).fetchone() or not _deleted_scope(db, owner, app):
        _fail()


def _jobs(db):
    result = {}
    for jid, owner, state, serialized, parameters, dependencies in db.execute('SELECT * FROM jobs'):
        try:
            job = c.DurableJob.model_validate_json(serialized)
        except ValueError:
            _fail()
        if (job.id, job.profile_id, job.state) != (jid, owner, state):
            _fail()
        result[jid] = job
        if job.kind in ('draft', 'assess'):
            _scope(db, owner, job.application_id)
            marker = _tombstone(db, jid)
            forgotten = _forgotten(db, job, parameters)
            if marker is not None and marker != (owner, 'drafting_job_forgotten'):
                _fail()
            if (job.idempotency_key == 'forgotten' or marker or _deleted_scope(db, owner, job.application_id)) and not forgotten:
                _fail()
    annotated = 'kind' in {r[1] for r in db.execute('PRAGMA table_info(job_keys)')}
    query = 'SELECT owner,key,job_id,request_sha256' + (',application_id,kind' if annotated else '') + ' FROM job_keys'
    for row in db.execute(query):
        owner, key, jid, digest = row[:4]
        job = result.get(jid)
        if not job or (job.profile_id, job.idempotency_key) != (owner, key) or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            _fail()
        if annotated and (row[4] is not None and row[4] != job.application_id or row[5] is not None and row[5] != job.kind):
            _fail()
    return result


def _forgotten(db, job, parameters):
    try:
        empty = json.loads(parameters) == {} and isinstance(json.loads(parameters), dict)
    except (ValueError, TypeError):
        empty = False
    return (job.idempotency_key == 'forgotten' and empty
            and not db.execute('SELECT 1 FROM job_keys WHERE job_id=?', (job.id,)).fetchone()
            and not db.execute('SELECT 1 FROM job_stages WHERE job_id=?', (job.id,)).fetchone()
            and not db.execute('SELECT 1 FROM provider_attempts WHERE job_id=? AND response IS NOT NULL', (job.id,)).fetchone())


def _live_apps(db, owner=None, application_id=None):
    apps = {}
    query = "SELECT id,owner FROM records WHERE kind='workspace:application'"
    args = ()
    if owner is not None:
        query += ' AND owner=?'
        args += (owner,)
    if application_id is not None:
        query += ' AND id=?'
        args += (application_id,)
    for app, owner in db.execute(query, args).fetchall():
        _identifier(app)
        _identifier(owner)
        value = _application(db, owner, app)
        if value is None:
            _fail()
        apps[(owner, app)] = value
    return apps


def _binding(db, row, jobs):
    jid, owner, app, kind, origin, captured, consumed, request, envelope = row
    for value in (jid, owner, app):
        _identifier(value)
    job = jobs.get(jid)
    if not job or (job.profile_id, job.application_id, job.kind) != (owner, app, kind) or kind not in ('draft', 'assess'):
        _fail()
    for digest in (request, envelope):
        if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            _fail()
    if origin == 'legacy':
        # Stage 1 consistently records NULL: a well-formed digest is not proof
        # of a verified historical request hash. No legacy verifier lives here.
        if captured is not None or consumed is not None or envelope is not None or request is not None:
            _fail()
    elif origin == 'admitted':
        if type(captured) is not int or type(consumed) is not int or not 0 <= captured < MAX_REVISION or consumed != captured + 1 or request is None or envelope is None:
            _fail()
    else:
        _fail()
    return row


def _head_jobs(db, rows):
    """Read only specifically referenced retained jobs, not unrelated owner data."""
    ids = {row[3] for row in rows if row[3] is not None}
    if not ids:
        return {}
    if not _job_schema(db):
        _fail()
    jobs = {}
    for jid in ids:
        row = db.execute('SELECT id,owner,state,data FROM jobs WHERE id=?', (jid,)).fetchone()
        try:
            job = c.DurableJob.model_validate_json(row[3]) if row else None
        except ValueError:
            _fail()
        if job is None or (job.id, job.profile_id, job.state) != row[:3]:
            _fail()
        jobs[jid] = job
    return jobs


def _head(db, row, jobs):
    owner, app, revision, last_job = row
    _scope(db, owner, app)
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        _fail()
    if last_job is not None:
        _identifier(last_job)
        binding = db.execute(f'SELECT * FROM {BINDINGS} WHERE job_id=?', (last_job,)).fetchone()
        if not binding:
            _fail()
        _binding(db, binding, jobs)
        if binding[1:3] != (owner, app) or binding[4] != 'admitted' or binding[6] > revision:
            _fail()
    return AdmissionHead(owner, app, revision, last_job)


def _validate_state(db, has_jobs):
    jobs = _jobs(db) if has_jobs else {}
    apps = _live_apps(db)
    heads = {(r[0], r[1]): _head(db, r, jobs) for r in db.execute(f'SELECT * FROM {HEADS}').fetchall()}
    if not apps.keys() <= heads.keys():
        _fail()
    bindings = {}
    captures = set()
    for row in db.execute(f'SELECT * FROM {BINDINGS}'):
        _binding(db, row, jobs)
        bindings[row[0]] = row
        if row[4] == 'admitted':
            identity = (row[1], row[2], row[5])
            head = heads.get((row[1], row[2]))
            if identity in captures or not head or row[6] > head.revision:
                _fail()
            captures.add(identity)
    if set(bindings) != {jid for jid, job in jobs.items() if job.kind in ('draft', 'assess')}:
        _fail()
    for jid, job in jobs.items():
        if job.kind in ('draft', 'assess'):
            marker = _tombstone(db, jid)
            if job.idempotency_key == 'forgotten' and marker != (job.profile_id, 'drafting_job_forgotten'):
                _fail()
    return jobs


@_safe_sql
def bootstrap_in_transaction(db):
    """Caller owns BEGIN IMMEDIATE and rollback, including migration DDL failures."""
    _writer(db)
    _base_schema(db)
    has_jobs = _job_schema(db)
    marker = db.execute('PRAGMA user_version').fetchone()[0]
    if marker == 0:
        if _tables(db) & {HEADS, BINDINGS}:
            _fail()
        apps = _live_apps(db)
        jobs = _jobs(db) if has_jobs else {}
        db.execute(_HEAD_SQL)
        db.execute(_BINDING_SQL)
        for owner, app in apps:
            db.execute(f'INSERT INTO {HEADS} VALUES(?,?,1,NULL)', (owner, app))
        for job in jobs.values():
            if job.kind not in ('draft', 'assess'):
                continue
            db.execute(f'INSERT INTO {BINDINGS} VALUES(?,?,?,?,?,NULL,NULL,NULL,NULL)',
                       (job.id, job.profile_id, job.application_id, job.kind, 'legacy'))
            if job.idempotency_key == 'forgotten':
                if _tombstone(db, job.id) is None:
                    db.execute('INSERT INTO tombstones VALUES(?,?,?)', (job.id, job.profile_id, 'drafting_job_forgotten'))
        db.execute('PRAGMA user_version=1')
    elif marker != 1:
        _fail()
    _admission_schema(db)
    _validate_state(db, has_jobs)


@_safe_sql
def register_new_application(db, application):
    """Own the first canonical record and head insertion; never infer newness later."""
    _writer(db)
    try:
        if not isinstance(application, c.ApplicationRecord):
            _fail('INVALID_INPUT', 422)
        application = c.ApplicationRecord.model_validate(_application_input(application))
    except (ValueError, TypeError):
        _fail('INVALID_INPUT', 422)
    owner, app = application.profile_id, application.application_id
    _base_schema(db)
    _admission_schema(db)
    if (_tombstone(db, app) or _tombstone(db, owner)
            or db.execute('SELECT 1 FROM records WHERE id=?', (app,)).fetchone()
            or db.execute(f'SELECT 1 FROM {HEADS} WHERE application_id=?', (app,)).fetchone()
            or db.execute(f'SELECT 1 FROM {BINDINGS} WHERE application_id=?', (app,)).fetchone()):
        _fail('CONFLICT', 409)
    if not _live_owner(db, owner):
        _fail('NOT_FOUND', 404)
    db.execute('INSERT INTO records(id,owner,kind,data) VALUES(?,?,?,?)',
               (app, owner, 'workspace:application', application.model_dump_json()))
    db.execute(f'INSERT INTO {HEADS} VALUES(?,?,0,NULL)', (owner, app))


@_safe_sql
def read_head(db, owner, application_id):
    """SELECT/PRAGMA-only metadata observation, never an admission certificate."""
    _identifier(owner, public=True)
    _identifier(application_id, public=True)
    _base_schema(db)
    if _application(db, owner, application_id) is None:
        _fail('NOT_FOUND', 404)
    _admission_schema(db)
    row = db.execute(f'SELECT * FROM {HEADS} WHERE owner=? AND application_id=?', (owner, application_id)).fetchone()
    if not row:
        _fail()
    jobs = _head_jobs(db, (row,))
    return _head(db, row, jobs)


@_safe_sql
def invalidate_scope(db, owner, application_id=None):
    """Monotonic privacy invalidation; saturation does not prevent deletion."""
    _writer(db)
    _identifier(owner, public=True)
    if application_id is not None:
        _identifier(application_id, public=True)
    _base_schema(db)
    _admission_schema(db)
    clause, args = ('owner=?', (owner,)) if application_id is None else ('owner=? AND application_id=?', (owner, application_id))
    rows = db.execute(f'SELECT * FROM {HEADS} WHERE {clause}', args).fetchall()
    jobs = _head_jobs(db, rows)
    heads = {(row[0], row[1]): _head(db, row, jobs) for row in rows}
    for key in _live_apps(db, owner, application_id):
        if key[0] == owner and (application_id is None or key[1] == application_id) and key not in heads:
            _fail()
    if application_id is not None and not heads:
        _fail('NOT_FOUND', 404)
    for head in heads.values():
        db.execute(f'UPDATE {HEADS} SET revision=? WHERE owner=? AND application_id=?',
                   (min(head.revision + 1, MAX_REVISION), head.owner, head.application_id))
