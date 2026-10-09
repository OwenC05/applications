"""Structural packet envelopes; no stored authorization or semantic-support claim."""
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from copilot.domain.contracts import (
    EvidencePacket,
    EvidenceReference,
    HiringCriterion,
    Question,
    RevisionVector,
    SourceSpan,
    canonical_hash,
    text_hash,
)
from copilot.retrieval.packet_contracts import (
    CorpusScope,
    CoverLetterTarget,
    GenerationBinding,
    ManualRequirement,
    PacketBatch,
    PacketCount,
)


def uid(n):
    return str(UUID(int=n, version=4))


OWNER, APP, RUN, FACT_GEN, EMP_GEN, JOB = (uid(n) for n in range(1, 7))
REVISIONS = RevisionVector(facts=2, application_input=3, research=4)
NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def question(id='q1', **changes):
    values = dict(id=id, text='Why not 😀?', type='writing', constraint_origin='form',
                  max_words=20, max_chars=100, options=('é', 'e\u0301'))
    return Question(**(values | changes))


def binding(corpus='facts', **changes):
    scope = CorpusScope(profile_id=OWNER, corpus=corpus,
                        application_id=APP if corpus == 'employer' else None,
                        research_run_id=RUN if corpus == 'employer' else None)
    values = dict(generation_id=EMP_GEN if corpus == 'employer' else FACT_GEN, scope=scope,
                  revision=4 if corpus == 'employer' else 2, model_fingerprint='a' * 64,
                  chunker_version='chunks-v1', chunk_checksum='b' * 64)
    return GenerationBinding(**(values | changes))


def reference(kind='confirmed_fact', **changes):
    values = dict(profile_id=OWNER, kind=kind,
                  generation_id=EMP_GEN if kind == 'employer' else FACT_GEN,
                  application_id=APP if kind == 'employer' else None,
                  research_run_id=RUN if kind == 'employer' else None,
                  span=SourceSpan(record_id=uid(20), unit_id=uid(21) if kind == 'employer' else None,
                                  start=0, end=1, excerpt='😀', text_sha256=text_hash('😀 full')))
    return EvidenceReference(**(values | changes))


def packet(q=None, **changes):
    q = q or question()
    values = dict(id=uid(30), profile_id=OWNER, application_id=APP, research_run_id=RUN,
                  question=q, query_variants=(q.text,), revisions=REVISIONS,
                  model_version='a' * 64, prompt_version='compiler-v1', facts=(), employer=(),
                  gaps=('No fact hits; not assessed',))
    return EvidencePacket(**(values | changes))


def batch(**changes):
    packets = changes.get('packets', (packet(),))
    counts = tuple(PacketCount(packet_id=p.id, token_count=5) for p in packets)
    questions = changes.get('canonical_questions', (question(),))
    values = dict(id=uid(40), profile_id=OWNER, application_id=APP, research_run_id=RUN,
                  job_id=JOB, fence=1, revisions=REVISIONS,
                  dependencies=('facts', 'application_input', 'research'),
                  facts_generation=binding(), employer_generation=binding('employer'),
                  canonical_questions=questions, question_set_sha256=canonical_hash([q.model_dump(mode='json') for q in questions]),
                  hiring_criteria=(), packets=packets, manual_requirements=(),
                  compiler_version='compiler-v1', selection_version='selection-v1',
                  tokenizer_version='retrieval-v1', tokenizer_fingerprint='d' * 64,
                  count_policy='local_retrieval_tokenizer',
                  per_packet_budget=24000, aggregate_budget=24000,
                  per_packet_token_counts=counts, token_count=sum(c.token_count for c in counts),
                  created_at=NOW)
    return PacketBatch(**(values | changes))


def test_empty_hits_still_bind_both_generations_and_are_immutable():
    result = batch()
    assert result.facts_generation.generation_id == FACT_GEN
    assert result.employer_generation.generation_id == EMP_GEN
    assert PacketBatch.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.token_count = 0
    for field in ('facts_generation', 'employer_generation'):
        payload = result.model_dump()
        del payload[field]
        with pytest.raises(ValidationError):
            PacketBatch(**payload)


@pytest.mark.parametrize('corpus', ['facts', 'documents'])
@pytest.mark.parametrize('field', ['application_id', 'research_run_id'])
def test_personal_scope_forbids_app_and_run(corpus, field):
    with pytest.raises(ValidationError):
        CorpusScope(profile_id=OWNER, corpus=corpus, **{field: APP})


@pytest.mark.parametrize('changes', [dict(application_id=None), dict(research_run_id=None)])
def test_employer_scope_requires_both(changes):
    with pytest.raises(ValidationError):
        CorpusScope(**(dict(profile_id=OWNER, corpus='employer', application_id=APP,
                            research_run_id=RUN) | changes))


@pytest.mark.parametrize('changes', [
    dict(generation_id=uid(99)), dict(profile_id=uid(99)), dict(application_id=APP),
    dict(span=SourceSpan(record_id=uid(20), unit_id=uid(21), start=0, end=1,
                        excerpt='😀', text_sha256=text_hash('😀'))),
])
def test_foreign_or_document_fact_reference_rejected(changes):
    with pytest.raises(ValidationError):
        batch(packets=(packet(facts=(reference(**changes),)),))


@pytest.mark.parametrize('changes', [dict(generation_id=uid(99)), dict(application_id=uid(99)),
                                     dict(research_run_id=uid(99)), dict(profile_id=uid(99))])
def test_criterion_foreign_reference_rejected(changes):
    criterion = HiringCriterion(id=uid(60), text='Exact role', inferred=False,
                               evidence=(reference('employer', **changes),))
    with pytest.raises(ValidationError):
        batch(hiring_criteria=(criterion,))


def test_exact_question_set_and_manual_coverage():
    manual = question('upload', type='file')
    result = batch(canonical_questions=(question(), manual),
                   manual_requirements=(ManualRequirement(question_id='upload', reason='Upload manually'),))
    assert result.canonical_questions[1] == manual
    for changes in (dict(manual_requirements=()), dict(packets=()),
                    dict(manual_requirements=(ManualRequirement(question_id='q1', reason='Duplicate'),))):
        with pytest.raises(ValidationError):
            batch(canonical_questions=(question(), manual), **changes)
    unsupported = question(type='text')
    assert batch(canonical_questions=(unsupported,), packets=(),
                 manual_requirements=(ManualRequirement(question_id='q1', reason='Needs user input'),))


@pytest.mark.parametrize('changes', [dict(text='Different'), dict(options=('other',)),
                                     dict(max_words=21), dict(type='text'), dict(max_chars=99)])
def test_packet_must_preserve_every_question_field(changes):
    with pytest.raises(ValidationError):
        batch(packets=(packet(question(**changes)),))


def test_question_boundaries_duplicates_reserved_and_unicode():
    q = question(text='😀' * 2000)
    assert batch(canonical_questions=(q,), packets=(packet(q),))
    with pytest.raises(ValidationError):
        batch(canonical_questions=(question(text='😀' * 2001),), packets=())
    with pytest.raises(ValidationError):
        batch(canonical_questions=(question(), question()))
    with pytest.raises(ValidationError):
        batch(canonical_questions=(question('cover_letter'),))
    questions = tuple(question(f'q{i}') for i in range(12))
    manuals = tuple(ManualRequirement(question_id=q.id, reason='Clarify') for q in questions)
    assert batch(canonical_questions=questions, packets=(), manual_requirements=manuals)
    with pytest.raises(ValidationError):
        batch(canonical_questions=questions + (question('q12'),), packets=(), manual_requirements=manuals)


def test_explicit_optional_cover_letter():
    target = CoverLetterTarget(text='Cover letter', constraint_origin='user')
    assert target.max_words == 400
    p = packet(target)
    assert batch(cover_letter_target=target, packets=(packet(), p.model_copy(update={'id': uid(31)})))
    with pytest.raises(ValidationError):
        batch(cover_letter_target=question('wrong'))


def test_cover_letter_json_roundtrip_preserves_full_fields_and_batch_hash():
    target = CoverLetterTarget(text='Explain the exact role fit 😀.', constraint_origin='user')
    result = batch(cover_letter_target=target,
                   packets=(packet(), packet(target, id=uid(31))))
    restored = PacketBatch.model_validate_json(result.model_dump_json())
    # EvidencePacket.question reloads as base Question, not CoverLetterTarget.
    # Persistence binds exact field values rather than Python subclass identity.
    assert restored.model_dump(mode='json') == result.model_dump(mode='json')
    assert restored.batch_sha256 == result.batch_sha256
    assert restored.packets[1].question.max_words == 400
    altered = result.model_dump(mode='json')
    altered['packets'][1]['question']['max_words'] = 401
    import json
    with pytest.raises(ValidationError):
        PacketBatch.model_validate_json(json.dumps(altered))


@pytest.mark.parametrize('changes', [dict(fence=True), dict(aggregate_budget=24001),
    dict(per_packet_budget=0), dict(token_count=0), dict(per_packet_token_counts=()),
    dict(count_policy='provider-exact'), dict(question_set_sha256='0' * 64),
    dict(dependencies=('facts',)), dict(compiler_version=''), dict(created_at=NOW.replace(tzinfo=None))])
def test_counts_hash_versions_and_budgets_fail_closed(changes):
    with pytest.raises(ValidationError):
        batch(**changes)


def test_generation_revision_model_and_packet_versions():
    for changes in (dict(facts_generation=binding(revision=9)),
                    dict(employer_generation=binding('employer', model_fingerprint='c' * 64)),
                    dict(packets=(packet(model_version='different'),)),
                    dict(packets=(packet(prompt_version='different'),)),
                    dict(packets=(packet(revisions=RevisionVector()),))):
        with pytest.raises(ValidationError):
            batch(**changes)


def test_criteria_ids_and_full_envelope_hash_not_just_packet_hash():
    criterion = HiringCriterion(id=uid(60), text='Role evidence', inferred=True,
                               evidence=(reference('employer'),))
    result = batch(hiring_criteria=(criterion,), packets=(packet(criteria=(criterion.id,)),))
    assert result.batch_sha256 == canonical_hash(result)
    changed = batch(hiring_criteria=(criterion.model_copy(update={'text': 'Changed'}),),
                    packets=result.packets)
    assert changed.packets[0].packet_sha256 == result.packets[0].packet_sha256
    assert changed.batch_sha256 != result.batch_sha256
    with pytest.raises(ValidationError):
        batch(packets=(packet(criteria=('unbound criterion',)),))
    with pytest.raises(ValidationError):
        batch(hiring_criteria=(criterion, criterion))
    payload = result.model_dump()
    payload['canonical_questions'][0]['options'] = ('tampered',)
    with pytest.raises(ValidationError):
        PacketBatch(**payload)


def test_declared_local_token_counts_and_budget():
    p = packet(facts=(reference(),), employer=(reference('employer'),))
    count = 5
    assert count > 1  # Synthetic metadata, not proof of actual tokenizer output.
    assert batch(packets=(p,), per_packet_budget=count, aggregate_budget=count)
    for changes in (dict(per_packet_budget=count - 1), dict(aggregate_budget=count - 1),
                    dict(per_packet_token_counts=(PacketCount(packet_id=p.id, token_count=count + 1),))):
        with pytest.raises(ValidationError):
            batch(packets=(p,), **changes)


@pytest.mark.parametrize('field', ['profile_id', 'application_id', 'research_run_id'])
def test_foreign_packet_envelope(field):
    with pytest.raises(ValidationError):
        batch(packets=(packet(**{field: uid(99)}),))


def test_duplicate_packet_manual_and_count_ids_and_count_types():
    p = packet()
    for changes in (
        dict(packets=(p, p)),
        dict(per_packet_token_counts=(PacketCount(packet_id=p.id, token_count=5),) * 2),
        dict(per_packet_token_counts=(PacketCount(packet_id=uid(99), token_count=5),)),
        dict(packets=(), manual_requirements=(ManualRequirement(question_id='q1', reason='Manual'),) * 2),
        dict(packets=(), manual_requirements=(ManualRequirement(question_id='foreign', reason='Manual'),)),
    ):
        with pytest.raises(ValidationError):
            batch(**changes)
    for value in (True, '5', 0, -1, 24001):
        with pytest.raises(ValidationError):
            PacketCount(packet_id=p.id, token_count=value)


def test_strict_binding_fields_and_unsupported_versions():
    for changes in (dict(model_fingerprint='unpinned'), dict(chunk_checksum='bad'),
                    dict(chunker_version=''), dict(revision=True), dict(schema_version=2)):
        with pytest.raises(ValidationError):
            binding(**changes)
    with pytest.raises(ValidationError):
        batch(schema_version=2)
    with pytest.raises(ValidationError):
        batch(unknown='extra')


def test_full_batch_hash_binds_generation_even_no_hits():
    original = batch()
    changed = batch(facts_generation=binding(generation_id=uid(99)))
    assert original.packets[0].packet_sha256 == changed.packets[0].packet_sha256
    assert original.batch_sha256 != changed.batch_sha256
    assert original.batch_sha256 != batch(facts_generation=binding(chunk_checksum='c' * 64)).batch_sha256
    with pytest.raises(ValidationError):
        batch(facts_generation=binding('documents'))


def test_unicode_normalization_and_all_question_constraints_hash_exactly():
    first = question(text='é', required=False)
    second = question(text='e\u0301', required=False)
    a = batch(canonical_questions=(first,), packets=(packet(first),))
    b = batch(canonical_questions=(second,), packets=(packet(second),))
    assert a.question_set_sha256 != b.question_set_sha256
    with pytest.raises(ValidationError):
        batch(canonical_questions=(first,), packets=(packet(question(text='é', required=True)),))


def test_employer_unit_and_manual_nonwriting():
    with pytest.raises(ValidationError):
        batch(packets=(packet(employer=(reference('employer', span=reference().span),)),))
    for kind in ('text', 'select', 'radio', 'checkbox', 'file', 'sensitive'):
        q = question(type=kind)
        with pytest.raises(ValidationError):
            batch(canonical_questions=(q,), packets=(packet(q),))
        assert batch(canonical_questions=(q,), packets=(), manual_requirements=(
            ManualRequirement(question_id=q.id, reason='Requires explicit manual action'),))
