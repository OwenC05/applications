"""Durable one-call assessment of an immutable unassessed local edit."""
import json

from ..contracts import EvidenceError
from ..domain import contracts as c
from ..domain.repository import uid
from ..jobs import encode
from ..provider import TransmissionGuard, build_payload, conservative_reservation
from ..retrieval.packet_service import conflict
from .assessment_contracts import (
    AssessmentIntent,
    AssessmentParameters,
    AssessmentPreviewRequest,
    AssessmentPublication,
    AssessmentRequest,
    assessment_request_hash,
)
from .edit_contracts import EditPublication
from .pipeline import context_payload, reassess
from .prompts import PROMPT_VERSION, stage_instructions
from .quality import validated
from .schemas import CritiqueOutput, DraftOutput, GeneratedText


class DraftAssessments:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.jobs = service.jobs
        self.domain = service.domain

    def _parent(self, db, owner, app, parent_id, request, *, visited=()):
        association = self.service._association(db, owner, app, parent_id, visited=visited)
        parent, publication, parameters, batch = association
        if (not isinstance(publication, EditPublication) or parent.assessment_state != 'unassessed'
                or parent.ledger or parent.inventory_complete
                or request.expected_revisions != parent.revisions
                or (request.parent_text_sha256, request.parent_ledger_sha256, request.parent_publication_sha256)
                != (parent.text_sha256, parent.ledger_sha256, publication.publication_sha256)):
            conflict('Assessment requires the exact unassessed local edit')
        output = DraftOutput(targets=tuple(GeneratedText(target=t.question.id,
            text=parent.cover_letter if t.question.id == 'cover_letter' else
            next(a.text for a in parent.answers if a.question_id == t.question.id)) for t in parameters.context.targets))
        return association, output

    def _capture(self, db, owner, app, parent_id, request):
        parent, output = self._parent(db, owner, app, parent_id, request)
        if self.jobs.capture(db, owner, app) != parent[0].revisions:
            conflict('Assessment parent is no longer current')
        # Counting the creation link prevents publishing an unreadable history.
        self._lineage(db, owner, app, parent_id)
        source = self.service._capture(db, owner, app, parent[2].request, expected=False)
        return parent, output, source

    def _lineage(self, db, owner, app, draft_id):
        seen = set()
        while True:
            if draft_id in seen:
                conflict('Assessment lineage is cyclic')
            seen.add(draft_id)
            rows = db.execute("SELECT data FROM records WHERE owner=? AND kind='workspace:draft_dependency' AND json_extract(data,'$.draft_id')=?", (owner, draft_id)).fetchall()
            if len(rows) != 1:
                conflict('Assessment lineage association unavailable')
            value = json.loads(rows[0][0])
            if 'origin' not in value:
                return
            if len(seen) >= 128:
                conflict('Assessment lineage limit reached')
            draft_id = value['parent_draft_id']

    def _disclosure(self, context, output, request, limits):
        value = dict(schema_version=1, selected_context=context_payload(context),
            input_draft=output.model_dump(mode='json'), parent_text_sha256=request.parent_text_sha256,
            parent_ledger_sha256=request.parent_ledger_sha256,
            parent_publication_sha256=request.parent_publication_sha256,
            batch_id=context.batch_id, batch_sha256=context.batch_sha256,
            expected_revisions=request.expected_revisions.model_dump(mode='json'),
            model=request.model, max_output_tokens=request.max_output_tokens,
            prompt_version=PROMPT_VERSION, output_schema_version=1,
            consent_revision=request.expected_revisions.consent, maximum_calls=1,
            conservative_reservation_units=131072 + request.max_output_tokens,
            reservation_unit_description='Conservative UTF-8 byte input plus output ceiling; not exact tokens or money',
            configured_limits=limits, monetary_cost=None, pricing_status='unknown',
            semantic_assessment='fallible_not_release_qualified')
        value['selected_context_sha256'] = c.canonical_hash(value['selected_context'])
        build_payload(model=request.model, instructions=stage_instructions('reassess'),
            input_value=dict(context=value['selected_context'], draft=value['input_draft'],
                text_sha256=request.parent_text_sha256), output_model=CritiqueOutput,
            max_output_tokens=request.max_output_tokens)
        return value

    def _limits(self, db, owner):
        row = db.execute('SELECT daily_tokens,daily_calls,job_tokens,job_calls FROM budget_limits WHERE owner=?', (owner,)).fetchone()
        return dict(zip(('daily_tokens', 'daily_calls', 'job_tokens', 'job_calls'), row)) if row else None

    def _request(self, request, model):
        if type(request) is not model:
            conflict('Strict assessment request required')
        # JSON serializers can coerce invalid copied scalars (True -> 1).
        # Preserve Python scalar types until strict validation has succeeded.
        try:
            return model.model_validate(request.model_dump(mode='python', warnings='error'))
        except (ValueError, TypeError):
            conflict('Strict assessment request required')

    def preview(self, owner, app, parent_id, request):
        try:
            return self._preview(owner, app, parent_id, request)
        except ValueError:
            conflict('Assessment stored schema or integrity mismatch')

    def _preview(self, owner, app, parent_id, request):
        request = self._request(request, AssessmentPreviewRequest)
        with self.store._read() as db:
            capture = self._capture(db, owner, app, parent_id, request)
            disclosure = self._disclosure(capture[0][2].context, capture[1], request, self._limits(db, owner))
        self.service._verify(capture[2])
        with self.store._read() as db:
            if (self._capture(db, owner, app, parent_id, request) != capture
                    or self._disclosure(capture[0][2].context, capture[1], request, self._limits(db, owner)) != disclosure):
                conflict('Assessment disclosure changed during inspection')
        return dict(schema_version=1, disclosure=disclosure, disclosure_sha256=c.canonical_hash(disclosure),
            review_eligible=False, browser_eligible=False)

    def _parameters(self, db, job, raw, *, parent_capture=None):
        parameters = AssessmentParameters.model_validate_json(encode(raw))
        request = parameters.request
        parent, output = parent_capture if parent_capture is not None else self._parent(db, job.profile_id, job.application_id, parameters.parent_draft_id, request)
        if (parameters.parent_draft_id != parent[0].id
                or job.idempotency_key != request.idempotency_key or job.kind != 'assess' or (parameters.profile_id, parameters.application_id) != (job.profile_id, job.application_id)
                or job.revisions != request.expected_revisions
                or parameters.request_sha256 != assessment_request_hash(job.profile_id, job.application_id, parameters.parent_draft_id, request)
                or parameters.source_request != parent[2].request or parameters.context != parent[2].context
                or parameters.input_draft != output
                or (parameters.batch_id, parameters.batch_sha256) != (parent[3].id, parent[3].batch_sha256)
                or parameters.prompt_version != PROMPT_VERSION
                or parameters.disclosure != self._disclosure(parameters.context, output, request, parameters.disclosure.get('configured_limits'))
                or c.canonical_hash(parameters.disclosure) != request.disclosure_sha256):
            conflict('Assessment captured parameters mismatch')
        return parameters, parent

    def _replay(self, db, owner, app, parent_id, request, digest):
        row = db.execute('SELECT job_id,request_sha256,application_id,kind FROM job_keys WHERE owner=? AND key=?', (owner, request.idempotency_key)).fetchone()
        if not row:
            return None
        if row[1:] != (digest, app, 'assess'):
            conflict('Assessment idempotency key belongs to another request')
        job, raw, _ = self.jobs._job(db, row[0], owner)
        sql = db.execute('SELECT owner,state FROM jobs WHERE id=?', (row[0],)).fetchone()
        if (job.id != row[0] or sql != (owner, job.state) or job.profile_id != owner or job.application_id != app
                or job.kind != 'assess' or job.idempotency_key != request.idempotency_key):
            conflict('Assessment retained replay association changed')
        parameters, _ = self._parameters(db, job, raw)
        if parameters.request != request or parameters.parent_draft_id != parent_id:
            conflict('Assessment retained replay association changed')
        return job

    def enqueue(self, owner, app, parent_id, request):
        try:
            return self._enqueue(owner, app, parent_id, request)
        except ValueError:
            conflict('Assessment stored schema or integrity mismatch')

    def _enqueue(self, owner, app, parent_id, request):
        request = self._request(request, AssessmentRequest)
        digest = assessment_request_hash(owner, app, parent_id, request)
        with self.store._read() as db:
            old = self._replay(db, owner, app, parent_id, request, digest)
            if old is not None:
                return old
        preview_request = AssessmentPreviewRequest.model_validate_json(request.model_dump_json(exclude={'idempotency_key', 'disclosure_sha256', 'acknowledged'}))
        preview = self.preview(owner, app, parent_id, preview_request)
        if preview['disclosure_sha256'] != request.disclosure_sha256:
            conflict('Exact assessment disclosure acknowledgement required')
        with self.store._tx() as db:
            old = self._replay(db, owner, app, parent_id, request, digest)
            if old is not None:
                return old
            capture = self._capture(db, owner, app, parent_id, request)
            disclosure = self._disclosure(capture[0][2].context, capture[1], request, self._limits(db, owner))
            if c.canonical_hash(disclosure) != request.disclosure_sha256:
                conflict('Assessment disclosure changed')
            limits = disclosure['configured_limits']
            if not limits:
                raise EvidenceError('BUDGET_REQUIRED', 'Configure explicit assessment reservation limits', 403)
            rows = db.execute("SELECT data FROM provider_attempts WHERE owner=? AND day=? AND state!='definitely_unsent'", (owner, self.jobs._now().date().isoformat())).fetchall()
            attempts = [c.ProviderAttempt.model_validate_json(row[0]) for row in rows]
            bound = disclosure['conservative_reservation_units']
            if (limits['job_tokens'] < bound or limits['job_calls'] < 1
                    or limits['daily_tokens'] < bound + sum(max(a.reserved_tokens, a.reported_tokens or 0) for a in attempts)
                    or limits['daily_calls'] < 1 + len(attempts)):
                raise EvidenceError('BUDGET_EXCEEDED', 'Configured headroom must cover conservative one-call bound', 403)
            parameters = AssessmentParameters(profile_id=owner, application_id=app, parent_draft_id=parent_id,
                request=request, request_sha256=digest, source_request=capture[0][2].request,
                context=capture[0][2].context, input_draft=capture[1], batch_id=capture[0][3].id,
                batch_sha256=capture[0][3].batch_sha256, disclosure=disclosure)
            return self.jobs.enqueue(owner, 'assess', parameters.model_dump(mode='json'), request.idempotency_key,
                app, transaction=db, request_hash=digest)

    def _draft(self, owner, app, draft_id, created_at, parent, result):
        return c.DraftRevision(id=draft_id, profile_id=owner, application_id=app,
            revisions=parent.revisions.model_copy(update={'application_output': parent.revisions.application_output + 1}),
            research_run_id=parent.research_run_id, cover_letter=parent.cover_letter, answers=parent.answers,
            ledger=result.quality.ledger, assessment_state='assessed', inventory_complete=result.quality.inventory_complete,
            created_at=created_at)

    def run(self, job_id, worker, fence):
        with self.store._tx() as db:
            job, raw = self.jobs.assert_current(db, job_id, worker, fence)
            parameters, parent = self._parameters(db, job, raw)
            request = parameters.request
            capture = self._capture(db, job.profile_id, job.application_id, parameters.parent_draft_id, request)
            intent = AssessmentIntent(**parameters.model_dump(), id=uid(), job_id=job.id, fence=fence,
                pre_revisions=job.revisions, state='registered')
            self.service._save_new(db, job.profile_id, 'draft_intent', intent)

        def assert_job(db):
            current, current_raw = self.jobs.assert_current(db, job_id, worker, fence)
            if ((current.id, current.profile_id, current.application_id, current.kind, current.revisions, current.fence)
                    != (job.id, job.profile_id, job.application_id, 'assess', job.revisions, fence)
                    or AssessmentParameters.model_validate_json(encode(current_raw)) != parameters):
                conflict('Assessment job association changed')
            return current

        def prepare():
            with self.store._read() as db:
                assert_job(db)
                certificate = self._capture(db, job.profile_id, job.application_id, parameters.parent_draft_id, request)
                if certificate != capture:
                    conflict('Assessment source capture changed')
            self.service._verify(certificate[2])
            return certificate

        def validate(db, certificate):
            assert_job(db)
            if certificate != capture or self._capture(db, job.profile_id, job.application_id, parameters.parent_draft_id, request) != certificate:
                conflict('Assessment canonical transmission capture changed')
            if self.domain._get(db, job.profile_id, intent.id, 'draft_intent', AssessmentIntent) != intent:
                conflict('Assessment intent changed')

        guard = TransmissionGuard(prepare, validate)

        def call(stage, *, instructions, input_value, output_model):
            options = dict(model=request.model, instructions=instructions, input_value=input_value,
                output_model=output_model, max_output_tokens=request.max_output_tokens)
            with self.store._read() as db:
                old = db.execute('SELECT state FROM provider_attempts WHERE job_id=? AND stage=?', (job_id, stage)).fetchone()
                if old and old[0] != 'completed':
                    raise EvidenceError('PROVIDER_UNKNOWN', 'Resolve previous provider stage explicitly', 409)
                if old:
                    retained = db.execute('SELECT id,owner,job_id,response,request_sha256,data FROM provider_attempts WHERE job_id=? AND stage=?', (job_id, stage)).fetchone()
                    authority = c.ProviderAttempt.model_validate_json(retained[5])
                    payload = build_payload(**options)
                    if ((retained[0], retained[1], retained[2]) != (authority.id, job.profile_id, job.id)
                            or (authority.profile_id, authority.application_id, authority.job_id, authority.state,
                                authority.model_version, authority.consent_revision, authority.provider)
                            != (job.profile_id, job.application_id, job.id, 'completed', request.model, job.revisions.consent, 'openai')
                            or not 1 <= authority.fence <= fence
                            or authority.reserved_tokens != conservative_reservation(**options)
                            or retained[4] != c.canonical_hash(payload) or not retained[3]):
                        conflict('Assessment reusable provider authority mismatch')
                    reusable = output_model.model_validate_json(retained[3])
                    stored = db.execute('SELECT fence,data,sha256 FROM job_stages WHERE job_id=? AND stage=?', (job_id, 'assessment_stage_reassess')).fetchone()
                    if stored and (type(stored[0]) is not int or not 1 <= stored[0] <= fence
                            or stored[2] != c.text_hash(stored[1])
                            or json.loads(stored[1]) != dict(stage=stage, output_sha256=c.canonical_hash(reusable))):
                        conflict('Assessment reusable completed stage mismatch')
            output = self.service.provider.send(job_id, worker, fence, stage, **options,
                reserved_tokens=conservative_reservation(**options), guard=guard)
            self.jobs.commit_stage(job_id, worker, fence, 'assessment_stage_reassess',
                dict(stage=stage, output_sha256=c.canonical_hash(output)))
            return output

        result = reassess(parameters.context, parameters.input_draft, call)
        draft = self._draft(job.profile_id, job.application_id, uid(), self.jobs._now(), parent[0], result)
        if draft.text_sha256 != parent[0].text_sha256:
            conflict('Assessment must not change text')
        publication = AssessmentPublication(id=uid(), profile_id=job.profile_id, application_id=job.application_id,
            draft_id=draft.id, parent_draft_id=parent[0].id, parent_text_sha256=parent[0].text_sha256,
            parent_ledger_sha256=parent[0].ledger_sha256, parent_publication_id=parent[1].id,
            parent_publication_sha256=parent[1].publication_sha256, batch_id=parameters.batch_id,
            batch_sha256=parameters.batch_sha256, job_id=job.id, intent_id=intent.id, fence=fence,
            pre_revisions=job.revisions, edit_depth=parent[1].edit_depth, created_at=draft.created_at,
            request_sha256=parameters.request_sha256, disclosure_sha256=request.disclosure_sha256,
            prompt_version=PROMPT_VERSION, text_sha256=draft.text_sha256, ledger_sha256=draft.ledger_sha256,
            stage_names=result.stage_names, reason_codes=result.quality.reason_codes, style_notes=result.quality.style_notes)
        certificate = prepare()

        def publish(db, current, summary):
            validate(db, certificate)
            self.service._save_new(db, job.profile_id, 'draft', draft)
            self.service._save_new(db, job.profile_id, 'draft_dependency', publication)
            self.domain._save(db, job.profile_id, 'draft_intent', intent.model_copy(update={'state': 'committed'}))
            self.domain._save(db, job.profile_id, 'application', capture[2][0].model_copy(update={'output_revision': draft.revisions.application_output}))

        self.jobs.commit_stage(job_id, worker, fence, 'assessment_published', self.service._summary(draft, publication), publish=publish, terminal=True)
        return draft

    def association(self, db, owner, app, draft_id, publication, visited):
        publication = validated(publication, AssessmentPublication)
        if (not visited or visited[-1] != draft_id or len(visited) > 128 or len(set(visited)) != len(visited)
                or (publication.profile_id, publication.application_id, publication.draft_id) != (owner, app, draft_id)):
            conflict('Assessment publication scope/lineage mismatch')
        job, raw, dependencies = self.jobs._job(db, publication.job_id, owner)
        parameters = AssessmentParameters.model_validate_json(encode(raw))
        parent, output = self._parent(db, owner, app, publication.parent_draft_id, parameters.request, visited=visited)
        # _parameters repeats the immutable validation without currentness/source IO.
        checked, _ = self._parameters(db, job, raw, parent_capture=(parent, output))
        intent = self.domain._get(db, owner, publication.intent_id, 'draft_intent', AssessmentIntent)
        draft = self.domain._get(db, owner, draft_id, 'draft', c.DraftRevision)
        terminal = db.execute('SELECT fence,data,sha256 FROM job_stages WHERE job_id=? AND stage=?', (job.id, 'assessment_published')).fetchone()
        summary = self.service._summary(draft, publication)
        if ((job.id, job.profile_id, job.application_id, job.kind, job.state, job.stage, job.fence, job.revisions)
                != (publication.job_id, owner, app, 'assess', 'completed', 'assessment_published', publication.fence, parent[0].revisions)
                or job.cancellation_requested or db.execute('SELECT owner,state FROM jobs WHERE id=?', (job.id,)).fetchone() != (owner, 'completed')
                or intent.id != publication.intent_id
                or (intent.job_id, intent.fence, intent.pre_revisions, intent.state) != (job.id, job.fence, job.revisions, 'committed')
                or AssessmentParameters.model_validate_json(intent.model_dump_json(exclude={'id','job_id','fence','pre_revisions','state','origin'})) != checked
                or (publication.parent_draft_id, publication.parent_text_sha256, publication.parent_ledger_sha256, publication.parent_publication_id, publication.parent_publication_sha256)
                != (parent[0].id, parent[0].text_sha256, parent[0].ledger_sha256, parent[1].id, parent[1].publication_sha256)
                or publication.pre_revisions != parent[0].revisions or publication.edit_depth != parent[1].edit_depth
                or (publication.batch_id, publication.batch_sha256, publication.request_sha256, publication.disclosure_sha256, publication.prompt_version)
                != (parameters.batch_id, parameters.batch_sha256, parameters.request_sha256, parameters.request.disclosure_sha256, PROMPT_VERSION)
                or dependencies != ('metadata','facts','documents','consent','application_input','application_output','research')
                or terminal is None or terminal[0] != job.fence or json.loads(terminal[1]) != summary or terminal[2] != c.text_hash(encode(summary))):
            conflict('Assessment publication integrity mismatch')

        def replay(stage, *, instructions, input_value, output_model):
            row = db.execute('SELECT owner,job_id,state,response,request_sha256,data,id FROM provider_attempts WHERE job_id=? AND stage=?', (job.id, stage)).fetchone()
            stored = db.execute('SELECT fence,data,sha256 FROM job_stages WHERE job_id=? AND stage=?', (job.id, 'assessment_stage_reassess')).fetchone()
            authority = c.ProviderAttempt.model_validate_json(row[5]) if row else None
            payload = build_payload(model=parameters.request.model, instructions=instructions, input_value=input_value,
                output_model=output_model, max_output_tokens=parameters.request.max_output_tokens)
            if (authority is None or row[6] != authority.id or row[:3] != (owner, job.id, 'completed') or not row[3]
                    or (authority.profile_id, authority.application_id, authority.job_id, authority.model_version, authority.consent_revision, authority.state)
                    != (owner, app, job.id, parameters.request.model, job.revisions.consent, 'completed')
                    or authority.reserved_tokens != conservative_reservation(model=parameters.request.model, instructions=instructions,
                        input_value=input_value, output_model=output_model, max_output_tokens=parameters.request.max_output_tokens)
                    or authority.provider != 'openai' or not 1 <= authority.fence <= job.fence or row[4] != c.canonical_hash(payload)
                    or stored is None or type(stored[0]) is not int or not 1 <= stored[0] <= job.fence or stored[2] != c.text_hash(stored[1])
                    or json.loads(stored[1]) != dict(stage=stage, output_sha256=c.canonical_hash(json.loads(row[3])))):
                conflict('Assessment completed provider stage integrity mismatch')
            return output_model.model_validate_json(row[3])

        result = reassess(parameters.context, output, replay)
        expected = self._draft(owner, app, draft_id, publication.created_at, parent[0], result)
        if (draft != expected or draft.text_sha256 != parent[0].text_sha256
                or publication.text_sha256 != draft.text_sha256 or publication.ledger_sha256 != draft.ledger_sha256
                or publication.stage_names != result.stage_names or publication.reason_codes != result.quality.reason_codes
                or publication.style_notes != result.quality.style_notes):
            conflict('Assessment exact text/ledger integrity mismatch')
        return draft, publication, parent[2], parent[3]
