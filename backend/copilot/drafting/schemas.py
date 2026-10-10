"""Pure, bounded drafting protocols. Model classifications are fallible."""
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from ..domain.contracts import (
    Contract,
    Digest,
    EvidenceReference,
    HiringCriterion,
    Identifier,
    Question,
    Sector,
    SupportSpan,
    canonical_hash,
)
from ..retrieval.packet_contracts import ManualRequirement

BoundedText = Annotated[str, Field(max_length=1000)]
TargetID = Annotated[str, Field(min_length=1, max_length=200)]


def check_unicode(value):
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('Invalid Unicode')
    elif isinstance(value, dict):
        for key, item in value.items():
            check_unicode(key)
            check_unicode(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            check_unicode(item)


class DraftContract(Contract):
    @model_validator(mode='after')
    def unicode_valid(self):
        check_unicode(self.model_dump(mode='json'))
        return self


class SelectedEvidence(DraftContract):
    id: Digest
    reference: EvidenceReference

    @model_validator(mode='after')
    def canonical_id(self):
        if self.id != canonical_hash(self.reference):
            raise ValueError('Noncanonical evidence ID')
        return self


class TargetContext(DraftContract):
    question: Question
    packet_id: Identifier
    facts: Annotated[tuple[SelectedEvidence, ...], Field(max_length=64)] = ()
    employer: Annotated[tuple[SelectedEvidence, ...], Field(max_length=64)] = ()
    criteria: Annotated[tuple[HiringCriterion, ...], Field(max_length=64)] = ()
    gaps: Annotated[tuple[BoundedText, ...], Field(max_length=100)] = ()

    @model_validator(mode='after')
    def selected_only(self):
        if (len(self.question.text) > 2000 or len(self.question.options) > 100
                or any(len(option) > 1000 for option in self.question.options)):
            raise ValueError('Question exceeds bounded drafting protocol')
        if self.question.type != 'writing':
            raise ValueError('Writing targets only')
        evidence = self.facts + self.employer
        if len({item.id for item in evidence}) != len(evidence):
            raise ValueError('Duplicate selected evidence')
        for kind, items in (('confirmed_fact', self.facts), ('employer', self.employer)):
            if any(item.reference.kind != kind for item in items):
                raise ValueError('Wrong selected evidence kind')
        if any(len(item.evidence) > 64
               or len({canonical_hash(ref) for ref in item.evidence}) != len(item.evidence)
               for item in self.criteria):
            raise ValueError('Criterion evidence exceeds bounded unique selection')
        selected = {item.id for item in self.employer}
        if len({item.id for item in self.criteria}) != len(self.criteria):
            raise ValueError('Duplicate criterion')
        if any(not {canonical_hash(ref) for ref in item.evidence} <= selected for item in self.criteria):
            raise ValueError('Unselected criterion evidence')
        return self


class DraftContext(DraftContract):
    batch_id: Identifier
    batch_sha256: Digest
    profile_id: Identifier
    application_id: Identifier
    research_run_id: Identifier
    company: Annotated[str, Field(min_length=1, max_length=300)]
    role: Annotated[str, Field(min_length=1, max_length=300)]
    sector: Sector
    targets: Annotated[tuple[TargetContext, ...], Field(min_length=1, max_length=13)]
    manual_requirements: Annotated[tuple[ManualRequirement, ...], Field(max_length=13)] = ()
    required_manual_question_ids: Annotated[tuple[TargetID, ...], Field(max_length=13)]

    @model_validator(mode='after')
    def exact_targets(self):
        ids = [item.question.id for item in self.targets]
        if len(set(ids)) != len(ids) or ('cover_letter' in ids and ids[-1] != 'cover_letter'):
            raise ValueError('Duplicate or misplaced target')
        if len({item.packet_id for item in self.targets}) != len(ids):
            raise ValueError('Duplicate packet')
        manual_ids = [item.question_id for item in self.manual_requirements]
        if len(set(manual_ids)) != len(manual_ids) or set(manual_ids) & set(ids):
            raise ValueError('Duplicate manual target')
        required_ids = self.required_manual_question_ids
        if len(set(required_ids)) != len(required_ids) or not set(required_ids) <= set(manual_ids):
            raise ValueError('Required manual IDs must be a unique subset of manual requirements')
        for target in self.targets:
            for item in target.facts + target.employer:
                ref = item.reference
                if (ref.profile_id != self.profile_id
                        or ref.application_id not in (None, self.application_id)
                        or ref.research_run_id not in (None, self.research_run_id)):
                    raise ValueError('Evidence scope mismatch')
        return self


class PlanItem(DraftContract):
    target: TargetID
    outline: Annotated[tuple[BoundedText, ...], Field(max_length=20)] = ()
    evidence_ids: Annotated[tuple[Digest, ...], Field(max_length=64)] = ()
    gaps: Annotated[tuple[BoundedText, ...], Field(max_length=100)] = ()


class PlanOutput(DraftContract):
    targets: Annotated[tuple[PlanItem, ...], Field(min_length=1, max_length=13)]


class GeneratedText(DraftContract):
    target: TargetID
    text: Annotated[str, Field(max_length=100000)]


class DraftOutput(DraftContract):
    targets: Annotated[tuple[GeneratedText, ...], Field(min_length=1, max_length=13)]


class TextRange(DraftContract):
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    excerpt: Annotated[str, Field(min_length=1, max_length=100000)]

    @model_validator(mode='after')
    def range_length(self):
        if self.end - self.start != len(self.excerpt):
            raise ValueError('Invalid code-point range')
        return self


class Claim(TextRange):
    kind: Literal['personal', 'employer', 'inference']
    assessment: Literal['supported', 'unsupported', 'contradicted', 'needs_confirmation', 'unassessed']
    evidence_ids: Annotated[tuple[Digest, ...], Field(max_length=64)] = ()


class TargetAssessment(DraftContract):
    target: TargetID
    inventory_complete: StrictBool
    claims: Annotated[tuple[Claim, ...], Field(max_length=500)] = ()
    nonfactual_ranges: Annotated[tuple[TextRange, ...], Field(max_length=500)] = ()


class CritiqueOutput(DraftContract):
    text_sha256: Digest
    targets: Annotated[tuple[TargetAssessment, ...], Field(min_length=1, max_length=13)]
    style_notes: Annotated[tuple[BoundedText, ...], Field(max_length=20)] = ()
    rewrite_required: StrictBool


class QualityResult(DraftContract):
    ledger: Annotated[tuple[SupportSpan, ...], Field(max_length=6500)]
    inventory_complete: StrictBool
    reason_codes: Annotated[tuple[BoundedText, ...], Field(max_length=100)]
    style_notes: Annotated[tuple[BoundedText, ...], Field(max_length=20)]


class PipelineResult(DraftContract):
    draft: DraftOutput
    quality: QualityResult
    stage_names: Annotated[tuple[Literal['plan', 'draft', 'critique', 'rewrite',
                                       'final_critique', 'reassess'], ...],
                           Field(min_length=1, max_length=5)]
