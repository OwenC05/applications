"""One durable local worker; heartbeats do not hold model/network/storage locks."""
import json
import sys
import threading
import time
import uuid
from contextlib import contextmanager

from .config import worker_lock
from .contracts import EvidenceError
from .jobs import Jobs

SAFE_CODES = {'CONFLICT', 'NOT_FOUND', 'CLEANUP_PENDING', 'MODEL_NOT_READY', 'INDEX_NOT_READY',
              'LIMIT_EXCEEDED', 'HANDLER_UNAVAILABLE', 'INVALID_INPUT', 'UNAVAILABLE',
              'CONSENT_REQUIRED', 'BUDGET_REQUIRED', 'BUDGET_EXCEEDED', 'PROVIDER_UNKNOWN',
              'PROVIDER_UNSENT', 'INVALID_PROVIDER_OUTPUT'}


def safe_code(error):
    return error.code if isinstance(error, EvidenceError) and error.code in SAFE_CODES else 'UNAVAILABLE'


class Worker:
    def __init__(self, store, service_factory, *, lease_seconds=30, heartbeat_interval=None, research_factory=None, packet_factory=None, drafting_factory=None):
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ValueError('Worker lease must be 5 to 300 seconds')
        interval = heartbeat_interval if heartbeat_interval is not None else lease_seconds / 3
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 0 < interval < lease_seconds / 2:
            raise ValueError('Heartbeat interval must be positive and less than half the lease')
        self.store, self.service_factory = store, service_factory
        self.jobs = Jobs(store)
        self.worker_id = str(uuid.uuid4())
        self.lease_seconds, self.interval = lease_seconds, interval
        self._service = None
        self._research = None
        self.research_factory = research_factory
        self.packet_factory = packet_factory
        self._packets = None
        self.drafting_factory = drafting_factory
        self._drafting = None
        self.stop = threading.Event()
        self.last_outcome = None
        self._last_emitted = None

    def service(self):
        if self._service is None:
            self._service = self.service_factory()
            # Same clock/authority for handler guards and the heartbeat owner.
            self._service.jobs = self.jobs
            self._service.generations.jobs = self.jobs
        return self._service

    def research(self):
        if self._research is None:
            from .research.service import ResearchService
            self._research = (self.research_factory(self.store, self.jobs) if self.research_factory
                              else ResearchService(self.store, jobs=self.jobs))
        return self._research

    def packets(self):
        if self._packets is None:
            from .retrieval.packet_service import PacketService
            self._packets = (self.packet_factory(self.store, self.jobs) if self.packet_factory
                             else PacketService(self.store, self.service(), jobs=self.jobs))
        return self._packets

    def drafting(self):
        if self._drafting is None:
            from .drafting.service import DraftingService
            self._drafting = (self.drafting_factory(self.store, self.jobs) if self.drafting_factory
                              else DraftingService(self.store, jobs=self.jobs))
        return self._drafting

    @contextmanager
    def heartbeat(self, job):
        stopped = threading.Event()
        outcome = {'state': 'active'}
        def renew():
            while not stopped.wait(self.interval):
                try:
                    self.jobs.heartbeat(job.id, self.worker_id, job.fence, self.lease_seconds)
                except Exception as exc:
                    if isinstance(exc, EvidenceError) and exc.code in ('CONFLICT', 'NOT_FOUND'):
                        outcome.update(state='authority_lost', code='AUTHORITY_LOST')
                    else:
                        outcome.update(state='failed', code=safe_code(exc))
                    self._emit({'schema_version': 1, 'event': 'heartbeat', 'job_id': job.id,
                                'outcome': dict(outcome)})
                    return
        thread = threading.Thread(target=renew, name='copilot-heartbeat', daemon=True)
        thread.start()
        try:
            yield outcome
        finally:
            stopped.set()
            thread.join()
            if outcome['state'] == 'active':
                outcome['state'] = 'stopped'

    def recover(self):
        self.research().recover()
        blobs = self.store.reconcile_blobs()
        # Blob-only deletion does not need a healthy Chroma/model service.
        for ticket in self.store.pending_cleanup():
            if not ticket.generation_ids:
                try:
                    self.store.complete_cleanup(ticket.id)
                except EvidenceError as exc:
                    if exc.code != 'CLEANUP_PENDING':
                        raise
        with self.store._tx() as db:
            known_indexes = bool(db.execute('SELECT 1 FROM generation_intents LIMIT 1').fetchone()
                                 or db.execute('SELECT 1 FROM manifests LIMIT 1').fetchone())
        indexes = self.service().recover() if known_indexes else {'external_recovery': 'not_required'}
        return {'blobs': blobs, 'indexes': indexes}

    @staticmethod
    def _emit(value):
        try:
            print(json.dumps(value, separators=(',', ':')), file=sys.stderr, flush=True)
        except OSError:
            # A broken log transport cannot erase the retained operation outcome.
            pass

    def _outcome(self, value):
        self.last_outcome = value
        # Daemon outcomes are observed, not discarded; idle polls never spam logs.
        if value != self._last_emitted and (value['job'] is not None or self.outcome_failed(value)):
            self._emit(value)
            self._last_emitted = value

    @staticmethod
    def outcome_failed(value):
        return (value.get('claim', {}).get('state') == 'failed'
                or value['heartbeat']['state'] == 'failed'
                or value['recovery']['state'] in ('failed', 'pending')
                or bool(value['job'] and 'error' in value['job']))

    def run_once(self, job_id=None, *, recovery=True):
        heartbeat = {'state': 'not_started'}
        recovery_outcome = {'state': 'not_requested'}
        try:
            job = self.jobs.claim(self.worker_id, self.lease_seconds, job_id=job_id)
        except Exception as exc:
            self._outcome({'schema_version': 1, 'job': None, 'heartbeat': heartbeat,
                           'recovery': recovery_outcome, 'claim': {'state': 'failed', 'code': safe_code(exc)}})
            return None
        result = None
        if job:
            try:
                with self.heartbeat(job) as heartbeat:
                    if self.jobs.retry_lineage(job.profile_id, job.id):
                        raise EvidenceError('HANDLER_UNAVAILABLE', 'Paid retry execution is not available in this worker', 503)
                    if job.kind == 'index':
                        self.service().run_index(job.id, self.worker_id, job.fence)
                    elif job.kind == 'packets':
                        self.packets().run_packets(job.id, self.worker_id, job.fence)
                    elif job.kind == 'research':
                        self.research().run_research(job.id, self.worker_id, job.fence)
                    elif job.kind == 'draft':
                        self.drafting().run_draft(job.id, self.worker_id, job.fence)
                    else:
                        raise EvidenceError('HANDLER_UNAVAILABLE', 'No handler is available for this job', 503)
            except Exception as exc:
                try:
                    if job.kind == 'packets':
                        self.jobs.fail_packet(job.id, self.worker_id, job.fence)
                    elif job.kind in ('draft', 'assess'):
                        self.jobs.fail_drafting(job.id, self.worker_id, job.fence)
                    else:
                        self.jobs.finish(job.id, self.worker_id, job.fence, 'failed')
                except EvidenceError as finish_error:
                    if finish_error.code not in ('CONFLICT', 'NOT_FOUND'):
                        heartbeat = {'state': 'failed', 'code': safe_code(finish_error)}
                except Exception as finish_error:
                    heartbeat = {'state': 'failed', 'code': safe_code(finish_error)}
                result = {'job_id': job.id, 'error': safe_code(exc)}
            else:
                result = {'job_id': job.id, 'state': 'completed'}
        if recovery:
            try:
                report = self.recover()
                pending = (report['blobs'].get('cleanup_pending', 0)
                           + report['indexes'].get('cleanup_pending', 0)
                           + len(self.store.pending_cleanup()))
                recovery_outcome = {'state': 'pending', 'code': 'CLEANUP_PENDING'} if pending else {'state': 'complete'}
            except Exception as exc:
                recovery_outcome = {'state': 'failed', 'code': safe_code(exc)}
        self._outcome({'schema_version': 1, 'job': result, 'heartbeat': heartbeat, 'recovery': recovery_outcome})
        return result

    def run_job(self, job_id, wait_seconds=900):
        """Synchronous CLI joins a competing worker's durable completion, not a second build."""
        deadline = time.monotonic() + wait_seconds
        while True:
            with self.store._tx() as db:
                job, _, _ = self.jobs._job(db, job_id)
            if job.state == 'completed':
                return {'job_id': job.id, 'state': 'completed'}
            if job.state in ('failed', 'cancelled', 'indeterminate'):
                return {'job_id': job.id, 'error': 'JOB_NOT_COMPLETED'}
            if job.state == 'queued' or job.lease_expires_at <= self.jobs._now():
                result = self.run_once(job.id, recovery=False)
                if result is not None:
                    return result
            if self.stop.is_set() or time.monotonic() >= deadline:
                return {'job_id': job.id, 'error': 'JOB_STILL_RUNNING'}
            self.stop.wait(0.1)

    def run_forever(self, poll_seconds=0.5):
        with worker_lock(self.store.root):
            while not self.stop.is_set():
                self.run_once()
                self.stop.wait(poll_seconds)
