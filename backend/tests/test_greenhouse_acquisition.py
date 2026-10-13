"""Unused Greenhouse composition: synthetic transport only, never live employer IO."""
import gzip
import hashlib
import importlib
import json
import zlib
from dataclasses import FrozenInstanceError, replace

import pytest
from test_research_fetch import GH_API, GH_HOSTS, gh_response
from test_research_fetch import transport as transport_fixture

from copilot.research import fetch, greenhouse

transport = transport_fixture

OPTIONS = dict(expected_company='Synthetic Company', expected_title='Synthetic Engineer',
               board_token='synthetic_board', post_id=12345,
               source_id='synthetic-source', unit_id='synthetic-unit')


def body(**changes):
    value = dict(id=12345, internal_job_id=67890, title=OPTIONS['expected_title'],
                 company_name=OPTIONS['expected_company'],
                 content='<p>Café 🚀 &amp; no experience required.</p>\r\nNot relocation.',
                 absolute_url='https://boards.greenhouse.io/synthetic_board/jobs/12345',
                 updated_at='2026-10-10T12:00:00Z')
    return json.dumps(value | changes, ensure_ascii=False).encode()


def module():
    # Missing implementation is RED during execution, not an import collection error.
    return importlib.import_module('copilot.research.greenhouse_acquisition')


def acquire(budget=None, **changes):
    return module().acquire_greenhouse_detail(
        **(OPTIONS | changes), confirmed_hosts=GH_HOSTS,
        budget=budget or fetch.FetchBudget())


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate'])
def test_adapter_composes_actual_transport_with_literal_pure_parser(transport, encoding):
    decoded = body()
    raw = gzip.compress(decoded) if encoding == 'gzip' else (
        zlib.compress(decoded) if encoding == 'deflate' else decoded)
    transport[1].append(gh_response(raw, headers=[
        ('Content-Type', 'application/json'), ('Content-Encoding', encoding)]))
    result = acquire()
    candidate = result.candidate
    direct = greenhouse.parse_greenhouse_detail(
        decoded, **OPTIONS, requested_url=GH_API, final_url=GH_API)
    assert candidate == direct
    assert result.transport.original_bytes == raw
    assert result.transport.original_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.transport.decoded_json_bytes == candidate.original_bytes == decoded
    assert result.transport.decoded_json_sha256 == candidate.original_sha256
    if encoding != 'identity':
        assert result.transport.original_sha256 != candidate.original_sha256
    assert '<p>Café 🚀 &amp; no experience required.</p>\r\nNot relocation.' in candidate.canonical_text
    for span in candidate.spans:
        assert candidate.canonical_text[span.start:span.end] == span.excerpt
    assert candidate.role_state == 'unknown' and candidate.eligible is False
    assert result.provenance == 'anonymous_greenhouse_detail_get'
    assert result.adapter_version == 'greenhouse-acquisition-v1-localcontract'
    candidate.revalidate()
    with pytest.raises(FrozenInstanceError):
        result.provenance = 'trusted'
    assert len(transport[0]) == 1 and transport[0][0]['closed']


@pytest.mark.parametrize('changes', [
    {'post_id': True}, {'post_id': 1.0}, {'post_id': -1}, {'post_id': 0},
    {'post_id': 2**63}, {'board_token': 'unsafe/path'}, {'board_token': 'x' * 101},
    {'board_token': ''}, {'board_token': 'é'}, {'board_token': None},
    {'expected_company': ''}, {'expected_company': ' '}, {'expected_title': None},
    {'expected_title': '\ud800'}, {'expected_company': 'x' * (greenhouse.MAX_STRING + 1)},
    {'source_id': ''}, {'source_id': 'x' * 201}, {'unit_id': 123}, {'unit_id': 'x' * 201},
])
def test_adapter_preflight_matches_parser_rejections_without_io(transport, monkeypatch, changes):
    dns = []
    monkeypatch.setattr(fetch, '_resolve', lambda *args: dns.append(args))
    # Prove this input is rejected by the existing parser's own preflight too.
    with pytest.raises(greenhouse.GreenhouseError):
        greenhouse.parse_greenhouse_detail(body(), **(OPTIONS | changes),
                                           requested_url=GH_API, final_url=GH_API)
    budget = fetch.FetchBudget()
    adapter = module()
    with pytest.raises((greenhouse.GreenhouseError, fetch.FetchError)):
        adapter.acquire_greenhouse_detail(**(OPTIONS | changes), confirmed_hosts=GH_HOSTS,
                                           budget=budget)
    assert budget.requests == budget.bytes_read == 0 and dns == [] and transport[0] == []


@pytest.mark.parametrize('field,limit', [
    ('expected_company', greenhouse.MAX_STRING), ('expected_title', greenhouse.MAX_STRING),
    ('source_id', 200), ('unit_id', 200), ('board_token', 100),
])
def test_adapter_preflight_accepts_parser_exact_field_boundaries(transport, field, limit):
    value = 'x' * limit
    changes = {field: value}
    response_changes = {'company_name': value} if field == 'expected_company' else (
        {'title': value} if field == 'expected_title' else {})
    if field == 'board_token':
        response_changes['absolute_url'] = f'https://boards.greenhouse.io/{value}/jobs/12345'
    transport[1].append(gh_response(body(**response_changes)))
    candidate = acquire(**changes).candidate
    assert getattr(candidate, field) == value
    assert candidate.role_state == 'unknown' and candidate.eligible is False


@pytest.mark.parametrize('changes', [
    {'id': 67890}, {'internal_job_id': None}, {'internal_job_id': True},
    {'title': None}, {'content': ''}, {'updated_at': None},
])
def test_adapter_rejects_wrong_post_prospect_and_unsupported_schema(transport, changes):
    transport[1].append(gh_response(body(**changes)))
    adapter = module()
    with pytest.raises(greenhouse.GreenhouseError):
        adapter.acquire_greenhouse_detail(**OPTIONS, confirmed_hosts=GH_HOSTS,
                                           budget=fetch.FetchBudget())
    assert len(transport[0]) == 1 and transport[0][0]['closed']


@pytest.mark.parametrize('changes,gap', [
    ({'company_name': 'Other'}, 'company_mismatch'),
    ({'title': 'Other'}, 'title_mismatch'),
    ({'absolute_url': 'https://careers.synthetic.example/jobs/12345'},
     'absolute_url_custom_identity_unverified'),
])
def test_adapter_keeps_visible_identity_gaps_and_never_follows_public_url(transport, changes, gap):
    transport[1].append(gh_response(body(**changes)))
    candidate = acquire().candidate
    assert gap in candidate.gaps
    assert candidate.identity_matches is False
    assert candidate.role_state == 'unknown' and candidate.eligible is False
    assert len(transport[0]) == 1 and transport[0][0]['target'].startswith('/v1/boards/')


def test_adapter_additive_status_and_deadline_cannot_promote_eligibility(transport):
    transport[1].append(gh_response(body(status='open', deadline='2099-12-31', verified=True)))
    candidate = acquire().candidate
    assert candidate.role_state == 'unknown' and candidate.eligible is False
    assert not hasattr(candidate, 'generation_id') and not hasattr(candidate, 'authority')


def test_adapter_parser_failure_does_not_leave_transport_open(transport):
    transport[1].append(gh_response(b'{not valid JSON}'))
    adapter = module()
    with pytest.raises(greenhouse.GreenhouseError):
        adapter.acquire_greenhouse_detail(**OPTIONS, confirmed_hosts=GH_HOSTS,
                                           budget=fetch.FetchBudget())
    assert transport[0][0]['closed']


def test_adapter_transport_failure_never_calls_parser(transport, monkeypatch):
    adapter = module()
    parser_calls = []
    def parser(*args, **kwargs):
        parser_calls.append(True)
        raise AssertionError('parser must not consume failed transport')
    monkeypatch.setattr(adapter, 'parse_greenhouse_detail', parser)
    transport[1].append(gh_response(status=302))
    with pytest.raises(fetch.FetchError):
        acquire()
    assert parser_calls == [] and transport[0][0]['closed']


def test_adapter_postparse_expiry_cannot_return_capsule(transport, monkeypatch):
    adapter = module()
    clock = [100.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    original = adapter.parse_greenhouse_detail
    def delayed_parse(*args, **kwargs):
        candidate = original(*args, **kwargs)
        clock[0] = 115
        return candidate
    monkeypatch.setattr(adapter, 'parse_greenhouse_detail', delayed_parse)
    transport[1].append(gh_response(body()))
    with pytest.raises(fetch.FetchError):
        acquire(fetch.FetchBudget(started=100))
    assert transport[0][0]['closed']


def test_adapter_preflight_time_is_part_of_transport_deadline(transport, monkeypatch):
    module()  # Capture adapter imports before controlled timing starts.
    clock = [100.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    original = fetch._host
    def delayed_host(host):
        clock[0] += 1
        return original(host)
    monkeypatch.setattr(fetch, '_host', delayed_host)
    transport[1].append(gh_response(body()))
    acquire(fetch.FetchBudget(started=100))
    assert transport[0][0]['deadline'] == 115


def test_adapter_shared_run_deadline_shortens_acquisition(transport, monkeypatch):
    module()
    clock = [100.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    transport[1].append(gh_response(body()))
    acquire(fetch.FetchBudget(started=95, run_seconds=6))
    assert transport[0][0]['deadline'] == 101


def test_adapter_dual_representation_is_not_a_runtime_capability(transport):
    transport[1].append(gh_response(body()))
    result = acquire()
    # Frozen value cloning is possible, deliberately not authentication.
    clone = replace(result)
    assert clone == result
    assert not hasattr(clone, 'job_id') and not hasattr(clone, 'generation_binding')
    assert clone.candidate.eligible is False


@pytest.mark.parametrize('field,value', [
    ('source_id', 'different-source'), ('unit_id', 'different-unit'),
    ('expected_company', 'Different Company'), ('expected_title', 'Different Title'),
])
def test_adapter_rejects_self_consistent_parser_result_for_different_association(transport, monkeypatch, field, value):
    adapter = module()
    original = adapter.parse_greenhouse_detail
    def unrelated_candidate(raw, **options):
        return original(raw, **(options | {field: value}))
    monkeypatch.setattr(adapter, 'parse_greenhouse_detail', unrelated_candidate)
    transport[1].append(gh_response(body()))
    with pytest.raises(ValueError):
        acquire()
    assert transport[0][0]['closed']


@pytest.mark.parametrize('field,value', [
    ('original_sha256', '0' * 64), ('parser_version', 'unknown-parser'),
    ('eligible', True), ('role_state', 'live'),
])
def test_adapter_revalidates_parser_result_not_just_field_presence(transport, monkeypatch, field, value):
    adapter = module()
    original = adapter.parse_greenhouse_detail
    def altered_candidate(*args, **kwargs):
        return replace(original(*args, **kwargs), **{field: value})
    monkeypatch.setattr(adapter, 'parse_greenhouse_detail', altered_candidate)
    transport[1].append(gh_response(body()))
    with pytest.raises(ValueError):
        acquire()
    assert transport[0][0]['closed']
