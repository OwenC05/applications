"""Read-only retained admission checks, separate from dispatch authority."""
import json
from dataclasses import dataclass
from typing import Literal

from .. import admission_state as state
from ..contracts import EvidenceError
from ..domain import contracts as c
from .admission_contracts import (
    AssessmentLaunch,
    DraftLaunch,
    assessment_launch_hash,
    draft_launch_hash,
)
from .assessment_contracts import AssessmentParameters, assessment_request_hash
from .runtime_contracts import DraftParameters, draft_request_hash

DEPENDENCIES = ('metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research')


class AdmissionGuardError(EvidenceError):
    def __init__(self, kind: Literal['metadata', 'history', 'head_mismatch']):
        self.kind = kind
        super().__init__('ADMISSION_UNAVAILABLE', 'Drafting admission is unavailable', 503)


@dataclass(frozen=True)
class RetainedPaidJob:
    job_id: str
    owner: str
    application_id: str
    kind: Literal['draft', 'assess']
    origin: Literal['legacy', 'admitted']
    forgotten: bool
    captured_revision: int | None
    consumed_revision: int | None
    request_sha256: str | None
    envelope_sha256: str | None
    job_json: str
    parameters_json: str | None
    dependencies: tuple[str, ...] | None


def _schema(db):
    try:
        state._base_schema(db)
        state._admission_schema(db)
    except EvidenceError as exc:
        raise AdmissionGuardError('metadata') from exc
    return 'jobs' in state._tables(db)


def _binding(db, job_id):
    row = db.execute(f'SELECT * FROM {state.BINDINGS} WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        raise AdmissionGuardError('metadata')
    jid, owner, app, kind, origin, captured, consumed, request, envelope = row
    try:
        for value in (jid, owner, app):
            state._identifier(value)
    except EvidenceError as exc:
        raise AdmissionGuardError('metadata') from exc
    if kind not in ('draft', 'assess') or origin not in ('legacy', 'admitted'):
        raise AdmissionGuardError('metadata')
    if any(d is not None and (type(d) is not str or not state._DIGEST.fullmatch(d)) for d in (request, envelope)):
        raise AdmissionGuardError('metadata')
    if origin == 'legacy':
        valid = all(v is None for v in (captured, consumed, request, envelope))
    else:
        valid = (type(captured) is int and type(consumed) is int
                 and 0 <= captured < state.MAX_REVISION and consumed == captured + 1
                 and request is not None and envelope is not None)
    if not valid:
        raise AdmissionGuardError('metadata')
    return row


def inspect_admission_head(db, owner, application_id):
    state._identifier(owner, public=True)
    state._identifier(application_id, public=True)
    try:
        state._base_schema(db)
        if state._application(db, owner, application_id) is None:
            raise EvidenceError('NOT_FOUND', 'Application not found', 404)
    except EvidenceError as exc:
        if exc.code == 'NOT_FOUND':
            raise
        raise AdmissionGuardError('metadata') from exc
    has_jobs = _schema(db)
    row = db.execute(f'SELECT * FROM {state.HEADS} WHERE owner=? AND application_id=?',
                     (owner, application_id)).fetchone()
    if (not row or row[:2] != (owner, application_id) or type(row[2]) is not int
            or not 0 <= row[2] <= state.MAX_REVISION):
        raise AdmissionGuardError('metadata')
    if row[3] is not None:
        if not has_jobs:
            raise AdmissionGuardError('metadata')
        try:
            state._identifier(row[3])
        except EvidenceError as exc:
            raise AdmissionGuardError('metadata') from exc
        binding = _binding(db, row[3])
        job = db.execute('SELECT owner FROM jobs WHERE id=?', (row[3],)).fetchone()
        if (not job or binding[1:3] != (owner, application_id)
                or binding[4] != 'admitted' or binding[6] > row[2]):
            raise AdmissionGuardError('metadata')
    return state.AdmissionHead(*row)


def _context_payload(context):
    # Frozen, publication-independent projection of the selected context. Keep
    # this aligned with pipeline.context_payload without importing runtime code.
    return {'company': context.company, 'role': context.role, 'sector': context.sector,
        'targets': [{'question': target.question.model_dump(mode='json'), 'packet_id': target.packet_id,
            'facts': [{'id': item.id, 'kind': item.reference.kind, 'excerpt': item.reference.span.excerpt}
                      for item in target.facts],
            'employer': [{'id': item.id, 'kind': item.reference.kind, 'excerpt': item.reference.span.excerpt}
                         for item in target.employer],
            'criteria': [{'id': item.id, 'text': item.text, 'inferred': item.inferred} for item in target.criteria],
            'gaps': list(target.gaps)} for target in context.targets]}


def inspect_retained_paid_job(db, owner, job_id):
    state._identifier(owner, public=True)
    state._identifier(job_id, public=True)
    if not _schema(db):
        raise AdmissionGuardError('metadata')
    binding = _binding(db, job_id)
    if binding[4] == 'admitted':
        head = db.execute(f'SELECT revision FROM {state.HEADS} WHERE owner=? AND application_id=?',
                          binding[1:3]).fetchone()
        if (not head or type(head[0]) is not int or not binding[6] <= head[0] <= state.MAX_REVISION):
            raise AdmissionGuardError('metadata')
    row = db.execute('SELECT id,owner,state,data,parameters,dependencies FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise AdmissionGuardError('metadata')
    try:
        job = c.DurableJob.model_validate_json(row[3])
        if ((job.id, job.profile_id, job.state) != row[:3] or job.profile_id != owner
                or (job.id, job.profile_id, job.application_id, job.kind) != binding[:4]):
            raise ValueError('identity')
        marker = state._tombstone(db, job_id)
        forgotten = marker == (owner, 'drafting_job_forgotten') and state._forgotten(db, job, row[4])
        if (marker is not None or job.idempotency_key == 'forgotten') and not forgotten:
            raise ValueError('scrub')
        parameters_json = dependencies = None
        if binding[4] == 'admitted' and not forgotten:
            cls = DraftParameters if job.kind == 'draft' else AssessmentParameters
            parameters = cls.model_validate_json(row[4])
            request = parameters.request
            dependencies = tuple(json.loads(row[5]))
            if (dependencies != DEPENDENCIES or json.loads(row[5]) != list(DEPENDENCIES)
                    or (parameters.profile_id, parameters.application_id) != (owner, job.application_id)
                    or request.expected_revisions != job.revisions or request.idempotency_key != job.idempotency_key
                    or c.canonical_hash(parameters.disclosure) != request.disclosure_sha256
                    or parameters.context.profile_id != owner or parameters.context.application_id != job.application_id):
                raise ValueError('parameters')
            selected = _context_payload(parameters.context)
            if (parameters.disclosure.get('selected_context') != selected
                    or parameters.disclosure.get('selected_context_sha256') != c.canonical_hash(selected)
                    or parameters.disclosure.get('batch_id') != parameters.context.batch_id
                    or parameters.disclosure.get('batch_sha256') != parameters.context.batch_sha256
                    or parameters.disclosure.get('expected_revisions') != request.expected_revisions.model_dump(mode='json')
                    or parameters.disclosure.get('model') != request.model
                    or parameters.disclosure.get('max_output_tokens') != request.max_output_tokens
                    or parameters.disclosure.get('prompt_version') != parameters.prompt_version):
                raise ValueError('context disclosure')
            if job.kind == 'draft':
                if (parameters.context.batch_id, parameters.context.batch_sha256) != (request.batch_id, request.batch_sha256):
                    raise ValueError('batch')
                digest = draft_request_hash(owner, job.application_id, request)
                envelope = draft_launch_hash(owner, job.application_id,
                    DraftLaunch(admission_revision=binding[5], request=request))
            else:
                digest = assessment_request_hash(owner, job.application_id, parameters.parent_draft_id, request)
                envelope = assessment_launch_hash(owner, job.application_id, parameters.parent_draft_id,
                    AssessmentLaunch(admission_revision=binding[5], request=request))
            if (parameters.request_sha256, digest, envelope) != (binding[7], binding[7], binding[8]):
                raise ValueError('digest')
            keys = db.execute('SELECT owner,key,job_id,request_sha256,application_id,kind FROM job_keys WHERE job_id=? OR (owner=? AND key=?)',
                              (job_id, owner, request.idempotency_key)).fetchall()
            if keys != [(owner, request.idempotency_key, job_id, envelope, job.application_id, job.kind)]:
                raise ValueError('key')
            parameters_json = parameters.model_dump_json()
    except (ValueError, TypeError, KeyError) as exc:
        raise AdmissionGuardError('history') from exc
    return RetainedPaidJob(job_id, owner, job.application_id, job.kind, binding[4], forgotten,
        binding[5], binding[6], binding[7], binding[8], job.model_dump_json(), parameters_json, dependencies)


def require_dispatch_admission(db, owner, job_id):
    retained = inspect_retained_paid_job(db, owner, job_id)
    if retained.origin != 'admitted' or retained.forgotten:
        raise AdmissionGuardError('head_mismatch')
    try:
        head = inspect_admission_head(db, owner, retained.application_id)
    except EvidenceError as exc:
        if exc.code == 'NOT_FOUND':
            raise AdmissionGuardError('head_mismatch') from exc
        raise
    if (head.last_job_id, head.revision) != (job_id, retained.consumed_revision):
        raise AdmissionGuardError('head_mismatch')
    return retained
