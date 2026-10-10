"""Synthetic protocol tests; no semantic quality or paid-provider qualification."""
import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_packets import Search, application, compose, question, reference, uid

from copilot.contracts import EvidenceError
from copilot.domain.contracts import HiringCriterion
from copilot.drafting.pipeline import build_context, context_payload, reassess, run_pipeline
from copilot.drafting.quality import assess_output, text_hash
from copilot.drafting.schemas import (
    Claim,
    CritiqueOutput,
    DraftContext,
    DraftOutput,
    GeneratedText,
    PlanItem,
    PlanOutput,
    TargetAssessment,
    TextRange,
)


def context(questions=None, refs=None, **kwargs):
    app = application(questions)
    batch = compose(application=app, search_scoped=Search(refs or (reference(), reference('employer'))), **kwargs)
    return build_context(app, batch)


def draft(ctx, text='😀 did not lead 12 projects.'):
    return DraftOutput(targets=tuple(GeneratedText(target=t.question.id, text=text) for t in ctx.targets))


def critique(ctx, output, *, assessment='supported', rewrite=False, nonfactual=False):
    items = []
    for target, text in zip(ctx.targets, output.targets):
        ranges = (TextRange(start=0, end=len(text.text), excerpt=text.text),) if text.text else ()
        claims = () if nonfactual or not ranges else (Claim(**ranges[0].model_dump(exclude={'schema_version'}),
            kind='personal', assessment=assessment, evidence_ids=(target.facts[0].id,)),)
        items.append(TargetAssessment(target=text.target, inventory_complete=True,
                                      claims=claims, nonfactual_ranges=ranges if nonfactual else ()))
    return CritiqueOutput(text_sha256=text_hash(ctx, output), targets=tuple(items),
                          rewrite_required=rewrite)


class Calls:
    def __init__(self, ctx, rewrite=False):
        self.context = ctx
        self.rewrite = rewrite
        self.calls = []

    def __call__(self, stage, *, instructions, input_value, output_model):
        self.calls.append((stage, instructions, input_value, output_model))
        if stage == 'plan':
            return PlanOutput(targets=tuple(PlanItem(target=t.question.id,
                evidence_ids=(t.facts[0].id,)) for t in self.context.targets))
        if stage in ('draft', 'rewrite'):
            return draft(self.context, '😀 revised.' if stage == 'rewrite' else '😀 original.')
        generated = DraftOutput.model_validate_json(json.dumps(input_value['draft']))
        return critique(self.context, generated, rewrite=self.rewrite)


@pytest.mark.parametrize('rewrite,count', [(False, 3), (True, 5)])
def test_exact_calls_fresh_final_ledger_and_determinism(rewrite, count):
    ctx = context()
    calls = Calls(ctx, rewrite)
    result = run_pipeline(ctx, calls)
    assert len(calls.calls) == count
    assert tuple(item[0] for item in calls.calls) == result.stage_names
    assert result.quality.inventory_complete and not result.quality.reason_codes
    assert result.quality.ledger[0].excerpt == result.draft.targets[0].text
    again = Calls(ctx, rewrite)
    assert run_pipeline(ctx, again) == result
    assert again.calls == calls.calls
    assert calls.calls[-1][2]['text_sha256'] == text_hash(ctx, result.draft)


def test_reassess_exact_one_call_no_text_change_even_rewrite_requested():
    ctx = context()
    original = draft(ctx)
    calls = Calls(ctx, True)
    result = reassess(ctx, original, calls)
    assert result.draft == original
    assert result.stage_names == ('reassess',)
    assert len(calls.calls) == 1


def test_context_owner_questions_revision_and_zero_writing_fail_closed():
    app = application()
    batch = compose(application=app)
    for change in ({'profile_id': uid(99)}, {'application_id': uid(99)},
                   {'questions': (question(text='different'),)}, {'input_revision': 99}):
        with pytest.raises(EvidenceError):
            build_context(app.model_copy(update=change), batch)
    app = application((question(type='sensitive'),))
    with pytest.raises(EvidenceError, match='writing target'):
        build_context(app, compose(application=app))


def test_exact_selected_disclosure_and_all_criterion_support_required():
    first = reference('employer')
    second = reference('employer', span=first.span.model_copy(update={'record_id': uid(80)}))
    hidden = HiringCriterion(id=uid(70), text='UNSELECTED_CRITERION_CANARY', inferred=False,
                             evidence=(first, second))
    selected = HiringCriterion(id=uid(71), text='Selected criterion', inferred=True, evidence=(first,))
    app = application().model_copy(update={'job_description': 'JD_CANARY', 'location': 'PII_CANARY'})
    batch = compose(application=app, hiring_criteria=(hidden, selected),
                    search_scoped=Search((reference(), first)))
    ctx = build_context(app, batch)
    assert [item.id for item in ctx.targets[0].criteria] == [selected.id]
    value = json.dumps(context_payload(ctx))
    for secret in ('UNSELECTED_CRITERION_CANARY', 'JD_CANARY', 'PII_CANARY', 'generation_id',
                   'profile_id', 'application_id', 'text_sha256', 'record_id', 'research_run_id'):
        assert secret not in value
    assert 'Selected criterion' in value and '😀' in json.dumps(context_payload(ctx), ensure_ascii=False)


@pytest.mark.parametrize('assessment', ['unsupported', 'contradicted', 'needs_confirmation', 'unassessed'])
def test_wellformed_unsupported_preserved_blocked(assessment):
    ctx = context()
    generated = draft(ctx, 'I led 900 projects, not 12.')
    quality = assess_output(ctx, generated, critique(ctx, generated, assessment=assessment))
    assert quality.reason_codes == ('CLAIM_' + assessment.upper(),)
    assert quality.ledger[0].excerpt == generated.targets[0].text


@pytest.mark.parametrize('mutation', ['digest', 'target', 'unknown', 'kind', 'uncited', 'range', 'overlap', 'cross'])
def test_malformed_protocol_is_hard_error(mutation):
    ctx = context((question('q1'), question('q2')))
    generated = draft(ctx)
    assessed = critique(ctx, generated)
    claim = assessed.targets[0].claims[0]
    if mutation == 'digest':
        assessed = assessed.model_copy(update={'text_sha256': '0' * 64})
    elif mutation == 'target':
        assessed = assessed.model_copy(update={'targets': assessed.targets[::-1]})
    elif mutation == 'overlap':
        first = assessed.targets[0].model_copy(update={'nonfactual_ranges': (TextRange(start=0,end=1,excerpt='😀'),)})
        assessed = assessed.model_copy(update={'targets': (first, assessed.targets[1])})
    else:
        changes = {'unknown': {'evidence_ids': ('0' * 64,)},
                   'kind': {'kind': 'employer'}, 'uncited': {'evidence_ids': ()},
                   'range': {'excerpt': 'x' * len(claim.excerpt)},
                   'cross': {'evidence_ids': ('1' * 64,)}}[mutation]
        first = assessed.targets[0].model_copy(update={'claims': (claim.model_copy(update=changes),)})
        assessed = assessed.model_copy(update={'targets': (first, assessed.targets[1])})
    with pytest.raises(EvidenceError) as caught:
        assess_output(ctx, generated, assessed)
    assert caught.value.code == 'INVALID_PROVIDER_OUTPUT'
    assert generated.targets[0].text == '😀 did not lead 12 projects.'


def test_coverage_inventory_limits_blank_and_manual_block():
    ctx = context((question('q', max_chars=2), question('manual', type='sensitive')))
    generated = draft(ctx, '😀 x')
    assessed = critique(ctx, generated, nonfactual=True)
    quality = assess_output(ctx, generated, assessed)
    assert set(quality.reason_codes) == {'OUTPUT_LIMIT_EXCEEDED', 'MANUAL_REQUIREMENTS_UNRESOLVED'}
    first = assessed.targets[0].model_copy(update={'inventory_complete': False, 'nonfactual_ranges': ()})
    quality = assess_output(ctx, generated, assessed.model_copy(update={'targets': (first,)}))
    assert {'INCOMPLETE_INVENTORY', 'INCOMPLETE_COVERAGE'} <= set(quality.reason_codes)
    assert not quality.inventory_complete
    blank = draft(ctx, '')
    assert 'REQUIRED_ANSWER_MISSING' in assess_output(ctx, blank, critique(ctx, blank)).reason_codes


def test_nonfactual_classification_is_not_a_fact_confirmation():
    ctx = context()
    generated = draft(ctx)
    quality = assess_output(ctx, generated, critique(ctx, generated, nonfactual=True))
    assert quality.ledger == ()  # Coverage is fallible classification, not entailment.
    assert not hasattr(quality, 'fact_confirmed')
    calls = Calls(ctx)
    run_pipeline(ctx, calls)
    assert all('fallible' in item[1] and 'not proof' in item[1] for item in calls.calls)


def test_surrogates_extra_fields_and_constructed_models_rejected():
    with pytest.raises(ValidationError):
        GeneratedText(target='q', text='\ud800')
    with pytest.raises(ValidationError):
        GeneratedText.model_validate_json('{"target":"q","text":"ok","integrity":"valid"}')
    ctx = context()
    malicious = DraftOutput.model_construct(targets=(GeneratedText.model_construct(target='q', text='\udfff'),))
    with pytest.raises(EvidenceError):
        text_hash(ctx, malicious)
    with pytest.raises(EvidenceError):
        run_pipeline(ctx.model_copy(update={'targets': ()}), Calls(ctx))


def test_plan_cannot_borrow_and_target_sets_are_exact_before_next_call():
    ctx = context()
    calls = []
    def invalid(stage, **kwargs):
        calls.append(stage)
        return PlanOutput(targets=(PlanItem(target='q', evidence_ids=('0' * 64,)),))
    with pytest.raises(EvidenceError):
        run_pipeline(ctx, invalid)
    assert calls == ['plan']


def test_pure_lane_has_no_runtime_imports():
    package = Path(__file__).parents[1] / 'copilot' / 'drafting'
    for name in ('__init__', 'schemas', 'quality', 'prompts', 'pipeline'):
        tree = ast.parse((package / (name + '.py')).read_text())
        modules = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(part in {'provider', 'models', 'store', 'service', 'socket', 'requests', 'httpx', 'os'}
                       for module in modules for part in module.split('.'))


def test_true_cross_question_refs_and_exact_per_question_disclosure():
    app = application((question('first'), question('second')))
    batch = compose(application=app, search_scoped=Search((reference(),)))
    other = reference(span=reference().span.model_copy(update={'record_id': uid(90),
                       'excerpt': 'Z', 'end': 1}))
    second = batch.packets[1].model_copy(update={'facts': (other,)})
    batch = type(batch).model_validate_json(batch.model_copy(update={
        'packets': (batch.packets[0], second)}).model_dump_json())
    ctx = build_context(app, batch)
    disclosed = context_payload(ctx)['targets']
    assert disclosed[0]['facts'][0]['excerpt'] == '😀'
    assert disclosed[1]['facts'][0]['excerpt'] == 'Z'
    assert disclosed[0]['facts'][0]['id'] != disclosed[1]['facts'][0]['id']
    generated = draft(ctx)
    assessed = critique(ctx, generated)
    first = assessed.targets[0]
    claim = first.claims[0].model_copy(update={'evidence_ids': (ctx.targets[1].facts[0].id,)})
    assessed = assessed.model_copy(update={'targets': (
        first.model_copy(update={'claims': (claim,)}), assessed.targets[1])})
    with pytest.raises(EvidenceError):
        assess_output(ctx, generated, assessed)


def test_unicode_codepoints_whitespace_coverage_and_existing_revision_hash():
    from test_packets import NOW, REV

    from copilot.domain.contracts import DraftAnswer, DraftRevision
    from copilot.retrieval.packet_contracts import CoverLetterTarget

    ctx = context((question('q1'), question('q2')), cover_letter_target=CoverLetterTarget(
        text='Cover letter', constraint_origin='user'))
    generated = DraftOutput(targets=tuple(GeneratedText(target=t.question.id, text='😀 e\u0301\n')
                                         for t in ctx.targets))
    assessed = critique(ctx, generated)
    assert all(item.claims[0].end == 5 for item in assessed.targets)
    assert assess_output(ctx, generated, assessed).inventory_complete
    revision = DraftRevision(id=uid(91), profile_id=ctx.profile_id, application_id=ctx.application_id,
        revisions=REV, research_run_id=ctx.research_run_id, cover_letter=generated.targets[-1].text,
        answers=tuple(DraftAnswer(question_id=t.question.id, text=item.text, packet_id=t.packet_id)
                      for t, item in zip(ctx.targets[:-1], generated.targets[:-1])), created_at=NOW)
    assert text_hash(ctx, generated) == revision.text_sha256
    ranges = tuple(TargetAssessment(target=t.question.id, inventory_complete=True,
        nonfactual_ranges=(TextRange(start=0, end=1, excerpt='😀'),
                           TextRange(start=2, end=4, excerpt='e\u0301'))) for t in ctx.targets)
    assert assess_output(ctx, generated, CritiqueOutput(text_sha256=text_hash(ctx, generated),
        targets=ranges, rewrite_required=False)).inventory_complete


def test_callback_cannot_mutate_context_for_later_stage():
    ctx = context()
    calls = Calls(ctx)
    def mutating(stage, **kwargs):
        result = calls(stage, **kwargs)
        if stage == 'plan':
            kwargs['input_value']['context']['targets'][0]['facts'].clear()
        return result
    run_pipeline(ctx, mutating)
    assert calls.calls[1][2]['context']['targets'][0]['facts']


def test_pipeline_preserves_wellformed_overlimit_text_for_blocked_publication():
    ctx = context((question(max_chars=1),))
    result = run_pipeline(ctx, Calls(ctx))
    assert result.draft.targets[0].text == '😀 original.'
    assert result.quality.reason_codes == ('OUTPUT_LIMIT_EXCEEDED',)


def test_final_critique_cannot_reuse_previous_text_digest_or_trigger_sixth_call():
    ctx = context()
    calls = Calls(ctx, True)
    initial = None
    def stale(stage, **kwargs):
        nonlocal initial
        result = calls(stage, **kwargs)
        if stage == 'critique':
            initial = result
        return initial if stage == 'final_critique' else result
    with pytest.raises(EvidenceError):
        run_pipeline(ctx, stale)
    assert [item[0] for item in calls.calls] == ['plan', 'draft', 'critique', 'rewrite', 'final_critique']


def test_missing_extra_duplicate_draft_targets_fail_before_critique():
    ctx = context()
    for targets in ((GeneratedText(target='wrong', text='hello'),),
                    (GeneratedText(target='q', text='hello'), GeneratedText(target='q', text='again'))):
        calls = Calls(ctx)
        def bad_draft(stage, **kwargs):
            result = calls(stage, **kwargs)
            return DraftOutput(targets=targets) if stage == 'draft' else result
        with pytest.raises(EvidenceError):
            run_pipeline(ctx, bad_draft)
        assert len(calls.calls) == 2


@pytest.mark.parametrize('required', [False, True])
def test_only_required_manual_questions_block_and_all_remain_inspectable(required):
    ctx = context((question('writing'), question('manual', type='sensitive', required=required)))
    generated = draft(ctx)
    quality = assess_output(ctx, generated, critique(ctx, generated))
    assert ('MANUAL_REQUIREMENTS_UNRESOLVED' in quality.reason_codes) is required
    assert tuple(item.question_id for item in ctx.manual_requirements) == ('manual',)
    assert ctx.required_manual_question_ids == (('manual',) if required else ())


def test_required_manual_ids_are_bounded_unique_subset():
    ctx = context((question('writing'), question('manual', type='sensitive')))
    for invalid in (('unknown',), ('manual', 'manual'), ('writing',), ('manual',) * 14):
        with pytest.raises(ValidationError):
            DraftContext.model_validate_json(ctx.model_copy(update={
                'required_manual_question_ids': invalid}).model_dump_json())


@pytest.mark.parametrize('required', [False, True])
def test_manual_cover_letter_required_ids_derive_from_exact_cover_target(required):
    from copilot.retrieval.packet_contracts import CoverLetterTarget

    ctx = context(cover_letter_target=CoverLetterTarget(text='x' * 129,
        constraint_origin='user', required=required))
    assert tuple(item.question_id for item in ctx.manual_requirements) == ('cover_letter',)
    assert ctx.required_manual_question_ids == (('cover_letter',) if required else ())
    generated = draft(ctx)
    quality = assess_output(ctx, generated, critique(ctx, generated))
    assert ('MANUAL_REQUIREMENTS_UNRESOLVED' in quality.reason_codes) is required
