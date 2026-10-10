"""Unpaid durable packet runtime; only canonical SQLite publication is atomic.

Model/file work is outside writers. Inspection needs no retrieval service or model.
Current means canonical dependencies, never semantic support or index readiness.
"""
import json

from ..contracts import EvidenceError, decode_chunk
from ..domain import contracts as c
from ..domain.repository import uid
from ..jobs import Jobs, encode
from .packet_contracts import CorpusScope, GenerationBinding, PacketBatch
from .packet_runtime_contracts import (
    BinderOmission,
    PacketIntent,
    PacketParameters,
    PacketPublication,
    PacketReport,
    PacketRequest,
    packet_request_hash,
)


def conflict(message):
    raise EvidenceError('CONFLICT', message, 409)


class PacketService:
    def __init__(self, store, evidence=None, *, jobs=None):
        self.store, self.evidence = store, evidence
        self.jobs = jobs or Jobs(store)
        self.domain = self.jobs.domain

    def _binding(self, db, scope):
        manifest = self.store.active_scoped_manifest(db, scope, self.jobs._now())
        if manifest is None:
            raise EvidenceError('INDEX_NOT_READY', 'Build both packet corpus indexes first', 503)
        binding = GenerationBinding(generation_id=manifest.generation_id, scope=scope,
            revision=manifest.revision, model_fingerprint=manifest.model_fingerprint,
            chunker_version=manifest.chunker_version, chunk_checksum=manifest.chunk_ids_sha256)
        if not db.execute('SELECT 1 FROM generation_intents WHERE id=?', (binding.generation_id,)).fetchone():
            conflict('Packets require registered generations')
        self.store.require_generation_binding(db, binding, self.jobs._now())
        return binding

    def enqueue(self, owner, application_id, run_id, request):
        request = PacketRequest.model_validate_json(request.model_dump_json())
        digest = packet_request_hash(owner, application_id, run_id, request)
        with self.store._tx() as db:
            old = db.execute('SELECT job_id,request_sha256 FROM job_keys WHERE owner=? AND key=?',
                             (owner, request.idempotency_key)).fetchone()
            if old:
                if old[1] != digest:
                    conflict('Idempotency key belongs to a different request')
                return self.jobs._job(db, old[0], owner)[0]
            parameters = PacketParameters(profile_id=owner, application_id=application_id,
                research_run_id=run_id, request=request, request_sha256=digest,
                facts_generation=self._binding(db, CorpusScope(profile_id=owner, corpus='facts')),
                employer_generation=self._binding(db, CorpusScope(profile_id=owner, corpus='employer',
                    application_id=application_id, research_run_id=run_id)))
            return self.jobs.enqueue(owner, 'packets', parameters.model_dump(mode='json'),
                request.idempotency_key, application_id, transaction=db, request_hash=digest)

    def _capture(self, db, parameters):
        revisions = self.jobs.capture(db, parameters.profile_id, parameters.application_id)
        self.jobs._packet_current(db, parameters, revisions)
        app = self.domain._get(db, parameters.profile_id, parameters.application_id,
                               'application', c.ApplicationRecord)
        sources = tuple(self.store.capture_scoped_sources(db, binding.scope, self.jobs._now())
                        for binding in (parameters.facts_generation, parameters.employer_generation))
        # Metadata, consent, documents and output are deliberately not packet dependencies.
        app = app.model_copy(update={'output_revision': 0})
        revisions = c.RevisionVector(facts=revisions.facts, application_input=revisions.application_input,
                                     research=revisions.research)
        return app, revisions, sources

    def _save_new(self, db, owner, kind, record):
        if db.execute('SELECT 1 FROM tombstones WHERE id=?', (record.id,)).fetchone():
            conflict('Packet identifier was forgotten')
        if db.execute('SELECT 1 FROM records WHERE id=?', (record.id,)).fetchone():
            conflict('Packet identifier already exists')
        self.domain._save(db, owner, kind, record)

    def _criteria(self, db, parameters, employer_values):
        from ..research.criteria import compile_excerpt_criteria
        compilation = compile_excerpt_criteria(parameters.employer_generation.scope, employer_values,
            tuple(selection.model_dump() for selection in parameters.request.selections))
        chunks = [decode_chunk(json.loads(row[0])) for row in db.execute(
            'SELECT data FROM generation_chunks WHERE generation_id=? AND owner=?',
            (parameters.employer_generation.generation_id, parameters.profile_id))]
        criteria, omissions = [], []
        for candidate in compilation.candidates:
            span = candidate.span
            fits = any(chunk.record_id == span.record_id and chunk.unit_id == span.unit_id
                       and chunk.start <= span.start < span.end <= chunk.end for chunk in chunks)
            if not fits:
                indices = tuple(i for i, item in enumerate(parameters.request.selections)
                    if (item.source_id, item.unit_id, item.start, item.end)
                    == (span.record_id, span.unit_id, span.start, span.end))
                omissions.append(BinderOmission(candidate_id=candidate.id, selection_indices=indices,
                                                reason='not_in_generation_chunk'))
                continue
            reference = c.EvidenceReference(profile_id=parameters.profile_id, kind='employer',
                application_id=parameters.application_id, research_run_id=parameters.research_run_id,
                generation_id=parameters.employer_generation.generation_id, span=span)
            self.store.resolve_evidence_reference(db, parameters.employer_generation, reference, self.jobs._now())
            criteria.append(c.HiringCriterion(id=candidate.id, text=candidate.text, inferred=False,
                                             evidence=(reference,)))
        report = PacketReport.model_validate_json(encode(dict(
            compiler_version=compilation.compiler_version,
            candidate_ids=[item.id for item in compilation.candidates],
            compiler_omissions=[item.model_dump(mode='json') for item in compilation.omissions],
            compiler_gaps=compilation.gaps,
            binder_omissions=[item.model_dump(mode='json') for item in omissions])))
        return tuple(criteria), report

    def _resolve_batch(self, db, batch):
        for binding, references in (
            (batch.facts_generation, tuple(ref for packet in batch.packets for ref in packet.facts)),
            (batch.employer_generation, tuple(ref for packet in batch.packets for ref in packet.employer)
             + tuple(ref for criterion in batch.hiring_criteria for ref in criterion.evidence)),
        ):
            for reference in references:
                self.store.resolve_evidence_reference(db, binding, reference, self.jobs._now())

    def run_packets(self, job_id, worker_id, fence):
        with self.store._tx() as db:
            job, raw = self.jobs.assert_current(db, job_id, worker_id, fence)
            if job.kind != 'packets':
                conflict('Packet handler requires packet job')
            parameters = PacketParameters.model_validate_json(encode(raw))
            capture = self._capture(db, parameters)
            intent = PacketIntent(**parameters.model_dump(), id=uid(), job_id=job.id, fence=fence,
                                  revisions=job.revisions, state='registered')
            self._save_new(db, job.profile_id, 'packet_intent', intent)
        try:
            return self._compose(job, parameters, intent, capture, worker_id)
        except BaseException:
            # DB-only failure history. Never recreate a forgotten intent or grant a
            # reclaimed fence authority; only this exact registered row is changed.
            with self.store._tx() as db:
                row = db.execute("SELECT data FROM records WHERE id=? AND owner=? AND kind='workspace:packet_intent'",
                                 (intent.id, job.profile_id)).fetchone()
                if row and PacketIntent.model_validate_json(row[0]) == intent:
                    self.domain._save(db, job.profile_id, 'packet_intent', intent.model_copy(update={'state': 'failed'}))
            raise

    def _compose(self, job, parameters, intent, capture, worker_id):
        from .packets import compose_batch
        if self.evidence is None:
            raise EvidenceError('HANDLER_UNAVAILABLE', 'Packet retrieval service unavailable', 503)
        application, _, sources = capture
        revisions = job.revisions
        bindings = (parameters.facts_generation, parameters.employer_generation)

        def guard():
            with self.store._read() as db:
                self.jobs.assert_current(db, job.id, worker_id, job.fence)
                if self._capture(db, parameters) != capture:
                    conflict('Canonical packet capture changed')

        for binding, (_, values) in zip(bindings, sources, strict=True):
            self.store.verify_scoped_sources(binding.scope, values)
        guard()
        fingerprint = self.evidence.models.fingerprint
        if any(binding.model_fingerprint != fingerprint for binding in bindings):
            conflict('Loaded packet model does not match both captured generations')
        with self.store._read() as db:
            criteria, report = self._criteria(db, parameters, sources[1][1])
        guard()
        batch = compose_batch(application=application, research_run_id=parameters.research_run_id,
            job_id=job.id, fence=job.fence, revisions=revisions,
            facts_generation=bindings[0], employer_generation=bindings[1], hiring_criteria=criteria,
            tokenize=self.evidence.models.tokenize_offsets, tokenizer_version='local-retrieval-offsets-v1',
            tokenizer_fingerprint=fingerprint, search_scoped=self.evidence.search_scoped,
            currentness_guard=guard, created_at=self.jobs._now(),
            per_packet_budget=parameters.request.per_packet_budget,
            aggregate_budget=parameters.request.aggregate_budget,
            cover_letter_target=parameters.request.cover_letter_target)
        publication = PacketPublication(id=uid(), profile_id=job.profile_id,
            application_id=job.application_id, batch_id=batch.id, batch_sha256=batch.batch_sha256,
            intent_id=intent.id, job_id=job.id, fence=job.fence,
            request_sha256=parameters.request_sha256, compiler_version=batch.compiler_version,
            selection_version=batch.selection_version, report=report)
        # Final IO/model identity checks precede the writer. Inside it only compare
        # this verified fingerprint/capture and resolve canonical SQLite references.
        for binding, (_, values) in zip(bindings, sources, strict=True):
            self.store.verify_scoped_sources(binding.scope, values)
        verified_fingerprint = self.evidence.models.fingerprint
        guard()

        def publish(db, current_job, result):
            if (current_job.id, current_job.profile_id, current_job.application_id, current_job.fence, current_job.revisions) != (job.id, job.profile_id, job.application_id, job.fence, job.revisions):
                conflict('Packet job changed')
            if any(binding.model_fingerprint != verified_fingerprint for binding in bindings):
                conflict('Packet model fingerprint changed')
            if self._capture(db, parameters) != capture:
                conflict('Final canonical packet capture changed')
            stored = self.domain._get(db, job.profile_id, intent.id, 'packet_intent', PacketIntent)
            if stored != intent or current_job.revisions != intent.revisions:
                conflict('Packet intent authority changed')
            self._resolve_batch(db, batch)
            self._save_new(db, job.profile_id, 'packet_batch', batch)
            self._save_new(db, job.profile_id, 'packet_dependency', publication)
            self.domain._save(db, job.profile_id, 'packet_intent', intent.model_copy(update={'state': 'committed'}))

        result = dict(batch_id=batch.id, batch_sha256=batch.batch_sha256,
                      publication_id=publication.id, publication_sha256=publication.publication_sha256,
                      intent_id=intent.id)
        self.jobs.commit_stage(job.id, worker_id, job.fence, 'packets_published', result,
                               publish=publish, terminal=True)
        return batch

    def _association(self, db, owner, application_id, batch_id):
        self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
        batch = self.domain._get(db, owner, batch_id, 'packet_batch', PacketBatch)
        if batch.id != batch_id:
            conflict('Packet stored identifier mismatch')
        if batch.application_id != application_id or batch.profile_id != owner:
            raise EvidenceError('NOT_FOUND', 'Packet batch not found', 404)
        rows = db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:packet_dependency' AND json_extract(data,'$.batch_id')=?",
                          (owner, batch.id)).fetchall()
        if len(rows) != 1:
            conflict('Packet publication association is unavailable')
        publication = PacketPublication.model_validate_json(rows[0][1])
        if publication.id != rows[0][0]:
            conflict('Packet publication stored identifier mismatch')
        intent = self.domain._get(db, owner, publication.intent_id, 'packet_intent', PacketIntent)
        job, raw, dependencies = self.jobs._job(db, publication.job_id, owner)
        parameters = PacketParameters.model_validate_json(encode(raw))
        row = db.execute('SELECT fence,data,sha256 FROM job_stages WHERE job_id=? AND stage=?',
                         (job.id, 'packets_published')).fetchone()
        expected = dict(batch_id=batch.id, batch_sha256=batch.batch_sha256,
                        publication_id=publication.id, publication_sha256=publication.publication_sha256,
                        intent_id=intent.id)
        if (publication.batch_sha256 != batch.batch_sha256
                or (publication.profile_id, publication.application_id, publication.job_id, publication.fence,
                    publication.compiler_version, publication.selection_version)
                != (owner, application_id, batch.job_id, batch.fence, batch.compiler_version, batch.selection_version)
                or intent.state != 'committed' or intent.job_id != job.id or intent.fence != job.fence
                or intent.revisions != batch.revisions or job.revisions != batch.revisions
                or db.execute('SELECT state FROM jobs WHERE id=?', (job.id,)).fetchone() != ('completed',)
                or job.kind != 'packets' or job.state != 'completed' or job.stage != 'packets_published'
                or job.cancellation_requested or job.fence != batch.fence
                or (job.profile_id, job.application_id) != (owner, application_id)
                or dependencies != ('facts', 'application_input', 'research')
                or PacketParameters.model_validate_json(intent.model_dump_json(exclude={'id', 'job_id', 'fence', 'revisions', 'state'})) != parameters
                or (batch.facts_generation, batch.employer_generation, batch.research_run_id)
                != (parameters.facts_generation, parameters.employer_generation, parameters.research_run_id)
                or (batch.per_packet_budget, batch.aggregate_budget, batch.cover_letter_target)
                != (parameters.request.per_packet_budget, parameters.request.aggregate_budget, parameters.request.cover_letter_target)
                or publication.request_sha256 != parameters.request_sha256
                or row is None or row[0] != batch.fence or json.loads(row[1]) != expected
                or row[2] != c.text_hash(encode(expected))):
            conflict('Packet publication integrity mismatch')
        return batch, publication, parameters

    def _stale_reasons(self, db, batch):
        revisions = self.jobs.capture(db, batch.profile_id, batch.application_id)
        reasons = []
        if not batch.revisions.matches(revisions, batch.dependencies):
            reasons.append('revision_changed')
        head = db.execute('SELECT run_id FROM research_heads WHERE owner=? AND application_id=?',
                          (batch.profile_id, batch.application_id)).fetchone()
        if head != (batch.research_run_id,):
            reasons.append('research_head_changed')
        run = self.domain._get(db, batch.profile_id, batch.research_run_id, 'research_run', c.ResearchRun)
        if run.state != 'complete':
            reasons.append('research_not_complete')
        if self.jobs._now() >= run.expires_at:
            reasons.append('research_expired')
        for binding in (batch.facts_generation, batch.employer_generation):
            rows = db.execute("SELECT data FROM manifests WHERE owner=? AND corpus=? AND state='active'",
                              (batch.profile_id, binding.scope.corpus)).fetchall()
            manifests = [json.loads(row[0]) for row in rows]
            if binding.scope.corpus == 'employer':
                manifests = [m for m in manifests if (m.get('application_id'), m.get('research_run_id'))
                             == (batch.application_id, batch.research_run_id)]
            if len(manifests) > 1:
                conflict('Ambiguous packet generation graph')
            if not manifests or manifests[0]['generation_id'] != binding.generation_id:
                reasons.append('generation_changed')
        return tuple(dict.fromkeys(reasons))

    def detail(self, owner, application_id, batch_id):
        try:
            return self._inspect(owner, application_id, batch_id)
        except ValueError:
            conflict('Packet stored schema or integrity mismatch')

    def _inspect(self, owner, application_id, batch_id):
        with self.store._read() as db:
            batch, publication, parameters = self._association(db, owner, application_id, batch_id)
            reasons = self._stale_reasons(db, batch)
            capture = None if reasons else self._capture(db, parameters)
            if capture is not None:
                self._resolve_batch(db, batch)
        if capture is not None:
            for binding, (_, values) in zip((batch.facts_generation, batch.employer_generation), capture[2], strict=True):
                self.store.verify_scoped_sources(binding.scope, values)
            with self.store._read() as db:
                again = self._association(db, owner, application_id, batch_id)
                if again[:2] != (batch, publication):
                    conflict('Packet publication changed during inspection')
                reasons = self._stale_reasons(db, batch)
                if not reasons and self._capture(db, parameters) != capture:
                    conflict('Packet sources changed during inspection')
        return dict(schema_version=1, batch=batch.model_dump(mode='json'),
                    batch_sha256=batch.batch_sha256, publication=publication.model_dump(mode='json'),
                    publication_sha256=publication.publication_sha256, current=not reasons,
                    reason_codes=reasons, semantic_support='not_assessed',
                    review_eligible=False, browser_eligible=False)

    def list(self, owner, application_id):
        with self.store._read() as db:
            self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
            ids = [row[0] for row in db.execute("SELECT id FROM records WHERE owner=? AND kind='workspace:packet_batch' AND json_extract(data,'$.application_id')=? ORDER BY rowid",
                                               (owner, application_id))]
        return dict(schema_version=1, batches=[self.detail(owner, application_id, id) for id in ids])

    def question(self, owner, application_id, batch_id, question_id):
        result = self.detail(owner, application_id, batch_id)
        batch = PacketBatch.model_validate_json(encode(result['batch']))
        packet = next((p for p in batch.packets if p.question.id == question_id), None)
        manual = next((m for m in batch.manual_requirements if m.question_id == question_id), None)
        if packet is None and manual is None:
            raise EvidenceError('NOT_FOUND', 'Packet question not found', 404)
        targets = batch.canonical_questions + ((batch.cover_letter_target,) if batch.cover_letter_target else ())
        question = next(item for item in targets if item.id == question_id)
        return dict(schema_version=1, batch_id=batch.id, batch_sha256=batch.batch_sha256,
                    question=question.model_dump(mode='json'),
                    packet=packet.model_dump(mode='json') if packet else None,
                    manual_requirement=manual.model_dump(mode='json') if manual else None,
                    **{key: result[key] for key in ('current', 'reason_codes', 'semantic_support',
                                                    'review_eligible', 'browser_eligible')})
