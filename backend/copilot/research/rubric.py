"""Pure, inactive validation of interpretations against supplied canonical text.

Supplied provenance is a label, not authenticated authority. Nothing here proves
entailment, publication, currentness, eligibility or hiring-manager preferences.
"""
import re

from ..domain.contracts import SourceSpan, canonical_hash, text_hash
from ..retrieval.packet_contracts import CorpusScope
from .rubric_contracts import (
    MAX_CANONICAL_CODEPOINTS,
    MAX_PROPOSALS,
    MAX_SOURCES,
    MAX_SUPPORTING_SPANS,
    MAX_TOTAL_SPANS,
    VALIDATOR_VERSION,
    RubricCandidate,
    RubricOmission,
    RubricValidation,
    candidate_id,
    span_key,
    valid_text,
)

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_PROVENANCES = ('public_fetch', 'anonymous_rendered_dom', 'user_supplied', 'legacy_model_text')
_SPAN_FIELDS = {'schema_version', 'record_id', 'unit_id', 'start', 'end', 'text_sha256', 'excerpt'}


def _map(value, maximum, fields=None):
    # Never perform dictionary lookup before inspecting every raw key.
    return (type(value) is dict and len(value) <= maximum
            and all(type(key) is str and len(key) <= 128
                    and not any(0xD800 <= ord(char) <= 0xDFFF for char in key) for key in value)
            and (fields is None or value.keys() <= fields))


def _scalar(value, pattern, length):
    return type(value) is str and len(value) == length and pattern.fullmatch(value) is not None


def _schema(value):
    return 'schema_version' not in value or (type(value['schema_version']) is int
                                             and value['schema_version'] == 1)


def _span_container(value):
    return (_map(value, 3, {'schema_version', 'text', 'supporting_spans'}) and _schema(value)
            and type(value.get('supporting_spans')) in (list, tuple))


def _proposal(value):
    return _span_container(value) and valid_text(value.get('text'))


def _raw_span(value):
    return (_map(value, 7, _SPAN_FIELDS) and _schema(value)
            and all(_scalar(value.get(key), _UUID, 36) for key in ('record_id', 'unit_id'))
            and _scalar(value.get('text_sha256'), _DIGEST, 64)
            and type(value.get('start')) is int and type(value.get('end')) is int
            and 0 <= value['start'] < value['end']
            and type(value.get('excerpt')) is str
            and (len(value['excerpt']) > 2000 or not any(
                0xD800 <= ord(char) <= 0xDFFF for char in value['excerpt'])))


def _captures(scope, values):
    sources, units = {}, set()
    for value in values:
        if not _map(value, 64):
            raise ValueError('Invalid rubric captures')
        source, unit = value.get('source'), value.get('unit')
        if not (_map(source, 64) and _map(unit, 64) and _schema(source) and _schema(unit)):
            raise ValueError('Invalid rubric captures')
        if not (all(_scalar(source.get(key), _UUID, 36) for key in
                    ('id', 'profile_id', 'application_id', 'research_run_id'))
                and all(_scalar(unit.get(key), _UUID, 36) for key in
                        ('id', 'source_id', 'application_id', 'research_run_id'))
                and _scalar(source.get('canonical_sha256'), _DIGEST, 64)
                and _scalar(unit.get('text_sha256'), _DIGEST, 64)
                and type(source.get('provenance')) is str and source['provenance'] in _PROVENANCES
                and type(unit.get('text')) is str
                and 0 < len(unit['text']) <= MAX_CANONICAL_CODEPOINTS
                and not any(0xD800 <= ord(char) <= 0xDFFF for char in unit['text'])):
            raise ValueError('Invalid rubric captures')
        if ((source['profile_id'], source['application_id'], source['research_run_id'])
                != (scope.profile_id, scope.application_id, scope.research_run_id)
                or (unit['source_id'], unit['application_id'], unit['research_run_id'])
                != (source['id'], scope.application_id, scope.research_run_id)
                or ('profile_id' in unit and (type(unit['profile_id']) is not str
                                             or unit['profile_id'] != scope.profile_id))
                or source['id'] in sources or unit['id'] in units
                or source['canonical_sha256'] != unit['text_sha256']
                or unit['text_sha256'] != text_hash(unit['text'])):
            raise ValueError('Invalid rubric captures')
        sources[source['id']] = (source, unit)
        units.add(unit['id'])
    return sources


def _correspondence(span, sources):
    pair = sources.get(span['record_id'])
    if pair is None:
        return 'unknown_source'
    source, unit = pair
    if span['unit_id'] != unit['id']:
        return 'unit_mismatch'
    if source['provenance'] != 'public_fetch':
        return 'unsupported_provenance'
    if span['text_sha256'] != unit['text_sha256']:
        return 'hash_mismatch'
    if span['end'] > len(unit['text']):
        return 'range_out_of_bounds'
    if len(span['excerpt']) > 2000 or span['end'] - span['start'] > 2000:
        return 'excerpt_too_long'
    if not span['excerpt'].strip():
        return 'blank_excerpt'
    if span['excerpt'] != unit['text'][span['start']:span['end']]:
        return 'excerpt_mismatch'
    return None


def validate_rubric(scope, captured_values, proposals) -> RubricValidation:
    """Validate bounded plain data; all invocation errors have fixed safe text."""
    if not (_map(scope, 5, {'schema_version', 'profile_id', 'corpus', 'application_id', 'research_run_id'})
            and _schema(scope) and type(scope.get('corpus')) is str and scope['corpus'] == 'employer'
            and all(_scalar(scope.get(key), _UUID, 36) for key in
                    ('profile_id', 'application_id', 'research_run_id'))):
        raise ValueError('Invalid rubric scope')
    if (type(captured_values) not in (list, tuple) or len(captured_values) > MAX_SOURCES
            or type(proposals) not in (list, tuple) or len(proposals) > MAX_PROPOSALS):
        raise ValueError('Invalid rubric bounds')
    if sum(len(p['supporting_spans']) for p in proposals if _span_container(p)) > MAX_TOTAL_SPANS:
        raise ValueError('Invalid rubric bounds')
    scope = CorpusScope.model_validate(scope)
    sources = _captures(scope, captured_values)
    capture_sha256 = canonical_hash({
        'validator_version': VALIDATOR_VERSION, 'scope': scope.model_dump(mode='json'),
        'captures': sorted((s['id'], u['id'], s['provenance'], s['canonical_sha256'])
                           for s, u in sources.values()),
    })
    candidates, omissions, seen_candidates = [], [], set()
    for index, proposal in enumerate(proposals):
        reason, spans = None, []
        if not _proposal(proposal):
            reason = 'malformed_proposal'
        elif not 1 <= len(proposal['supporting_spans']) <= MAX_SUPPORTING_SPANS:
            reason = 'supporting_span_limit'
        elif not all(_raw_span(span) for span in proposal['supporting_spans']):
            reason = 'malformed_proposal'
        else:
            reason = next((failure for raw in proposal['supporting_spans']
                           if (failure := _correspondence(raw, sources))), None)
            seen_spans = set()
            for raw in proposal['supporting_spans']:
                identity = tuple(raw[key] for key in ('record_id', 'unit_id', 'start', 'end'))
                if reason:
                    break
                if identity in seen_spans:
                    reason = 'duplicate_span'
                    break
                seen_spans.add(identity)
                span = SourceSpan.model_validate(raw)
                span.validate_text(sources[span.record_id][1]['text'])
                spans.append(span)
            if not reason:
                spans = tuple(sorted(spans, key=span_key))
                identity = candidate_id(scope, capture_sha256, proposal['text'], spans)
                if identity in seen_candidates:
                    reason = 'duplicate_candidate'
                else:
                    seen_candidates.add(identity)
                    candidates.append(RubricCandidate(id=identity, proposal_index=index,
                                                      text=proposal['text'], supporting_spans=spans))
        if reason:
            omissions.append(RubricOmission(proposal_index=index, reason=reason))
    gaps = tuple(gap for gap, applies in (
        ('no_sources', not sources), ('no_proposals', not proposals),
        ('no_candidates', not candidates), ('proposals_omitted', bool(omissions)),
    ) if applies)
    return RubricValidation(scope=scope, capture_sha256=capture_sha256,
                            submitted_proposal_count=len(proposals), candidates=tuple(candidates),
                            omissions=tuple(omissions), gaps=gaps)
