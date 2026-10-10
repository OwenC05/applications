"""Pure packet composition with injected retrieval, tokenizer and freshness guard.

Arguments and callbacks do not establish stored authorization or eligibility.
The runtime must supply canonical captures, exact-generation search, a guard that
checks stored currentness/fence, and atomic persistence. No scores imply truth.
"""
import json
from datetime import datetime
from typing import Callable, Sequence
from uuid import uuid4

from ..contracts import EvidenceError
from ..domain.contracts import (
    ApplicationRecord,
    EvidencePacket,
    EvidenceReference,
    HiringCriterion,
    RevisionVector,
    canonical_hash,
)
from .packet_contracts import (
    CoverLetterTarget,
    GenerationBinding,
    ManualRequirement,
    PacketBatch,
    PacketCount,
)
from .queries import compile_queries

COMPILER_VERSION = 'full-question-v1'
SELECTION_VERSION = 'whole-spans-round-robin-v1'


def _validated(model_type, value):
    """Dump first: Pydantic instance validation alone trusts model_copy updates."""
    if not isinstance(value, model_type):
        raise ValueError(f'Expected {model_type.__name__}')
    return model_type.model_validate(value.model_dump(mode='python'))


def serialize_selected_context(packet: EvidencePacket, hiring_criteria: tuple[HiringCriterion, ...]) -> str:
    """S5 selected-context wire format: canonical JSON, no Unicode normalization.

    Includes the full question constraints, only selected criteria (complete sourced
    records in packet criterion-ID order), and whole selected references. Budgets
    count THIS text only, not query variants, gaps/omissions, envelope metadata or
    the entire provider prompt. S5 must budget any additional prompt separately.
    """
    packet = _validated(EvidencePacket, packet)
    criteria = tuple(_validated(HiringCriterion, item) for item in hiring_criteria)
    by_id = {item.id: item for item in criteria}
    if (len(by_id) != len(criteria) or len(set(packet.criteria)) != len(packet.criteria)
            or not set(packet.criteria) <= by_id.keys()):
        raise ValueError('Selected criterion IDs must resolve uniquely')
    value = dict(question=packet.question.model_dump(mode='json'),
                 criteria=[by_id[id].model_dump(mode='json') for id in packet.criteria],
                 facts=[ref.model_dump(mode='json') for ref in packet.facts],
                 employer=[ref.model_dump(mode='json') for ref in packet.employer])
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def compose_batch(
    *, application: ApplicationRecord, research_run_id: str, job_id: str, fence: int,
    revisions: RevisionVector, facts_generation: GenerationBinding,
    employer_generation: GenerationBinding, hiring_criteria: tuple[HiringCriterion, ...],
    tokenize: Callable[[str], Sequence[tuple[int, int]]], tokenizer_version: str,
    tokenizer_fingerprint: str, search_scoped: Callable, currentness_guard: Callable[[], None],
    created_at: datetime, per_packet_budget: int = 6000, aggregate_budget: int = 24000,
    cover_letter_target: CoverLetterTarget | None = None,
) -> PacketBatch:
    """Compose exact writing/manual coverage; callback failures abort, never fallback.

    Search is called once per fixed corpus binding with all compiled variants.
    Its ranking/fusion is trusted only as ordering, never as semantic support.
    No cropping is performed. Invalid/foreign references abort even if over budget.
    """
    application = _validated(ApplicationRecord, application)
    revisions = _validated(RevisionVector, revisions)
    facts_generation = _validated(GenerationBinding, facts_generation)
    employer_generation = _validated(GenerationBinding, employer_generation)
    if type(hiring_criteria) is not tuple:
        raise ValueError('Hiring criteria must be an explicit tuple')
    hiring_criteria = tuple(_validated(HiringCriterion, item) for item in hiring_criteria)
    if cover_letter_target is not None:
        cover_letter_target = _validated(CoverLetterTarget, cover_letter_target)
    if application.input_revision != revisions.application_input:
        raise ValueError('Canonical application input revision mismatch')
    if not all(callable(callback) for callback in (tokenize, search_scoped, currentness_guard)):
        raise ValueError('Tokenizer, scoped search and currentness guard are required')
    targets = application.questions + ((cover_letter_target,) if cover_letter_target is not None else ())
    values = dict(
        id=str(uuid4()), profile_id=application.profile_id, application_id=application.application_id,
        research_run_id=research_run_id, job_id=job_id, fence=fence, revisions=revisions,
        dependencies=('facts', 'application_input', 'research'), facts_generation=facts_generation,
        employer_generation=employer_generation, canonical_questions=application.questions,
        question_set_sha256=canonical_hash([q.model_dump(mode='json') for q in application.questions]),
        cover_letter_target=cover_letter_target, hiring_criteria=hiring_criteria,
        compiler_version=COMPILER_VERSION, selection_version=SELECTION_VERSION,
        tokenizer_version=tokenizer_version, tokenizer_fingerprint=tokenizer_fingerprint,
        count_policy='local_retrieval_tokenizer', per_packet_budget=per_packet_budget,
        aggregate_budget=aggregate_budget, created_at=created_at,
    )
    # Validate all envelope metadata/scopes BEFORE invoking retrieval or tokenizer.
    PacketBatch(**values, packets=(), manual_requirements=tuple(
        ManualRequirement(question_id=q.id, reason='Pending composition') for q in targets),
        per_packet_token_counts=(), token_count=0)

    def guarded(callback, *args, **kwargs):
        currentness_guard()
        try:
            return callback(*args, **kwargs)
        finally:
            currentness_guard()

    def checked_tokens(text):
        offsets = guarded(lambda: tuple(tokenize(text)))
        previous = 0
        for start, end in offsets:
            if type(start) is not int or type(end) is not int or not previous <= start < end <= len(text):
                raise ValueError('Retrieval tokenizer returned invalid code-point offsets')
            previous = end
        if text.strip() and not offsets:
            raise ValueError('Retrieval tokenizer returned no tokens for substantive context')
        return offsets

    def count(packet):
        return len(checked_tokens(serialize_selected_context(packet, hiring_criteria)))

    packets, manual, counts = [], [], []
    total = 0
    for question in targets:
        if question.type != 'writing':
            manual.append(ManualRequirement(question_id=question.id,
                                            reason=f'Manual {question.type} requirement; no automatic writing'))
            continue
        compiler_records = tuple(item for item in hiring_criteria[:20] if len(item.text) <= 2000)
        compiler_criteria = tuple(item.text for item in compiler_records)
        pre_omitted = tuple(f'criterion:{item.id}:compiler_input_limit' for i, item in enumerate(hiring_criteria)
                            if i >= 20 or len(item.text) > 2000)
        try:
            compilation = compile_queries(question, checked_tokens, compiler_criteria)
        except EvidenceError as error:
            if error.code != 'CLARIFICATION_REQUIRED':
                raise
            manual.append(ManualRequirement(question_id=question.id, reason=str(error)))
            continue
        refs_by_corpus = []
        for binding in (facts_generation, employer_generation):
            result = guarded(search_scoped, binding.scope, compilation.variants, binding=binding, limit=8)
            actual_binding = _validated(GenerationBinding, result.binding)
            if actual_binding != binding:
                raise ValueError('Scoped search changed the fixed generation binding')
            references = tuple(_validated(EvidenceReference, item) for item in result.references)
            for reference in references:
                binding.validate_reference(reference)
            # Deduplicate exact references without discarding distinct canonical spans.
            unique = {canonical_hash(ref): ref for ref in references}
            refs_by_corpus.append(tuple(unique.values()))
        packet = EvidencePacket(
            id=str(uuid4()), profile_id=application.profile_id, application_id=application.application_id,
            research_run_id=research_run_id, question=question, query_variants=compilation.variants,
            revisions=revisions, model_version=facts_generation.model_fingerprint,
            prompt_version=COMPILER_VERSION,
        )
        available = min(per_packet_budget, aggregate_budget - total)
        if count(packet) > available:
            manual.append(ManualRequirement(question_id=question.id,
                                            reason='Selected-context token budget cannot fit the exact full question'))
            continue
        omitted = list(pre_omitted)
        for compiler_omission in compilation.omitted:
            _, position, reason = compiler_omission.split(':', 2)
            omitted.append(f'criterion:{compiler_records[int(position)].id}:{reason}')
        for criterion in hiring_criteria:
            candidate = packet.model_copy(update={'criteria': packet.criteria + (criterion.id,)})
            if count(candidate) <= available:
                packet = candidate
            else:
                omitted.append(f'criterion:{criterion.id}:context_budget')
        # Alternate corpora while preserving each corpus ranking; never compare scores.
        for position in range(max(map(len, refs_by_corpus), default=0)):
            for field, refs in zip(('facts', 'employer'), refs_by_corpus, strict=True):
                if position >= len(refs):
                    continue
                reference = refs[position]
                candidate = packet.model_copy(update={field: getattr(packet, field) + (reference,)})
                if count(candidate) <= available:
                    packet = candidate
                else:
                    omitted.append(f'{field}:{canonical_hash(reference)}:context_budget')
        gaps = []
        if not packet.facts:
            gaps.append('No selected confirmed fact evidence; personal support not assessed')
        if not packet.employer:
            gaps.append('No selected employer evidence; employer support not assessed')
        packet = _validated(EvidencePacket, packet.model_copy(update={
            'omitted': tuple(omitted), 'gaps': tuple(gaps), 'truncated': bool(omitted)}))
        actual_count = count(packet)
        packets.append(packet)
        counts.append(PacketCount(packet_id=packet.id, token_count=actual_count))
        total += actual_count
    result = PacketBatch(**values, packets=tuple(packets), manual_requirements=tuple(manual),
                         per_packet_token_counts=tuple(counts), token_count=total)
    currentness_guard()
    return result
