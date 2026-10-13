"""Strict local writing-review captures; schema validity grants no permission."""
from datetime import datetime
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic_core import core_schema

from ..domain.contracts import (
    ApplicationOwned,
    Contract,
    Digest,
    DraftAnswer,
    Identifier,
    Question,
    RevisionVector,
    SupportSpan,
    canonical_hash,
)
from ..retrieval.packet_contracts import CoverLetterTarget, ManualRequirement

_IDENTIFIER = TypeAdapter(Identifier, config={'strict': True})
_Model = TypeVar('_Model', bound=BaseModel)
ReviewWarnings = tuple[
    Literal['WRITING_ONLY'],
    Literal['AUTOMATED_SUPPORT_FALLIBLE'],
    Literal['NOT_FACT_CONFIRMATION'],
    Literal['MANUAL_AND_ATTACHMENTS_NOT_REVIEWED'],
    Literal['NO_BROWSER_AUTHORIZATION'],
]


def _validation_input(value):
    """Preserve unchecked missing/extra values recursively, not model_dump output.

    Python tuples remain tuples and lists remain lists. JSON arrays are handled by
    Pydantic's JSON validation path, not by coercing Python input through JSON.
    """
    if isinstance(value, BaseModel):
        values = dict(value.__dict__)
        values.update(value.__pydantic_extra__ or {})
        return {key: _validation_input(item) for key, item in values.items()}
    if isinstance(value, dict):
        return {key: _validation_input(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_validation_input(item) for item in value)
    if isinstance(value, list):
        return [_validation_input(item) for item in value]
    return value


def review_validated(value, model: type[_Model]) -> _Model:
    """Strictly reconstruct a review capture, including forged nested instances."""
    return model.model_validate(_validation_input(value))


def _full_vector(value):
    value = _validation_input(value)
    if isinstance(value, dict) and set(value) - {'schema_version'} != set(
            RevisionVector.model_fields) - {'schema_version'}:
        raise ValueError('Every revision dimension must be explicit')
    return value


class _StrictReview(Contract):
    model_config = ConfigDict(revalidate_instances='always')

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        # Keep native strict JSON array/datetime handling. A before-model validator
        # would convert JSON to Python containers before strict field validation.
        schema = handler(source)
        return core_schema.json_or_python_schema(
            json_schema=schema,
            python_schema=core_schema.no_info_before_validator_function(
                _validation_input, schema))


class ReviewPreviewRequest(_StrictReview):
    expected_revisions: RevisionVector
    parent_text_sha256: Digest
    parent_ledger_sha256: Digest
    parent_publication_sha256: Digest

    _vector = field_validator('expected_revisions', mode='before')(_full_vector)


class ReviewRequest(ReviewPreviewRequest):
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    disclosure_sha256: Digest
    acknowledged: Literal[True]

    @field_validator('acknowledged', mode='before')
    @classmethod
    def explicit_ack(cls, value):
        if value is not True:
            raise ValueError('Explicit acknowledgement required')
        return value


def review_request_hash(owner, application_id, parent_id, request):
    capture = review_validated(request, ReviewRequest)
    return canonical_hash(dict(operation='human_writing_review',
        profile_id=_IDENTIFIER.validate_python(owner),
        application_id=_IDENTIFIER.validate_python(application_id),
        parent_draft_id=_IDENTIFIER.validate_python(parent_id),
        request=capture.model_dump(mode='json')))


class ReviewDisclosure(_StrictReview, ApplicationOwned):
    operation: Literal['human_writing_review'] = 'human_writing_review'
    parent_draft_id: Identifier
    parent_publication_id: Identifier
    parent_publication_sha256: Digest
    batch_id: Identifier
    batch_sha256: Digest
    research_run_id: Identifier
    pre_revisions: RevisionVector
    post_revisions: RevisionVector
    text_sha256: Digest
    ledger_sha256: Digest
    cover_letter: Annotated[str, Field(max_length=100_000)]
    answers: tuple[DraftAnswer, ...]
    ledger: tuple[SupportSpan, ...]
    canonical_questions: tuple[Question, ...]
    cover_letter_target: CoverLetterTarget | None
    manual_requirements: tuple[ManualRequirement, ...]
    style_notes: tuple[str, ...]
    warnings: ReviewWarnings

    _vectors = field_validator('pre_revisions', 'post_revisions', mode='before')(_full_vector)

    @model_validator(mode='after')
    def output_only_next_vector(self):
        expected = self.pre_revisions.model_dump()
        expected['application_output'] += 1
        if self.post_revisions.model_dump() != expected:
            raise ValueError('Review changes only the output revision by exactly one')
        return self


class ReviewPublication(_StrictReview, ApplicationOwned):
    origin: Literal['review'] = 'review'
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
    edit_depth: Annotated[int, Field(ge=0, le=64)]
    created_at: datetime
    request: ReviewRequest
    request_sha256: Digest
    disclosure: ReviewDisclosure
    review_snapshot_id: Identifier
    review_snapshot_sha256: Digest
    text_sha256: Digest
    ledger_sha256: Digest
    reason_codes: tuple[str, ...]
    style_notes: tuple[str, ...]

    _vector = field_validator('pre_revisions', mode='before')(_full_vector)

    @field_validator('reason_codes')
    @classmethod
    def no_blocking_reasons(cls, value):
        if value:
            raise ValueError('Reviewed writing cannot retain blocking reasons')
        return value

    @property
    def publication_sha256(self):
        return canonical_hash(review_validated(self, ReviewPublication))
