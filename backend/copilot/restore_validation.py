"""Pure bounded logical ROW SHAPES, not complete snapshot validation or authority.

Increment one is intentionally incomplete: cross-row graphs, persisted job-stage
variants, legacy provenance decoding, integrity checks and original bytes are
separate required gates. No successful graph observation is exposed here.
"""
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from .contracts import EvidenceError

_MAX_INTEGER = 2**63 - 1
_MAX_REVISION = 9_007_199_254_740_991
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_VERSION = 'copilot-logical-snapshot-v1'
_MESSAGES = {
    'RESTORE_LOGICAL_INVALID': 'Unsupported or malformed logical snapshot',
    'RESTORE_LOGICAL_LIMIT': 'Logical snapshot validation exceeds supported bounds',
    'RESTORE_ORIGINAL_INVALID': 'Original inventory does not match retained evidence',
    'RESTORE_ORIGINAL_LIMIT': 'Original inventory exceeds supported bounds',
}


def _fail(code='RESTORE_LOGICAL_INVALID'):
    raise EvidenceError(code, _MESSAGES[code]) from None


TABLE_COLUMNS = MappingProxyType({'app_revisions': ('owner', 'application_id', 'research'), 'budget_limits': ('owner', 'daily_tokens', 'daily_calls', 'job_tokens', 'job_calls'), 'dense_mutations': ('generation_id', 'fence', 'state'), 'drafting_admission_bindings': ('job_id', 'owner', 'application_id', 'kind', 'origin', 'captured_revision', 'consumed_revision', 'request_sha256', 'envelope_sha256'), 'drafting_admission_heads': ('owner', 'application_id', 'revision', 'last_job_id'), 'fact_metadata': ('id', 'owner', 'data'), 'generation_chunks': ('generation_id', 'id', 'owner', 'data'), 'generation_intents': ('id', 'owner', 'corpus', 'state', 'data', 'captured'), 'job_keys': ('owner', 'key', 'job_id', 'request_sha256', 'application_id', 'kind'), 'job_retries': ('new_job_id', 'owner', 'prior_job_id', 'prior_attempt_id', 'warning_version'), 'job_stages': ('job_id', 'stage', 'fence', 'data', 'sha256'), 'jobs': ('id', 'owner', 'state', 'data', 'parameters', 'dependencies'), 'manifests': ('id', 'owner', 'corpus', 'state', 'data'), 'profile_revisions': ('owner', 'metadata', 'facts', 'documents', 'consent'), 'profiles': ('id', 'data'), 'provider_attempts': ('id', 'owner', 'job_id', 'state', 'data', 'dispatched', 'day', 'response', 'stage', 'request_sha256'), 'provider_retry_links': ('attempt_id', 'prior_attempt_id'), 'records': ('id', 'owner', 'kind', 'data'), 'research_blob_origins': ('id', 'owner', 'application_id', 'run_id', 'intent_id', 'job_id', 'fence'), 'research_blob_scopes': ('id', 'owner', 'application_id', 'run_id', 'intent_id'), 'research_heads': ('application_id', 'owner', 'run_id', 'research', 'output'), 'research_intents': ('id', 'owner', 'application_id', 'job_id', 'fence', 'run_id', 'state'), 'tickets': ('id', 'data'), 'tombstones': ('id', 'owner', 'kind'), 'upload_intents': ('id', 'owner', 'state', 'blob_sha256'), 'upload_receipts': ('id', 'device', 'inode')})
RECORD_KINDS = frozenset({
    'source', 'unit', 'fact', 'chunk:facts', 'chunk:documents',
    'workspace:consent', 'workspace:typed_value', 'workspace:interview_answer',
    'workspace:interview_progress', 'workspace:proposal', 'workspace:application',
    'workspace:employer_source', 'workspace:research_run', 'workspace:draft',
    'workspace:draft_review', 'workspace:packet_batch', 'workspace:packet_intent',
    'workspace:packet_dependency', 'workspace:draft_intent', 'workspace:draft_dependency',
    'workspace:preferences', 'workspace:history', 'workspace:interview_archive',
    'workspace:legacy_archive', 'workspace:legacy_relation', 'workspace:employer_unit',
})
# Presence/counts cannot substitute for future explicit complete-graph coverage.
TABLE_VALIDATION_COVERAGE = MappingProxyType({name: 'shape_only' for name in TABLE_COLUMNS})
RECORD_VALIDATION_COVERAGE = MappingProxyType({name: 'shape_only' for name in RECORD_KINDS})
GRAPH_VALIDATION_COMPLETE = False


@dataclass
class LogicalBudget:
    rows: int = 0
    json_bytes: int = 0
    index_bytes: int = 0
    reads: int = 0
    row_nodes: int = 0

    def add(self, field, amount, maximum):
        if type(amount) is not int or amount < 0:
            _fail()
        current = getattr(self, field)
        if type(current) is not int or current < 0 or current + amount > maximum:
            _fail('RESTORE_LOGICAL_LIMIT')
        setattr(self, field, current + amount)


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        _fail()


def _integer(value, *, minimum=0, maximum=_MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _text(value, *, maximum=16777216, minimum=0):
    if type(value) is not str or not minimum <= len(value) <= maximum:
        _fail()
    try:
        value.encode('utf-8')
    except UnicodeError:
        _fail()
    return value


def _identifier(value):
    _text(value)
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        _fail()
    if canonical != value:
        _fail()
    return value


def _digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        _fail()
    return value


def _timestamp(value):
    _text(value, maximum=100, minimum=1)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail()
    if parsed.utcoffset() != timedelta(0):
        _fail()


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _decode_json(serialized, budget):
    if type(serialized) is not str:
        _fail()
    try:
        encoded = serialized.encode('utf-8')
    except UnicodeError:
        _fail()
    if len(encoded) > 16777216:
        _fail('RESTORE_LOGICAL_LIMIT')
    budget.add('json_bytes', len(encoded), 67108864)
    try:
        value = json.loads(serialized, object_pairs_hook=_json_pairs,
                           parse_constant=lambda _: _fail())
    except RecursionError:
        _fail('RESTORE_LOGICAL_LIMIT')
    except (ValueError, UnicodeError):
        _fail()
    if type(value) not in (dict, list):
        _fail()
    frames = [iter(((value, 1),))]
    while frames:
        try:
            item, depth = next(frames[-1])
        except StopIteration:
            frames.pop()
            continue
        if depth > 64:
            _fail('RESTORE_LOGICAL_LIMIT')
        budget.add('row_nodes', 1, 100000)
        if type(item) in (dict, list):
            minimum_children = len(item) * (2 if type(item) is dict else 1)
            if budget.row_nodes + minimum_children > 100000:
                _fail('RESTORE_LOGICAL_LIMIT')
            frames.append(_json_children(item, depth + 1))
        elif type(item) is str:
            _text(item)
        elif type(item) is float and not math.isfinite(item):
            _fail()
        elif type(item) not in (int, float, bool, type(None)):
            _fail()
    return value


def _json_children(value, depth):
    # Iterator frames retain only the current ancestry, never all siblings.
    if type(value) is dict:
        for key, child in value.items():
            yield key, depth
            yield child, depth
    else:
        for child in value:
            yield child, depth


def _model_shapes():
    # Contract-only imports; no Store, service, model inference or provider broker.
    from .domain import contracts as c
    from .drafting.assessment_contracts import AssessmentIntent, AssessmentPublication
    from .drafting.edit_contracts import EditPublication
    from .drafting.review_contracts import ReviewPublication
    from .drafting.runtime_contracts import DraftIntent, DraftPublication
    from .retrieval.packet_contracts import PacketBatch
    from .retrieval.packet_runtime_contracts import PacketIntent, PacketPublication
    return {
        'workspace:consent': (c.ConsentRecord,),
        'workspace:typed_value': (c.TypedValue,),
        'workspace:interview_answer': (c.InterviewAnswer,),
        'workspace:interview_progress': (c.InterviewProgress,),
        'workspace:proposal': (c.Proposal,),
        'workspace:application': (c.ApplicationRecord,),
        'workspace:employer_source': (c.EmployerSource,),
        'workspace:research_run': (c.ResearchRun,),
        'workspace:draft': (c.DraftRevision,),
        'workspace:draft_review': (c.ReviewSnapshot,),
        'workspace:packet_batch': (PacketBatch,),
        'workspace:packet_intent': (PacketIntent,),
        'workspace:packet_dependency': (PacketPublication,),
        'workspace:draft_intent': (DraftIntent, AssessmentIntent),
        'workspace:draft_dependency': (DraftPublication, EditPublication,
                                       AssessmentPublication, ReviewPublication),
    }


def _require_dump_shape(original, reconstructed):
    pending = [(original, reconstructed)]
    while pending:
        before, after = pending.pop()
        if type(after) is dict:
            if type(before) is not dict or set(before) != set(after):
                _fail()
            pending.extend((before[key], after[key]) for key in after)
        elif type(after) is list:
            if type(before) is not list or len(before) != len(after):
                _fail()
            pending.extend(zip(before, after))


def _model_numeric_limits(instance):
    from pydantic import BaseModel

    from .domain.contracts import RevisionVector
    pending = [(instance, False)]
    while pending:
        value, revision = pending.pop()
        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                is_revision = (isinstance(value, RevisionVector) or name in ('revision', 'fence')
                               or name.endswith('_revision'))
                pending.append((getattr(value, name), is_revision))
        elif type(value) is int:
            if not -2**63 <= value <= _MAX_INTEGER:
                _fail()
            if revision and not 0 <= value <= _MAX_REVISION:
                _fail()
        elif type(value) in (tuple, list):
            pending.extend((child, revision) for child in value)
        elif type(value) is dict:
            # Integer range checks do not make arbitrary dicts closed graph schemas.
            pending.extend((child, False) for child in value.values())


def _contract(value, model):
    try:
        if type(value) is not dict or set(value) != set(model.model_fields):
            _fail()
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        instance = model.model_validate_json(encoded, strict=True)
        _model_numeric_limits(instance)
        _require_dump_shape(value, instance.model_dump(mode='json'))
        return instance
    except (ValueError, TypeError, UnicodeError):
        _fail()


def _hash_text(value, checksum):
    _text(value)
    if hashlib.sha256(value.encode('utf-8')).hexdigest() != _digest(checksum):
        _fail()


def _source_shape(value):
    _keys(value, 'id profile_id filename media_type blob_sha256 parser_version state')
    _identifier(value['id'])
    _identifier(value['profile_id'])
    name = _text(value['filename'], minimum=1, maximum=200)
    if '/' in name or '\\' in name or any(ord(c) < 32 for c in name):
        _fail()
    if value['media_type'] not in ('text/plain', 'text/markdown', 'application/pdf'):
        _fail()
    _digest(value['blob_sha256'])
    _text(value['parser_version'], minimum=1, maximum=200)
    if value['state'] != 'active':
        _fail()


def _unit_shape(value):
    _keys(value, 'id profile_id source_id ordinal page text text_sha256')
    for name in ('id', 'profile_id', 'source_id'):
        _identifier(value[name])
    _integer(value['ordinal'], maximum=99)
    if value['page'] is not None:
        _integer(value['page'], minimum=1, maximum=100)
    _text(value['text'], maximum=1000000)
    _hash_text(value['text'], value['text_sha256'])


def _span(value):
    _keys(value, 'unit_id start end')
    _identifier(value['unit_id'])
    _integer(value['start'])
    _integer(value['end'], minimum=value['start'] + 1)


def _fact_shape(value):
    _keys(value, 'id profile_id text text_sha256 confirmed_at provenance origin state')
    _identifier(value['id'])
    _identifier(value['profile_id'])
    _text(value['text'], minimum=1, maximum=20000)
    _hash_text(value['text'], value['text_sha256'])
    _timestamp(value['confirmed_at'])
    if value['provenance'] not in ('manual', 'interview', 'document_derived', 'legacy'):
        _fail()
    if value['state'] not in ('active', 'superseded'):
        _fail()
    if value['origin'] is not None:
        _span(value['origin'])


def _chunk_shape(value, *, employer=False):
    names = 'id profile_id corpus record_id unit_id start end text text_sha256'
    _keys(value, names + (' application_id research_run_id' if employer else ''))
    _digest(value['id'])
    for name in ('profile_id', 'record_id'):
        _identifier(value[name])
    if value['unit_id'] is not None:
        _identifier(value['unit_id'])
    _integer(value['start'])
    _integer(value['end'], minimum=value['start'] + 1)
    _hash_text(value['text'], value['text_sha256'])
    if len(value['text']) != value['end'] - value['start']:
        _fail()
    if employer:
        if value['corpus'] != 'employer':
            _fail()
        _identifier(value['application_id'])
        _identifier(value['research_run_id'])
    elif value['corpus'] not in ('facts', 'documents'):
        _fail()


def _raw_record(kind, value):
    from .domain.contracts import ApplicationRecord, InterviewAnswer, SourceSpan
    if kind == 'source':
        _source_shape(value)
    elif kind == 'unit':
        _unit_shape(value)
    elif kind == 'fact':
        _fact_shape(value)
    elif kind in ('chunk:facts', 'chunk:documents'):
        _chunk_shape(value)
        if kind != 'chunk:' + value['corpus']:
            _fail()
    elif kind == 'workspace:preferences':
        _keys(value, 'id writing_preferences')
        _identifier(value['id'])
        _text(value['writing_preferences'], maximum=10000)
    elif kind == 'workspace:history':
        _keys(value, 'id profile_id application_id kind text recorded_at input_revision '
                     'output_revision application_snapshot')
        for name in ('id', 'profile_id', 'application_id'):
            _identifier(value[name])
        if value['kind'] not in ('feedback', 'user_reported_submitted'):
            _fail()
        _text(value['text'], minimum=1, maximum=20000)
        _timestamp(value['recorded_at'])
        for name in ('input_revision', 'output_revision'):
            _integer(value[name], maximum=_MAX_REVISION)
        _contract(value['application_snapshot'], ApplicationRecord)
    elif kind == 'workspace:interview_archive':
        _keys(value, 'id answer')
        _identifier(value['id'])
        _contract(value['answer'], InterviewAnswer)
    elif kind == 'workspace:legacy_archive':
        _keys(value, 'id profile_id source_sha256 original_bytes_base64 legacy_profile '
                     'current_approval legacy_cloud_consent_not_carried_forward')
        _identifier(value['id'])
        _identifier(value['profile_id'])
        _digest(value['source_sha256'])
        _text(value['original_bytes_base64'], maximum=1398104)
        if (value['current_approval'] is not False
                or type(value['legacy_cloud_consent_not_carried_forward']) is not bool
                or type(value['legacy_profile']) is not dict):
            _fail()
        # Full bounded base64/source decode/provenance is a later graph gate.
    elif kind == 'workspace:legacy_relation':
        _keys(value, 'id proposal_id legacy_source')
        _identifier(value['id'])
        _identifier(value['proposal_id'])
        source = value['legacy_source']
        if type(source) is not dict or set(source) not in ({'kind', 'id', 'label'},
                                                         {'kind', 'id', 'label', 'question_id'}):
            _fail()
        _text(source['kind'], minimum=1, maximum=100)
        for name in ('id', 'label', 'question_id'):
            if name in source and source[name] is not None:
                _text(source[name])
        if 'question_id' in source and source['kind'] != 'interview':
            _fail()
    elif kind == 'workspace:employer_unit':
        _keys(value, 'id application_id research_run_id source_id text text_sha256 '
                     'media_type content_encoding identity_spans')
        for name in ('id', 'application_id', 'research_run_id', 'source_id'):
            _identifier(value[name])
        _text(value['text'], maximum=1000000)
        _hash_text(value['text'], value['text_sha256'])
        _text(value['media_type'], maximum=200, minimum=1)
        _text(value['content_encoding'], maximum=100, minimum=1)
        if type(value['identity_spans']) is not list:
            _fail()
        for item in value['identity_spans']:
            _contract(item, SourceSpan)
    else:
        _fail()


def _validate_record(kind, serialized, *, row_id, owner, budget=None):
    """Check record envelope and mirrored identities only, not cross-row authority."""
    if kind not in RECORD_KINDS:
        _fail()
    budget = budget if budget is not None else LogicalBudget()
    value = _decode_json(serialized, budget)
    if type(value) is not dict:
        _fail()
    models = _model_shapes().get(kind)
    if models:
        matches = []
        for model in models:
            try:
                matches.append(_contract(value, model))
            except EvidenceError:
                pass
        if len(matches) != 1:
            _fail()
    else:
        _raw_record(kind, value)
    _identifier(owner)
    if kind in ('chunk:facts', 'chunk:documents'):
        _digest(row_id)
    else:
        _identifier(row_id)
    if 'id' in value and value['id'] != row_id:
        _fail()
    if 'profile_id' in value and value['profile_id'] != owner:
        _fail()
    if kind == 'workspace:interview_archive' and value['answer']['profile_id'] != owner:
        _fail()
    return value


def _manifest_shape(value):
    names = ('generation_id profile_id corpus revision model_fingerprint chunker_version '
             'chunk_ids_sha256 chunk_count dense_collection sparse_relpath state')
    employer = value.get('corpus') == 'employer' if type(value) is dict else False
    _keys(value, names + (' application_id research_run_id' if employer else ''))
    for name in ('generation_id', 'profile_id'):
        _identifier(value[name])
    for name in ('model_fingerprint', 'chunk_ids_sha256'):
        _digest(value[name])
    _integer(value['revision'], maximum=_MAX_REVISION)
    _integer(value['chunk_count'], maximum=100000)
    _text(value['chunker_version'], minimum=1, maximum=200)
    if (type(value['dense_collection']) is not str
            or not re.fullmatch(r'evidence_[a-z0-9_]+', value['dense_collection'])
            or type(value['sparse_relpath']) is not str
            or not re.fullmatch(r'[a-zA-Z0-9_-]+', value['sparse_relpath'])
            or value['state'] not in ('active', 'retired')):
        _fail()
    if employer:
        _identifier(value['application_id'])
        _identifier(value['research_run_id'])
    elif value['corpus'] not in ('facts', 'documents'):
        _fail()


def _profile_shape(value):
    _keys(value, 'id name sectors revision')
    _identifier(value['id'])
    if not _text(value['name'], minimum=1, maximum=200).strip():
        _fail()
    sectors = value['sectors']
    if (type(sectors) is not list or not 1 <= len(sectors) <= 2
            or any(type(item) is not str or item not in ('tech', 'finance') for item in sectors)
            or len(set(sectors)) != len(sectors)):
        _fail()
    _integer(value['revision'], maximum=_MAX_REVISION)


def _ticket_shape(value):
    _keys(value, 'id profile_id generation_ids source_ids fact_ids state')
    for name in ('id', 'profile_id'):
        _identifier(value[name])
    for name in ('generation_ids', 'source_ids', 'fact_ids'):
        ids = value[name]
        if type(ids) is not list or len(ids) > 100000:
            _fail()
        for item in ids:
            _identifier(item)
        if len(set(ids)) != len(ids):
            _fail()
    if value['state'] not in ('pending', 'complete'):
        _fail()


def _fact_metadata_shape(value):
    required = {'proposal_id', 'source_spans', 'supersedes_fact_id', 'confirmation_event_id'}
    if type(value) is not dict or set(value) not in (required, required | {'legacy_source'}):
        _fail()
    _identifier(value['proposal_id'])
    _identifier(value['confirmation_event_id'])
    if value['supersedes_fact_id'] is not None:
        _identifier(value['supersedes_fact_id'])
    if type(value['source_spans']) is not list:
        _fail()
    from .domain.contracts import SourceSpan
    for span in value['source_spans']:
        _contract(span, SourceSpan)
    if 'legacy_source' in value and type(value['legacy_source']) is not dict:
        _fail()


def _primitive_row(table, row):
    columns = TABLE_COLUMNS[table]
    if type(row) is not tuple or len(row) != len(columns):
        _fail()
    value = dict(zip(columns, row))
    nullable = {
        ('generation_intents', 'captured'), ('research_intents', 'run_id'),
        ('job_keys', 'application_id'), ('job_keys', 'kind'),
        ('provider_attempts', 'response'), ('drafting_admission_heads', 'last_job_id'),
        *{('drafting_admission_bindings', name) for name in
          ('captured_revision', 'consumed_revision', 'request_sha256', 'envelope_sha256')},
    }
    numeric = {'metadata', 'facts', 'documents', 'consent', 'fence', 'device', 'inode',
               'research', 'output', 'dispatched', 'daily_tokens', 'daily_calls',
               'job_tokens', 'job_calls', 'revision', 'captured_revision', 'consumed_revision'}
    ids = {'id', 'owner', 'generation_id', 'job_id', 'application_id', 'run_id', 'intent_id',
           'new_job_id', 'prior_job_id', 'prior_attempt_id', 'attempt_id', 'last_job_id'}
    for name, scalar in value.items():
        if scalar is None:
            if (table, name) not in nullable:
                _fail()
            continue
        if name in numeric:
            _integer(scalar, maximum=_MAX_REVISION if name in {
                'metadata', 'facts', 'documents', 'consent', 'research', 'output',
                'revision', 'captured_revision', 'consumed_revision', 'fence'} else _MAX_INTEGER)
            if name == 'dispatched' and scalar not in (0, 1):
                _fail()
        elif name in ids:
            if (table == 'records' and name == 'id' and value['kind'] in ('chunk:facts', 'chunk:documents')
                    or table == 'generation_chunks' and name == 'id'):
                _digest(scalar)
            elif (table == 'tombstones' and name == 'id'
                  and value['kind'] in ('chunk:facts', 'chunk:documents')):
                _digest(scalar)
            else:
                _identifier(scalar)
        elif name.endswith('sha256'):
            _digest(scalar)
        else:
            _text(scalar)
    return value


def _validate_table_row(table, row_tuple, *, budget=None):
    """Strict local row shapes only. Cross-row/logical snapshot validation is incomplete."""
    if type(table) is not str or table not in TABLE_COLUMNS:
        _fail()
    budget = budget if budget is not None else LogicalBudget()
    budget.add('rows', 1, 100000)
    budget.add('reads', 1, 1000000)
    budget.row_nodes = 0
    if (type(row_tuple) is not tuple or len(row_tuple) != len(TABLE_COLUMNS[table])
            or any(type(item) not in (str, int, type(None)) for item in row_tuple)):
        _fail()
    encoded_bytes = 0
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    try:
        for piece in encoder.iterencode(row_tuple):
            encoded_bytes += len(piece.encode('utf-8'))
            if encoded_bytes > 16777216:
                _fail('RESTORE_LOGICAL_LIMIT')
    except (UnicodeError, ValueError):
        _fail()
    value = _primitive_row(table, row_tuple)
    decoded = {}
    for name in ('data', 'parameters', 'dependencies', 'response', 'captured'):
        if name in value and value[name] is not None:
            if table == 'records' and name == 'data':
                decoded[name] = _validate_record(value['kind'], value[name], row_id=value['id'],
                                                 owner=value['owner'], budget=budget)
            else:
                decoded[name] = _decode_json(value[name], budget)
    data = decoded.get('data')
    if table == 'profiles':
        _profile_shape(data)
        if data['id'] != value['id']:
            _fail()
    elif table == 'fact_metadata':
        _fact_metadata_shape(data)
    elif table == 'manifests':
        _manifest_shape(data)
        if (data['generation_id'], data['profile_id'], data['corpus']) != (
                value['id'], value['owner'], value['corpus']):
            _fail()
        if value['state'] not in ('active', 'retired'):
            _fail()
    elif table == 'generation_chunks':
        _chunk_shape(data, employer=data.get('corpus') == 'employer' if type(data) is dict else False)
        if (data['id'], data['profile_id']) != (value['id'], value['owner']):
            _fail()
    elif table in ('generation_intents', 'jobs', 'provider_attempts'):
        from .domain.contracts import DurableJob, GenerationIntent, ProviderAttempt
        model = {'generation_intents': GenerationIntent, 'jobs': DurableJob,
                 'provider_attempts': ProviderAttempt}[table]
        _contract(data, model)
        identifier = data['generation_id'] if table == 'generation_intents' else data['id']
        if (identifier, data['profile_id'], data['state']) != (
                value['id'], value['owner'], value['state']):
            _fail()
        if table == 'generation_intents' and data['corpus'] != value['corpus']:
            _fail()
        if table == 'jobs':
            if data['kind'] == 'cleanup':
                _fail()  # No current concrete producer/handler/parameter contract.
            if type(decoded['parameters']) is not dict or type(decoded['dependencies']) is not list:
                _fail()
        if table == 'provider_attempts':
            if data['job_id'] != value['job_id']:
                _fail()
            try:
                if datetime.strptime(value['day'], '%Y-%m-%d').strftime('%Y-%m-%d') != value['day']:
                    _fail()
            except ValueError:
                _fail()
    elif table == 'tickets':
        _ticket_shape(data)
        if data['id'] != value['id']:
            _fail()
    elif table == 'dense_mutations':
        if value['state'] not in ('not_attempted', 'inflight', 'acknowledged', 'indeterminate'):
            _fail()
        _integer(value['fence'], minimum=1, maximum=_MAX_REVISION)
    elif table == 'upload_intents':
        if value['state'] not in ('writing', 'committed', 'cleanup_pending', 'cleaned', 'not_owned'):
            _fail()
    elif table == 'research_intents':
        if value['state'] not in ('registered', 'writing', 'published', 'cleanup_pending', 'cleaned'):
            _fail()
    elif table == 'tombstones':
        if value['kind'] not in RECORD_KINDS | {'profile', 'employer_generation', 'drafting_job_forgotten'}:
            _fail()
    elif table == 'drafting_admission_bindings':
        if value['kind'] not in ('draft', 'assess') or value['origin'] not in ('admitted', 'legacy'):
            _fail()
        if value['origin'] == 'legacy':
            if any(value[name] is not None for name in
                   ('captured_revision', 'consumed_revision', 'envelope_sha256')):
                _fail()
        elif (value['captured_revision'] is None or value['consumed_revision'] is None
              or value['request_sha256'] is None or value['envelope_sha256'] is None
              or value['consumed_revision'] != value['captured_revision'] + 1):
            _fail()
    # Stage/parameter/provider/retry/accounting variant closure and all cross-row
    # associations are intentionally pending increments 2–4, not admitted graphs.
    return value
