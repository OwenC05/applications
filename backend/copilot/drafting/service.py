"""Consent-gated durable generation; inspection never loads retrieval models."""
import json
import os

from ..contracts import EvidenceError
from ..domain import contracts as c
from ..domain.repository import uid
from ..jobs import Jobs, encode
from ..provider import Provider, TransmissionGuard, build_payload, conservative_reservation
from ..retrieval.packet_service import PacketService, conflict
from .pipeline import build_context, context_payload, run_pipeline
from .prompts import PROMPT_VERSION, stage_instructions
from .runtime_contracts import (
    DraftIntent,
    DraftParameters,
    DraftPreviewRequest,
    DraftPublication,
    DraftRequest,
    draft_request_hash,
)
from .schemas import PlanOutput


class DraftingService:
    def __init__(self, store, *, jobs=None, provider=None):
        self.store = store
        self.jobs = jobs or Jobs(store)
        self.domain = self.jobs.domain
        self.provider = provider or Provider(self.jobs, lambda: os.environ.get('OPENAI_API_KEY', ''))
        self.packets = PacketService(store, jobs=self.jobs)

    def _capture(self, db, owner, application_id, request, *, expected=True):
        revisions = self.jobs.capture(db, owner, application_id)
        if expected and revisions != request.expected_revisions:
            conflict('Draft revision capture changed')
        batch, publication, parameters = self.packets._association(db, owner, application_id, request.batch_id)
        if batch.batch_sha256 != request.batch_sha256 or self.packets._stale_reasons(db, batch):
            conflict('Draft packet is not current')
        packet_capture = self.packets._capture(db, parameters)
        self.packets._resolve_batch(db, batch)
        application = self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
        return application, revisions, batch, publication, packet_capture

    def _verify(self, capture):
        batch = capture[2]
        for binding, (_, values) in zip((batch.facts_generation, batch.employer_generation), capture[4][2], strict=True):
            self.store.verify_scoped_sources(binding.scope, values)

    def _disclosure(self, db, capture, request):
        limits = db.execute('SELECT daily_tokens,daily_calls,job_tokens,job_calls FROM budget_limits WHERE owner=?',
                            (capture[0].profile_id,)).fetchone()
        payload = dict(schema_version=1, selected_context=context_payload(build_context(capture[0], capture[2])),
            batch_id=request.batch_id, batch_sha256=request.batch_sha256,
            expected_revisions=request.expected_revisions.model_dump(mode='json'),
            model=request.model, max_output_tokens=request.max_output_tokens,
            prompt_version=PROMPT_VERSION, output_schema_version=1,
            consent_revision=capture[1].consent, maximum_calls=5,
            conservative_reservation_units=5 * (131072 + request.max_output_tokens),
            reservation_unit_description='Conservative UTF-8 byte input plus output ceiling; not exact tokens or money',
            configured_limits=dict(zip(('daily_tokens', 'daily_calls', 'job_tokens', 'job_calls'), limits)) if limits else None,
            monetary_cost=None, pricing_status='unknown', semantic_assessment='fallible_not_release_qualified')
        # The disclosed context must actually fit the fixed provider payload envelope.
        build_payload(model=request.model, instructions=stage_instructions('plan'),
            input_value={'context': payload['selected_context']}, output_model=PlanOutput,
            max_output_tokens=request.max_output_tokens)
        payload['selected_context_sha256'] = c.canonical_hash(payload['selected_context'])
        return payload

    def preview(self, owner, application_id, request):
        request = DraftPreviewRequest.model_validate_json(request.model_dump_json())
        with self.store._read() as db:
            capture = self._capture(db, owner, application_id, request)
            disclosure = self._disclosure(db, capture, request)
        self._verify(capture)
        with self.store._read() as db:
            if self._capture(db, owner, application_id, request) != capture or self._disclosure(db, capture, request) != disclosure:
                conflict('Draft disclosure changed during inspection')
        return dict(schema_version=1, disclosure=disclosure, disclosure_sha256=c.canonical_hash(disclosure),
                    review_eligible=False, browser_eligible=False)

    def enqueue(self, owner, application_id, request):
        request = DraftRequest.model_validate_json(request.model_dump_json())
        digest = draft_request_hash(owner, application_id, request)
        # Exact replay intentionally precedes source/currentness/disclosure checks.
        with self.store._read() as db:
            old = db.execute('SELECT job_id,request_sha256 FROM job_keys WHERE owner=? AND key=?', (owner, request.idempotency_key)).fetchone()
            if old:
                if old[1] != digest:
                    conflict('Idempotency key belongs to a different request')
                return self.jobs._job(db, old[0], owner)[0]
        preview_request = DraftPreviewRequest.model_validate_json(request.model_dump_json(exclude={'idempotency_key', 'disclosure_sha256', 'acknowledged'}))
        preview = self.preview(owner, application_id, preview_request)
        if preview['disclosure_sha256'] != request.disclosure_sha256:
            conflict('Exact drafting disclosure acknowledgement required')
        with self.store._tx() as db:
            old = db.execute('SELECT job_id,request_sha256 FROM job_keys WHERE owner=? AND key=?', (owner, request.idempotency_key)).fetchone()
            if old:
                if old[1] != digest:
                    conflict('Idempotency key belongs to a different request')
                return self.jobs._job(db, old[0], owner)[0]
            capture = self._capture(db, owner, application_id, request)
            disclosure = self._disclosure(db, capture, request)
            if c.canonical_hash(disclosure) != request.disclosure_sha256:
                conflict('Draft disclosure changed')
            limits = disclosure['configured_limits']
            if not limits:
                raise EvidenceError('BUDGET_REQUIRED', 'Configure explicit drafting reservation limits', 403)
            bound = disclosure['conservative_reservation_units']
            day = self.jobs._now().date().isoformat()
            rows = db.execute("SELECT data FROM provider_attempts WHERE owner=? AND day=? AND state!='definitely_unsent'", (owner, day)).fetchall()
            attempts = [c.ProviderAttempt.model_validate_json(row[0]) for row in rows]
            if (limits['job_tokens'] < bound or limits['job_calls'] < 5
                    or limits['daily_tokens'] < bound + sum(max(a.reserved_tokens, a.reported_tokens or 0) for a in attempts)
                    or limits['daily_calls'] < 5 + len(attempts)):
                raise EvidenceError('BUDGET_EXCEEDED', 'Configured headroom must cover conservative five-call bound', 403)
            parameters = DraftParameters(profile_id=owner, application_id=application_id, request=request, request_sha256=digest,
                context=build_context(capture[0], capture[2]), disclosure=disclosure)
            return self.jobs.enqueue(owner, 'draft', parameters.model_dump(mode='json'), request.idempotency_key,
                                     application_id, transaction=db, request_hash=digest)

    def _save_new(self, db, owner, kind, record):
        if db.execute('SELECT 1 FROM tombstones WHERE id=?', (record.id,)).fetchone() or db.execute('SELECT 1 FROM records WHERE id=?', (record.id,)).fetchone():
            conflict('Draft identifier already exists or was forgotten')
        self.domain._save(db, owner, kind, record)

    def run_draft(self, job_id, worker_id, fence):
        with self.store._tx() as db:
            job, raw = self.jobs.assert_current(db, job_id, worker_id, fence)
            if job.kind != 'draft':
                conflict('Draft handler requires draft job')
            parameters = DraftParameters.model_validate_json(encode(raw))
            request = parameters.request
            if parameters.prompt_version != PROMPT_VERSION or parameters.request_sha256 != draft_request_hash(job.profile_id, job.application_id, request):
                conflict('Draft request/version integrity mismatch')
            capture = self._capture(db, job.profile_id, job.application_id, request)
            if (parameters.context != build_context(capture[0], capture[2])
                    or c.canonical_hash(parameters.disclosure) != request.disclosure_sha256):
                conflict('Captured draft context/disclosure mismatch')
            intent = DraftIntent(**parameters.model_dump(), id=uid(), job_id=job.id, fence=fence,
                                 revisions=job.revisions, state='registered')
            self._save_new(db, job.profile_id, 'draft_intent', intent)

        def assert_job(db):
            current, current_raw = self.jobs.assert_current(db, job_id, worker_id, fence)
            if ((current.id, current.profile_id, current.application_id, current.kind, current.fence, current.revisions)
                    != (job.id, job.profile_id, job.application_id, 'draft', fence, job.revisions)
                    or DraftParameters.model_validate_json(encode(current_raw)) != parameters):
                conflict('Draft job association changed')
            return current

        def prepare():
            with self.store._read() as db:
                assert_job(db)
                certificate = self._capture(db, job.profile_id, job.application_id, request)
                if certificate != capture:
                    conflict('Draft source capture changed')
            self._verify(certificate)
            return certificate

        def validate(db, certificate):
            assert_job(db)
            if certificate != capture or self._capture(db, job.profile_id, job.application_id, request) != certificate:
                conflict('Draft canonical transmission capture changed')
            if self.domain._get(db, job.profile_id, intent.id, 'draft_intent', DraftIntent) != intent:
                conflict('Draft intent changed')

        guard = TransmissionGuard(prepare, validate)
        context = build_context(capture[0], capture[2])

        def call(stage, *, instructions, input_value, output_model):
            options = dict(model=request.model, instructions=instructions, input_value=input_value,
                           output_model=output_model, max_output_tokens=request.max_output_tokens)
            # Prepared/unknown stages are never re-entered, even if no dispatch was recorded.
            with self.store._read() as db:
                old = db.execute('SELECT state FROM provider_attempts WHERE job_id=? AND stage=?', (job_id, stage)).fetchone()
                if old and old[0] != 'completed':
                    raise EvidenceError('PROVIDER_UNKNOWN', 'Resolve previous provider stage explicitly', 409)
            output = self.provider.send(job_id, worker_id, fence, stage, **options,
                reserved_tokens=conservative_reservation(**options), guard=guard)
            self.jobs.commit_stage(job_id, worker_id, fence, 'draft_stage_' + stage,
                                   dict(stage=stage, output_sha256=c.canonical_hash(output)))
            return output

        result = run_pipeline(context, call)
        texts = {item.target: item.text for item in result.draft.targets}
        post = job.revisions.model_copy(update={'application_output': job.revisions.application_output + 1})
        draft = c.DraftRevision(id=uid(), profile_id=job.profile_id, application_id=job.application_id,
            revisions=post, research_run_id=context.research_run_id, cover_letter=texts.get('cover_letter', ''),
            answers=tuple(c.DraftAnswer(question_id=t.question.id, text=texts[t.question.id], packet_id=t.packet_id)
                          for t in context.targets if t.question.id != 'cover_letter'),
            ledger=result.quality.ledger, assessment_state='assessed', inventory_complete=result.quality.inventory_complete,
            created_at=self.jobs._now())
        publication = DraftPublication(id=uid(), profile_id=job.profile_id, application_id=job.application_id,
            draft_id=draft.id, intent_id=intent.id, job_id=job.id, fence=fence, batch_id=request.batch_id,
            batch_sha256=request.batch_sha256, text_sha256=draft.text_sha256, ledger_sha256=draft.ledger_sha256,
            request_sha256=parameters.request_sha256, disclosure_sha256=request.disclosure_sha256,
            prompt_version=PROMPT_VERSION, stage_names=result.stage_names, reason_codes=result.quality.reason_codes,
            style_notes=result.quality.style_notes)
        certificate = prepare()

        def publish(db, current, summary):
            validate(db, certificate)
            if current != job:
                # Stage changes are expected; authority/revisions/fence are not.
                if (current.id, current.fence, current.revisions) != (job.id, fence, job.revisions):
                    conflict('Draft publication job changed')
            self._save_new(db, job.profile_id, 'draft', draft)
            self._save_new(db, job.profile_id, 'draft_dependency', publication)
            self.domain._save(db, job.profile_id, 'draft_intent', intent.model_copy(update={'state': 'committed'}))
            self.domain._save(db, job.profile_id, 'application', capture[0].model_copy(update={'output_revision': post.application_output}))

        self.jobs.commit_stage(job_id, worker_id, fence, 'draft_published', self._summary(draft, publication), publish=publish, terminal=True)
        return draft

    def _summary(self, draft, publication):
        return dict(draft_id=draft.id, text_sha256=draft.text_sha256, ledger_sha256=draft.ledger_sha256,
                    publication_id=publication.id, publication_sha256=publication.publication_sha256,
                    intent_id=publication.intent_id)

    def edit(self, owner, application_id, parent_id, request):
        from .edits import DraftEdits
        return DraftEdits(self).edit(owner, application_id, parent_id, request)

    def export(self, owner, application_id, draft_id):
        inspection = self.detail(owner, application_id, draft_id)
        draft = inspection['draft']
        questions = {item['id']: item['text'] for item in inspection['canonical_questions']}
        parts = ['Application draft — export is not review or submission approval.']
        if draft['cover_letter']:
            parts.append('Cover letter\n' + draft['cover_letter'])
        for answer in draft['answers']:
            if answer['question_id'] not in questions:
                conflict('Draft export question association unavailable')
            parts.append(questions[answer['question_id']] + '\n' + answer['text'])
        return '\n\n'.join(parts)

    def _association(self, db, owner, application_id, draft_id, *, visited=()):
        if draft_id in visited or len(visited) > 64:
            conflict('Draft lineage is cyclic or exceeds bounds')
        self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
        draft = self.domain._get(db, owner, draft_id, 'draft', c.DraftRevision)
        if (draft.id, draft.profile_id, draft.application_id) != (draft_id, owner, application_id):
            raise EvidenceError('NOT_FOUND', 'Draft not found', 404)
        rows = db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:draft_dependency' AND json_extract(data,'$.draft_id')=?", (owner, draft_id)).fetchall()
        if len(rows) != 1:
            conflict('Draft publication association unavailable')
        raw = json.loads(rows[0][1])
        if not isinstance(raw, dict):
            conflict('Draft publication schema mismatch')
        if 'origin' not in raw:
            return self._generation_association(db, owner, application_id, draft_id)
        if raw['origin'] != 'edit' or len(visited) >= 64:
            conflict('Draft publication origin or lineage exceeds bounds')
        from .edit_contracts import EditPublication
        from .edits import DraftEdits
        publication = EditPublication.model_validate_json(rows[0][1])
        if publication.id != rows[0][0]:
            conflict('Draft publication identity mismatch')
        return DraftEdits(self).association(db, owner, application_id, draft_id,
                                           publication, visited + (draft_id,))

    def _generation_association(self, db, owner, application_id, draft_id):
        self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
        draft = self.domain._get(db, owner, draft_id, 'draft', c.DraftRevision)
        if (draft.id, draft.profile_id, draft.application_id) != (draft_id, owner, application_id):
            raise EvidenceError('NOT_FOUND', 'Draft not found', 404)
        rows = db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:draft_dependency' AND json_extract(data,'$.draft_id')=?", (owner, draft_id)).fetchall()
        if len(rows) != 1:
            conflict('Draft publication association unavailable')
        publication = DraftPublication.model_validate_json(rows[0][1])
        intent = self.domain._get(db, owner, publication.intent_id, 'draft_intent', DraftIntent)
        job, raw, dependencies = self.jobs._job(db, publication.job_id, owner)
        parameters = DraftParameters.model_validate_json(encode(raw))
        row = db.execute('SELECT fence,data,sha256 FROM job_stages WHERE job_id=? AND stage=?', (job.id, 'draft_published')).fetchone()
        summary = self._summary(draft, publication)
        pre = draft.revisions.model_copy(update={'application_output': draft.revisions.application_output - 1})
        batch, _, _ = self.packets._association(db, owner, application_id, publication.batch_id)
        expected_stages = ('plan', 'draft', 'critique')
        if publication.stage_names not in (expected_stages, expected_stages + ('rewrite', 'final_critique')):
            conflict('Draft stage history mismatch')
        if (publication.id != rows[0][0] or (publication.profile_id, publication.application_id) != (owner, application_id)
                or publication.text_sha256 != draft.text_sha256 or publication.ledger_sha256 != draft.ledger_sha256
                or publication.batch_sha256 != batch.batch_sha256 or batch.research_run_id != draft.research_run_id
                or (intent.profile_id, intent.application_id, intent.job_id, intent.fence, intent.revisions, intent.state)
                != (owner, application_id, job.id, job.fence, pre, 'committed')
                or DraftParameters.model_validate_json(intent.model_dump_json(exclude={'id', 'job_id', 'fence', 'revisions', 'state'})) != parameters
                or job.kind != 'draft' or job.state != 'completed' or job.stage != 'draft_published'
                or db.execute('SELECT state FROM jobs WHERE id=?', (job.id,)).fetchone() != ('completed',)
                or job.cancellation_requested or job.revisions != pre or job.fence != publication.fence
                or (job.profile_id, job.application_id) != (owner, application_id)
                or parameters.request.expected_revisions != pre
                or parameters.request_sha256 != draft_request_hash(owner, application_id, parameters.request)
                or (publication.request_sha256, publication.disclosure_sha256, publication.batch_id, publication.batch_sha256, publication.prompt_version)
                != (parameters.request_sha256, parameters.request.disclosure_sha256, parameters.request.batch_id, parameters.request.batch_sha256, parameters.prompt_version)
                or row is None or row[0] != job.fence or json.loads(row[1]) != summary or row[2] != c.text_hash(encode(summary))
                or dependencies != ('metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research')):
            conflict('Draft publication integrity mismatch')
        for stage in publication.stage_names:
            stored = db.execute('SELECT data,sha256 FROM job_stages WHERE job_id=? AND stage=?', (job.id, 'draft_stage_' + stage)).fetchone()
            attempt = db.execute('SELECT state,response FROM provider_attempts WHERE job_id=? AND stage=?', (job.id, stage)).fetchone()
            if (not stored or not attempt or attempt[0] != 'completed' or not attempt[1]
                    or stored[1] != c.text_hash(stored[0])
                    or json.loads(stored[0]) != dict(stage=stage, output_sha256=c.canonical_hash(json.loads(attempt[1])))):
                conflict('Draft completed stage integrity mismatch')
        context = parameters.context
        historical_application = self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord).model_copy(update={
            'company': context.company, 'role': context.role, 'sector': context.sector,
            'questions': batch.canonical_questions, 'input_revision': batch.revisions.application_input})
        if (build_context(historical_application, batch) != context
                or context.batch_id != batch.id or context.batch_sha256 != batch.batch_sha256
                or context.profile_id != owner or context.application_id != application_id
                or c.canonical_hash(parameters.disclosure) != parameters.request.disclosure_sha256
                or parameters.disclosure.get('selected_context') != context_payload(context)
                or parameters.disclosure.get('selected_context_sha256') != c.canonical_hash(context_payload(context))):
            conflict('Draft frozen context/disclosure integrity mismatch')

        def replay(stage, *, instructions, input_value, output_model):
            stored = db.execute('SELECT state,response,request_sha256 FROM provider_attempts WHERE job_id=? AND stage=?', (job.id, stage)).fetchone()
            authority_row = db.execute('SELECT owner,job_id,data FROM provider_attempts WHERE job_id=? AND stage=?', (job.id, stage)).fetchone()
            authority = c.ProviderAttempt.model_validate_json(authority_row[2]) if authority_row else None
            if (authority is None or authority_row[:2] != (owner, job.id)
                    or (authority.profile_id, authority.application_id, authority.job_id,
                        authority.model_version, authority.consent_revision, authority.state)
                    != (owner, application_id, job.id, parameters.request.model, pre.consent, 'completed')
                    or not 1 <= authority.fence <= job.fence):
                conflict('Draft provider stage authority mismatch')
            payload = build_payload(model=parameters.request.model, instructions=instructions,
                input_value=input_value, output_model=output_model,
                max_output_tokens=parameters.request.max_output_tokens)
            if not stored or stored[0] != 'completed' or stored[2] != c.canonical_hash(payload):
                conflict('Draft stage payload association mismatch')
            return output_model.model_validate_json(stored[1])

        replayed = run_pipeline(context, replay)
        if (replayed.stage_names != publication.stage_names
                or replayed.quality.ledger != draft.ledger
                or replayed.quality.inventory_complete != draft.inventory_complete
                or replayed.quality.reason_codes != publication.reason_codes
                or replayed.quality.style_notes != publication.style_notes
                or draft.assessment_state != 'assessed'
                or tuple((item.target, item.text) for item in replayed.draft.targets)
                != tuple((t.question.id, draft.cover_letter if t.question.id == 'cover_letter' else
                          next((a.text for a in draft.answers if a.question_id == t.question.id), None)) for t in context.targets)
                or tuple((a.question_id, a.packet_id) for a in draft.answers)
                != tuple((t.question.id, t.packet_id) for t in context.targets if t.question.id != 'cover_letter')):
            conflict('Draft final text/assessment association mismatch')
        return draft, publication, parameters, batch

    def detail(self, owner, application_id, draft_id):
        try:
            return self._inspect(owner, application_id, draft_id)
        except ValueError:
            conflict('Draft stored schema or integrity mismatch')

    def _inspect(self, owner, application_id, draft_id):
        with self.store._read() as db:
            draft, publication, parameters, batch = self._association(db, owner, application_id, draft_id)
            reasons = list(self.packets._stale_reasons(db, batch))
            if self.jobs.capture(db, owner, application_id) != draft.revisions:
                reasons.append('revision_changed')
            capture = None if reasons else self._capture(db, owner, application_id, parameters.request, expected=False)
        if capture is not None:
            self._verify(capture)
            with self.store._read() as db:
                if self._association(db, owner, application_id, draft_id) != (draft, publication, parameters, batch):
                    conflict('Draft publication changed during inspection')
                if self._capture(db, owner, application_id, parameters.request, expected=False) != capture:
                    conflict('Draft source capture changed during inspection')
        return dict(schema_version=1, draft=draft.model_dump(mode='json'), publication=publication.model_dump(mode='json'),
            text_sha256=draft.text_sha256, ledger_sha256=draft.ledger_sha256, current=not reasons,
            reason_codes=tuple(dict.fromkeys(reasons + list(publication.reason_codes))),
            canonical_questions=[q.model_dump(mode='json') for q in batch.canonical_questions],
            manual_requirements=[m.model_dump(mode='json') for m in batch.manual_requirements],
            semantic_assessment='fallible_not_release_qualified', review_eligible=False, browser_eligible=False,
            capabilities=dict(generation=True, edit=True, reassess=False, review=False))

    def list(self, owner, application_id):
        with self.store._read() as db:
            self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
            ids = [row[0] for row in db.execute("SELECT id FROM records WHERE owner=? AND kind='workspace:draft' AND json_extract(data,'$.application_id')=? ORDER BY rowid", (owner, application_id))]
        return dict(schema_version=1, drafts=[self.detail(owner, application_id, item) for item in ids])
