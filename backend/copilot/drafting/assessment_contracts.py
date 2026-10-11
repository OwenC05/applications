"""Strict exact-text reassessment captures; no caller-supplied evidence authority."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from ..domain.contracts import (
    ApplicationOwned,
    Contract,
    Digest,
    Identifier,
    RevisionVector,
    canonical_hash,
)
from .prompts import PROMPT_VERSION
from .runtime_contracts import DraftRequest
from .schemas import DraftContext, DraftOutput


class AssessmentPreviewRequest(Contract):
    expected_revisions: RevisionVector
    parent_text_sha256: Digest
    parent_ledger_sha256: Digest
    parent_publication_sha256: Digest
    model: Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')]
    max_output_tokens: Annotated[int, Field(ge=1, le=8000)] = 2000

    @field_validator('expected_revisions', mode='before')
    @classmethod
    def full_vector(cls, value):
        if isinstance(value, dict) and set(value) - {'schema_version'} != set(RevisionVector.model_fields) - {'schema_version'}:
            raise ValueError('Every revision dimension must be explicit')
        return value


class AssessmentRequest(AssessmentPreviewRequest):
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    disclosure_sha256: Digest
    acknowledged: Literal[True]

    @field_validator('acknowledged', mode='before')
    @classmethod
    def explicit_ack(cls, value):
        if value is not True:
            raise ValueError('Explicit acknowledgement required')
        return value


def assessment_request_hash(owner, application_id, parent_id, request):
    return canonical_hash(dict(operation='draft_reassessment', profile_id=owner,
        application_id=application_id, parent_draft_id=parent_id, request=request.model_dump(mode='json')))


class AssessmentParameters(ApplicationOwned):
    parent_draft_id: Identifier
    request: AssessmentRequest
    request_sha256: Digest
    source_request: DraftRequest
    context: DraftContext
    input_draft: DraftOutput
    batch_id: Identifier
    batch_sha256: Digest
    disclosure: dict
    prompt_version: str = PROMPT_VERSION


class AssessmentIntent(AssessmentParameters):
    id: Identifier
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    pre_revisions: RevisionVector
    state: Literal['registered', 'committed']
    origin: Literal['assessment'] = 'assessment'

    @field_validator('pre_revisions', mode='before')
    @classmethod
    def full_pre_vector(cls, value):
        return AssessmentPreviewRequest.full_vector(value)


class AssessmentPublication(ApplicationOwned):
    origin: Literal['assessment'] = 'assessment'
    id: Identifier
    draft_id: Identifier
    parent_draft_id: Identifier
    parent_text_sha256: Digest
    parent_ledger_sha256: Digest
    parent_publication_id: Identifier
    parent_publication_sha256: Digest
    batch_id: Identifier
    batch_sha256: Digest
    job_id: Identifier
    intent_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    pre_revisions: RevisionVector
    edit_depth: Annotated[int, Field(ge=1, le=64)]
    created_at: datetime
    request_sha256: Digest
    disclosure_sha256: Digest
    prompt_version: str
    text_sha256: Digest
    ledger_sha256: Digest
    stage_names: tuple[Literal['reassess']]
    reason_codes: tuple[str, ...]
    style_notes: tuple[str, ...]

    @field_validator('pre_revisions', mode='before')
    @classmethod
    def full_pre_vector(cls, value):
        return AssessmentPreviewRequest.full_vector(value)

    @property
    def publication_sha256(self):
        return canonical_hash(self)
