"""Strict local-only edit requests and immutable parent-linked publications."""
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
from .quality import validate_targets, validated
from .schemas import DraftContext, DraftOutput


class EditRequest(Contract):
    expected_revisions: RevisionVector
    parent_text_sha256: Digest
    parent_ledger_sha256: Digest
    parent_publication_sha256: Digest
    draft: DraftOutput
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator('expected_revisions', mode='before')
    @classmethod
    def full_vector(cls, value):
        if isinstance(value, dict) and set(value) - {'schema_version'} != set(RevisionVector.model_fields) - {'schema_version'}:
            raise ValueError('Every revision dimension must be explicit')
        return value


def edit_request_hash(owner, application_id, parent_id, request):
    request = validated(request, EditRequest)
    return canonical_hash(dict(operation='local_draft_edit', profile_id=owner,
        application_id=application_id, parent_draft_id=parent_id, request=request.model_dump(mode='json')))


def edit_reason_codes(context: DraftContext, output: DraftOutput):
    context = validated(context, DraftContext)
    output = validated(output, DraftOutput)
    validate_targets(context, output.targets)
    reasons = ['ASSESSMENT_REQUIRED']
    for target, generated in zip(context.targets, output.targets, strict=True):
        question, text = target.question, generated.text
        if question.required and not text.strip():
            reasons.append('REQUIRED_ANSWER_MISSING')
        if (question.max_chars is not None and len(text) > question.max_chars
                or question.max_words is not None and len(text.split()) > question.max_words):
            reasons.append('OUTPUT_LIMIT_EXCEEDED')
    if context.required_manual_question_ids:
        reasons.append('MANUAL_REQUIREMENTS_UNRESOLVED')
    return tuple(dict.fromkeys(reasons))


class EditPublication(ApplicationOwned):
    origin: Literal['edit'] = 'edit'
    id: Identifier
    draft_id: Identifier
    parent_draft_id: Identifier
    parent_text_sha256: Digest
    parent_ledger_sha256: Digest
    parent_publication_id: Identifier
    parent_publication_sha256: Digest
    batch_id: Identifier
    batch_sha256: Digest
    pre_revisions: RevisionVector
    edit_depth: Annotated[int, Field(ge=1, le=64)]
    created_at: datetime
    request: EditRequest
    request_sha256: Digest
    text_sha256: Digest
    ledger_sha256: Digest
    reason_codes: tuple[str, ...]
    style_notes: tuple[str, ...] = ()

    @field_validator('pre_revisions', mode='before')
    @classmethod
    def full_pre_vector(cls, value):
        if isinstance(value, dict) and set(value) - {'schema_version'} != set(RevisionVector.model_fields) - {'schema_version'}:
            raise ValueError('Every revision dimension must be explicit')
        return value

    @property
    def publication_sha256(self):
        return canonical_hash(self)
