"""Pure quote candidates from caller-selected canonical employer spans.

No IO, model, store writes, relevance selection or currentness/authorization
proof. Capture envelopes must come from the runtime's current canonical capture;
this function validates only their supplied scope, relationships and text hashes.
Original bytes/origin/receipt are deliberately not inspected or authenticated.
Candidates are NOT HiringCriterion or generation-bound evidence. A later binder
must obtain a current employer generation and resolve each canonical span again.
"""
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, field_validator, model_validator

from ..domain.contracts import Contract, Digest, Identifier, SourceSpan, canonical_hash, text_hash
from ..retrieval.packet_contracts import CorpusScope
from .broker import MAX_CANONICAL_CODEPOINTS, MAX_SOURCES

COMPILER_VERSION = 'excerpt-criteria-v1'
MAX_SELECTIONS = 100
MAX_CANDIDATES = 20
MAX_EXCERPT_CODEPOINTS = 2000


class ExcerptSelection(Contract):
    source_id: Identifier
    unit_id: Identifier
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('Selection must be a nonempty half-open code-point range')
        return self


class CriterionCandidate(Contract):
    """Exact quote only; no inference, entailment or hiring-manager attribution."""
    id: Identifier
    scope: CorpusScope
    span: SourceSpan
    text: Annotated[str, Field(min_length=1, max_length=MAX_EXCERPT_CODEPOINTS)]
    inferred: Literal[False] = False
    compiler_version: Literal['excerpt-criteria-v1'] = COMPILER_VERSION

    @field_validator('scope', mode='before')
    @classmethod
    def validated_scope(cls, value):
        return _revalidate(CorpusScope, value)

    @field_validator('span', mode='before')
    @classmethod
    def validated_span(cls, value):
        return _revalidate(SourceSpan, value)

    @field_validator('inferred', mode='before')
    @classmethod
    def actual_false(cls, value):
        if type(value) is not bool or value is not False:
            raise ValueError('Candidate is never inferred')
        return value

    @model_validator(mode='after')
    def exact_quote(self):
        if (type(self.inferred) is not bool or self.inferred is not False
                or self.scope.corpus != 'employer' or self.span.unit_id is None
                or self.text != self.span.excerpt
                or self.id != _candidate_id(self.scope, self.span)):
            raise ValueError('Candidate must be an exact scoped quote with deterministic identity')
        return self


class SelectionOmission(Contract):
    selection_index: Annotated[int, Field(ge=0, lt=MAX_SELECTIONS)]
    reason: Literal['unknown_source', 'unit_mismatch', 'unsupported_provenance',
                    'range_out_of_bounds', 'excerpt_too_long', 'duplicate_span',
                    'candidate_limit']


class CriterionCompilation(Contract):
    scope: CorpusScope
    candidates: Annotated[tuple[CriterionCandidate, ...], Field(max_length=MAX_CANDIDATES)]
    omissions: Annotated[tuple[SelectionOmission, ...], Field(max_length=MAX_SELECTIONS)]
    gaps: tuple[Literal['no_sources', 'no_selections', 'no_candidates', 'selections_omitted'], ...]
    compiler_version: Literal['excerpt-criteria-v1'] = COMPILER_VERSION


class _Source(Contract):
    id: Identifier
    profile_id: Identifier
    application_id: Identifier
    research_run_id: Identifier
    canonical_sha256: Digest
    provenance: Literal['public_fetch', 'anonymous_rendered_dom', 'user_supplied', 'legacy_model_text']


class _Unit(Contract):
    id: Identifier
    source_id: Identifier
    application_id: Identifier
    research_run_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=MAX_CANONICAL_CODEPOINTS)]
    text_sha256: Digest


def _candidate_id(scope, span):
    identity = canonical_hash({'scope': scope.model_dump(mode='json'),
                               'span': span.model_dump(mode='json'),
                               'compiler_version': COMPILER_VERSION})
    return str(uuid5(NAMESPACE_URL, identity))


def _revalidate(cls, value):
    # model_copy/model_construct bypass validation; never trust model instances.
    return cls.model_validate(value.model_dump() if isinstance(value, cls) else value)


def compile_excerpt_criteria(
    scope: CorpusScope,
    captured_values: list | tuple,
    selections: list | tuple,
) -> CriterionCompilation:
    """Compile <=20 exact quotes; malformed capture fails closed with ValueError.

    <=100 explicit selections are accepted, preserving an indexed omission for
    every unsupported/duplicate/overflow selection. No iterable is consumed
    without an upfront bound. Extra capture metadata is ignored, not trusted.
    """
    scope = _revalidate(CorpusScope, scope)
    if scope.corpus != 'employer':
        raise ValueError('Exact employer scope required')
    if type(captured_values) not in (list, tuple) or len(captured_values) > MAX_SOURCES:
        raise ValueError('Bounded canonical source sequence required')
    if type(selections) not in (list, tuple) or len(selections) > MAX_SELECTIONS:
        raise ValueError('Bounded explicit selection sequence required')
    selections = tuple(_revalidate(ExcerptSelection, value) for value in selections)
    sources = {}
    unit_ids = set()
    for value in captured_values:
        if (type(value) is not dict or type(value.get('source')) is not dict
                or type(value.get('unit')) is not dict):
            raise ValueError('Canonical source/unit envelope required')
        raw_source, raw_unit = value['source'], value['unit']
        source = _Source.model_validate({key: raw_source[key] for key in _Source.model_fields
                                        if key in raw_source})
        unit = _Unit.model_validate({key: raw_unit[key] for key in _Unit.model_fields
                                    if key in raw_unit})
        if ((source.profile_id, source.application_id, source.research_run_id)
                != (scope.profile_id, scope.application_id, scope.research_run_id)
                or (unit.source_id, unit.application_id, unit.research_run_id)
                != (source.id, scope.application_id, scope.research_run_id)
                or ('profile_id' in raw_unit and raw_unit['profile_id'] != scope.profile_id)
                or unit.text_sha256 != source.canonical_sha256
                or text_hash(unit.text) != unit.text_sha256
                or source.id in sources or unit.id in unit_ids):
            raise ValueError('Canonical scope, graph or full text hash mismatch')
        sources[source.id] = (source, unit)
        unit_ids.add(unit.id)

    candidates, omissions, seen = [], [], set()
    for index, selection in enumerate(selections):
        reason = None
        pair = sources.get(selection.source_id)
        if pair is None:
            reason = 'unknown_source'
        else:
            source, unit = pair
            key = (source.id, unit.id, selection.start, selection.end)
            if selection.unit_id != unit.id:
                reason = 'unit_mismatch'
            elif source.provenance != 'public_fetch':
                reason = 'unsupported_provenance'
            elif selection.end > len(unit.text):
                reason = 'range_out_of_bounds'
            elif selection.end - selection.start > MAX_EXCERPT_CODEPOINTS:
                reason = 'excerpt_too_long'
            elif key in seen:
                reason = 'duplicate_span'
            elif len(candidates) >= MAX_CANDIDATES:
                reason = 'candidate_limit'
            else:
                seen.add(key)
                excerpt = unit.text[selection.start:selection.end]
                span = SourceSpan(record_id=source.id, unit_id=unit.id,
                                  start=selection.start, end=selection.end,
                                  text_sha256=unit.text_sha256, excerpt=excerpt)
                span.validate_text(unit.text)
                candidates.append(CriterionCandidate(id=_candidate_id(scope, span), scope=scope,
                                                     span=span, text=excerpt))
        if reason:
            omissions.append(SelectionOmission(selection_index=index, reason=reason))
    gaps = []
    if not sources:
        gaps.append('no_sources')
    if not selections:
        gaps.append('no_selections')
    if not candidates:
        gaps.append('no_candidates')
    if omissions:
        gaps.append('selections_omitted')
    return CriterionCompilation(scope=scope, candidates=tuple(candidates),
                                omissions=tuple(omissions), gaps=tuple(gaps))
