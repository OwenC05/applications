"""Registered immutable index generations; OS locks prove producer quiescence only."""
import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import asdict

from .contracts import EvidenceError, Manifest
from .domain.contracts import GenerationIntent, canonical_hash
from .jobs import Jobs
from .store import identifier


class Generations:
    def __init__(self, store, jobs=None):
        self.store, self.jobs = store, jobs or Jobs(store)
        self.locks = store.root / 'generation-locks'
        self.locks.mkdir(exist_ok=True)
        if self.locks.is_symlink() or self.locks.resolve().parent != store.root:
            raise EvidenceError('CLEANUP_PENDING', 'Generation lock storage is unsafe', 503)
        with store._tx() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS generation_intents(id TEXT PRIMARY KEY,
                       owner TEXT NOT NULL, corpus TEXT NOT NULL, state TEXT NOT NULL,
                       data TEXT NOT NULL, captured TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS generation_chunks(generation_id TEXT NOT NULL,
                       id TEXT NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL,
                       PRIMARY KEY(generation_id,id))''')

    @contextmanager
    def producer(self, generation_id):
        """Nonblocking per-generation lock, never a store-wide/model lock."""
        identifier(generation_id)
        path = self.locks / generation_id
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EvidenceError('CLEANUP_PENDING', 'Generation producer has not quiesced', 503) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self, db, generation_id):
        row = db.execute('SELECT data,captured FROM generation_intents WHERE id=?', (generation_id,)).fetchone()
        if not row:
            raise EvidenceError('NOT_FOUND', 'Generation not found', 404)
        return GenerationIntent.model_validate_json(row[0]), row[1]

    def _save(self, db, intent):
        db.execute('UPDATE generation_intents SET state=?,data=? WHERE id=?',
                   (intent.state, intent.model_dump_json(), intent.generation_id))

    def register(self, job_id, worker_id, fence, generation_id, fingerprint, chunker_version):
        identifier(generation_id)
        with self.store._tx() as db:
            job, parameters = self.jobs.assert_current(db, job_id, worker_id, fence)
            if job.kind != 'index':
                raise EvidenceError('INVALID_INPUT', 'Index job required', 422)
            corpus = parameters['corpus']
            _, values = self.store.capture_sources(db, job.profile_id, corpus)
            intent = GenerationIntent(profile_id=job.profile_id, generation_id=generation_id,
                corpus=corpus, job_id=job.id, fence=fence, lease_expires_at=job.lease_expires_at,
                revisions=job.revisions, snapshot_sha256=canonical_hash(values),
                model_fingerprint=fingerprint, chunker_version=chunker_version,
                dense_collection='evidence_' + generation_id.replace('-', ''),
                sparse_relpath=generation_id, state='registered')
            db.execute('INSERT INTO generation_intents VALUES(?,?,?,?,?,?)',
                       (generation_id, job.profile_id, corpus, intent.state, intent.model_dump_json(), json.dumps(values)))
            db.execute('INSERT INTO dense_mutations VALUES(?,?,?)', (generation_id, fence, 'not_attempted'))
            return intent, values

    def guard(self, generation_id, worker_id, fence, state=None):
        with self.store._tx() as db:
            intent, _ = self._load(db, generation_id)
            job, _ = self.jobs.assert_current(db, intent.job_id, worker_id, fence)
            if (intent.fence != fence or intent.state not in ('registered', 'building', 'ready')
                    or not intent.revisions.matches(job.revisions, (intent.corpus,))):
                raise EvidenceError('CONFLICT', 'Generation authority is no longer current', 409)
            if state:
                intent = intent.model_copy(update={'state': state, 'lease_expires_at': job.lease_expires_at})
                self._save(db, intent)
            return intent

    def stage(self, generation_id, worker_id, fence, snapshot):
        with self.store._tx() as db:
            intent, captured = self._load(db, generation_id)
            self.jobs.assert_current(db, intent.job_id, worker_id, fence)
            if intent.fence != fence or intent.state != 'registered' or captured is None:
                raise EvidenceError('CONFLICT', 'Generation cannot accept staged chunks', 409)
            values = json.loads(captured)
            if canonical_hash(values) != intent.snapshot_sha256:
                raise EvidenceError('CONFLICT', 'Generation capture mismatch', 409)
            revision = getattr(intent.revisions, intent.corpus)
            if (snapshot.profile_id, snapshot.corpus, snapshot.revision) != (intent.profile_id, intent.corpus, revision):
                raise EvidenceError('CONFLICT', 'Generation snapshot scope mismatch', 409)
            # Validate slices against the immutable capture without invoking a tokenizer.
            def selected(record):
                return [c for c in snapshot.chunks if (c.record_id == record.id if intent.corpus == 'facts' else c.unit_id == record.id)]
            checked = self.store.chunk_capture(intent.profile_id, intent.corpus, revision, values, selected)
            if checked.chunks != snapshot.chunks:
                raise EvidenceError('CONFLICT', 'Generation chunk association mismatch', 409)
            for chunk in snapshot.chunks:
                db.execute('INSERT INTO generation_chunks VALUES(?,?,?,?)',
                           (generation_id, chunk.id, intent.profile_id, json.dumps(asdict(chunk))))
            self._save(db, intent.model_copy(update={'state': 'building'}))
            db.execute('UPDATE generation_intents SET captured=NULL WHERE id=?', (generation_id,))

    def begin_dense(self, generation_id, worker_id, fence):
        """Commit possible-dispatch evidence before the entire remote write envelope."""
        with self.store._tx() as db:
            intent, _ = self._load(db, generation_id)
            self.jobs.assert_current(db, intent.job_id, worker_id, fence)
            if intent.fence != fence or intent.state != 'building':
                raise EvidenceError('CONFLICT', 'Generation cannot start a dense mutation', 409)
            changed = db.execute("UPDATE dense_mutations SET state='inflight' WHERE generation_id=? AND fence=? AND state='not_attempted'",
                                 (generation_id, fence)).rowcount
            if changed != 1:
                raise EvidenceError('CONFLICT', 'Dense mutation cannot be replayed', 409)

    def dense_result(self, generation_id, fence, acknowledged):
        """Cleanup-only receipt for the original envelope; never publication authority.

        A full successful adapter return is the acknowledgement. Exceptions and
        process loss cannot prove no dispatch. Receipt recording remains possible
        after revocation, but requires the exact existing generation/original fence.
        """
        if type(acknowledged) is not bool:
            raise ValueError('Trusted acknowledgement must be boolean')
        with self.store._tx() as db:
            intent, _ = self._load(db, generation_id)
            if intent.fence != fence:
                raise EvidenceError('CONFLICT', 'Dense receipt scope mismatch', 409)
            state = 'acknowledged' if acknowledged else 'indeterminate'
            changed = db.execute("UPDATE dense_mutations SET state=? WHERE generation_id=? AND fence=? AND state IN ('inflight','indeterminate')",
                                 (state, generation_id, fence)).rowcount
            if changed != 1:
                raise EvidenceError('CONFLICT', 'Dense receipt does not match an existing unresolved envelope', 409)

    def dense_pending(self, generation_id):
        with self.store._tx() as db:
            row = db.execute('SELECT state FROM dense_mutations WHERE generation_id=?', (generation_id,)).fetchone()
            return row is None or row[0] in ('inflight', 'indeterminate')

    def publish(self, generation_id, worker_id, fence, manifest):
        self.guard(generation_id, worker_id, fence, 'ready')
        def commit(db, job, _result):
            intent, _ = self._load(db, generation_id)
            expected = Manifest(generation_id, intent.profile_id, intent.corpus,
                getattr(intent.revisions, intent.corpus), intent.model_fingerprint,
                intent.chunker_version, manifest.chunk_ids_sha256, manifest.chunk_count,
                intent.dense_collection, intent.sparse_relpath)
            if intent.fence != fence or manifest != expected:
                raise EvidenceError('CONFLICT', 'Generation manifest scope mismatch', 409)
            self.store.publish_manifest(manifest, expected.revision, transaction=db,
                                        jobs=self.jobs, worker_id=worker_id, fence=fence)
            self._save(db, intent.model_copy(update={'state': 'published'}))
        with self.store._tx() as db:
            intent, _ = self._load(db, generation_id)
        self.jobs.commit_stage(intent.job_id, worker_id, fence, 'index_published',
                               asdict(manifest), publish=commit, terminal=True)
        return manifest

    def abandon(self, generation_id):
        # Recovery may only retire authority, never revive or publish expired work.
        with self.store._tx() as db:
            intent, _ = self._load(db, generation_id)
            if intent.state != 'published':
                self._save(db, intent.model_copy(update={'state': 'cleanup_pending'}))
                db.execute('UPDATE generation_intents SET captured=NULL WHERE id=?', (generation_id,))
                db.execute('DELETE FROM generation_chunks WHERE generation_id=?', (generation_id,))

    def list(self):
        with self.store._tx() as db:
            return [GenerationIntent.model_validate_json(row[0]) for row in db.execute('SELECT data FROM generation_intents')]

    def live(self, intent):
        with self.store._tx() as db:
            try:
                row = db.execute('SELECT data FROM jobs WHERE id=?', (intent.job_id,)).fetchone()
                if not row:
                    return False
                from .domain.contracts import DurableJob
                job = DurableJob.model_validate_json(row[0])
                self.jobs.assert_current(db, job.id, job.lease_owner, intent.fence)
                return job.fence == intent.fence and intent.state in ('registered', 'building', 'ready')
            except EvidenceError:
                return False

    def cleaned(self, generation_id):
        with self.store._tx() as db:
            intent, _ = self._load(db, generation_id)
            mutation = db.execute('SELECT state FROM dense_mutations WHERE generation_id=?', (generation_id,)).fetchone()
            if mutation is None or mutation[0] in ('inflight', 'indeterminate'):
                raise EvidenceError('CLEANUP_PENDING', 'External dense write completion remains unknown', 503)
            self._save(db, intent.model_copy(update={'state': 'cleaned'}))
            db.execute('UPDATE generation_intents SET captured=NULL WHERE id=?', (generation_id,))
            db.execute('DELETE FROM generation_chunks WHERE generation_id=?', (generation_id,))


def new_generation():
    return str(uuid.uuid4())
