"""Durable SQLite jobs: leases confer authority only with current scope and fence.

No scheduler retries paid ambiguity. Payloads/results stay private in canonical
storage; API summaries and errors contain no applicant/provider content.
"""
import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from .contracts import EvidenceError
from .domain import contracts as c
from .domain.repository import DomainRepository, uid
from .retrieval.packet_contracts import CorpusScope
from .retrieval.packet_runtime_contracts import PacketParameters
from .store import identifier


def utc_now():
    return datetime.now(timezone.utc)


def error(code, message, status=409):
    raise EvidenceError(code, message, status)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class Jobs:
    def __init__(self, store, clock=utc_now):
        self.store, self.clock = store, clock
        self.domain = DomainRepository(store)
        with store._tx() as db:
            for sql in (
                '''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                   state TEXT NOT NULL, data TEXT NOT NULL, parameters TEXT NOT NULL,
                   dependencies TEXT NOT NULL, UNIQUE(owner,id))''',
                '''CREATE TABLE IF NOT EXISTS job_keys(owner TEXT NOT NULL, key TEXT NOT NULL,
                   job_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, PRIMARY KEY(owner,key))''',
                '''CREATE TABLE IF NOT EXISTS job_stages(job_id TEXT NOT NULL, stage TEXT NOT NULL,
                   fence INTEGER NOT NULL, data TEXT NOT NULL, sha256 TEXT NOT NULL,
                   PRIMARY KEY(job_id,stage))''',
                '''CREATE TABLE IF NOT EXISTS job_retries(new_job_id TEXT PRIMARY KEY,
                   owner TEXT NOT NULL, prior_job_id TEXT NOT NULL, prior_attempt_id TEXT NOT NULL,
                   warning_version TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS provider_retry_links(attempt_id TEXT PRIMARY KEY,
                   prior_attempt_id TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS app_revisions(owner TEXT NOT NULL,
                   application_id TEXT PRIMARY KEY, research INTEGER NOT NULL DEFAULT 0)''',
                '''CREATE TABLE IF NOT EXISTS provider_attempts(id TEXT PRIMARY KEY,
                   owner TEXT NOT NULL, job_id TEXT NOT NULL, state TEXT NOT NULL,
                   data TEXT NOT NULL, dispatched INTEGER NOT NULL DEFAULT 0,
                   day TEXT NOT NULL, response TEXT, stage TEXT NOT NULL, request_sha256 TEXT NOT NULL, UNIQUE(job_id,stage))''',
                '''CREATE TABLE IF NOT EXISTS budget_limits(owner TEXT PRIMARY KEY,
                   daily_tokens INTEGER NOT NULL, daily_calls INTEGER NOT NULL,
                   job_tokens INTEGER NOT NULL, job_calls INTEGER NOT NULL)''',
            ):
                db.execute(sql)
            # Additive legacy migration: annotate keys only from a proven retained
            # canonical job. Unknown orphan scope stays unknown, never guessed.
            columns = {row[1] for row in db.execute('PRAGMA table_info(job_keys)')}
            for name in ('application_id', 'kind'):
                if name not in columns:
                    db.execute(f'ALTER TABLE job_keys ADD COLUMN {name} TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS job_keys_scope ON job_keys(owner,application_id)')
            for owner, key, jid, job_owner, state, serialized in db.execute(
                    '''SELECT k.owner,k.key,k.job_id,j.owner,j.state,j.data FROM job_keys k
                       LEFT JOIN jobs j ON j.id=k.job_id WHERE k.kind IS NULL''').fetchall():
                if job_owner != owner or serialized is None:
                    continue
                try:
                    job = c.DurableJob.model_validate_json(serialized)
                except ValueError:
                    continue
                if (job.id, job.profile_id, job.state, job.idempotency_key) == (jid, owner, state, key):
                    db.execute('UPDATE job_keys SET application_id=?,kind=? WHERE owner=? AND key=? AND kind IS NULL',
                               (job.application_id, job.kind, owner, key))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
            raise ValueError('Trusted clock must return UTC-aware time')
        return value

    def _job(self, db, job_id, owner=None):
        identifier(job_id)
        row = db.execute('SELECT owner,data,parameters,dependencies FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or owner is not None and row[0] != owner:
            error('NOT_FOUND', 'Job not found', 404)
        job = c.DurableJob.model_validate_json(row[1])
        self.store._profile(db, job.profile_id)
        return job, json.loads(row[2]), tuple(json.loads(row[3]))

    def _save(self, db, job):
        db.execute('UPDATE jobs SET state=?,data=? WHERE id=?', (job.state, job.model_dump_json(), job.id))

    def capture(self, db, owner, application_id=None):
        revisions = self.domain.revisions(db, owner)
        if application_id:
            app = self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
            row = db.execute('SELECT research FROM app_revisions WHERE owner=? AND application_id=?',
                             (owner, application_id)).fetchone()
            revisions = revisions.model_copy(update={'application_input': app.input_revision,
                                                      'application_output': app.output_revision,
                                                      'research': row[0] if row else 0})
        return revisions

    def enqueue(self, owner, kind, parameters, idempotency_key, application_id=None, *, transaction=None, request_hash=None):
        if kind not in ('index', 'research', 'packets', 'draft', 'assess', 'cleanup'):
            error('INVALID_INPUT', 'Unknown job kind', 422)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            error('INVALID_INPUT', 'Provide a bounded idempotency key', 422)
        if not isinstance(parameters, dict) or len(encode(parameters).encode()) > 131072:
            error('INVALID_INPUT', 'Job parameters exceed bounds', 422)
        if kind == 'index':
            if parameters.get('corpus') == 'employer':
                if set(parameters) != {'corpus', 'research_run_id'} or not application_id:
                    error('INVALID_INPUT', 'Employer index requires exact application and research run', 422)
                try:
                    scope = CorpusScope(profile_id=owner, corpus='employer', application_id=application_id,
                                        research_run_id=parameters['research_run_id'])
                except ValueError:
                    error('INVALID_INPUT', 'Invalid employer index scope', 422)
                dependencies = self.store.scope_dependencies(scope)
            else:
                if set(parameters) != {'corpus'} or parameters['corpus'] not in ('facts', 'documents') or application_id:
                    error('INVALID_INPUT', 'Index requires an explicitly selected profile corpus', 422)
                dependencies = (parameters['corpus'],)
        elif kind == 'packets':
            try:
                packet = PacketParameters.model_validate_json(encode(parameters))
            except ValueError:
                error('INVALID_INPUT', 'Invalid captured packet parameters', 422)
            if (packet.profile_id, packet.application_id) != (owner, application_id):
                error('INVALID_INPUT', 'Exact packet application scope required', 422)
            if request_hash is not None and request_hash != packet.request_sha256:
                error('INVALID_INPUT', 'Packet request digest mismatch', 422)
            request_hash = packet.request_sha256
            dependencies = ('facts', 'application_input', 'research')
        elif kind in ('research', 'draft', 'assess'):
            if not application_id:
                error('INVALID_INPUT', 'Application scope required', 422)
            dependencies = (('consent', 'application_input', 'application_output', 'research')
                            if kind == 'research' else ('metadata', 'facts', 'documents', 'consent',
                                                       'application_input', 'application_output', 'research'))
        else:
            dependencies = ('facts', 'documents')
        with nullcontext(transaction) if transaction is not None else self.store._tx() as db:
            revisions = self.capture(db, owner, application_id)
            request_hash = request_hash or c.canonical_hash({'kind': kind, 'parameters': parameters,
                                             'application_id': application_id,
                                             'revisions': {k: getattr(revisions, k) for k in dependencies}})
            old = db.execute('SELECT job_id,request_sha256 FROM job_keys WHERE owner=? AND key=?',
                             (owner, idempotency_key)).fetchone()
            if old:
                if old[1] != request_hash:
                    error('CONFLICT', 'Idempotency key belongs to different captured inputs')
                return self._job(db, old[0], owner)[0]
            if kind == 'index' and parameters['corpus'] == 'employer':
                self.store.require_current_research(db, scope, self._now())
            if kind == 'packets':
                self._packet_current(db, packet, revisions)
            job = c.DurableJob(id=uid(), profile_id=owner, application_id=application_id,
                               kind=kind, stage='queued', idempotency_key=idempotency_key,
                               state='queued', revisions=revisions, fence=0)
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                       (job.id, owner, job.state, job.model_dump_json(), encode(parameters), encode(dependencies)))
            db.execute('INSERT INTO job_keys(owner,key,job_id,request_sha256,application_id,kind) VALUES(?,?,?,?,?,?)',
                       (owner, idempotency_key, job.id, request_hash, application_id, kind))
            return job

    def _packet_current(self, db, packet, revisions):
        request = packet.request
        if (revisions.facts, revisions.application_input, revisions.research) != (
                request.expected_facts_revision, request.expected_input_revision, request.expected_research_revision):
            error('CONFLICT', 'Packet captured revisions changed')
        for binding in (packet.facts_generation, packet.employer_generation):
            if not db.execute('SELECT 1 FROM generation_intents WHERE id=?', (binding.generation_id,)).fetchone():
                error('CONFLICT', 'Packets require actual registered generations')
            self.store.require_generation_binding(db, binding, self._now())

    def _create_warned_retry(self, db, prior, parameters, dependencies, body, request_hash):
        """Internal same-transaction insert after Provider verifies purpose/budget/scope.

        Never rebase the original capture or copy completed/uncertain provider stages.
        """
        retry = prior.model_copy(update={'id': uid(), 'state': 'queued', 'stage': 'queued',
            'idempotency_key': body.idempotency_key, 'fence': 0, 'attempt_count': 0,
            'lease_owner': None, 'heartbeat_at': None, 'lease_expires_at': None,
            'cancellation_requested': False})
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (retry.id, retry.profile_id, retry.state,
                   retry.model_dump_json(), encode(parameters), encode(dependencies)))
        db.execute('INSERT INTO job_keys(owner,key,job_id,request_sha256,application_id,kind) VALUES(?,?,?,?,?,?)',
                   (retry.profile_id, body.idempotency_key, retry.id, request_hash, retry.application_id, retry.kind))
        db.execute('INSERT INTO job_retries VALUES(?,?,?,?,?)', (retry.id, retry.profile_id, prior.id,
                   body.prior_attempt_id, body.warning_version))
        return retry

    def retry_lineage(self, owner, job_id):
        with self.store._read() as db:
            self._job(db, job_id, owner)
            row = db.execute('SELECT prior_job_id,prior_attempt_id,warning_version FROM job_retries WHERE new_job_id=? AND owner=?',
                             (job_id, owner)).fetchone()
        return dict(zip(('prior_job_id', 'prior_attempt_id', 'warning_version'), row)) if row else None

    def get(self, owner, job_id):
        with self.store._tx() as db:
            return self._job(db, job_id, owner)[0]

    def list(self, owner):
        with self.store._tx() as db:
            self.store._profile(db, owner)
            return tuple(c.DurableJob.model_validate_json(row[0]) for row in db.execute(
                'SELECT data FROM jobs WHERE owner=? ORDER BY rowid', (owner,)))

    def _pause_attempts(self, db, job_id):
        rows = db.execute("SELECT id,data FROM provider_attempts WHERE job_id=? AND state='prepared'", (job_id,)).fetchall()
        for attempt_id, serialized in rows:
            attempt = c.ProviderAttempt.model_validate_json(serialized).model_copy(update={'state': 'indeterminate'})
            db.execute('UPDATE provider_attempts SET state=?,data=? WHERE id=?',
                       ('indeterminate', attempt.model_dump_json(), attempt_id))
        return bool(rows) or bool(db.execute("SELECT 1 FROM provider_attempts WHERE job_id=? AND state='indeterminate'", (job_id,)).fetchone())

    def claim(self, worker_id, lease_seconds=60, job_id=None):
        identifier(worker_id)
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            error('INVALID_INPUT', 'Lease must be between 5 and 300 seconds', 422)
        if job_id:
            identifier(job_id)
        with self.store._tx() as db:
            now = self._now()
            rows = db.execute("SELECT id FROM jobs WHERE state IN ('queued','running') ORDER BY rowid").fetchall()
            for (candidate_id,) in rows:
                if job_id and job_id != candidate_id:
                    continue
                try:
                    job, _, dependencies = self._job(db, candidate_id)
                except EvidenceError as exc:
                    if exc.code == 'NOT_FOUND':
                        raw = db.execute('SELECT data FROM jobs WHERE id=?', (candidate_id,)).fetchone()
                        if raw:
                            missing = c.DurableJob.model_validate_json(raw[0])
                            uncertain = self._pause_attempts(db, candidate_id)
                            self._save(db, missing.model_copy(update={
                                'state': 'indeterminate' if uncertain else 'failed',
                                'stage': 'provider_unknown' if uncertain else 'missing_scope'}))
                        continue
                    raise
                if job.state == 'running' and job.lease_expires_at > now:
                    continue
                if self._pause_attempts(db, job.id):
                    self._save(db, job.model_copy(update={'state': 'indeterminate', 'stage': 'provider_unknown'}))
                    continue
                if job.cancellation_requested:
                    self._save(db, job.model_copy(update={'state': 'cancelled'}))
                    continue
                if job.attempt_count >= 5:
                    self._save(db, job.model_copy(update={'state': 'failed', 'stage': 'lease_limit'}))
                    continue
                try:
                    current = self.capture(db, job.profile_id, job.application_id)
                except EvidenceError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                    self._save(db, job.model_copy(update={'state': 'failed', 'stage': 'missing_scope'}))
                    continue
                if not job.revisions.matches(current, dependencies):
                    self._save(db, job.model_copy(update={'state': 'failed', 'stage': 'stale_inputs'}))
                    continue
                leased = job.model_copy(update={'state': 'running', 'stage': job.stage if job.stage != 'queued' else 'started',
                                                'fence': job.fence + 1, 'lease_owner': worker_id,
                                                'lease_expires_at': now + timedelta(seconds=lease_seconds),
                                                'heartbeat_at': now, 'attempt_count': job.attempt_count + 1})
                self._save(db, leased)
                return leased
        return None

    def assert_current(self, db, job_id, worker_id, fence):
        job, parameters, dependencies = self._job(db, job_id)
        if (job.state != 'running' or job.lease_owner != worker_id or job.fence != fence
                or job.cancellation_requested or job.lease_expires_at <= self._now()):
            error('CONFLICT', 'Job lease or operation authority is no longer current')
        current = self.capture(db, job.profile_id, job.application_id)
        if not job.revisions.matches(current, dependencies):
            error('CONFLICT', 'Job captured inputs changed')
        if job.kind == 'index' and parameters['corpus'] == 'employer':
            scope = CorpusScope(profile_id=job.profile_id, corpus='employer', application_id=job.application_id,
                                research_run_id=parameters['research_run_id'])
            self.store.require_current_research(db, scope, self._now())
        if job.kind == 'packets':
            self._packet_current(db, PacketParameters.model_validate_json(encode(parameters)), current)
        return job, parameters

    def heartbeat(self, job_id, worker_id, fence, lease_seconds=60):
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            error('INVALID_INPUT', 'Lease must be between 5 and 300 seconds', 422)
        with self.store._tx() as db:
            job, _ = self.assert_current(db, job_id, worker_id, fence)
            updated = job.model_copy(update={'heartbeat_at': self._now(),
                                            'lease_expires_at': self._now() + timedelta(seconds=lease_seconds)})
            self._save(db, updated)
            return updated

    def cancel(self, owner, job_id):
        with self.store._tx() as db:
            job, _, _ = self._job(db, job_id, owner)
            if job.state in ('completed', 'cancelled', 'failed', 'indeterminate'):
                return job
            uncertain = self._pause_attempts(db, job.id)
            updated = job.model_copy(update={'cancellation_requested': True,
                                            'state': 'indeterminate' if uncertain else 'cancelled',
                                            'stage': 'provider_unknown' if uncertain else 'cancelled'})
            self._save(db, updated)
            return updated

    def stage_result(self, owner, job_id, stage):
        with self.store._tx() as db:
            self._job(db, job_id, owner)
            row = db.execute('SELECT data FROM job_stages WHERE job_id=? AND stage=?', (job_id, stage)).fetchone()
            return json.loads(row[0]) if row else None

    def commit_stage(self, job_id, worker_id, fence, stage, result, publish=None, *, terminal=False):
        if type(terminal) is not bool:
            error('INVALID_INPUT', 'Terminal publication flag must be explicit boolean', 422)
        if not isinstance(stage, str) or not 1 <= len(stage) <= 200:
            error('INVALID_INPUT', 'Stage identifier exceeds bounds', 422)
        serialized = encode(result)
        if len(serialized.encode()) > 1024 * 1024:
            error('LIMIT_EXCEEDED', 'Stage result exceeds bounds', 413)
        checksum = hashlib.sha256(serialized.encode()).hexdigest()
        with self.store._tx() as db:
            job, _ = self.assert_current(db, job_id, worker_id, fence)
            old = db.execute('SELECT sha256 FROM job_stages WHERE job_id=? AND stage=?', (job_id, stage)).fetchone()
            if old:
                if old[0] != checksum:
                    error('CONFLICT', 'Completed stage cannot be overwritten')
                return result
            if publish:
                # Internal domain callback only: no network/model/file work here.
                publish(db, job, result)
                if not terminal:
                    # A callback that advances its own guarded dependencies must
                    # publish and terminalize atomically, not silently rebase capture.
                    _, _, dependencies = self._job(db, job_id)
                    if not job.revisions.matches(self.capture(db, job.profile_id, job.application_id), dependencies):
                        error('CONFLICT', 'Dependency-changing publication requires atomic terminal transition')
            db.execute('INSERT INTO job_stages VALUES(?,?,?,?,?)', (job_id, stage, fence, serialized, checksum))
            self._save(db, job.model_copy(update={'stage': stage, 'state': 'completed' if terminal else 'running'}))
            return result

    def finish(self, job_id, worker_id, fence, state='completed'):
        if state not in ('completed', 'failed'):
            error('INVALID_INPUT', 'Unknown job completion state', 422)
        with self.store._tx() as db:
            job, _ = self.assert_current(db, job_id, worker_id, fence)
            updated = job.model_copy(update={'state': state})
            self._save(db, updated)
            return updated

    def fail_packet(self, job_id, worker_id, fence):
        """Record unpaid packet failure without reauthorizing invalid inputs.

        This grants no publication authority. A cancelled, expired, reclaimed or
        deleted operation cannot be overwritten; paid uncertainty fails closed.
        """
        with self.store._tx() as db:
            job, _, _ = self._job(db, job_id)
            if (job.kind != 'packets' or job.state != 'running'
                    or job.lease_owner != worker_id or job.fence != fence
                    or job.cancellation_requested or job.lease_expires_at <= self._now()):
                error('CONFLICT', 'Packet failure lease is no longer current')
            self.domain._get(db, job.profile_id, job.application_id, 'application', c.ApplicationRecord)
            if db.execute("SELECT 1 FROM provider_attempts WHERE job_id=? AND state IN ('prepared','indeterminate')",
                          (job.id,)).fetchone():
                error('CONFLICT', 'Uncertain provider attempt prevents unpaid failure transition')
            updated = job.model_copy(update={'state': 'failed', 'stage': 'packet_failed'})
            self._save(db, updated)
            return updated

    def fail_drafting(self, job_id, worker_id, fence):
        """Record definite drafting failure, never publish or release uncertainty.

        Failed inputs need not become eligible again just to record failure. The
        exact surviving lease/scope is still required; uncertainty wins instead.
        """
        with self.store._tx() as db:
            job, _, _ = self._job(db, job_id)
            if (job.kind not in ('draft', 'assess') or job.state != 'running'
                    or job.lease_owner != worker_id or job.fence != fence
                    or job.cancellation_requested or job.lease_expires_at <= self._now()):
                error('CONFLICT', 'Drafting failure lease is no longer current')
            self.domain._get(db, job.profile_id, job.application_id, 'application', c.ApplicationRecord)
            if db.execute("SELECT 1 FROM provider_attempts WHERE job_id=? AND state IN ('prepared','indeterminate')",
                          (job.id,)).fetchone():
                error('CONFLICT', 'Uncertain provider attempt prevents definite failure transition')
            updated = job.model_copy(update={'state': 'failed', 'stage': 'drafting_failed'})
            self._save(db, updated)
            return updated
