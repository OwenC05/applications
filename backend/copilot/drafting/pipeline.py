"""Pure three/five-stage drafting and unchanged-text one-call reassessment."""
from ..contracts import EvidenceError
from ..domain.contracts import ApplicationRecord, canonical_hash
from ..retrieval.packet_contracts import PacketBatch
from .prompts import stage_instructions
from .quality import assess_output, invalid_output, text_hash, validate_targets, validated
from .schemas import (
    CritiqueOutput,
    DraftContext,
    DraftOutput,
    PipelineResult,
    PlanOutput,
    SelectedEvidence,
    TargetContext,
)


def build_context(application: ApplicationRecord, batch: PacketBatch) -> DraftContext:
    application = validated(application, ApplicationRecord)
    batch = validated(batch, PacketBatch)
    if (application.profile_id != batch.profile_id
            or application.application_id != batch.application_id
            or application.questions != batch.canonical_questions
            or application.input_revision != batch.revisions.application_input):
        raise EvidenceError('INVALID_DRAFT_CONTEXT', 'Drafting context does not match application', 409)
    packets = {packet.question.id: packet for packet in batch.packets}
    criteria = {criterion.id: criterion for criterion in batch.hiring_criteria}
    ordered = batch.canonical_questions + ((batch.cover_letter_target,) if batch.cover_letter_target else ())
    targets = []
    for question in ordered:
        if question.id not in packets:
            continue
        packet = packets[question.id]
        selected = {canonical_hash(ref) for ref in packet.employer}
        included = tuple(criteria[item] for item in packet.criteria
                         if {canonical_hash(ref) for ref in criteria[item].evidence} <= selected)
        targets.append(TargetContext(question=packet.question, packet_id=packet.id,
            facts=tuple(SelectedEvidence(id=canonical_hash(ref), reference=ref) for ref in packet.facts),
            employer=tuple(SelectedEvidence(id=canonical_hash(ref), reference=ref) for ref in packet.employer),
            criteria=included, gaps=packet.gaps))
    if not targets:
        raise EvidenceError('NO_WRITING_TARGETS', 'Drafting requires a selected writing target', 422)
    return DraftContext(batch_id=batch.id, batch_sha256=batch.batch_sha256,
                        profile_id=batch.profile_id, application_id=batch.application_id,
                        research_run_id=batch.research_run_id, company=application.company,
                        role=application.role, sector=application.sector, targets=tuple(targets),
                        manual_requirements=batch.manual_requirements,
                        required_manual_question_ids=tuple(question.id for question in ordered
                            if question.required and question.id not in packets))


def context_payload(context: DraftContext) -> dict:
    context = validated(context, DraftContext)
    return {'company': context.company, 'role': context.role, 'sector': context.sector,
            'targets': [{'question': target.question.model_dump(mode='json'),
                         'packet_id': target.packet_id,
                         'facts': [{'id': item.id, 'kind': item.reference.kind,
                                    'excerpt': item.reference.span.excerpt} for item in target.facts],
                         'employer': [{'id': item.id, 'kind': item.reference.kind,
                                       'excerpt': item.reference.span.excerpt} for item in target.employer],
                         'criteria': [{'id': item.id, 'text': item.text, 'inferred': item.inferred}
                                      for item in target.criteria],
                         'gaps': list(target.gaps)} for target in context.targets]}


def _call(call, stage, payload, model):
    return validated(call(stage, instructions=stage_instructions(stage),
                          input_value=payload, output_model=model), model)


def _plan(context, plan):
    validate_targets(context, plan.targets)
    for target, item in zip(context.targets, plan.targets):
        allowed = {ref.id for ref in target.facts + target.employer}
        if (len(set(item.evidence_ids)) != len(item.evidence_ids)
                or not set(item.evidence_ids) <= allowed):
            raise invalid_output()


def _critique_payload(context, draft):
    return {'context': context_payload(context), 'draft': draft.model_dump(mode='json'),
            'text_sha256': text_hash(context, draft)}


def run_pipeline(context: DraftContext, call) -> PipelineResult:
    context = validated(context, DraftContext)
    plan = _call(call, 'plan', {'context': context_payload(context)}, PlanOutput)
    _plan(context, plan)
    draft = _call(call, 'draft', {'context': context_payload(context), 'plan': plan.model_dump(mode='json')}, DraftOutput)
    validate_targets(context, draft.targets)
    critique = _call(call, 'critique', _critique_payload(context, draft), CritiqueOutput)
    quality = assess_output(context, draft, critique)
    stages = ('plan', 'draft', 'critique')
    if critique.rewrite_required:
        draft = _call(call, 'rewrite', {'context': context_payload(context), 'plan': plan.model_dump(mode='json'),
                      'draft': draft.model_dump(mode='json'),
                      'critique': critique.model_dump(mode='json')}, DraftOutput)
        validate_targets(context, draft.targets)
        critique = _call(call, 'final_critique', _critique_payload(context, draft), CritiqueOutput)
        quality = assess_output(context, draft, critique)
        stages += ('rewrite', 'final_critique')
    return PipelineResult(draft=draft, quality=quality, stage_names=stages)


def reassess(context: DraftContext, draft: DraftOutput, call) -> PipelineResult:
    context = validated(context, DraftContext)
    draft = validated(draft, DraftOutput)
    validate_targets(context, draft.targets)
    critique = _call(call, 'reassess', _critique_payload(context, draft), CritiqueOutput)
    return PipelineResult(draft=draft, quality=assess_output(context, draft, critique),
                          stage_names=('reassess',))
