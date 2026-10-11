"""Atomic, service-owned drafting admission and read-only historical eligibility."""
import json
from dataclasses import dataclass

from pydantic import TypeAdapter

from ..admission_state import BINDINGS, HEADS, MAX_REVISION
from ..contracts import EvidenceError
from ..domain import contracts as c
from ..domain.repository import uid
from ..jobs import encode
from ..retrieval.packet_service import conflict
from .admission_contracts import (
    AssessmentLaunch,
    DraftLaunch,
    _validation_input,
    assessment_launch_hash,
    draft_launch_hash,
)
from .admission_guard import AdmissionGuardError, inspect_admission_head, inspect_retained_paid_job
from .assessment_contracts import (
    AssessmentParameters,
    AssessmentPublication,
    AssessmentRequest,
    assessment_request_hash,
)
from .assessments import DraftAssessments
from .edit_contracts import EditPublication
from .pipeline import build_context, context_payload
from .runtime_contracts import (
    DraftParameters,
    DraftPublication,
    DraftRequest,
    draft_request_hash,
)

_IDENTIFIER = TypeAdapter(c.Identifier, config={'strict': True})
_DEPENDENCIES = ('metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research')


@dataclass(frozen=True)
class CompletedPublicationCertificate:
    owner: str
    application_id: str
    job_id: str
    kind: str
    draft_id: str
    publication_id: str
    intent_id: str
    fence: int
    batch_id: str
    batch_sha256: str
    request_sha256: str
    disclosure_sha256: str
    text_sha256: str
    ledger_sha256: str
    publication_sha256: str
    pre_revisions_json: str
    post_revisions_json: str
    parent_identity: tuple | None
    association_sha256: str
    own_parameters_sha256: str
    terminal_sha256: str


class DraftAdmission:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.jobs = service.jobs
        self.assessments = DraftAssessments(service)

    def _ids(self, *values):
        try:
            for value in values:
                _IDENTIFIER.validate_python(value)
        except ValueError:
            raise EvidenceError('INVALID_INPUT', 'Strict application identifiers required', 422) from None

    def _stored_job_id(self, value):
        # These references came from SQLite, not a caller's public input.
        try:
            return _IDENTIFIER.validate_python(value)
        except ValueError:
            raise AdmissionGuardError('metadata') from None

    def _input(self, value, assessment):
        raw_type, launch_type = (AssessmentRequest, AssessmentLaunch) if assessment else (DraftRequest, DraftLaunch)
        if not isinstance(value, dict) and type(value) not in (raw_type, launch_type):
            raise EvidenceError('INVALID_INPUT', 'Strict drafting request required', 422)
        data = _validation_input(value)
        model = launch_type if isinstance(data, dict) and ('request' in data or 'admission_revision' in data) else raw_type
        try:
            return model.model_validate(data)
        except (ValueError, TypeError):
            raise EvidenceError('INVALID_INPUT', 'Strict drafting request required', 422) from None

    def _job(self, row):
        try:
            job = c.DurableJob.model_validate_json(row[3])
            if row[:3] != (job.id, job.profile_id, job.state):
                raise ValueError('association')
            return job
        except (ValueError, TypeError):
            raise AdmissionGuardError('history') from None

    def _attempts(self, db, owner, app):
        unresolved = False
        invalid = False
        rows = db.execute('SELECT a.id,a.owner,a.job_id,a.state,a.data FROM provider_attempts a WHERE a.owner=? OR EXISTS(SELECT 1 FROM jobs j WHERE j.id=a.job_id AND j.owner=?)', (owner, owner)).fetchall()
        for row in rows:
            try:
                attempt = c.ProviderAttempt.model_validate_json(row[4])
                if row[:4] != (attempt.id, attempt.profile_id, attempt.job_id, attempt.state):
                    raise ValueError('attempt association')
                linked = db.execute('SELECT id,owner,state,data FROM jobs WHERE id=?', (attempt.job_id,)).fetchone()
                if linked:
                    job = self._job(linked)
                    if (job.profile_id, job.application_id) != (attempt.profile_id, attempt.application_id):
                        raise ValueError('attempt scope')
                if attempt.application_id == app and attempt.state in ('prepared', 'indeterminate'):
                    unresolved = True
            except (ValueError, TypeError, AdmissionGuardError):
                invalid = True
        return invalid, unresolved

    def _history(self, db, owner, app):
        invalid, unresolved = self._attempts(db, owner, app)
        active = False
        certificates = []
        # A retained binding is metadata even when its non-head job was lost.
        # Starting only from jobs would silently omit that corruption. Keep the
        # reverse association audit scoped to this exact owner/application.
        for (job_id,) in db.execute(f'SELECT job_id FROM {BINDINGS} WHERE owner=? AND application_id=?', (owner, app)).fetchall():
            try:
                inspect_retained_paid_job(db, owner, self._stored_job_id(job_id))
            except AdmissionGuardError as exc:
                if exc.kind != 'history':
                    raise
                invalid = True
        try:
            self._publication_rows(db, owner)
        except (EvidenceError, ValueError, TypeError):
            invalid = True
        for row in db.execute('SELECT id,owner,state,data FROM jobs WHERE owner=? ORDER BY id', (owner,)).fetchall():
            try:
                job = self._job(row)
            except AdmissionGuardError:
                invalid = True
                continue
            # Discoverable binding/job scope contradictions belong to this
            # SQL job owner, not whichever app the corrupted JSON now claims.
            # Do not globally validate unrelated binding metadata: its full
            # metadata proof remains scoped to the requested application.
            binding_scope = db.execute(f'SELECT owner,application_id,kind FROM {BINDINGS} WHERE job_id=?', (job.id,)).fetchone()
            if binding_scope is not None and binding_scope != (job.profile_id, job.application_id, job.kind):
                invalid = True
            if job.application_id != app or job.kind not in ('draft', 'assess'):
                continue
            try:
                retained = inspect_retained_paid_job(db, owner, job.id)
                if job.state == 'indeterminate' or job.stage == 'provider_unknown':
                    unresolved = True
                if retained.forgotten:
                    continue
                if job.state in ('queued', 'running'):
                    active = True
                if job.state == 'completed':
                    certificates.append(self._completed_publication_certificate(db, owner, app, job.id))
            except AdmissionGuardError as exc:
                if exc.kind != 'history':
                    raise
                invalid = True
            except (EvidenceError, ValueError, TypeError, KeyError):
                invalid = True
        reasons = [code for code, applies in (('integrity_blocked', invalid), ('provider_outcome_unresolved', unresolved), ('active_operation', active)) if applies]
        return reasons, tuple(certificates)

    def state(self, owner, application_id):
        self._ids(owner, application_id)
        with self.store._read() as db:
            head = inspect_admission_head(db, owner, application_id)
            reasons, _ = self._history(db, owner, application_id)
            head_job = None
            if head.last_job_id:
                try:
                    retained = inspect_retained_paid_job(db, owner, head.last_job_id)
                    job = c.DurableJob.model_validate_json(retained.job_json)
                    head_job = dict(schema_version=1, id=job.id, kind=job.kind, state=job.state,
                        captured_revision=retained.captured_revision, consumed_revision=retained.consumed_revision)
                except AdmissionGuardError as exc:
                    if exc.kind != 'history':
                        raise
                    if 'integrity_blocked' not in reasons:
                        reasons.insert(0, 'integrity_blocked')
            if head.revision == MAX_REVISION:
                reasons.append('admission_exhausted')
            return dict(schema_version=1, profile_id=owner, application_id=application_id,
                admission_revision=head.revision, eligible=not reasons, reason_codes=reasons, head_job=head_job)

    def _publication_rows(self, db, owner):
        owned, foreign = [], []
        models = {'edit': EditPublication, 'assessment': AssessmentPublication}
        for identifier, sql_owner, data in db.execute("SELECT id,owner,data FROM records WHERE kind='workspace:draft_dependency' ORDER BY id"):
            try:
                value = json.loads(data)
            except (ValueError, TypeError):
                if sql_owner == owner:
                    conflict('Owned publication schema mismatch')
                continue
            if sql_owner != owner:
                if isinstance(value, dict):
                    foreign.append(value)
                continue
            if not isinstance(value, dict):
                conflict('Owned publication schema mismatch')
            model = DraftPublication if 'origin' not in value else models.get(value['origin'])
            if model is None:
                conflict('Owned publication origin mismatch')
            publication = model.model_validate_json(data)
            if (publication.id, publication.profile_id) != (identifier, sql_owner):
                conflict('Owned publication identity mismatch')
            owned.append(publication)
        return owned, foreign

    def _completed_publication_certificate(self, db, owner, app, job_id):
        if self._attempts(db, owner, app)[0]:
            conflict('Retained provider attempt identity mismatch')
        retained = inspect_retained_paid_job(db, owner, job_id)
        job = c.DurableJob.model_validate_json(retained.job_json)
        if job.state != 'completed' or retained.forgotten:
            conflict('Completed drafting publication unavailable')
        owned, foreign = self._publication_rows(db, owner)
        matches = [p for p in owned if getattr(p, 'job_id', None) == job_id]
        if len(matches) != 1:
            conflict('Completed publication cardinality mismatch')
        publication = matches[0]
        if (publication.application_id != app or type(publication) is not (DraftPublication if job.kind == 'draft' else AssessmentPublication)
                or len([p for p in owned if p.draft_id == publication.draft_id]) != 1
                or any(p.get('job_id') == job_id or p.get('draft_id') == publication.draft_id for p in foreign)):
            conflict('Completed publication association mismatch')
        draft_row = db.execute('SELECT id,owner,kind,data FROM records WHERE id=?', (publication.draft_id,)).fetchone()
        if not draft_row or draft_row[:3] != (publication.draft_id, owner, 'workspace:draft'):
            conflict('Completed draft scope mismatch')
        draft = c.DraftRevision.model_validate_json(draft_row[3])
        if (draft.id, draft.profile_id, draft.application_id) != (publication.draft_id, owner, app):
            conflict('Completed draft association mismatch')
        association = self.service._association(db, owner, app, draft.id)
        if association[:2] != (draft, publication):
            conflict('Completed publication graph mismatch')
        raw = db.execute('SELECT parameters FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
        own_parameters = (DraftParameters if job.kind == 'draft' else AssessmentParameters).model_validate_json(raw)
        terminal = 'draft_published' if job.kind == 'draft' else 'assessment_published'
        row = db.execute('SELECT fence,data,sha256 FROM job_stages WHERE job_id=? AND stage=?', (job_id, terminal)).fetchone()
        if job.stage != terminal or job.fence != publication.fence or not row or row[0] != job.fence or row[2] != c.text_hash(row[1]):
            conflict('Completed terminal association mismatch')
        pre = own_parameters.request.expected_revisions
        if job.revisions != pre or draft.revisions != pre.model_copy(update={'application_output': pre.application_output + 1}):
            conflict('Completed publication revisions mismatch')
        parent = None if job.kind == 'draft' else (publication.parent_draft_id, publication.parent_text_sha256,
            publication.parent_ledger_sha256, publication.parent_publication_id, publication.parent_publication_sha256)
        return CompletedPublicationCertificate(owner, app, job_id, job.kind, draft.id, publication.id,
            publication.intent_id, publication.fence, publication.batch_id, publication.batch_sha256,
            own_parameters.request_sha256, own_parameters.request.disclosure_sha256, draft.text_sha256,
            draft.ledger_sha256, publication.publication_sha256, encode(pre.model_dump(mode='json')),
            encode(draft.revisions.model_dump(mode='json')), parent,
            c.canonical_hash(dict(zip(('draft', 'publication', 'parameters', 'batch'), (v.model_dump(mode='json') for v in association), strict=True))),
            c.canonical_hash(own_parameters.model_dump(mode='json')), c.canonical_hash((row[0], json.loads(row[1]), row[2])))

    def _replay(self, db, owner, app, parent, value):
        wrapped = type(value) in (DraftLaunch, AssessmentLaunch)
        request = value.request if wrapped else value
        row = db.execute('SELECT job_id,request_sha256,application_id,kind FROM job_keys WHERE owner=? AND key=?', (owner, request.idempotency_key)).fetchone()
        if not row:
            if not wrapped:
                raise EvidenceError('ADMISSION_REQUIRED', 'Captured drafting admission required', 409)
            return None
        assessment = parent is not None
        kind = 'assess' if assessment else 'draft'
        retained = inspect_retained_paid_job(db, owner, self._stored_job_id(row[0]))
        if retained.forgotten:
            conflict('Forgotten drafting request cannot replay')
        if wrapped and retained.origin != 'admitted':
            conflict('Idempotency key belongs to a legacy request')
        digest = (assessment_launch_hash(owner, app, parent, value) if assessment else draft_launch_hash(owner, app, value)) if wrapped else (assessment_request_hash(owner, app, parent, request) if assessment else draft_request_hash(owner, app, request))
        if row[2:] != (app, kind) or (row[1] != digest and not (not wrapped and retained.origin == 'admitted')):
            conflict('Idempotency key belongs to a different request')
        job, raw, dependencies = self.jobs._job(db, row[0], owner)
        parameters = (AssessmentParameters if assessment else DraftParameters).model_validate_json(encode(raw))
        inner = assessment_request_hash(owner, app, parent, request) if assessment else draft_request_hash(owner, app, request)
        if (c.canonical_hash(parameters.disclosure) != request.disclosure_sha256
                or parameters.disclosure.get('selected_context') != context_payload(parameters.context)
                or parameters.disclosure.get('selected_context_sha256') != c.canonical_hash(context_payload(parameters.context))
                or (parameters.context.profile_id, parameters.context.application_id) != (owner, app)):
            conflict('Retained drafting context/disclosure changed')
        if assessment and retained.origin == 'legacy':
            self.assessments._parameters(db, job, raw)
        if (job.id, job.profile_id, job.application_id, job.kind, job.idempotency_key) != (row[0], owner, app, kind, request.idempotency_key) or parameters.request != request or parameters.request_sha256 != inner or parameters.request.expected_revisions != job.revisions or dependencies != _DEPENDENCIES or (parameters.profile_id, parameters.application_id) != (owner, app) or (assessment and parameters.parent_draft_id != parent):
            conflict('Retained drafting replay association changed')
        if not wrapped and retained.origin == 'admitted':
            raise EvidenceError('ADMISSION_REQUIRED', 'Original captured drafting admission required', 409)
        return job

    def _eligible(self, db, owner, app, value):
        head = inspect_admission_head(db, owner, app)
        if head.revision != value.admission_revision:
            conflict('Drafting admission capture changed')
        reasons, certificates = self._history(db, owner, app)
        if 'integrity_blocked' in reasons:
            conflict('Drafting history integrity unavailable')
        if reasons or head.revision == MAX_REVISION:
            raise EvidenceError('ADMISSION_BLOCKED', 'Drafting admission history is not eligible', 409)
        return certificates

    def _require_consent(self, db, owner, app, purpose):
        # Provider's historical singleton helper may ensure/write and does not
        # prove SQL/serialized singleton identity. Fresh admission needs a
        # canonical, read-only proof in this exact snapshot instead.
        rows = db.execute("SELECT id,owner,data FROM records WHERE owner=? AND kind='workspace:consent'", (owner,)).fetchall()
        if len(rows) > 1:
            conflict('Consent singleton is inconsistent')
        consent = c.ConsentRecord.model_validate_json(rows[0][2]) if rows else None
        if consent is not None and ((rows[0][0], rows[0][1]) != (consent.id, consent.profile_id)
                or not consent.purposes or len(set(consent.purposes)) != len(consent.purposes)):
            conflict('Consent association is inconsistent')
        current = self.jobs.capture(db, owner, app)
        if (consent is None or not consent.granted or consent.provider != 'openai'
                or purpose not in consent.purposes or consent.revision != current.consent):
            raise EvidenceError('CONSENT_REQUIRED', 'Current explicit provider/purpose consent required', 403)

    def _parameters(self, db, owner, app, parent, request):
        self._require_consent(db, owner, app, 'drafting' if parent is None else 'assessment')
        if parent is None:
            capture = self.service._capture(db, owner, app, request)
            disclosure = self.service._disclosure(db, capture, request)
            parameters = DraftParameters(profile_id=owner, application_id=app, request=request,
                request_sha256=draft_request_hash(owner, app, request), context=build_context(capture[0], capture[2]), disclosure=disclosure)
        else:
            capture = self.assessments._capture(db, owner, app, parent, request)
            disclosure = self.assessments._disclosure(capture[0][2].context, capture[1], request, self.assessments._limits(db, owner))
            parameters = AssessmentParameters(profile_id=owner, application_id=app, parent_draft_id=parent,
                request=request, request_sha256=assessment_request_hash(owner, app, parent, request),
                source_request=capture[0][2].request, context=capture[0][2].context, input_draft=capture[1],
                batch_id=capture[0][3].id, batch_sha256=capture[0][3].batch_sha256, disclosure=disclosure)
        if c.canonical_hash(disclosure) != request.disclosure_sha256:
            conflict('Exact drafting disclosure acknowledgement required')
        limits = disclosure['configured_limits']
        if not limits:
            raise EvidenceError('BUDGET_REQUIRED', 'Configure explicit drafting reservation limits', 403)
        calls, bound = disclosure['maximum_calls'], disclosure['conservative_reservation_units']
        rows = db.execute("SELECT data FROM provider_attempts WHERE owner=? AND day=? AND state!='definitely_unsent'", (owner, self.jobs._now().date().isoformat())).fetchall()
        attempts = [c.ProviderAttempt.model_validate_json(row[0]) for row in rows]
        if (limits['job_tokens'] < bound or limits['job_calls'] < calls or limits['daily_tokens'] < bound + sum(max(a.reserved_tokens, a.reported_tokens or 0) for a in attempts) or limits['daily_calls'] < calls + len(attempts)):
            raise EvidenceError('BUDGET_EXCEEDED', 'Configured headroom must cover conservative drafting bound', 403)
        return parameters, capture

    def enqueue_generation(self, owner, application_id, request):
        return self._enqueue(owner, application_id, None, request)

    def enqueue_assessment(self, owner, application_id, parent_draft_id, request):
        return self._enqueue(owner, application_id, parent_draft_id, request)

    def _enqueue(self, owner, app, parent, value):
        self._ids(owner, app, *((parent,) if parent is not None else ()))
        value = self._input(value, parent is not None)
        try:
            with self.store._read() as db:
                inspect_admission_head(db, owner, app)
                replay = self._replay(db, owner, app, parent, value)
                if replay is not None:
                    return replay
                certificates = self._eligible(db, owner, app, value)
                parameters, capture = self._parameters(db, owner, app, parent, value.request)
            self.service._verify(capture if parent is None else capture[2])
            with self.store._tx() as db:
                inspect_admission_head(db, owner, app)
                replay = self._replay(db, owner, app, parent, value)
                if replay is not None:
                    return replay
                if self._eligible(db, owner, app, value) != certificates:
                    conflict('Completed drafting history changed')
                current, _ = self._parameters(db, owner, app, parent, value.request)
                if current != parameters:
                    conflict('Drafting canonical capture changed')
                kind = 'draft' if parent is None else 'assess'
                digest = draft_launch_hash(owner, app, value) if parent is None else assessment_launch_hash(owner, app, parent, value)
                job = c.DurableJob(id=uid(), profile_id=owner, application_id=app, kind=kind,
                    stage='queued', idempotency_key=value.request.idempotency_key, state='queued',
                    revisions=value.request.expected_revisions, fence=0)
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (job.id, owner, job.state, job.model_dump_json(), encode(parameters.model_dump(mode='json')), encode(_DEPENDENCIES)))
                db.execute('INSERT INTO job_keys(owner,key,job_id,request_sha256,application_id,kind) VALUES(?,?,?,?,?,?)', (owner, job.idempotency_key, job.id, digest, app, kind))
                db.execute(f'INSERT INTO {BINDINGS} VALUES(?,?,?,?,?,?,?,?,?)', (job.id, owner, app, kind, 'admitted', value.admission_revision, value.admission_revision + 1, parameters.request_sha256, digest))
                if db.execute(f'UPDATE {HEADS} SET revision=?,last_job_id=? WHERE owner=? AND application_id=? AND revision=?', (value.admission_revision + 1, job.id, owner, app, value.admission_revision)).rowcount != 1:
                    conflict('Drafting admission capture changed')
                inspect_retained_paid_job(db, owner, job.id)
                return job
        except AdmissionGuardError as exc:
            if exc.kind == 'history':
                conflict('Drafting history integrity unavailable')
            raise
        except (ValueError, TypeError):
            conflict('Drafting retained schema or integrity mismatch')
