"""Inactive structural inference contracts; no semantic or authority approval."""
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import core_schema

from ..domain.contracts import Contract, Digest, Identifier, SourceSpan, canonical_hash
from ..retrieval.packet_contracts import CorpusScope

VALIDATOR_VERSION = 'inferred-rubric-structural-v1'
MAX_SOURCES = 6
MAX_CANONICAL_CODEPOINTS = 1_000_000
MAX_PROPOSALS = 20
MAX_SUPPORTING_SPANS = 4
MAX_TOTAL_SPANS = 80
MAX_TEXT_CODEPOINTS = 2000


def valid_text(value, maximum=MAX_TEXT_CODEPOINTS):
    return (type(value) is str and 0 < len(value) <= maximum
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
            and bool(value.strip()))


def span_key(span):
    return (span.record_id, span.unit_id, span.start, span.end, span.text_sha256, span.excerpt)


def candidate_id(scope, capture_sha256, text, spans):
    return str(uuid5(NAMESPACE_URL, canonical_hash({
        'validator_version': VALIDATOR_VERSION, 'scope': scope.model_dump(mode='json'),
        'capture_sha256': capture_sha256, 'text': text,
        'supporting_spans': [span.model_dump(mode='json') for span in spans],
    })))


def _validation_input(value):
    """Retain unchecked attributes, extras and container types recursively."""
    if isinstance(value, BaseModel):
        values = {**value.__dict__, **(value.__pydantic_extra__ or {})}
        return {key: _validation_input(item) for key, item in values.items()}
    if isinstance(value, dict):
        return {key: _validation_input(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_validation_input(item) for item in value)
    if isinstance(value, list):
        return [_validation_input(item) for item in value]
    return value


class _RubricContract(Contract):
    model_config = ConfigDict(revalidate_instances='always')

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        # Preserve native strict JSON arrays without coercing Python lists into
        # tuples. Python model instances must first expose all raw attributes.
        schema = handler(source)
        return core_schema.json_or_python_schema(
            json_schema=schema,
            python_schema=core_schema.no_info_before_validator_function(
                _validation_input, schema))


class RubricProposal(_RubricContract):
    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT_CODEPOINTS)]
    supporting_spans: Annotated[tuple[SourceSpan, ...], Field(min_length=1, max_length=4)]

    @field_validator('text')
    @classmethod
    def nonblank_unicode(cls, value):
        if not valid_text(value):
            raise ValueError('Invalid rubric text')
        return value

    @model_validator(mode='after')
    def bounded_spans(self):
        if any(span.unit_id is None or not valid_text(span.excerpt) for span in self.supporting_spans):
            raise ValueError('Invalid rubric spans')
        return self


class RubricCandidate(RubricProposal):
    id: Identifier
    proposal_index: Annotated[int, Field(ge=0, lt=MAX_PROPOSALS)]
    inferred: Literal[True] = True

    @field_validator('inferred', mode='before')
    @classmethod
    def actual_true(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError('Rubric candidates are inferred')
        return value

    @model_validator(mode='after')
    def canonical_spans(self):
        keys = tuple(span_key(span) for span in self.supporting_spans)
        identities = {(s.record_id, s.unit_id, s.start, s.end) for s in self.supporting_spans}
        if keys != tuple(sorted(keys)) or len(identities) != len(keys):
            raise ValueError('Invalid canonical rubric spans')
        return self


class RubricOmission(_RubricContract):
    proposal_index: Annotated[int, Field(ge=0, lt=MAX_PROPOSALS)]
    reason: Literal['malformed_proposal', 'supporting_span_limit', 'unknown_source',
                    'unit_mismatch', 'unsupported_provenance', 'hash_mismatch',
                    'range_out_of_bounds', 'excerpt_too_long', 'blank_excerpt',
                    'excerpt_mismatch', 'duplicate_span', 'duplicate_candidate']


class RubricValidation(_RubricContract):
    scope: CorpusScope
    capture_sha256: Digest
    validator_version: Literal['inferred-rubric-structural-v1'] = VALIDATOR_VERSION
    submitted_proposal_count: Annotated[int, Field(ge=0, le=MAX_PROPOSALS)]
    candidates: Annotated[tuple[RubricCandidate, ...], Field(max_length=MAX_PROPOSALS)]
    omissions: Annotated[tuple[RubricOmission, ...], Field(max_length=MAX_PROPOSALS)]
    gaps: tuple[Literal['no_sources', 'no_proposals', 'no_candidates', 'proposals_omitted'], ...]
    citation_validation: Literal['supplied_canonical_text_only'] = 'supplied_canonical_text_only'
    semantic_validation: Literal['not_assessed'] = 'not_assessed'
    authority_validation: Literal['not_checked'] = 'not_checked'

    @model_validator(mode='after')
    def complete_identity(self):
        indices = [child.proposal_index for child in (*self.candidates, *self.omissions)]
        ids = [child.id for child in self.candidates]
        if (self.scope.corpus != 'employer' or len(set(ids)) != len(ids)
                or sorted(indices) != list(range(self.submitted_proposal_count))
                or any(child.id != candidate_id(self.scope, self.capture_sha256, child.text,
                                               child.supporting_spans) for child in self.candidates)):
            raise ValueError('Invalid rubric identity or outcome accounting')
        # Source presence is not recoverable from a digest; other gaps are checkable.
        expected = tuple(gap for gap, applies in (
            ('no_sources', 'no_sources' in self.gaps),
            ('no_proposals', self.submitted_proposal_count == 0),
            ('no_candidates', not self.candidates), ('proposals_omitted', bool(self.omissions)),
        ) if applies)
        if self.gaps != expected or ('no_sources' in self.gaps and self.candidates):
            raise ValueError('Invalid rubric gaps')
        return self
