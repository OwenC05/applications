"""Pure synthetic captures: no store, network, model, credentials or personal data."""
import builtins
import socket
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from copilot.domain.contracts import text_hash
from copilot.research.broker import MAX_CANONICAL_CODEPOINTS, MAX_SOURCES
from copilot.research.criteria import (
    CriterionCandidate,
    ExcerptSelection,
    compile_excerpt_criteria,
)
from copilot.retrieval.packet_contracts import CorpusScope


def fixture(text='No prior experience required; at least 2 projects. Café e\u0301 🚀'):
    scope = CorpusScope(profile_id=str(uuid4()), corpus='employer',
                        application_id=str(uuid4()), research_run_id=str(uuid4()))
    source = dict(id=str(uuid4()), profile_id=scope.profile_id,
                  application_id=scope.application_id, research_run_id=scope.research_run_id,
                  provenance='public_fetch', canonical_sha256=text_hash(text),
                  company='Same Company', purpose='exact_role')
    unit = dict(id=str(uuid4()), source_id=source['id'], application_id=scope.application_id,
                research_run_id=scope.research_run_id, text=text, text_sha256=text_hash(text),
                identity_spans=[])
    values = [dict(source=source, unit=unit, origin=['ignored'], receipt=['ignored'])]
    selection = ExcerptSelection(source_id=source['id'], unit_id=unit['id'], start=0, end=len(text))
    return scope, values, selection


def test_exact_unicode_negation_numbers_and_determinism():
    scope, values, selection = fixture()
    before = deepcopy(values)
    result = compile_excerpt_criteria(scope, values, [selection])
    candidate, = result.candidates
    assert candidate.text == values[0]['unit']['text']
    assert candidate.span.excerpt == candidate.text
    candidate.span.validate_text(values[0]['unit']['text'])
    assert candidate.scope == scope and candidate.inferred is False
    assert candidate.span.record_id == selection.source_id
    assert candidate.span.unit_id == selection.unit_id
    assert 'No prior' in candidate.text and '2 projects' in candidate.text
    assert 'e\u0301 🚀' in candidate.text
    assert result == compile_excerpt_criteria(scope, values, [selection])
    assert not result.gaps and not result.omissions and values == before
    assert 'generation_id' not in candidate.model_dump()
    with pytest.raises(ValidationError):
        candidate.text = 'paraphrase'
    with pytest.raises(ValidationError):
        candidate.scope.corpus = 'facts'


def test_selected_code_points_not_bytes_or_normalized_text():
    text = '🚀 No more than 2 e\u0301 projects.'
    scope, values, selection = fixture(text)
    selection = selection.model_copy(update={'start': 2, 'end': 19})
    candidate, = compile_excerpt_criteria(scope, values, [selection]).candidates
    assert candidate.text == text[2:19] == 'No more than 2 e\u0301'
    assert candidate.span.start == 2 and candidate.span.end == 19


@pytest.mark.parametrize('part,field', [
    ('source', 'profile_id'), ('source', 'application_id'), ('source', 'research_run_id'),
    ('unit', 'application_id'), ('unit', 'research_run_id'), ('unit', 'source_id'),
    ('unit', 'profile_id'),
])
def test_cross_scope_or_broken_graph_rejected_despite_same_company(part, field):
    scope, values, selection = fixture()
    values[0][part][field] = str(uuid4())
    with pytest.raises(ValueError):
        compile_excerpt_criteria(scope, values, [selection])


@pytest.mark.parametrize('part,field,value', [
    ('source', 'canonical_sha256', '0' * 64),
    ('unit', 'text_sha256', '0' * 64),
    ('unit', 'text', 'tampered text'),
    ('unit', 'id', 'not-a-uuid'),
    ('unit', 'text', ''),
    ('source', 'provenance', 'personal_fact'),
    ('source', 'provenance', 'user_jd'),
])
def test_malformed_capture_rejected(part, field, value):
    scope, values, selection = fixture()
    values[0][part][field] = value
    with pytest.raises(ValueError):
        compile_excerpt_criteria(scope, values, [selection])


@pytest.mark.parametrize('provenance', ['anonymous_rendered_dom', 'user_supplied', 'legacy_model_text'])
def test_unsupported_provenance_visible_not_promoted(provenance):
    scope, values, selection = fixture()
    values[0]['source']['provenance'] = provenance
    result = compile_excerpt_criteria(scope, values, [selection])
    assert not result.candidates
    assert result.omissions[0].reason == 'unsupported_provenance'
    assert result.gaps == ('no_candidates', 'selections_omitted')


@pytest.mark.parametrize('change,reason', [
    ({'source_id': str(uuid4())}, 'unknown_source'),
    ({'unit_id': str(uuid4())}, 'unit_mismatch'),
    ({'end': 999}, 'range_out_of_bounds'),
    ({'start': 998, 'end': 999}, 'range_out_of_bounds'),
])
def test_unknown_selection_and_range_omissions(change, reason):
    scope, values, selection = fixture()
    result = compile_excerpt_criteria(scope, values, [selection.model_copy(update=change)])
    assert not result.candidates
    assert result.omissions[0].reason == reason
    assert result.omissions[0].selection_index == 0


def test_duplicate_disclosure_and_identity_depends_on_full_hash_span_and_scope():
    scope, values, selection = fixture('ab')
    first = compile_excerpt_criteria(scope, values, [selection, selection])
    assert len(first.candidates) == 1
    assert first.omissions[0].reason == 'duplicate_span'
    assert first.omissions[0].selection_index == 1
    original = first.candidates[0].id
    different_span = compile_excerpt_criteria(scope, values, [selection.model_copy(update={'end': 1})])
    assert different_span.candidates[0].id != original
    # Same excerpt/source/span; changing canonical text outside the excerpt changes identity.
    subset = selection.model_copy(update={'end': 1})
    values[0]['unit']['text'] = 'ac'
    values[0]['unit']['text_sha256'] = values[0]['source']['canonical_sha256'] = text_hash('ac')
    assert compile_excerpt_criteria(scope, values, [subset]).candidates[0].id != different_span.candidates[0].id
    scope2 = scope.model_copy(update={'research_run_id': str(uuid4())})
    for part in ('source', 'unit'):
        values[0][part]['research_run_id'] = scope2.research_run_id
    assert compile_excerpt_criteria(scope2, values, [selection]).candidates[0].id != original


@pytest.mark.parametrize('count', [20, 21, 100])
def test_candidate_limit_discloses_each_overflow(count):
    scope, values, selection = fixture('x' * 100)
    selections = [selection.model_copy(update={'start': i, 'end': i + 1}) for i in range(count)]
    result = compile_excerpt_criteria(scope, values, selections)
    assert len(result.candidates) == min(count, 20)
    assert len(result.omissions) == max(0, count - 20)
    assert all(x.reason == 'candidate_limit' for x in result.omissions)
    assert tuple(x.selection_index for x in result.omissions) == tuple(range(20, count))


@pytest.mark.parametrize('length', [2000, 2001])
def test_excerpt_codepoint_limit(length):
    scope, values, selection = fixture('🚀' * length)
    result = compile_excerpt_criteria(scope, values, [selection])
    if length == 2000:
        assert len(result.candidates[0].text) == 2000
    else:
        assert not result.candidates and result.omissions[0].reason == 'excerpt_too_long'


@pytest.mark.parametrize('field,value', [
    ('start', True), ('end', False), ('start', 0.0), ('end', '1'),
    ('start', -1), ('end', 0), ('start', 2), ('schema_version', True),
])
def test_selection_strict_types_and_model_copy_revalidation(field, value):
    scope, values, selection = fixture('ab')
    tampered = selection.model_copy(update={field: value})
    with pytest.raises(ValueError):
        compile_excerpt_criteria(scope, values, [tampered])
    with pytest.raises(ValueError):
        ExcerptSelection.model_validate(tampered.model_dump())


@pytest.mark.parametrize('value', [0, True, 'false'])
def test_candidate_inferred_requires_actual_false(value):
    scope, values, selection = fixture()
    candidate = compile_excerpt_criteria(scope, values, [selection]).candidates[0]
    with pytest.raises(ValueError):
        CriterionCandidate.model_validate(candidate.model_copy(update={'inferred': value}).model_dump())


def test_scope_model_copy_and_personal_scope_rejected():
    scope, values, selection = fixture()
    with pytest.raises(ValueError):
        compile_excerpt_criteria(scope.model_copy(update={'application_id': None}), values, [selection])
    personal = CorpusScope(profile_id=scope.profile_id, corpus='facts')
    with pytest.raises(ValueError):
        compile_excerpt_criteria(personal, values, [selection])


def test_empty_inputs_explicit_gaps():
    scope, values, selection = fixture()
    assert compile_excerpt_criteria(scope, [], []).gaps == ('no_sources', 'no_selections', 'no_candidates')
    assert compile_excerpt_criteria(scope, values, []).gaps == ('no_selections', 'no_candidates')
    result = compile_excerpt_criteria(scope, [], [selection])
    assert result.gaps == ('no_sources', 'no_candidates', 'selections_omitted')
    assert result.omissions[0].reason == 'unknown_source'


def test_upfront_bounds_and_canonical_limit():
    scope, values, selection = fixture()
    for sources, selections in ((values * (MAX_SOURCES + 1), []), (values, [selection] * 101),
                                 (iter(values), []), (values, iter([selection]))):
        with pytest.raises(ValueError):
            compile_excerpt_criteria(scope, sources, selections)
    scope, values, selection = fixture('x' * (MAX_CANONICAL_CODEPOINTS + 1))
    with pytest.raises(ValueError):
        compile_excerpt_criteria(scope, values, [])


def test_duplicate_capture_identity_fails_closed():
    scope, values, selection = fixture()
    with pytest.raises(ValueError):
        compile_excerpt_criteria(scope, values * 2, [selection])


def test_no_external_effects_or_capture_metadata_access(monkeypatch):
    scope, values, selection = fixture()

    class Unreadable:
        def __getattribute__(self, name):
            raise AssertionError('Original metadata must not be authenticated here')

    values[0]['origin'] = values[0]['receipt'] = Unreadable()

    def forbidden(*args, **kwargs):
        raise AssertionError('No file IO during compilation')

    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        patch.setattr(socket, 'create_connection', forbidden)
        patch.setattr(socket, 'getaddrinfo', forbidden)
        result = compile_excerpt_criteria(scope, values, [selection])
    assert len(result.candidates) == 1


def test_candidate_revalidates_nested_copied_span_and_scope():
    scope, values, selection = fixture()
    candidate = compile_excerpt_criteria(scope, values, [selection]).candidates[0]
    with pytest.raises(ValueError):
        CriterionCandidate(**{**candidate.model_dump(), 'span': candidate.span.model_copy(update={'start': False})})
    with pytest.raises(ValueError):
        CriterionCandidate(**{**candidate.model_dump(), 'scope': scope.model_copy(update={'application_id': None})})


def test_broker_source_and_canonical_text_caps_are_inclusive():
    scope, values, selection = fixture('x' * MAX_CANONICAL_CODEPOINTS)
    selection = selection.model_copy(update={'end': 1})
    for _ in range(MAX_SOURCES - 1):
        _, extra, _ = fixture('x')
        for part in ('source', 'unit'):
            extra[0][part]['application_id'] = scope.application_id
            extra[0][part]['research_run_id'] = scope.research_run_id
        extra[0]['source']['profile_id'] = scope.profile_id
        values.extend(extra)
    result = compile_excerpt_criteria(scope, values, [selection])
    assert len(result.candidates) == 1 and not result.gaps


def test_selection_cannot_supply_replacement_wording():
    scope, values, selection = fixture()
    with pytest.raises(ValueError):
        compile_excerpt_criteria(scope, values, [{**selection.model_dump(), 'text': 'Invented wishes'}])
