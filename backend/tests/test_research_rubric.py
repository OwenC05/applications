"""Synthetic structural-only boundary tests; no runtime activation."""
import builtins
import socket
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from copilot.domain.contracts import SourceSpan, text_hash
from copilot.research import rubric
from copilot.research.rubric import validate_rubric
from copilot.research.rubric_contracts import (
    MAX_CANONICAL_CODEPOINTS,
    MAX_PROPOSALS,
    MAX_SOURCES,
    MAX_TEXT_CODEPOINTS,
    RubricValidation,
)
from copilot.retrieval.packet_contracts import CorpusScope


def fixture(text='No experience required; 2 projects. Café e\u0301 🚀\r\n'):
    scope = dict(profile_id=str(uuid4()), corpus='employer',
                 application_id=str(uuid4()), research_run_id=str(uuid4()))
    source = dict(id=str(uuid4()), profile_id=scope['profile_id'],
                  application_id=scope['application_id'], research_run_id=scope['research_run_id'],
                  provenance='public_fetch', canonical_sha256=text_hash(text))
    unit = dict(id=str(uuid4()), source_id=source['id'], application_id=scope['application_id'],
                research_run_id=scope['research_run_id'], text=text, text_sha256=text_hash(text))
    span = dict(record_id=source['id'], unit_id=unit['id'], start=0, end=len(text),
                text_sha256=text_hash(text), excerpt=text)
    return scope, [dict(source=source, unit=unit)], dict(text='  No experience; 2 projects. e\u0301 🚀\r\n',
                                                       supporting_spans=[span])


def reason(scope, values, proposal):
    result = validate_rubric(scope, values, [proposal])
    assert not result.candidates
    assert result.omissions[0].proposal_index == 0
    return result.omissions[0].reason


def test_exact_preservation_labels_frozen_and_no_mutation():
    scope, values, proposal = fixture()
    before = deepcopy((scope, values, proposal))
    result = validate_rubric(scope, values, [proposal])
    candidate, = result.candidates
    assert candidate.text == proposal['text']
    assert candidate.supporting_spans[0].excerpt == values[0]['unit']['text']
    assert candidate.inferred is True
    assert result.citation_validation == 'supplied_canonical_text_only'
    assert result.semantic_validation == 'not_assessed'
    assert result.authority_validation == 'not_checked'
    assert (scope, values, proposal) == before
    assert result == validate_rubric(scope, values, [proposal])
    assert RubricValidation.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        candidate.text = 'changed'


@pytest.mark.parametrize('part,field', [('source', 'profile_id'), ('source', 'application_id'),
    ('source', 'research_run_id'), ('unit', 'application_id'), ('unit', 'research_run_id'),
    ('unit', 'source_id'), ('unit', 'profile_id')])
def test_capture_scope_graph_failures(part, field):
    scope, values, proposal = fixture()
    values[0][part][field] = str(uuid4())
    with pytest.raises(ValueError, match='^Invalid rubric captures$'):
        validate_rubric(scope, values, [proposal])


@pytest.mark.parametrize('part,field,value', [
    ('source', 'id', 'x'), ('unit', 'id', 'A' * 36), ('source', 'canonical_sha256', 'F' * 64),
    ('unit', 'text_sha256', '0' * 64), ('unit', 'text', 'changed'), ('unit', 'text', ''),
    ('unit', 'text', '\ud800'), ('source', 'provenance', 'private'),
    ('source', 'schema_version', True), ('unit', 'schema_version', 1.0),
])
def test_malformed_capture_fixed_errors(part, field, value):
    scope, values, proposal = fixture()
    values[0][part][field] = value
    with pytest.raises(ValueError, match='^Invalid rubric captures$'):
        validate_rubric(scope, values, [proposal])


@pytest.mark.parametrize('provenance', ['anonymous_rendered_dom', 'user_supplied', 'legacy_model_text'])
def test_provenance_not_promoted(provenance):
    scope, values, proposal = fixture()
    values[0]['source']['provenance'] = provenance
    assert reason(scope, values, proposal) == 'unsupported_provenance'


@pytest.mark.parametrize('change,expected', [
    ({'record_id': str(uuid4())}, 'unknown_source'),
    ({'unit_id': str(uuid4())}, 'unit_mismatch'),
    ({'text_sha256': '0' * 64}, 'hash_mismatch'),
    ({'end': 999999, 'excerpt': 'wrong'}, 'range_out_of_bounds'),
    ({'excerpt': ' '}, 'blank_excerpt'), ({'excerpt': 'wrong'}, 'excerpt_mismatch'),
    ({'start': True}, 'malformed_proposal'), ({'end': '4'}, 'malformed_proposal'),
    ({'start': -1}, 'malformed_proposal'), ({'end': 0}, 'malformed_proposal'),
    ({'generation_id': str(uuid4())}, 'malformed_proposal'),
    ({'schema_version': True}, 'malformed_proposal'),
])
def test_span_omission_reasons(change, expected):
    scope, values, proposal = fixture()
    proposal['supporting_spans'][0].update(change)
    assert reason(scope, values, proposal) == expected


@pytest.mark.parametrize('change', [{'text': ''}, {'text': ' '}, {'text': '\udfff'},
    {'text': 'x' * 2001}, {'inferred': False}, {'id': str(uuid4())}, {'schema_version': True},
    {'supporting_spans': iter(())}, {'supporting_spans': None}])
def test_malformed_proposals(change):
    scope, values, proposal = fixture()
    proposal.update(change)
    assert reason(scope, values, proposal) == 'malformed_proposal'


@pytest.mark.parametrize('count', [0, 5, 80])
def test_per_proposal_span_limit(count):
    scope, values, proposal = fixture()
    proposal['supporting_spans'] *= count
    assert reason(scope, values, proposal) == 'supporting_span_limit'


def test_global_limits_before_hashing(monkeypatch):
    scope, values, proposal = fixture()
    def forbidden(*args):
        raise AssertionError('Hashing must not run')
    monkeypatch.setattr(rubric, 'text_hash', forbidden)
    for captures, proposals in ((values * 7, []), (values, [proposal] * 21),
                               (values, [dict(text='x', supporting_spans=[{}] * 81)])):
        with pytest.raises(ValueError, match='^Invalid rubric bounds$'):
            validate_rubric(scope, captures, proposals)


def test_twenty_proposals_eighty_spans_and_exact_caps():
    scope, values, proposal = fixture('🚀' * 1_000_000)
    spans = []
    for index in range(4):
        spans.append(dict(proposal['supporting_spans'][0], start=index * 2000,
                          end=(index + 1) * 2000, excerpt='🚀' * 2000))
    proposals = [dict(text=f'{index}' + '🚀' * (2000 - len(str(index))), supporting_spans=spans)
                 for index in range(20)]
    for _ in range(5):
        _, extra, _ = fixture(' ')
        for part in ('source', 'unit'):
            extra[0][part].update(application_id=scope['application_id'],
                                   research_run_id=scope['research_run_id'])
        extra[0]['source']['profile_id'] = scope['profile_id']
        values.extend(extra)
    result = validate_rubric(scope, values, proposals)
    assert len(result.candidates) == 20 and not result.omissions
    values[0]['unit']['text'] += 'x'
    with pytest.raises(ValueError, match='^Invalid rubric captures$'):
        validate_rubric(scope, values, [])


@pytest.mark.parametrize('length,expected', [(2000, None), (2001, 'excerpt_too_long')])
def test_excerpt_bound(length, expected):
    scope, values, proposal = fixture('🚀' * length)
    result = validate_rubric(scope, values, [proposal])
    assert (result.omissions[0].reason if result.omissions else None) == expected


def test_permutation_duplicate_identity_and_outside_excerpt_hash():
    scope, values, proposal = fixture('abcdef')
    base = proposal['supporting_spans'][0]
    proposal['supporting_spans'] = [dict(base, start=0, end=1, excerpt='a'),
                                    dict(base, start=1, end=2, excerpt='b')]
    _, extra, _ = fixture('other')
    for part in ('source', 'unit'):
        extra[0][part].update(application_id=scope['application_id'], research_run_id=scope['research_run_id'])
    extra[0]['source']['profile_id'] = scope['profile_id']
    values += extra
    first = validate_rubric(scope, values, [proposal])
    reverse = dict(proposal, supporting_spans=list(reversed(proposal['supporting_spans'])))
    second = validate_rubric(scope, list(reversed(values)), [reverse])
    assert first.capture_sha256 == second.capture_sha256
    assert first.candidates[0].id == second.candidates[0].id
    result = validate_rubric(scope, values, [proposal, reverse, dict(proposal, text='Different interpretation')])
    assert len(result.candidates) == 2 and result.omissions[0].reason == 'duplicate_candidate'
    values[0]['unit']['text'] = 'abcdeg'
    values[0]['source']['canonical_sha256'] = values[0]['unit']['text_sha256'] = text_hash('abcdeg')
    for span in proposal['supporting_spans']:
        span['text_sha256'] = text_hash('abcdeg')
    changed = validate_rubric(scope, values, [proposal])
    assert changed.candidates[0].id != first.candidates[0].id


def test_precedence_all_structural_then_all_correspondence_then_duplicates():
    scope, values, proposal = fixture()
    span = proposal['supporting_spans'][0]
    unknown = dict(span, record_id=str(uuid4()))
    proposal['supporting_spans'] = [unknown, dict(span, start=False)]
    assert reason(scope, values, proposal) == 'malformed_proposal'
    proposal['supporting_spans'] = [span, span, unknown]
    assert reason(scope, values, proposal) == 'unknown_source'
    proposal['supporting_spans'] = [span, span]
    assert reason(scope, values, proposal) == 'duplicate_span'
    result = validate_rubric(scope, values, [proposal, dict(proposal, supporting_spans=[span])])
    assert result.candidates[0].proposal_index == 1


class DictSubclass(dict):
    pass


class ListSubclass(list):
    pass


@pytest.mark.parametrize('kind', ['scope_model', 'scope_subclass', 'capture_subclass',
    'captures_subclass', 'captures_iterator', 'proposals_subclass', 'proposals_iterator'])
def test_reject_nonplain_whole_inputs(kind):
    scope, values, proposal = fixture()
    proposals = [proposal]
    if kind == 'scope_model':
        scope = CorpusScope(**scope)
    elif kind == 'scope_subclass':
        scope = DictSubclass(scope)
    elif kind == 'capture_subclass':
        values[0] = DictSubclass(values[0])
    elif kind == 'captures_subclass':
        values = ListSubclass(values)
    elif kind == 'captures_iterator':
        values = iter(values)
    elif kind == 'proposals_subclass':
        proposals = ListSubclass(proposals)
    else:
        proposals = iter(proposals)
    with pytest.raises(ValueError) as error:
        validate_rubric(scope, values, proposals)
    assert type(error.value) is ValueError and str(error.value).startswith('Invalid rubric ')


@pytest.mark.parametrize('kind', ['proposal', 'span', 'span_model', 'span_container'])
def test_nonplain_nested_omitted(kind):
    scope, values, proposal = fixture()
    if kind == 'proposal':
        proposal = DictSubclass(proposal)
    elif kind == 'span':
        proposal['supporting_spans'][0] = DictSubclass(proposal['supporting_spans'][0])
    elif kind == 'span_model':
        proposal['supporting_spans'][0] = SourceSpan(**proposal['supporting_spans'][0])
    else:
        proposal['supporting_spans'] = ListSubclass(proposal['supporting_spans'])
    assert reason(scope, values, proposal) == 'malformed_proposal'


def test_hostile_keys_rejected_before_lookup_and_metadata_values_ignored(monkeypatch):
    scope, values, proposal = fixture()
    class Hostile:
        armed = False
        def __hash__(self):
            if self.armed:
                raise AssertionError('Caller hash hook')
            return hash('source')
        def __eq__(self, other):
            raise AssertionError('Caller equality hook')
    key = Hostile()
    hostile = {key: 'secret'}
    key.armed = True
    with pytest.raises(ValueError, match='^Invalid rubric captures$'):
        validate_rubric(scope, [hostile], [])
    assert reason(scope, values, hostile) == 'malformed_proposal'
    class Unreadable:
        def __getattribute__(self, name):
            raise AssertionError('Ignored metadata inspected')
    for mapping in (values[0], values[0]['source'], values[0]['unit']):
        mapping['metadata'] = Unreadable()
    def forbidden(*args, **kwargs):
        raise AssertionError('External IO forbidden')
    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        patch.setattr(socket, 'getaddrinfo', forbidden)
        patch.setattr(socket, 'create_connection', forbidden)
        assert len(validate_rubric(scope, values, [proposal]).candidates) == 1


@pytest.mark.parametrize('location', ['scope', 'envelope', 'source', 'unit', 'proposal', 'span'])
def test_map_key_bounds(location):
    scope, values, proposal = fixture()
    target = {'scope': scope, 'envelope': values[0], 'source': values[0]['source'],
              'unit': values[0]['unit'], 'proposal': proposal,
              'span': proposal['supporting_spans'][0]}[location]
    target['x' * 129] = None
    if location in ('proposal', 'span'):
        assert reason(scope, values, proposal) == 'malformed_proposal'
    else:
        with pytest.raises(ValueError):
            validate_rubric(scope, values, [proposal])


@pytest.mark.parametrize('change', ['drop_candidate', 'drop_omission', 'id', 'scope', 'capture',
    'inferred_int', 'inferred_false', 'semantic', 'authority', 'citation', 'count_bool', 'span_copy'])
def test_result_revalidation_rejects_forgery(change):
    scope, values, proposal = fixture()
    result = validate_rubric(scope, values, [proposal, {}])
    raw = {**result.__dict__}
    if change == 'drop_candidate':
        raw['candidates'] = ()
    elif change == 'drop_omission':
        raw['omissions'] = ()
    elif change in ('id', 'inferred_int', 'inferred_false', 'span_copy'):
        candidate = result.candidates[0]
        update = {'id': str(uuid4())} if change == 'id' else {'inferred': 1 if change == 'inferred_int' else False}
        if change == 'span_copy':
            update = {'supporting_spans': (candidate.supporting_spans[0].model_copy(update={'start': True}),)}
        raw['candidates'] = (candidate.model_copy(update=update),)
    elif change == 'scope':
        raw['scope'] = result.scope.model_copy(update={'research_run_id': str(uuid4())})
    elif change == 'capture':
        raw['capture_sha256'] = '0' * 64
    elif change == 'count_bool':
        raw['submitted_proposal_count'] = True
    else:
        raw[change + '_validation'] = 'approved'
    with pytest.raises(ValueError):
        RubricValidation.model_validate(raw)


def test_empty_gaps_duplicate_capture_and_broker_caps():
    scope, values, proposal = fixture()
    assert validate_rubric(scope, [], []).gaps == ('no_sources', 'no_proposals', 'no_candidates')
    with pytest.raises(ValueError, match='^Invalid rubric captures$'):
        validate_rubric(scope, values * 2, [])
    from copilot.research import broker, criteria
    assert MAX_SOURCES == broker.MAX_SOURCES == 6
    assert MAX_CANONICAL_CODEPOINTS == broker.MAX_CANONICAL_CODEPOINTS == 1_000_000
    assert MAX_PROPOSALS == criteria.MAX_CANDIDATES == 20
    assert MAX_TEXT_CODEPOINTS == criteria.MAX_EXCERPT_CODEPOINTS == 2000


@pytest.mark.parametrize('location,limit', [('scope', 5), ('envelope', 64), ('source', 64),
                                          ('unit', 64), ('proposal', 3), ('span', 7)])
def test_finite_key_count_before_lookup(location, limit):
    scope, values, proposal = fixture()
    target = {'scope': scope, 'envelope': values[0], 'source': values[0]['source'],
              'unit': values[0]['unit'], 'proposal': proposal,
              'span': proposal['supporting_spans'][0]}[location]
    for index in range(limit + 1):
        target[f'ignored_{index}'] = object()
    if location in ('proposal', 'span'):
        assert reason(scope, values, proposal) == 'malformed_proposal'
    else:
        with pytest.raises(ValueError):
            validate_rubric(scope, values, [proposal])


@pytest.mark.parametrize('change', ['scope', 'interpretation', 'span'])
def test_candidate_identity_binds_each_context_component(change):
    scope, values, proposal = fixture('abcdef')
    proposal['supporting_spans'][0].update(end=1, excerpt='a')
    original = validate_rubric(scope, values, [proposal]).candidates[0].id
    if change == 'scope':
        scope['research_run_id'] = str(uuid4())
        for part in ('source', 'unit'):
            values[0][part]['research_run_id'] = scope['research_run_id']
    elif change == 'interpretation':
        proposal['text'] += ' '
    else:
        proposal['supporting_spans'][0].update(start=1, end=2, excerpt='b')
    assert validate_rubric(scope, values, [proposal]).candidates[0].id != original


def test_candidate_identity_excludes_proposal_index():
    scope, values, proposal = fixture()
    direct = validate_rubric(scope, values, [proposal]).candidates[0]
    shifted = validate_rubric(scope, values, [{}, proposal]).candidates[0]
    assert shifted.proposal_index == 1 and shifted.id == direct.id


def test_capture_hash_binds_even_unselected_source():
    scope, values, proposal = fixture()
    _, extra, _ = fixture('other')
    for part in ('source', 'unit'):
        extra[0][part].update(application_id=scope['application_id'], research_run_id=scope['research_run_id'])
    extra[0]['source']['profile_id'] = scope['profile_id']
    values += extra
    first = validate_rubric(scope, values, [proposal])
    values[1]['unit']['text'] = 'changed outside all evidence'
    values[1]['unit']['text_sha256'] = values[1]['source']['canonical_sha256'] = text_hash(values[1]['unit']['text'])
    changed = validate_rubric(scope, values, [proposal])
    assert changed.capture_sha256 != first.capture_sha256
    assert changed.candidates[0].id != first.candidates[0].id


def test_all_correspondence_precedence_in_original_order():
    scope, values, proposal = fixture()
    span = proposal['supporting_spans'][0]
    span.update(unit_id=str(uuid4()), text_sha256='0' * 64, end=999999, excerpt=' ')
    assert reason(scope, values, proposal) == 'unit_mismatch'
    span['unit_id'] = values[0]['unit']['id']
    values[0]['source']['provenance'] = 'user_supplied'
    assert reason(scope, values, proposal) == 'unsupported_provenance'
    values[0]['source']['provenance'] = 'public_fetch'
    assert reason(scope, values, proposal) == 'hash_mismatch'
    span['text_sha256'] = values[0]['unit']['text_sha256']
    assert reason(scope, values, proposal) == 'range_out_of_bounds'
    span['end'] = len(values[0]['unit']['text'])
    span['excerpt'] = 'x' * 2001
    assert reason(scope, values, proposal) == 'excerpt_too_long'
    span['excerpt'] = ' '
    assert reason(scope, values, proposal) == 'blank_excerpt'
    span['excerpt'] = 'wrong'
    assert reason(scope, values, proposal) == 'excerpt_mismatch'


def test_codepoint_not_byte_or_normalized_range():
    scope, values, proposal = fixture('🚀 e\u0301\r\n')
    proposal['supporting_spans'][0].update(start=2, end=6, excerpt='e\u0301\r\n')
    span = validate_rubric(scope, values, [proposal]).candidates[0].supporting_spans[0]
    assert span.start == 2 and span.end == 6 and span.excerpt == 'e\u0301\r\n'


def test_unsorted_duplicate_and_forged_output_children_rejected():
    scope, values, proposal = fixture('ab')
    span = proposal['supporting_spans'][0]
    proposal['supporting_spans'] = [dict(span, end=1, excerpt='a'), dict(span, start=1, excerpt='b')]
    result = validate_rubric(scope, values, [proposal])
    candidate = result.candidates[0]
    for spans in (tuple(reversed(candidate.supporting_spans)),
                  (candidate.supporting_spans[0],) * 2):
        with pytest.raises(ValueError):
            RubricValidation.model_validate({**result.__dict__, 'candidates': (
                candidate.model_copy(update={'supporting_spans': spans}),)})
    forged = candidate.model_copy(update={'permission': True})
    with pytest.raises(ValueError):
        RubricValidation.model_validate({**result.__dict__, 'candidates': (forged,)})


def test_raw_proposal_models_forbidden_and_schema_metadata_permitted():
    scope, values, proposal = fixture()
    from copilot.research.rubric_contracts import RubricProposal
    assert reason(scope, values, RubricProposal(text=proposal['text'], supporting_spans=tuple(
        SourceSpan(**span) for span in proposal['supporting_spans']))) == 'malformed_proposal'
    for mapping in (scope, values[0]['source'], values[0]['unit'], proposal,
                    proposal['supporting_spans'][0]):
        mapping['schema_version'] = 1
    assert len(validate_rubric(scope, values, [proposal]).candidates) == 1


def test_global_concrete_span_count_does_not_depend_on_interpretation_validity():
    scope, values, _ = fixture()
    with pytest.raises(ValueError, match='^Invalid rubric bounds$'):
        validate_rubric(scope, values, [dict(text='', supporting_spans=[{}] * 81)])
    # Unknown fields make the map malformed; nested values must not be examined.
    assert reason(scope, values, dict(text='x', supporting_spans=[{}] * 81,
                                      permission=True)) == 'malformed_proposal'


def test_generators_never_consumed():
    scope, values, proposal = fixture()
    def forbidden_generator():
        raise AssertionError('Generator was consumed')
        yield None
    for captures, proposals in ((forbidden_generator(), []), (values, forbidden_generator())):
        with pytest.raises(ValueError):
            validate_rubric(scope, captures, proposals)
    proposal['supporting_spans'] = forbidden_generator()
    assert reason(scope, values, proposal) == 'malformed_proposal'


def test_result_cannot_claim_no_sources_with_accepted_candidate():
    scope, values, proposal = fixture()
    result = validate_rubric(scope, values, [proposal])
    with pytest.raises(ValueError):
        RubricValidation.model_validate({**result.__dict__, 'gaps': ('no_sources',)})


@pytest.mark.parametrize('change', ['semantic', 'authority', 'citation', 'count', 'permission',
    'missing', 'candidate_inferred', 'candidate_permission', 'candidate_missing',
    'omission_index', 'omission_permission', 'scope_permission', 'span_boolean',
    'span_permission', 'candidate_identity'])
@pytest.mark.parametrize('construction', ['copy', 'construct'])
def test_direct_result_model_revalidation_does_not_trust_instances(change, construction):
    scope, values, proposal = fixture()
    result = validate_rubric(scope, values, [proposal, {}])
    def forge(model, update, missing=None):
        raw = {**model.__dict__, **(model.__pydantic_extra__ or {}), **update}
        if missing:
            raw.pop(missing)
        if construction == 'construct':
            instance = type(model).model_construct(**raw)
            # model_construct drops unknown fields under extra=forbid. Preserve
            # an actual forged attribute, rather than testing a discarded value.
            for key, value in update.items():
                if key not in type(model).model_fields:
                    instance.__dict__[key] = value
            return instance
        instance = model.model_copy(update=update)
        if missing:
            instance.__dict__.pop(missing)
        return instance
    updates = {}
    if change in ('semantic', 'authority', 'citation'):
        updates[change + '_validation'] = 'approved'
    elif change == 'count':
        updates['submitted_proposal_count'] = True
    elif change == 'permission':
        updates['permission'] = True
    elif change == 'missing':
        forged = forge(result, {}, 'capture_sha256')
    elif change.startswith('candidate_'):
        child = result.candidates[0]
        update = {'inferred': False} if change == 'candidate_inferred' else (
            {'id': str(uuid4())} if change == 'candidate_identity' else {'permission': True})
        updates['candidates'] = (forge(child, update if change != 'candidate_missing' else {},
                                       'text' if change == 'candidate_missing' else None),)
    elif change.startswith('omission_'):
        updates['omissions'] = (forge(result.omissions[0],
            {'proposal_index': 0} if change == 'omission_index' else {'permission': True}),)
    elif change == 'scope_permission':
        updates['scope'] = forge(result.scope, {'permission': True})
    else:
        child = result.candidates[0]
        span = forge(child.supporting_spans[0],
                     {'start': True} if change == 'span_boolean' else {'permission': True})
        updates['candidates'] = (forge(child, {'supporting_spans': (span,)}),)
    if change != 'missing':
        forged = forge(result, updates)
    with pytest.raises(ValueError):
        RubricValidation.model_validate(forged)


@pytest.mark.parametrize('field', ['candidates', 'omissions', 'gaps', 'supporting_spans'])
def test_output_python_container_types_are_not_coerced(field):
    scope, values, proposal = fixture()
    result = validate_rubric(scope, values, [proposal, {}])
    if field == 'supporting_spans':
        candidate = result.candidates[0]
        copied = candidate.model_copy(update={field: list(candidate.supporting_spans)})
        forged = result.model_copy(update={'candidates': (copied,)})
    else:
        forged = result.model_copy(update={field: list(getattr(result, field))})
    with pytest.raises(ValueError):
        RubricValidation.model_validate(forged)


@pytest.mark.parametrize('boundary,update,missing', [
    ('scope', {}, 'profile_id'), ('span', {}, 'record_id'),
    ('omission', {}, 'reason'), ('candidate', {'proposal_index': True}, None),
    ('omission', {'proposal_index': True}, None), ('scope', {'schema_version': True}, None),
    ('span', {'schema_version': True}, None), ('candidate', {'schema_version': True}, None),
])
def test_nested_construct_missing_fields_and_strict_scalars(boundary, update, missing):
    scope, values, proposal = fixture()
    result = validate_rubric(scope, values, [proposal, {}])
    candidate = result.candidates[0]
    model = {'scope': result.scope, 'span': candidate.supporting_spans[0],
             'omission': result.omissions[0], 'candidate': candidate}[boundary]
    raw = {**model.__dict__, **update}
    if missing:
        raw.pop(missing)
    forged = type(model).model_construct(**raw)
    if boundary == 'scope':
        root = result.model_copy(update={'scope': forged})
    elif boundary == 'omission':
        root = result.model_copy(update={'omissions': (forged,)})
    else:
        if boundary == 'span':
            forged = candidate.model_copy(update={'supporting_spans': (forged,)})
        root = result.model_copy(update={'candidates': (forged,)})
    with pytest.raises(ValueError):
        RubricValidation.model_validate(root)


def test_valid_model_instances_reconstruct_without_changing_exact_values():
    scope, values, proposal = fixture()
    result = validate_rubric(scope, values, [proposal, {}])
    assert RubricValidation.model_validate(result) == result
    assert type(result.candidates[0]).model_validate(result.candidates[0]) == result.candidates[0]
    assert type(result.omissions[0]).model_validate(result.omissions[0]) == result.omissions[0]
