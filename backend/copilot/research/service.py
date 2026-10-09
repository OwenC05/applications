"""Fenced static research persistence. Source prose never enters a tool channel."""
import hashlib
import json
from contextlib import ExitStack
from typing import Annotated
from uuid import uuid4

from pydantic import Field, ValidationError

from ..domain import contracts as c
from ..jobs import Jobs
from ..store import fail, identifier
from .broker import MAX_CANONICAL_CODEPOINTS, MAX_RUN_BYTES, MAX_SOURCES, ResearchInput, research
from .fetch import MAX_BYTES, acquire


class ResearchRequest(c.Contract):
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    expected_input_revision: c.Revision
    expected_output_revision: c.Revision
    supporting_urls: Annotated[tuple[str, ...], Field(max_length=4)] = ()


class ResearchService:
    def __init__(self, store, *, jobs=None, acquire_source=acquire):
        self.store = store
        self.jobs = jobs or Jobs(store)
        self.domain = self.jobs.domain
        self.acquire = acquire_source

    def enqueue(self, owner, application_id, body):
        # Hash the explicit POST, not revisions advanced by its own successful job.
        request_hash = c.canonical_hash({'kind': 'research', 'application_id': application_id,
                                         'request': body.model_dump(mode='json')})
        with self.store._tx() as db:
            app = self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
            old = db.execute('SELECT job_id,request_sha256 FROM job_keys WHERE owner=? AND key=?',
                             (owner, body.idempotency_key)).fetchone()
            if old:
                if old[1] != request_hash:
                    fail('CONFLICT', 'Idempotency key belongs to different explicit inputs', 409)
                return self.jobs._job(db, old[0], owner)[0]
            if (app.input_revision, app.output_revision) != (body.expected_input_revision, body.expected_output_revision):
                fail('CONFLICT', 'Application changed; reload before research', 409)
            try:
                inputs = ResearchInput(profile_id=owner, application_id=application_id,
                    revisions=self.jobs.capture(db, owner, application_id), company=app.company,
                    role=app.role, company_url=app.company_url, vacancy_url=app.vacancy_url,
                    vacancy_id=app.vacancy_id, confirmed_hosts=app.official_domains,
                    supporting_urls=body.supporting_urls)
            except (ValueError, ValidationError):
                fail('INVALID_INPUT', 'Research requires bounded public identities, URLs and explicit hosts', 422)
            return self.jobs.enqueue(owner, 'research', inputs.model_dump(mode='json'), body.idempotency_key,
                                     application_id, transaction=db, request_hash=request_hash)

    def _current(self, job_id, worker_id, fence):
        with self.store._tx() as db:
            self.jobs.assert_current(db, job_id, worker_id, fence)
        return True

    def _validate(self, result, inputs):
        run = result.run
        if (run.profile_id, run.application_id, run.revisions) != (inputs.profile_id, inputs.application_id, inputs.revisions):
            fail('INVALID_INPUT', 'Research scope or capture mismatch', 422)
        if len(result.sources) > MAX_SOURCES or sum(len(x.original_bytes) for x in result.sources) > MAX_RUN_BYTES:
            fail('LIMIT_EXCEEDED', 'Research result exceeds source budget', 413)
        identifier(run.id)
        ids, units = set(), set()
        for item in result.sources:
            source = item.source
            identifier(source.id)
            identifier(item.unit_id)
            if (source.profile_id, source.application_id, source.research_run_id) != (run.profile_id, run.application_id, run.id):
                fail('INVALID_INPUT', 'Research source scope mismatch', 422)
            if source.id in ids or item.unit_id in units:
                fail('INVALID_INPUT', 'Duplicate research source identity', 422)
            ids.add(source.id)
            units.add(item.unit_id)
            if (not isinstance(item.original_bytes, bytes) or len(item.original_bytes) > MAX_BYTES
                    or len(item.canonical_text) > MAX_CANONICAL_CODEPOINTS
                    or hashlib.sha256(item.original_bytes).hexdigest() != source.original_sha256
                    or hashlib.sha256(item.canonical_text.encode()).hexdigest() != source.canonical_sha256):
                fail('INVALID_INPUT', 'Research source integrity mismatch', 422)
            for span in item.identity_spans:
                if (span.record_id, span.unit_id) != (source.id, item.unit_id):
                    fail('INVALID_INPUT', 'Research span scope mismatch', 422)
                try:
                    if not 0 <= span.start < span.end <= len(item.canonical_text):
                        raise ValueError('Invalid span bounds')
                    span.validate_text(item.canonical_text)
                except ValueError:
                    fail('INVALID_INPUT', 'Research span integrity mismatch', 422)
        if len(ids | units | {run.id}) != len(ids) + len(units) + 1:
            fail('INVALID_INPUT', 'Overlapping research identities', 422)
        if not set(run.company_source_ids + run.exact_role_source_ids) <= ids:
            fail('INVALID_INPUT', 'Research coverage references unavailable sources', 422)

    def run_research(self, job_id, worker_id, fence):
        intent_id = str(uuid4())
        with self.store._tx() as db:
            job, parameters = self.jobs.assert_current(db, job_id, worker_id, fence)
            if job.kind != 'research':
                fail('INVALID_INPUT', 'Research handler requires research job', 422)
            inputs = ResearchInput.model_validate_json(json.dumps(parameters))
            if (inputs.profile_id, inputs.application_id, inputs.revisions) != (job.profile_id, job.application_id, job.revisions):
                fail('CONFLICT', 'Research capture differs from job', 409)
            db.execute('INSERT INTO research_intents VALUES(?,?,?,?,?,?,?)',
                       (intent_id, job.profile_id, job.application_id, job.id, fence, None, 'acquiring'))
        def current():
            return self._current(job_id, worker_id, fence)
        result = research(inputs, current=current, acquire_source=self.acquire, clock=self.jobs._now)
        self._validate(result, inputs)
        # Bind broker-created run exactly once, before registering any blob.
        with self.store._tx() as db:
            self.jobs.assert_current(db, job_id, worker_id, fence)
            for record_id in (result.run.id, *(x.source.id for x in result.sources), *(x.unit_id for x in result.sources)):
                if (db.execute('SELECT 1 FROM records WHERE id=?', (record_id,)).fetchone()
                        or db.execute('SELECT 1 FROM tombstones WHERE id=?', (record_id,)).fetchone()):
                    fail('CONFLICT', 'Research identity collision', 409)
            db.execute("UPDATE research_intents SET run_id=?,state='writing' WHERE id=? AND run_id IS NULL",
                       (result.run.id, intent_id))
        # Keep every producer lock through canonical publication. A recovery worker
        # must never classify a written-but-not-yet-published blob as abandoned.
        with ExitStack() as locks:
            for item in result.sources:
                sid = item.source.id
                locks.enter_context(self.store._upload_lock(sid))
                path = self.store._blob(sid)
                if path.exists():
                    fail('CONFLICT', 'Research blob identity collision', 409)
                with self.store._tx() as db:
                    self.jobs.assert_current(db, job_id, worker_id, fence)
                    if (db.execute('SELECT 1 FROM records WHERE id=?', (sid,)).fetchone()
                            or db.execute('SELECT 1 FROM tombstones WHERE id=?', (sid,)).fetchone()):
                        fail('CONFLICT', 'Research source identity collision', 409)
                    db.execute('INSERT INTO research_blob_scopes VALUES(?,?,?,?,?)',
                               (sid, job.profile_id, job.application_id, result.run.id, intent_id))
                    db.execute('INSERT INTO research_blob_origins VALUES(?,?,?,?,?,?,?)',
                               (sid, job.profile_id, job.application_id, result.run.id, intent_id, job.id, fence))
                    db.execute('INSERT INTO upload_intents VALUES(?,?,?,?)',
                               (sid, job.profile_id, 'writing', item.source.original_sha256))
                self.store._write_blob(path, item.original_bytes)
                current()

            def publish(db, captured_job, summary):
                row = db.execute('SELECT run_id,state FROM research_intents WHERE id=? AND job_id=? AND fence=?',
                                 (intent_id, job_id, fence)).fetchone()
                if row != (result.run.id, 'writing'):
                    fail('CONFLICT', 'Research staging authority changed', 409)
                app = self.domain._get(db, job.profile_id, job.application_id, 'application', c.ApplicationRecord)
                for record_id in (result.run.id, *(x.source.id for x in result.sources), *(x.unit_id for x in result.sources)):
                    if (db.execute('SELECT 1 FROM records WHERE id=?', (record_id,)).fetchone()
                            or db.execute('SELECT 1 FROM tombstones WHERE id=?', (record_id,)).fetchone()):
                        fail('CONFLICT', 'Research publication identity collision', 409)
                self.domain._save(db, job.profile_id, 'research_run', result.run)
                for item in result.sources:
                    sid = item.source.id
                    if db.execute('SELECT state FROM upload_intents WHERE id=?', (sid,)).fetchone() != ('writing',):
                        fail('CONFLICT', 'Research source upload revoked', 409)
                    self.domain._save(db, job.profile_id, 'employer_source', item.source)
                    self.domain._save(db, job.profile_id, 'employer_unit', {
                        'id': item.unit_id, 'application_id': job.application_id, 'research_run_id': result.run.id,
                        'source_id': sid, 'text': item.canonical_text, 'text_sha256': item.source.canonical_sha256,
                        'media_type': item.media_type, 'content_encoding': item.content_encoding,
                        'identity_spans': [x.model_dump(mode='json') for x in item.identity_spans]}, key=item.unit_id)
                    db.execute("UPDATE upload_intents SET state='committed' WHERE id=?", (sid,))
                revision = captured_job.revisions.research + 1
                output = app.output_revision + 1
                db.execute('INSERT INTO app_revisions VALUES(?,?,?) ON CONFLICT(application_id) DO UPDATE SET research=excluded.research',
                           (job.profile_id, job.application_id, revision))
                db.execute('INSERT INTO research_heads VALUES(?,?,?,?,?) ON CONFLICT(application_id) DO UPDATE SET run_id=excluded.run_id,research=excluded.research,output=excluded.output',
                           (job.application_id, job.profile_id, result.run.id, revision, output))
                self.domain._save(db, job.profile_id, 'application', app.model_copy(update={'output_revision': output}))
                db.execute("UPDATE research_intents SET state='published' WHERE id=?", (intent_id,))
            summary = {'research_run_id': result.run.id, 'source_ids': [x.source.id for x in result.sources],
                       'source_count': len(result.sources), 'original_bytes': sum(len(x.original_bytes) for x in result.sources)}
            self.jobs.commit_stage(job_id, worker_id, fence, 'research_published', summary, publish, terminal=True)
        return summary

    def recover(self):
        # Only canonical current job authority protects an unpublished registration.
        with self.store._tx() as db:
            rows = db.execute("SELECT id,job_id,fence FROM research_intents WHERE state NOT IN ('published','cleaned','cleanup_pending')").fetchall()
            for intent_id, job_id, fence in rows:
                row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
                try:
                    job = c.DurableJob.model_validate_json(row[0]) if row else None
                    if job is None:
                        raise ValueError('Missing authority')
                    self.jobs.assert_current(db, job_id, job.lease_owner, fence)
                except (ValueError, TypeError):
                    db.execute("UPDATE research_intents SET state='cleanup_pending' WHERE id=?", (intent_id,))
                except Exception as exc:
                    from ..contracts import EvidenceError
                    if not isinstance(exc, EvidenceError) or exc.code not in ('CONFLICT', 'NOT_FOUND'):
                        raise
                    db.execute("UPDATE research_intents SET state='cleanup_pending' WHERE id=?", (intent_id,))
            for intent_id, in db.execute("SELECT id FROM research_intents WHERE state='cleanup_pending'").fetchall():
                if not db.execute("SELECT 1 FROM research_blob_scopes s JOIN upload_intents u ON s.id=u.id WHERE s.intent_id=? AND u.state NOT IN ('cleaned','not_owned')", (intent_id,)).fetchone():
                    db.execute("UPDATE research_intents SET state='cleaned' WHERE id=?", (intent_id,))

    def list(self, owner, application_id):
        with self.store._read() as db:
            self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
            head = db.execute('SELECT run_id FROM research_heads WHERE owner=? AND application_id=?', (owner, application_id)).fetchone()
            runs = [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE owner=? AND kind='workspace:research_run' AND json_extract(data,'$.application_id')=? ORDER BY rowid DESC", (owner, application_id))]
        return {'schema_version': 1, 'current_run_id': head[0] if head else None, 'runs': runs}

    def _run(self, db, owner, application_id, run_id):
        app = self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
        run = self.domain._get(db, owner, run_id, 'research_run', c.ResearchRun)
        if run.application_id != application_id:
            fail()
        return app, run

    def detail(self, owner, application_id, run_id):
        with self.store._read() as db:
            app, run = self._run(db, owner, application_id, run_id)
            head = db.execute('SELECT run_id,research FROM research_heads WHERE owner=? AND application_id=?', (owner, application_id)).fetchone()
            revision = db.execute('SELECT research FROM app_revisions WHERE owner=? AND application_id=?',
                                  (owner, application_id)).fetchone()
            current_research = revision[0] if revision else 0
            eligible = bool(head and head[0] == run.id and head[1] == current_research and app.input_revision == run.revisions.application_input
                            and run.state == 'complete' and self.jobs._now() < run.expires_at)
            sources = [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE owner=? AND kind='workspace:employer_source' AND json_extract(data,'$.research_run_id')=?", (owner, run_id))]
        return {'schema_version': 1, 'run': run.model_dump(mode='json'), 'eligibility': {'eligible': eligible}, 'sources': sources}

    def source(self, owner, application_id, run_id, source_id):
        with self.store._read() as db:
            self._run(db, owner, application_id, run_id)
            source = self.domain._get(db, owner, source_id, 'employer_source', c.EmployerSource)
            if (source.application_id, source.research_run_id) != (application_id, run_id):
                fail()
            row = db.execute("SELECT data FROM records WHERE owner=? AND kind='workspace:employer_unit' AND json_extract(data,'$.source_id')=?", (owner, source_id)).fetchone()
            if not row:
                fail()
            unit = json.loads(row[0])
        return {'schema_version': 1, 'source': source.model_dump(mode='json'), 'unit': unit,
                'identity_spans': unit['identity_spans']}

    def original(self, owner, application_id, run_id, source_id):
        captured = self.source(owner, application_id, run_id, source_id)
        try:
            with self.store._blob(source_id).open('rb') as original:
                raw = original.read(MAX_BYTES + 1)
        except OSError:
            fail('NOT_FOUND', 'Research original is unavailable', 404)
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != captured['source']['original_sha256']:
            fail('CONFLICT', 'Research original integrity mismatch', 409)
        if self.source(owner, application_id, run_id, source_id) != captured:
            fail('CONFLICT', 'Research original changed during read', 409)
        return raw
