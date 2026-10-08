"""Bounded lexical queries, never a semantic-support or negation repair claim.

The complete question is preserved in every query. If it alone exceeds the
retriever's token ceiling, ask for an explicit split instead of dropping words.
Employer criteria are untrusted retrieval text, not instructions or personal facts.
"""
from dataclasses import dataclass
from typing import Callable, Sequence

from ..contracts import EvidenceError
from ..domain.contracts import Question

TOKEN_LIMIT = 128
VARIANT_LIMIT = 4


@dataclass(frozen=True)
class QueryCompilation:
    question: Question
    variants: tuple[str, ...]
    omitted: tuple[str, ...]
    # No compiler path truncates the question or silently rewrites its meaning.
    truncated: bool = False


def compile_queries(
    question: Question,
    tokenize: Callable[[str], Sequence[tuple[int, int]]],
    criteria: Sequence[str] = (),
) -> QueryCompilation:
    """Add fitting criteria in caller-defined priority order; disclose omissions.

    Token counting uses the pinned retrieval tokenizer, not whitespace estimates.
    Keeping negation intact does not imply a dense model understands negation.
    A later semantic-support policy must separately assess retrieved quotations.
    """
    if not isinstance(question, Question) or len(question.text) > 2000:
        raise EvidenceError('LIMIT_EXCEEDED', 'Split generation questions explicitly at 2,000 characters', 413)
    if not question.text.strip():
        raise EvidenceError('INVALID_INPUT', 'A substantive question is required')
    if isinstance(criteria, (str, bytes)) or not isinstance(criteria, (list, tuple)) or len(criteria) > 20:
        raise EvidenceError('INVALID_INPUT', 'Supply at most 20 separately sourced priority criteria')

    def token_count(text):
        try:
            offsets = tokenize(text)
            previous = 0
            for start, end in offsets:
                if (type(start) is not int or type(end) is not int
                        or not previous <= start < end <= len(text)):
                    raise ValueError()
                previous = end
            if text.strip() and not offsets:
                raise ValueError()
            return len(offsets)
        except (TypeError, ValueError):
            raise EvidenceError('MODEL_NOT_READY', 'Retrieval tokenizer returned invalid offsets', 503) from None

    if token_count(question.text) > TOKEN_LIMIT:
        raise EvidenceError('CLARIFICATION_REQUIRED',
                            'The full question exceeds 128 retrieval tokens; explicitly split it without losing constraints', 422)
    variants = [question.text]
    omitted = []
    seen = set()
    for position, criterion in enumerate(criteria):
        if not isinstance(criterion, str) or not criterion.strip() or len(criterion) > 2000:
            raise EvidenceError('INVALID_INPUT', 'Each sourced criterion needs 1–2,000 characters')
        if criterion in seen:
            continue
        seen.add(criterion)
        candidate = question.text + '\n' + criterion
        if len(variants) >= VARIANT_LIMIT:
            omitted.append(f'criterion:{position}:variant_limit')
        elif token_count(candidate) > TOKEN_LIMIT:
            omitted.append(f'criterion:{position}:token_limit')
        else:
            variants.append(candidate)
    return QueryCompilation(question, tuple(variants), tuple(omitted))
