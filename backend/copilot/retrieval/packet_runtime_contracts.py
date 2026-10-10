"""Model-free durable packet request, intent and publication associations."""
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ..domain.contracts import (
    ApplicationOwned,
    Contract,
    Digest,
    Identifier,
    Revision,
    RevisionVector,
    canonical_hash,
)
from .packet_contracts import CorpusScope, CoverLetterTarget, GenerationBinding


class PacketSelection(Contract):
    source_id: Identifier
    unit_id: Identifier
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('Selection must be a nonempty code-point range')
        return self


class PacketRequest(Contract):
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    expected_facts_revision: Revision
    expected_input_revision: Revision
    expected_research_revision: Revision
    selections: Annotated[tuple[PacketSelection, ...], Field(max_length=100)] = ()
    cover_letter_target: CoverLetterTarget | None = None
    per_packet_budget: Annotated[int, Field(gt=0, le=6000)] = 6000
    aggregate_budget: Annotated[int, Field(gt=0, le=24000)] = 24000

    @field_validator('cover_letter_target')
    @classmethod
    def bounded_cover_letter(cls, value):
        if value is not None and len(value.text) > 2000:
            raise ValueError('Split the explicit cover-letter target at 2000 code points')
        return value


class PacketParameters(ApplicationOwned):
    research_run_id: Identifier
    request: PacketRequest
    request_sha256: Digest
    facts_generation: GenerationBinding
    employer_generation: GenerationBinding

    @model_validator(mode='after')
    def exact(self):
        for binding, scope, revision in (
            (self.facts_generation, CorpusScope(profile_id=self.profile_id, corpus='facts'), self.request.expected_facts_revision),
            (self.employer_generation, CorpusScope(profile_id=self.profile_id, corpus='employer', application_id=self.application_id, research_run_id=self.research_run_id), self.request.expected_research_revision),
        ):
            if binding.scope != scope or binding.revision != revision:
                raise ValueError('Packet binding scope/revision mismatch')
        if (self.facts_generation.generation_id == self.employer_generation.generation_id
                or self.facts_generation.model_fingerprint != self.employer_generation.model_fingerprint):
            raise ValueError('Distinct registered generations with one model required')
        if self.request_sha256 != packet_request_hash(self.profile_id, self.application_id, self.research_run_id, self.request):
            raise ValueError('Original packet request hash mismatch')
        return self


def packet_request_hash(owner, application, run, request):
    return canonical_hash({'profile_id': owner, 'application_id': application,
                           'research_run_id': run, 'request': request.model_dump(mode='json')})


class PacketIntent(PacketParameters):
    id: Identifier
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    revisions: RevisionVector
    state: Literal['registered', 'committed', 'failed']


class CompilerOmission(Contract):
    selection_index: Annotated[int, Field(ge=0, lt=100)]
    reason: Literal['unknown_source', 'unit_mismatch', 'unsupported_provenance', 'range_out_of_bounds',
                    'excerpt_too_long', 'duplicate_span', 'candidate_limit']


class BinderOmission(Contract):
    candidate_id: Identifier
    selection_indices: Annotated[tuple[Annotated[int, Field(ge=0, lt=100)], ...], Field(max_length=100)]
    reason: Literal['not_in_generation_chunk']


class PacketReport(Contract):
    compiler_version: Literal['excerpt-criteria-v1']
    candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=20)]
    compiler_omissions: Annotated[tuple[CompilerOmission, ...], Field(max_length=100)]
    compiler_gaps: Annotated[tuple[Literal['no_sources', 'no_selections', 'no_candidates', 'selections_omitted'], ...], Field(max_length=4)]
    binder_omissions: Annotated[tuple[BinderOmission, ...], Field(max_length=20)]


class PacketPublication(ApplicationOwned):
    id: Identifier
    batch_id: Identifier
    batch_sha256: Digest
    intent_id: Identifier
    job_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    request_sha256: Digest
    compiler_version: Literal['full-question-v1']
    selection_version: Literal['whole-spans-round-robin-v1']
    report: PacketReport

    @property
    def publication_sha256(self):
        return canonical_hash(self)
