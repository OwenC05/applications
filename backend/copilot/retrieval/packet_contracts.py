"""Immutable S4 batch metadata; validation is not authorization or entailment.

Counts describe the pinned local retrieval tokenizer over serialized selected
context. Structural validation checks declared budgets and sums only: the runtime
must measure/revalidate these counts. They are NOT exact provider token counts.
Canonical references must also be re-resolved against current stored generations.
"""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..domain.contracts import (
    ApplicationOwned,
    Contract,
    Digest,
    EvidencePacket,
    EvidenceReference,
    HiringCriterion,
    Identifier,
    Question,
    Revision,
    RevisionVector,
    Text,
    canonical_hash,
)


class CorpusScope(Contract):
    profile_id: Identifier
    corpus: Literal['facts', 'documents', 'employer']
    application_id: Identifier | None = None
    research_run_id: Identifier | None = None

    @model_validator(mode='after')
    def exact_scope(self):
        if self.corpus == 'employer':
            if self.application_id is None or self.research_run_id is None:
                raise ValueError('Employer scope requires application and research run')
        elif self.application_id is not None or self.research_run_id is not None:
            raise ValueError('Personal corpus forbids application and research run')
        return self


class GenerationBinding(Contract):
    generation_id: Identifier
    scope: CorpusScope
    revision: Revision
    model_fingerprint: Digest
    chunker_version: Text
    chunk_checksum: Digest

    def validate_reference(self, reference: EvidenceReference) -> None:
        expected_kind = 'confirmed_fact' if self.scope.corpus == 'facts' else 'employer'
        if (self.scope.corpus == 'documents' or reference.kind != expected_kind
                or reference.generation_id != self.generation_id
                or reference.profile_id != self.scope.profile_id
                or reference.application_id != self.scope.application_id
                or reference.research_run_id != self.scope.research_run_id
                or (reference.span.unit_id is None) != (self.scope.corpus == 'facts')):
            raise ValueError('Reference does not match exact generation and corpus scope')


class ManualRequirement(Contract):
    question_id: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Text


class CoverLetterTarget(Question):
    id: Literal['cover_letter'] = 'cover_letter'
    type: Literal['writing'] = 'writing'
    max_words: Annotated[int, Field(gt=0)] = 400


class PacketCount(Contract):
    packet_id: Identifier
    token_count: Annotated[int, Field(gt=0, le=24000)]


class PacketBatch(ApplicationOwned):
    id: Identifier
    research_run_id: Identifier
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    revisions: RevisionVector
    dependencies: tuple[Literal['facts', 'application_input', 'research'], ...]
    facts_generation: GenerationBinding
    employer_generation: GenerationBinding
    canonical_questions: Annotated[tuple[Question, ...], Field(max_length=12)]
    question_set_sha256: Digest
    # The optional synthetic target is separate from the actual canonical questions.
    cover_letter_target: CoverLetterTarget | None = None
    hiring_criteria: tuple[HiringCriterion, ...]
    packets: tuple[EvidencePacket, ...]
    manual_requirements: tuple[ManualRequirement, ...]
    compiler_version: Text
    selection_version: Text
    tokenizer_version: Text
    tokenizer_fingerprint: Digest
    count_policy: Literal['local_retrieval_tokenizer']
    per_packet_budget: Annotated[int, Field(gt=0, le=24000)]
    aggregate_budget: Annotated[int, Field(gt=0, le=24000)]
    per_packet_token_counts: tuple[PacketCount, ...]
    token_count: Annotated[int, Field(ge=0, le=24000)]
    created_at: datetime

    @model_validator(mode='after')
    def exact_batch(self):
        if (len(self.dependencies) != 3
                or set(self.dependencies) != {'facts', 'application_input', 'research'}):
            raise ValueError('Exact packet revision dependencies required')
        for binding, corpus, revision in (
            (self.facts_generation, 'facts', self.revisions.facts),
            (self.employer_generation, 'employer', self.revisions.research),
        ):
            expected_scope = CorpusScope(
                profile_id=self.profile_id, corpus=corpus,
                application_id=self.application_id if corpus == 'employer' else None,
                research_run_id=self.research_run_id if corpus == 'employer' else None,
            )
            if binding.scope != expected_scope or binding.revision != revision:
                raise ValueError('Generation scope/revision mismatch')
        if (self.facts_generation.generation_id == self.employer_generation.generation_id
                or self.facts_generation.model_fingerprint != self.employer_generation.model_fingerprint):
            raise ValueError('Distinct generations with the same retrieval model required')
        questions = {q.id: q for q in self.canonical_questions}
        if len(questions) != len(self.canonical_questions) or 'cover_letter' in questions:
            raise ValueError('Duplicate/reserved canonical question ID')
        if any(len(q.text) > 2000 for q in self.canonical_questions):
            raise ValueError('Canonical questions exceed 2000 code points')
        if self.question_set_sha256 != canonical_hash([q.model_dump(mode='json') for q in self.canonical_questions]):
            raise ValueError('Exact full canonical question set hash mismatch')
        if self.cover_letter_target is not None:
            target = self.cover_letter_target
            if (target.id != 'cover_letter' or target.type != 'writing'
                    or target.max_words is None or len(target.text) > 2000):
                raise ValueError('Explicit writing cover-letter target with word limit required')
            questions[target.id] = target
        criteria = {criterion.id: criterion for criterion in self.hiring_criteria}
        if len(criteria) != len(self.hiring_criteria):
            raise ValueError('Duplicate criterion ID')
        for criterion in self.hiring_criteria:
            for reference in criterion.evidence:
                self.employer_generation.validate_reference(reference)
        packet_ids = set()
        covered = set()
        for packet in self.packets:
            expected_question = questions.get(packet.question.id)
            if (packet.id in packet_ids or packet.question.id in covered
                    or expected_question is None
                    or packet.question.model_dump(mode='json') != expected_question.model_dump(mode='json')
                    or packet.question.type != 'writing'):
                raise ValueError('Duplicate, nonwriting or noncanonical packet target')
            packet_ids.add(packet.id)
            covered.add(packet.question.id)
            if (packet.profile_id != self.profile_id or packet.application_id != self.application_id
                    or packet.research_run_id != self.research_run_id
                    or packet.revisions != self.revisions
                    or packet.model_version != self.facts_generation.model_fingerprint
                    or packet.prompt_version != self.compiler_version):
                raise ValueError('Packet scope/revision/model/compiler mismatch')
            if len(set(packet.criteria)) != len(packet.criteria) or not set(packet.criteria) <= criteria.keys():
                raise ValueError('Packet criteria must be distinct sourced criterion IDs')
            for reference in packet.facts:
                self.facts_generation.validate_reference(reference)
            for reference in packet.employer:
                self.employer_generation.validate_reference(reference)
        for requirement in self.manual_requirements:
            if requirement.question_id in covered or requirement.question_id not in questions:
                raise ValueError('Duplicate or unknown manual requirement')
            covered.add(requirement.question_id)
        if covered != questions.keys():
            raise ValueError('Every exact question/cover target requires a packet or visible manual reason')
        counts = {count.packet_id: count.token_count for count in self.per_packet_token_counts}
        if len(counts) != len(self.per_packet_token_counts) or counts.keys() != packet_ids:
            raise ValueError('Exact unique packet count coverage required')
        if (any(count > self.per_packet_budget for count in counts.values())
                or sum(counts.values()) != self.token_count
                or self.token_count > self.aggregate_budget):
            raise ValueError('Declared local token counts exceed budget or do not sum')
        return self

    @property
    def batch_sha256(self) -> str:
        """Full canonical immutable envelope hash, not just individual packet hashes."""
        return canonical_hash(self)
