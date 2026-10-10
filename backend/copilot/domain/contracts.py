"""S0 contracts: validation is not authorization, entailment or a state transition.

Canonical hashes use UTF-8 JSON, sorted keys, compact separators, no Unicode
normalization and no nonfinite numbers. Text spans are half-open code points.
Services must enforce stored ownership, current revisions, leases and one-use grants.
"""
import hashlib
import json
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from ..contracts import Span

Identifier = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=100_000)]
Revision = Annotated[int, Field(ge=0)]
Sector = Literal['tech', 'finance']


def canonical_hash(value: BaseModel | dict | list) -> str:
    """Serialization contract; callers must supply schema-validated payloads."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def https_url(value: str) -> str:
    """Syntax only. DNS/egress safety belongs to the research/browser broker."""
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.port not in (None, 443) or parts.fragment):
        raise ValueError('Expected credential-free HTTPS URL on port 443 without fragment')
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    schema_version: Literal[1] = 1

    @field_validator('schema_version', 'reserved_calls', mode='before', check_fields=False)
    @classmethod
    def actual_integer_literal(cls, value):
        if type(value) is not int:
            raise ValueError('Integer literal must be an actual integer')
        return value

    @field_validator('*')
    @classmethod
    def aware_times(cls, value):
        if isinstance(value, datetime) and (value.utcoffset() is None or value.utcoffset() != timedelta(0)):
            raise ValueError('Timestamps must be UTC-aware')
        return value


class Owned(Contract):
    profile_id: Identifier


class ApplicationOwned(Owned):
    application_id: Identifier


class RevisionVector(Contract):
    metadata: Revision = 0
    facts: Revision = 0
    documents: Revision = 0
    consent: Revision = 0
    application_input: Revision = 0
    application_output: Revision = 0
    research: Revision = 0

    def matches(self, current: 'RevisionVector', dependencies: tuple[str, ...]) -> bool:
        allowed = set(type(self).model_fields) - {'schema_version'}
        if not dependencies or not set(dependencies) <= allowed:
            raise ValueError('Explicit known revision dependencies required')
        return all(getattr(self, name) == getattr(current, name) for name in dependencies)


class ProfileRecord(Owned):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    sectors: Annotated[tuple[Sector, ...], Field(min_length=1, max_length=2)]
    writing_preferences: Annotated[str, Field(max_length=10_000)] = ''
    revisions: RevisionVector

    @field_validator('sectors')
    @classmethod
    def unique_sectors(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Duplicate sector')
        return value


class ConsentRecord(Owned):
    id: Identifier
    provider: Annotated[str, Field(min_length=1, max_length=100)]
    purposes: tuple[Literal['research', 'drafting', 'assessment'], ...]
    granted: StrictBool
    revision: Revision
    disclosed_at: datetime


class TypedValue(Owned):
    """PII stays outside dense story retrieval; never inferred by a model."""
    id: Identifier
    kind: Literal['contact', 'identity', 'eligibility', 'demographic', 'declaration']
    field: Annotated[str, Field(min_length=1, max_length=200)]
    value: str | StrictBool
    purpose: Text
    jurisdiction: str | None = None
    application_id: Identifier | None = None
    explicitly_confirmed: StrictBool


    @field_validator('explicitly_confirmed')
    @classmethod
    def affirmative(cls, value):
        if value is not True:
            raise ValueError('Typed value requires explicit confirmation')
        return value


class InterviewAnswer(Owned):
    id: Identifier
    question_id: Annotated[str, Field(min_length=1, max_length=200)]
    question: Text
    section: Annotated[str, Field(min_length=1, max_length=200)]
    answer: Text
    created_at: datetime


class InterviewProgress(Owned):
    answer_ids: tuple[Identifier, ...] = ()
    skipped_question_ids: tuple[str, ...] = ()
    completed: StrictBool = False


class SourceSpan(Contract):
    record_id: Identifier
    unit_id: Identifier | None
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    text_sha256: Digest
    excerpt: Text

    @model_validator(mode='after')
    def bounds(self):
        if self.end <= self.start or len(self.excerpt) != self.end - self.start:
            raise ValueError('Span must contain exact code-point-length excerpt')
        return self

    def validate_text(self, text: str) -> None:
        if text_hash(text) != self.text_sha256 or text[self.start:self.end] != self.excerpt:
            raise ValueError('Span does not match original canonical text')

    def evidence_span(self) -> Span:
        """Reuse the existing evidence API span without changing its wire format."""
        if self.unit_id is None:
            raise ValueError('Confirmed fact citation has no document-unit span')
        return Span(self.unit_id, self.start, self.end)


class Proposal(Owned):
    id: Identifier
    text: Text
    status: Literal['pending', 'confirmed', 'rejected'] = 'pending'
    origin: Literal['manual', 'interview', 'document_derived', 'legacy', 'application_feedback']
    origin_id: Identifier | None = None
    source_spans: tuple[SourceSpan, ...] = ()
    supersedes_fact_id: Identifier | None = None
    created_at: datetime


class ConfirmFact(Owned):
    proposal_id: Identifier
    confirmed: StrictBool
    expected_facts_revision: Revision

    @field_validator('confirmed')
    @classmethod
    def affirmative(cls, value):
        if value is not True:
            raise ValueError('Explicit affirmative confirmation required')
        return value


class FactVersion(Owned):
    id: Identifier
    proposal_id: Identifier
    text: Text
    text_sha256: Digest
    origin: Literal['manual', 'interview', 'document_derived', 'legacy', 'application_feedback']
    confirmation_method: Literal['explicit_user'] = 'explicit_user'
    source_spans: tuple[SourceSpan, ...] = ()
    supersedes_fact_id: Identifier | None = None
    confirmation_event_id: Identifier
    confirmed_at: datetime

    @model_validator(mode='after')
    def exact_hash(self):
        if text_hash(self.text) != self.text_sha256:
            raise ValueError('Fact text hash mismatch')
        return self


class Question(Contract):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=10_000)]
    type: Literal['writing', 'text', 'select', 'radio', 'checkbox', 'file', 'sensitive']
    required: StrictBool = True
    max_words: Annotated[int, Field(gt=0)] | None = None
    max_chars: Annotated[int, Field(gt=0)] | None = None
    options: tuple[str, ...] = ()
    constraint_origin: Literal['user', 'vacancy', 'form']

    def check_answer(self, text: str) -> None:
        if (self.required and not text.strip()
                or self.max_words is not None and len(text.split()) > self.max_words
                or self.max_chars is not None and len(text) > self.max_chars):
            raise ValueError('Answer missing or exceeds declared limit')


class ApplicationRecord(ApplicationOwned):
    company: Annotated[str, Field(min_length=1, max_length=300)]
    role: Annotated[str, Field(min_length=1, max_length=300)]
    sector: Sector
    vacancy_url: str
    vacancy_id: str | None = None
    job_description: Annotated[str, Field(max_length=100_000)] = ''
    location: Annotated[str, Field(max_length=300)] = ''
    company_url: str | None = None
    official_domains: tuple[str, ...]
    questions: tuple[Question, ...]
    input_revision: Revision
    output_revision: Revision = 0
    created_at: datetime

    _vacancy_url = field_validator('vacancy_url')(https_url)
    _company_url = field_validator('company_url')(
        lambda value: https_url(value) if value is not None else None)

    @field_validator('questions')
    @classmethod
    def unique_questions(cls, value):
        if len({question.id for question in value}) != len(value):
            raise ValueError('Duplicate question ID')
        return value


class EmployerSource(ApplicationOwned):
    id: Identifier
    research_run_id: Identifier
    purpose: Literal['company', 'exact_role', 'supporting']
    provenance: Literal['public_fetch', 'anonymous_rendered_dom', 'user_supplied', 'legacy_model_text']
    original_url: str | None
    final_url: str | None
    acquired_at: datetime
    original_sha256: Digest
    canonical_sha256: Digest
    extraction_version: Text
    vacancy_id: str | None = None
    role_state: Literal['open', 'closed', 'unknown'] = 'unknown'

    _urls = field_validator('original_url', 'final_url')(
        lambda value: https_url(value) if value is not None else None)

    @model_validator(mode='after')
    def fetched_has_url(self):
        if self.provenance in ('public_fetch', 'anonymous_rendered_dom') and not self.final_url:
            raise ValueError('Fetched source requires final URL')
        return self


class ResearchRun(ApplicationOwned):
    id: Identifier
    revisions: RevisionVector
    state: Literal['incomplete', 'complete', 'closed_role', 'stale']
    company_source_ids: tuple[Identifier, ...] = ()
    exact_role_source_ids: tuple[Identifier, ...] = ()
    acquired_at: datetime
    expires_at: datetime
    gaps: tuple[str, ...] = ()

    @model_validator(mode='after')
    def completeness(self):
        if self.state == 'complete' and (not self.company_source_ids or not self.exact_role_source_ids
                                         or self.gaps):
            raise ValueError('Complete research requires company and exact-role sources without gaps')
        if not timedelta(0) < self.expires_at - self.acquired_at <= timedelta(days=7):
            raise ValueError('Research freshness must not exceed seven days')
        return self


class EvidenceReference(Owned):
    kind: Literal['confirmed_fact', 'employer']
    span: SourceSpan
    generation_id: Identifier
    application_id: Identifier | None = None
    research_run_id: Identifier | None = None

    @model_validator(mode='after')
    def employer_scope(self):
        if self.kind == 'employer' and (not self.application_id or not self.research_run_id):
            raise ValueError('Employer evidence requires application and run scope')
        if self.kind == 'confirmed_fact' and self.research_run_id is not None:
            raise ValueError('Personal evidence is not employer research')
        return self


class HiringCriterion(Contract):
    id: Identifier
    text: Text
    inferred: StrictBool
    evidence: Annotated[tuple[EvidenceReference, ...], Field(min_length=1)]

    @field_validator('evidence')
    @classmethod
    def employer_only(cls, value):
        if any(reference.kind != 'employer' for reference in value):
            raise ValueError('Hiring rubric requires employer evidence')
        return value


class EvidencePacket(ApplicationOwned):
    id: Identifier
    research_run_id: Identifier
    question: Question
    query_variants: Annotated[tuple[str, ...], Field(min_length=1, max_length=4)]
    criteria: tuple[str, ...] = ()
    facts: tuple[EvidenceReference, ...] = ()
    employer: tuple[EvidenceReference, ...] = ()
    revisions: RevisionVector
    model_version: Text
    prompt_version: Text
    truncated: StrictBool = False
    omitted: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    @model_validator(mode='after')
    def scoped_evidence(self):
        if len(self.question.text) > 2000:
            raise ValueError('Generation question exceeds 2000-character cap; explicit split required')
        for kind, references in [('confirmed_fact', self.facts), ('employer', self.employer)]:
            for reference in references:
                if (reference.kind != kind or reference.profile_id != self.profile_id
                        or reference.application_id not in (None, self.application_id)
                        or kind == 'employer' and reference.research_run_id != self.research_run_id):
                    raise ValueError('Evidence scope/kind/run mismatch')
        return self

    @property
    def packet_sha256(self) -> str:
        return canonical_hash(self)


class DraftAnswer(Contract):
    question_id: str
    text: Annotated[str, Field(max_length=100_000)]
    packet_id: Identifier


class SupportSpan(Contract):
    target: str  # question ID, or the reserved cover_letter target
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    excerpt: Text
    kind: Literal['personal', 'employer', 'inference']
    assessment: Literal['supported', 'unsupported', 'contradicted', 'needs_confirmation', 'unassessed']
    citations: tuple[EvidenceReference, ...] = ()
    integrity: Literal['valid', 'invalid', 'unassessed'] = 'unassessed'

    @model_validator(mode='after')
    def support_constraints(self):
        if self.end - self.start != len(self.excerpt):
            raise ValueError('Invalid support span')
        if self.assessment == 'supported' and (not self.citations or self.integrity != 'valid'):
            raise ValueError('Supported claim requires integrity-checked citations')
        required = {'personal': 'confirmed_fact', 'employer': 'employer'}.get(self.kind)
        if (self.assessment == 'supported' and required is not None
                and any(reference.kind != required for reference in self.citations)):
            raise ValueError('Supported claim requires the matching evidence kind')
        # Inference may cite either/both corpora; it is never a confirmed personal fact.
        return self


class DraftRevision(ApplicationOwned):
    id: Identifier
    revisions: RevisionVector
    research_run_id: Identifier
    cover_letter: Annotated[str, Field(max_length=100_000)] = ''
    answers: tuple[DraftAnswer, ...]
    ledger: tuple[SupportSpan, ...] = ()
    assessment_state: Literal['unassessed', 'assessed', 'stale'] = 'unassessed'
    inventory_complete: StrictBool = False
    created_at: datetime

    @model_validator(mode='after')
    def ledger_matches(self):
        targets = {answer.question_id: answer.text for answer in self.answers}
        if len(targets) != len(self.answers) or 'cover_letter' in targets:
            raise ValueError('Duplicate/reserved question ID')
        targets['cover_letter'] = self.cover_letter
        for span in self.ledger:
            if targets.get(span.target, '')[span.start:span.end] != span.excerpt:
                raise ValueError('Ledger does not match exact draft text')
            for reference in span.citations:
                if (reference.profile_id != self.profile_id
                        or reference.application_id not in (None, self.application_id)
                        or reference.research_run_id not in (None, self.research_run_id)):
                    raise ValueError('Ledger citation scope mismatch')
        return self

    @property
    def text_sha256(self) -> str:
        return canonical_hash({'cover_letter': self.cover_letter,
                               'answers': [{'question_id': answer.question_id, 'text': answer.text}
                                           for answer in self.answers]})

    @property
    def ledger_sha256(self) -> str:
        return canonical_hash([span.model_dump(mode='json') for span in self.ledger])

    @property
    def support_ready(self) -> bool:
        """Only the support gate; services also check limits, freshness and coverage."""
        return (self.assessment_state == 'assessed' and self.inventory_complete
                and all(span.assessment == 'supported' and span.integrity == 'valid'
                        for span in self.ledger))


class ReviewSnapshot(ApplicationOwned):
    id: Identifier
    draft_id: Identifier
    text_sha256: Digest
    ledger_sha256: Digest
    revisions: RevisionVector
    research_run_id: Identifier
    disclosure_sha256: Digest
    reviewed_at: datetime


class DurableJob(Owned):
    application_id: Identifier | None = None
    id: Identifier
    kind: Literal['research', 'index', 'packets', 'draft', 'assess', 'cleanup']
    stage: Text
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    state: Literal['queued', 'running', 'completed', 'cancelled', 'failed', 'indeterminate']
    revisions: RevisionVector
    fence: Revision
    lease_owner: Identifier | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancellation_requested: StrictBool = False
    attempt_count: Annotated[int, Field(ge=0, le=5)] = 0

    @model_validator(mode='after')
    def running_lease(self):
        if self.kind in ('research', 'packets', 'draft', 'assess') and self.application_id is None:
            raise ValueError('Application job requires application scope')
        if self.state == 'running' and (not self.lease_owner or not self.lease_expires_at or self.fence < 1):
            raise ValueError('Running job requires leased fence')
        return self


class ProviderAttempt(ApplicationOwned):
    id: Identifier
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    reserved_tokens: Annotated[int, Field(gt=0)]
    reserved_calls: Literal[1] = 1
    provider: Text
    model_version: Text
    consent_revision: Revision
    state: Literal['prepared', 'definitely_unsent', 'completed', 'indeterminate']
    prepared_at: datetime
    reported_tokens: Annotated[int, Field(ge=0)] | None = None


class GenerationIntent(Owned):
    generation_id: Identifier
    research_run_id: Identifier | None = None
    corpus: Literal['facts', 'documents', 'employer']
    application_id: Identifier | None = None
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    lease_expires_at: datetime
    revisions: RevisionVector
    snapshot_sha256: Digest
    model_fingerprint: Digest
    chunker_version: Text
    dense_collection: Annotated[str, Field(pattern=r'^evidence_[a-z0-9_]+$')]
    sparse_relpath: Annotated[str, Field(pattern=r'^[a-zA-Z0-9_-]+$')]
    state: Literal['registered', 'building', 'ready', 'published', 'abandoned', 'cleanup_pending', 'cleaned']

    @model_validator(mode='after')
    def generation_scope(self):
        if self.corpus == 'employer' and (self.application_id is None or self.research_run_id is None):
            raise ValueError('Employer generation requires application and research-run scope')
        return self


class FormField(Contract):
    id: Text
    label: str
    type: Literal['text', 'textarea', 'select', 'radio', 'checkbox', 'file', 'hidden']
    required: StrictBool
    options: tuple[str, ...] = ()
    max_chars: Annotated[int, Field(gt=0)] | None = None
    locator_hint: Text
    conditional_on: tuple[str, ...] = ()
    applicant_affecting: StrictBool = True


class FormSnapshot(ApplicationOwned):
    id: Identifier
    adapter: Text
    adapter_version: Text
    destination: str
    account_indicator: Text
    vacancy_id: Text
    step_id: Text
    revision: Revision
    fields: tuple[FormField, ...]
    captured_at: datetime

    _destination = field_validator('destination')(https_url)

    @field_validator('fields')
    @classmethod
    def distinct_fields(cls, value):
        if len({field.id for field in value}) != len(value):
            raise ValueError('Duplicate form field')
        return value


class FieldMapping(Contract):
    field_id: Text
    source_kind: Literal['answer', 'confirmed_fact', 'typed_value', 'attachment', 'manual']
    source_id: Text


class FieldMap(ApplicationOwned):
    id: Identifier
    form_snapshot_id: Identifier
    form_revision: Revision
    draft_id: Identifier
    mappings: tuple[FieldMapping, ...]

    @field_validator('mappings')
    @classmethod
    def distinct_mappings(cls, value):
        if len({mapping.field_id for mapping in value}) != len(value):
            raise ValueError('Duplicate field mapping')
        return value


class PreparedAttachment(Owned):
    id: Identifier
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    media_type: Literal['application/pdf']
    size_bytes: Annotated[int, Field(gt=0, le=10 * 1024 * 1024)]
    sha256: Digest
    approved_at: datetime

    @field_validator('filename')
    @classmethod
    def basename_only(cls, value):
        if '/' in value or '\\' in value or value in ('.', '..') or '\x00' in value:
            raise ValueError('Attachment filename must not be a path')
        return value


class SemanticField(Contract):
    step_id: Text
    id: Text
    value: str | StrictBool | tuple[str, ...]


class PayloadAttachment(Contract):
    step_id: Text
    field_id: Text
    attachment_id: Identifier
    filename: Text
    sha256: Digest
    media_type: Literal['application/pdf']
    size_bytes: Annotated[int, Field(gt=0)]


class SemanticPayload(ApplicationOwned):
    """destination is the exact action endpoint, not a host-only/canonical target.

    Adapter-normalized business values, never cookies or transport secrets.

    Per-step payloads have one entry per (step, field); callers include known
    applicant-affecting hidden defaults. Unknown encodings are not representable.
    """
    destination: str
    account_indicator: Text
    vacancy_id: Text
    form_snapshot_id: Identifier
    form_revision: Revision
    step_id: Text
    fields: tuple[SemanticField, ...]
    attachments: tuple[PayloadAttachment, ...] = ()

    _destination = field_validator('destination')(https_url)

    @model_validator(mode='after')
    def unique_fields(self):
        if any(field.step_id != self.step_id for field in (*self.fields, *self.attachments)):
            raise ValueError('Per-step payload contains another step')
        ids = [(field.step_id, field.id) for field in self.fields] + [(file.step_id, file.field_id) for file in self.attachments]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate semantic field')
        return self

    @property
    def sha256(self) -> str:
        value = self.model_dump(mode='json')
        value['fields'] = sorted(value['fields'], key=lambda field: (field['step_id'], field['id']))
        value['attachments'] = sorted(value['attachments'], key=lambda file: (file['step_id'], file['field_id']))
        return canonical_hash(value)

    def permits_subset(self, request: 'SemanticPayload') -> bool:
        """Fill/autosave subset only; not a grant/current-state authorization check."""
        excluded = {'fields', 'attachments'}
        if self.model_dump(exclude=excluded) != request.model_dump(exclude=excluded):
            return False
        fields = {(field.step_id, field.id): field for field in self.fields}
        attachments = {(file.step_id, file.field_id): file for file in self.attachments}
        return (all(fields.get((field.step_id, field.id)) == field for field in request.fields)
                and all(attachments.get((file.step_id, file.field_id)) == file for file in request.attachments))


class CompletedPayload(ApplicationOwned):
    """Final approval includes every step; repeated names stay step-qualified.

    Initial support requires one identical exact endpoint across steps. Multiple
    endpoints require manual handling until an explicit scoped contract revision.
    """
    steps: Annotated[tuple[SemanticPayload, ...], Field(min_length=1)]

    @model_validator(mode='after')
    def coherent_steps(self):
        first = self.steps[0]
        seen = set()
        for step in self.steps:
            if (step.profile_id != self.profile_id or step.application_id != self.application_id
                    or step.destination != first.destination
                    or step.account_indicator != first.account_indicator
                    or step.vacancy_id != first.vacancy_id or step.step_id in seen):
                raise ValueError('Completed payload has inconsistent or duplicate steps')
            seen.add(step.step_id)
        return self

    @property
    def sha256(self) -> str:
        # Preserve traversal order: swapping steps can change conditional semantics.
        return canonical_hash({'schema_version': self.schema_version,
                               'profile_id': self.profile_id, 'application_id': self.application_id,
                               'steps': [step.sha256 for step in self.steps]})


class ApprovalGrant(ApplicationOwned):
    id: Identifier
    payload_sha256: Digest
    review_id: Identifier
    issued_at: datetime
    expires_at: datetime
    revoked: StrictBool = False


class FillGrant(ApprovalGrant):
    kind: Literal['fill'] = 'fill'

    @model_validator(mode='after')
    def lifetime(self):
        if not timedelta(0) < self.expires_at - self.issued_at <= timedelta(minutes=15):
            raise ValueError('Fill grant lifetime must be at most 15 minutes')
        return self


class SubmitGrant(ApprovalGrant):
    kind: Literal['submit'] = 'submit'
    consumed_at: datetime | None = None

    @model_validator(mode='after')
    def lifetime(self):
        if not timedelta(0) < self.expires_at - self.issued_at <= timedelta(minutes=5):
            raise ValueError('Submit grant lifetime must be at most 5 minutes')
        if self.consumed_at is not None and not self.issued_at <= self.consumed_at <= self.expires_at:
            raise ValueError('Consumption outside grant lifetime')
        return self


class SubmissionAttempt(ApplicationOwned):
    id: Identifier
    grant_id: Identifier
    payload_sha256: Digest
    state: Literal['prepared', 'definitely_unsent', 'receipt_confirmed', 'submission_unknown']
    prepared_at: datetime
    final_action_at: datetime | None = None

    @model_validator(mode='after')
    def cannot_prove_unsent_after_action(self):
        if self.state == 'definitely_unsent' and self.final_action_at is not None:
            raise ValueError('Attempt with final action cannot be declared definitely unsent')
        return self


class SubmissionReceipt(ApplicationOwned):
    id: Identifier
    attempt_id: Identifier | None
    state: Literal['receipt_confirmed', 'user_reported_submitted', 'submission_unknown']
    reference: str | None = None
    confirmation_sha256: Digest | None = None
    recorded_at: datetime

    @model_validator(mode='after')
    def confirmed_evidence(self):
        if self.state == 'receipt_confirmed' and (not self.attempt_id or not self.confirmation_sha256):
            raise ValueError('Confirmed receipt requires attempt and confirmation evidence')
        if self.state == 'user_reported_submitted' and self.confirmation_sha256 is not None:
            raise ValueError('User report is not confirmation evidence')
        return self


class WorkspaceCapabilities(Contract):
    """Enable only after the corresponding implementation/release gate passes."""
    profile_management: StrictBool = False
    interview: StrictBool = False
    proposals: StrictBool = False
    application_management: StrictBool = False
    research: StrictBool = False
    drafting: StrictBool = False
    browser_fill: StrictBool = False
    browser_submit: StrictBool = False


class WorkspaceStatus(Contract):
    """Bootstrap envelope reusing X-Evidence-Token; never exposes a provider key."""
    boot_token: Annotated[str, Field(min_length=1, max_length=256)]
    capabilities: WorkspaceCapabilities = Field(default_factory=WorkspaceCapabilities)
    cloud_key_configured: StrictBool = False


class ProfileDetail(Contract):
    profile: ProfileRecord
    consent: ConsentRecord | None = None
    interview: InterviewProgress
    interview_answers: tuple[InterviewAnswer, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    applications: tuple[ApplicationRecord, ...] = ()
    typed_values: tuple[TypedValue, ...] = ()

    @model_validator(mode='after')
    def same_profile(self):
        owner = self.profile.profile_id
        records = (self.interview, *self.interview_answers, *self.proposals,
                   *self.applications, *self.typed_values)
        if self.consent is not None:
            records = (*records, self.consent)
        if any(record.profile_id != owner for record in records):
            raise ValueError('Profile detail contains foreign-owned records')
        return self


class ProfileCreate(Contract):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    sectors: Annotated[tuple[Sector, ...], Field(min_length=1, max_length=2)]

    @field_validator('sectors')
    @classmethod
    def unique_sectors(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Duplicate sector')
        return value


class ProfilePatch(Contract):
    """Consent changes require their separate explicit endpoint."""
    expected_metadata_revision: Revision
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    sectors: Annotated[tuple[Sector, ...], Field(min_length=1, max_length=2)] | None = None
    writing_preferences: Annotated[str, Field(max_length=10_000)] | None = None

    @field_validator('sectors')
    @classmethod
    def unique_sectors(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError('Duplicate sector')
        return value

    @model_validator(mode='after')
    def nonempty_patch(self):
        if all(getattr(self, name) is None for name in ('name', 'sectors', 'writing_preferences')):
            raise ValueError('Profile patch requires a metadata change')
        return self


class ProfileList(Contract):
    profiles: tuple[ProfileRecord, ...]


class DeleteResult(Contract):
    deleted: StrictBool
    cleanup_pending: StrictBool = False
