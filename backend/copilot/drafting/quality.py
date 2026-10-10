"""Deterministic protocol/coverage checks, not semantic entailment proof."""
from pydantic import ValidationError

from ..contracts import EvidenceError
from ..domain.contracts import Contract, SupportSpan, canonical_hash
from .schemas import CritiqueOutput, DraftContext, DraftOutput, QualityResult


def invalid_output():
    return EvidenceError('INVALID_PROVIDER_OUTPUT', 'Drafting output failed validation', 422)


def validated(value, model: type[Contract]):
    """Revalidate even frozen/copied models; never trust model_construct/copy."""
    try:
        if isinstance(value, Contract):
            if type(value) is not model:
                raise ValueError('Unexpected schema')
            serialized = value.model_dump_json(warnings='error')
        else:
            raise ValueError('Validated protocol model required')
        return model.model_validate_json(serialized)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise invalid_output() from None


def validate_targets(context, items):
    if [item.target for item in items] != [item.question.id for item in context.targets]:
        raise invalid_output()


def text_hash(context: DraftContext, draft: DraftOutput) -> str:
    context = validated(context, DraftContext)
    draft = validated(draft, DraftOutput)
    validate_targets(context, draft.targets)
    texts = {item.target: item.text for item in draft.targets}
    return canonical_hash({'cover_letter': texts.get('cover_letter', ''), 'answers': [
        {'question_id': target.question.id, 'text': texts[target.question.id]}
        for target in context.targets if target.question.id != 'cover_letter']})


def assess_output(context: DraftContext, draft: DraftOutput,
                  critique: CritiqueOutput) -> QualityResult:
    context = validated(context, DraftContext)
    draft = validated(draft, DraftOutput)
    critique = validated(critique, CritiqueOutput)
    validate_targets(context, draft.targets)
    validate_targets(context, critique.targets)
    if critique.text_sha256 != text_hash(context, draft):
        raise invalid_output()
    reasons = []
    ledger = []
    inventory_complete = True
    for target, generated, assessment in zip(context.targets, draft.targets, critique.targets):
        text = generated.text
        question = target.question
        if question.required and not text.strip():
            reasons.append('REQUIRED_ANSWER_MISSING')
        if (question.max_chars is not None and len(text) > question.max_chars
                or question.max_words is not None and len(text.split()) > question.max_words):
            reasons.append('OUTPUT_LIMIT_EXCEEDED')
        if not assessment.inventory_complete:
            inventory_complete = False
            reasons.append('INCOMPLETE_INVENTORY')
        refs = {item.id: item.reference for item in target.facts + target.employer}
        covered = bytearray(len(text))
        for span in (*assessment.claims, *assessment.nonfactual_ranges):
            if (span.end > len(text) or text[span.start:span.end] != span.excerpt
                    or any(covered[span.start:span.end])):
                raise invalid_output()
            covered[span.start:span.end] = b'\x01' * (span.end - span.start)
        if any(not covered[i] and not char.isspace() for i, char in enumerate(text)):
            inventory_complete = False
            reasons.append('INCOMPLETE_COVERAGE')
        for claim in assessment.claims:
            if len(set(claim.evidence_ids)) != len(claim.evidence_ids):
                raise invalid_output()
            if any(item not in refs for item in claim.evidence_ids):
                raise invalid_output()
            citations = tuple(refs[item] for item in claim.evidence_ids)
            expected = {'personal': 'confirmed_fact', 'employer': 'employer'}.get(claim.kind)
            if (expected is not None and any(ref.kind != expected for ref in citations)
                    or claim.assessment == 'supported' and not citations):
                raise invalid_output()
            if claim.assessment != 'supported':
                reasons.append('CLAIM_' + claim.assessment.upper())
            ledger.append(SupportSpan(target=generated.target, start=claim.start, end=claim.end,
                                      excerpt=claim.excerpt, kind=claim.kind,
                                      assessment=claim.assessment, citations=citations,
                                      integrity='valid'))
    if context.required_manual_question_ids:
        reasons.append('MANUAL_REQUIREMENTS_UNRESOLVED')
    return QualityResult(ledger=tuple(ledger), inventory_complete=inventory_complete,
                         reason_codes=tuple(dict.fromkeys(reasons)), style_notes=critique.style_notes)
