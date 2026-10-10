"""Pure synthetic composition tests, not runtime authorization or retrieval quality."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from copilot.contracts import EvidenceError
from copilot.domain.contracts import (
    ApplicationRecord,
    EvidenceReference,
    HiringCriterion,
    Question,
    RevisionVector,
    SourceSpan,
    text_hash,
)
from copilot.retrieval.packet_contracts import (
    CorpusScope,
    CoverLetterTarget,
    GenerationBinding,
    PacketBatch,
)
from copilot.retrieval.packets import compose_batch, serialize_selected_context


def uid(n):
    return str(UUID(int=n, version=4))


OWNER, APP, RUN, JOB = (uid(n) for n in range(1, 5))
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
REV = RevisionVector(facts=2, application_input=3, research=4)


def question(id='q', **changes):
    return Question(**(dict(id=id, text='Why not 😀?', type='writing',
                           max_words=20, options=('é', 'e\u0301'), constraint_origin='form') | changes))


def application(questions=None):
    return ApplicationRecord(profile_id=OWNER, application_id=APP, company='Company', role='Role',
                             sector='tech', vacancy_url='https://example.test/job',
                             official_domains=('example.test',), questions=questions or (question(),),
                             input_revision=3, created_at=NOW)


def binding(corpus):
    return GenerationBinding(generation_id=uid(10 if corpus == 'facts' else 11),
                             scope=CorpusScope(profile_id=OWNER, corpus=corpus,
                                               application_id=APP if corpus == 'employer' else None,
                                               research_run_id=RUN if corpus == 'employer' else None),
                             revision=2 if corpus == 'facts' else 4, model_fingerprint='a' * 64,
                             chunker_version='chunk-v1', chunk_checksum='b' * 64)


def reference(corpus='facts', **changes):
    return EvidenceReference(**(dict(profile_id=OWNER,
        kind='confirmed_fact' if corpus == 'facts' else 'employer',
        application_id=APP if corpus == 'employer' else None,
        research_run_id=RUN if corpus == 'employer' else None,
        generation_id=binding(corpus).generation_id,
        span=SourceSpan(record_id=uid(20), unit_id=uid(21) if corpus == 'employer' else None,
                        start=0, end=1, excerpt='😀', text_sha256=text_hash('😀 canonical'))) | changes))


def tokenize(text):
    # Deliberately synthetic offset tokenizer: production must inject pinned models.
    return tuple((i, i + 1) for i in range(len(text)))


class Search:
    def __init__(self, refs=()):
        self.calls = []
        self.refs = refs

    def __call__(self, scope, query_variants, *, binding, limit=8):
        self.calls.append((scope, query_variants, binding))
        refs = tuple(r for r in self.refs if (r.kind == 'confirmed_fact') == (scope.corpus == 'facts'))
        return SimpleNamespace(binding=binding, references=refs, hits=('UNUSED_SCORE_CANARY',))


def compose(**changes):
    return compose_batch(**(dict(application=application(), research_run_id=RUN, job_id=JOB,
        fence=1, revisions=REV, facts_generation=binding('facts'), employer_generation=binding('employer'),
        hiring_criteria=(), tokenize=tokenize, tokenizer_version='synthetic-v1',
        tokenizer_fingerprint='c' * 64, search_scoped=Search(), currentness_guard=lambda: None,
        created_at=NOW, per_packet_budget=24000, aggregate_budget=24000) | changes))


def test_zero_hits_bind_generations_and_exact_unicode_question_roundtrip():
    search = Search()
    batch = compose(search_scoped=search)
    assert len(search.calls) == 2
    assert batch.canonical_questions == application().questions
    assert batch.facts_generation == binding('facts')
    assert batch.employer_generation == binding('employer')
    assert batch.packets[0].gaps
    assert batch.count_policy == 'local_retrieval_tokenizer'
    assert PacketBatch.model_validate_json(batch.model_dump_json()) == batch
    assert batch.token_count == len(tokenize(serialize_selected_context(batch.packets[0], batch.hiring_criteria)))


def test_full_negation_and_128_129_compiler_boundary():
    q = question(text='not ' + '😀' * 124)
    search = Search()
    assert compose(application=application((q,)), search_scoped=search).packets
    assert all(call[1][0] == q.text for call in search.calls)
    result = compose(application=application((question(text=q.text + 'x'),)))
    assert not result.packets
    assert '128' in result.manual_requirements[0].reason


def test_question_codepoint_and_count_boundaries():
    def one_token(text):
        return ((0, len(text)),)
    qs = tuple(question(str(i), text='😀' * 2000) for i in range(12))
    assert len(compose(application=application(qs), tokenize=one_token).packets) == 12
    for invalid in (qs + (question('13'),), (question(text='😀' * 2001),)):
        with pytest.raises((ValidationError, ValueError, EvidenceError)):
            compose(application=application(invalid), tokenize=one_token)


def test_nonwriting_manual_controls_and_cover_default():
    qs = tuple(question(kind, type=kind) for kind in ('text', 'select', 'radio', 'checkbox', 'file', 'sensitive'))
    search = Search()
    result = compose(application=application(qs), search_scoped=search,
                     cover_letter_target=CoverLetterTarget(text='Cover', constraint_origin='user'))
    assert len(result.manual_requirements) == 6
    assert len(result.packets) == 1
    assert result.cover_letter_target.max_words == 400
    assert len(search.calls) == 2


def test_whole_references_criteria_and_no_hit_canary():
    fact, employer = reference(), reference('employer')
    criterion = HiringCriterion(id=uid(50), text='Not sales', inferred=True, evidence=(employer,))
    result = compose(search_scoped=Search((fact, employer)), hiring_criteria=(criterion,))
    packet = result.packets[0]
    assert packet.facts == (fact,) and packet.employer == (employer,)
    assert packet.criteria == (criterion.id,)
    context = serialize_selected_context(packet, result.hiring_criteria)
    assert 'UNUSED_SCORE_CANARY' not in context
    assert json.loads(context)['criteria'][0]['inferred'] is True
    assert json.loads(context)['question']['text'] == question().text


def test_exact_budget_and_no_silent_question_omission():
    first = compose()
    count = first.token_count
    assert compose(per_packet_budget=count, aggregate_budget=count).token_count == count
    result = compose(per_packet_budget=count - 1, aggregate_budget=count - 1)
    assert not result.packets and len(result.manual_requirements) == 1
    qs = (question(), question('second'))
    result = compose(application=application(qs), aggregate_budget=count)
    assert len(result.packets) + len(result.manual_requirements) == 2
    assert result.token_count <= count


def test_budget_omits_whole_span_visibly():
    baseline = compose()
    result = compose(search_scoped=Search((reference(),)), aggregate_budget=baseline.token_count)
    assert result.packets[0].facts == ()
    assert result.packets[0].omitted
    assert result.packets[0].gaps


@pytest.mark.parametrize('reason', ['variant_limit', 'token_limit'])
def test_compiler_omissions_keep_criterion_ids_after_filtering_and_duplicates(reason):
    texts = ('x' * 2001, 'Alpha', 'Alpha', 'Beta', 'Gamma', 'Delta') if reason == 'variant_limit' else ('x' * 2001, 'x' * 120)
    criteria = tuple(HiringCriterion(id=uid(50 + i), text=text, inferred=False,
                                    evidence=(reference('employer'),)) for i, text in enumerate(texts))
    packet = compose(hiring_criteria=criteria).packets[0]
    expected = criteria[5 if reason == 'variant_limit' else 1].id
    assert f'criterion:{expected}:{reason}' in packet.omitted
    assert all(item.split(':')[1] in {criterion.id for criterion in criteria}
               for item in packet.omitted if item.startswith('criterion:'))


@pytest.mark.parametrize('field,value', [('profile_id', uid(99)), ('generation_id', uid(99)),
                                        ('application_id', APP)])
def test_foreign_references_fail(field, value):
    with pytest.raises((ValueError, ValidationError)):
        compose(search_scoped=Search((reference(**{field: value}),)))


def test_result_binding_tamper_and_input_model_copy_tamper():
    def wrong(scope, variants, *, binding, limit=8):
        return SimpleNamespace(binding=binding.model_copy(update={'chunk_checksum': 'd' * 64}), references=(), hits=())
    with pytest.raises(ValueError):
        compose(search_scoped=wrong)
    for changes in (
        dict(facts_generation=binding('facts').model_copy(update={'revision': True})),
        dict(application=application().model_copy(update={'input_revision': 99})),
        dict(hiring_criteria=(HiringCriterion(id=uid(50), text='Role', inferred=False,
                                             evidence=(reference('employer'),)).model_copy(update={'evidence': (reference(),)}),)),
        dict(tokenizer_fingerprint='bad'), dict(aggregate_budget=True),
    ):
        with pytest.raises((ValueError, ValidationError)):
            compose(**changes)


@pytest.mark.parametrize('operation', ['tokenize', 'search'])
def test_stale_guard_after_callback_blocks_return(operation):
    state = {'stale': False}
    def guard():
        if state['stale']:
            raise RuntimeError('stale fence')
    def tokens(text):
        state['stale'] = True
        return tokenize(text)
    def search(scope, variants, *, binding, limit=8):
        state['stale'] = True
        return SimpleNamespace(binding=binding, references=(), hits=())
    with pytest.raises(RuntimeError, match='stale fence'):
        compose(currentness_guard=guard, **{operation if operation == 'tokenize' else 'search_scoped':
                                           tokens if operation == 'tokenize' else search})


def test_invalid_tokenizer_offsets_rejected():
    for bad in (lambda _: (), lambda _: ((True, 2),), lambda _: ((2, 1),)):
        with pytest.raises((ValueError, EvidenceError)):
            compose(tokenize=bad)


def test_context_excludes_nonselected_criteria_and_application_canaries():
    criteria = tuple(HiringCriterion(id=uid(50 + i), text='criterion ' + str(i), inferred=False,
                                    evidence=(reference('employer'),)) for i in range(5))
    app = application().model_copy(update={'job_description': 'RAW_DOCUMENT_CANARY',
                                         'location': 'TYPED_CONTACT_CANARY'})
    result = compose(application=app, hiring_criteria=criteria, aggregate_budget=900)
    packet = result.packets[0]
    serialized = serialize_selected_context(packet, criteria)
    assert 'RAW_DOCUMENT_CANARY' not in serialized and 'TYPED_CONTACT_CANARY' not in serialized
    assert [item['id'] for item in json.loads(serialized)['criteria']] == list(packet.criteria)
    assert len(packet.query_variants) <= 4
    assert any('variant_limit' in reason for reason in packet.omitted)
    assert any('context_budget' in reason for reason in packet.omitted)
    assert result.token_count == len(tokenize(serialized))


def test_all_metadata_and_nested_tamper_rejected_before_callbacks():
    search = Search()
    tampered = question().model_copy(update={'max_words': True})
    for changes in (
        dict(application=application((tampered,))),
        dict(application=application((question('cover_letter'),))),
        dict(application=application().model_copy(update={'questions': (question(), question())})),
        dict(aggregate_budget=24001), dict(per_packet_budget=24001),
        dict(employer_generation=binding('employer').model_copy(update={
            'scope': binding('employer').scope.model_copy(update={'research_run_id': uid(99)})})),
        dict(employer_generation=binding('employer').model_copy(update={'model_fingerprint': 'd' * 64})),
    ):
        with pytest.raises((ValueError, ValidationError)):
            compose(search_scoped=search, **changes)
    assert not search.calls


def test_guard_wraps_each_callback_and_final_return():
    events = []
    def guard():
        events.append('guard')
    def tokens(text):
        events.append('tokenize')
        return tokenize(text)
    base = Search()
    def search(*args, **kwargs):
        events.append('search')
        return base(*args, **kwargs)
    compose(tokenize=tokens, search_scoped=search, currentness_guard=guard)
    for i, event in enumerate(events):
        if event in ('tokenize', 'search'):
            assert events[i - 1] == events[i + 1] == 'guard'
    assert events[-1] == 'guard'


def test_guard_still_runs_after_callback_exception_and_manual_only():
    calls = []
    def guard():
        calls.append('guard')
    def failure(text):
        calls.append('failed')
        raise RuntimeError('tokenizer failure')
    with pytest.raises(RuntimeError, match='tokenizer failure'):
        compose(tokenize=failure, currentness_guard=guard)
    assert calls[-3:] == ['guard', 'failed', 'guard']
    calls.clear()
    result = compose(application=application((question(type='file'),)), currentness_guard=guard)
    assert not result.packets and calls == ['guard']


def test_search_refs_validated_even_when_budget_would_omit_them():
    foreign = reference(generation_id=uid(99))
    with pytest.raises(ValueError):
        compose(search_scoped=Search((foreign,)), aggregate_budget=1)


def test_full_batch_identity_hash_changes_and_counts_never_claim_provider_exact():
    a, b = compose(), compose()
    assert a.id != b.id and a.batch_sha256 != b.batch_sha256
    assert a.count_policy == 'local_retrieval_tokenizer'
    assert a.tokenizer_fingerprint == 'c' * 64
    with pytest.raises(ValueError):
        serialize_selected_context(a.packets[0].model_copy(update={'criteria': ('foreign',)}), ())
