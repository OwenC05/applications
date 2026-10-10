"""Model-free strict drafting request and immutable publication contracts."""
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
from .schemas import DraftContext


class DraftPreviewRequest(Contract):
    batch_id: Identifier
    batch_sha256: Digest
    expected_revisions: RevisionVector
    model: Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')]
    max_output_tokens: Annotated[int, Field(ge=1, le=8000)] = 2000

    @field_validator('expected_revisions', mode='before')
    @classmethod
    def full_vector(cls, value):
        if isinstance(value, dict) and set(value) - {'schema_version'} != set(RevisionVector.model_fields) - {'schema_version'}:
            raise ValueError('Every revision dimension must be explicit')
        return value


class DraftRequest(DraftPreviewRequest):
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    disclosure_sha256: Digest
    acknowledged: Literal[True]

    @field_validator('acknowledged', mode='before')
    @classmethod
    def explicit_ack(cls, value):
        if value is not True:
            raise ValueError('Explicit acknowledgement required')
        return value


def draft_request_hash(owner, application, request):
    return canonical_hash(dict(profile_id=owner, application_id=application,
                               request=request.model_dump(mode='json')))


class DraftParameters(ApplicationOwned):
    request: DraftRequest
    request_sha256: Digest
    context: DraftContext
    disclosure: dict
    prompt_version: str = PROMPT_VERSION


class DraftIntent(DraftParameters):
    id: Identifier
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    revisions: RevisionVector
    state: Literal['registered', 'committed']


class DraftPublication(ApplicationOwned):
    id: Identifier
    draft_id: Identifier
    intent_id: Identifier
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    batch_id: Identifier
    batch_sha256: Digest
    text_sha256: Digest
    ledger_sha256: Digest
    request_sha256: Digest
    disclosure_sha256: Digest
    prompt_version: str
    stage_names: tuple[Literal['plan', 'draft', 'critique', 'rewrite', 'final_critique'], ...]
    reason_codes: tuple[str, ...]
    style_notes: tuple[str, ...]

    @property
    def publication_sha256(self):
        return canonical_hash(self)
