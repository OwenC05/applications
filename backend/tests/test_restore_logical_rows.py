"""Writer-positive strict rows; shape success is explicitly not graph authority."""
import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from restore_logical_fixtures import (
    add_personal_evidence,
    add_workspace_rows,
    archive_bundle,
    create_installation,
    read_rows,
    sha256,
    table_columns,
)

from copilot.contracts import EvidenceError

EXPECTED_KINDS = frozenset({
    'source', 'unit', 'fact', 'chunk:facts', 'chunk:documents',
    'workspace:consent', 'workspace:typed_value', 'workspace:interview_answer',
    'workspace:interview_progress', 'workspace:proposal', 'workspace:application',
    'workspace:employer_source', 'workspace:research_run', 'workspace:draft',
    'workspace:draft_review', 'workspace:packet_batch', 'workspace:packet_intent',
    'workspace:packet_dependency', 'workspace:draft_intent', 'workspace:draft_dependency',
    'workspace:preferences', 'workspace:history', 'workspace:interview_archive',
    'workspace:legacy_archive', 'workspace:legacy_relation', 'workspace:employer_unit',
})


def validator():
    specification = importlib.util.find_spec('copilot.restore_validation')
    assert specification is not None, 'Pure inactive logical row validator is not implemented'
    from copilot import restore_validation

    return restore_validation


@pytest.fixture
def rich(tmp_path):
    bundle = create_installation(tmp_path / 'synthetic')
    add_personal_evidence(bundle)
    add_workspace_rows(bundle)
    return bundle


@pytest.mark.parametrize('sector', ['tech', 'finance'])
def test_fixture_writers_produce_registered_originals_without_runtime_or_sdk(tmp_path, sector):
    bundle = create_installation(tmp_path / sector, sector=sector)
    evidence = add_personal_evidence(bundle)
    add_workspace_rows(bundle)
    rows = read_rows(bundle)
    assert len(rows) == 26
    assert evidence.source.id in bundle.originals
    assert sha256(bundle.originals[evidence.source.id]) == evidence.source.blob_sha256
    assert evidence.unit.text == evidence.unit.text.replace('\r\n', '\n')
    assert evidence.fact.origin.unit_id == evidence.unit.id
    assert any(row[0] == evidence.fact.id for row in rows['fact_metadata'])
    assert any(row[0] == evidence.source.id and row[2] == 'committed' for row in rows['upload_intents'])
    archive = archive_bundle(bundle, tmp_path / 'synthetic.tar')
    from copilot.restore import _scan_archive

    assert _scan_archive(archive, sha256(archive.read_bytes())).model_lock_sha256 == sha256(
        (Path(__file__).parents[1] / 'models.lock.json').read_bytes())
    assert not {'copilot.api', 'copilot.worker', 'copilot.retrieval.service', 'chromadb'} & sys.modules.keys()


def test_closed_registries_cover_exact26tables_and26persisted_kinds():
    module = validator()
    assert dict(module.TABLE_COLUMNS) == table_columns()
    assert frozenset(module.RECORD_KINDS) == EXPECTED_KINDS


def test_actual_writer_rows_pass_shape_boundary_but_create_no_graph_authority(rich):
    module = validator()
    count = 0
    for table, rows in read_rows(rich).items():
        for row in rows:
            result = module._validate_table_row(table, row)
            assert isinstance(result, dict)
            count += 1
    assert count > 20
    assert not any(hasattr(module, name) for name in ('ValidatedCapture', 'prepare_destination', 'restore'))


@pytest.mark.parametrize('mutation', ['sql-id', 'sql-owner', 'unknown-kind', 'extra-json-field',
                                       'missing-json-field', 'bool-integer', 'bad-digest'])
def test_record_single_field_corruption_denied(rich, mutation):
    module = validator()
    row = list(next(row for row in read_rows(rich)['records'] if row[2] == 'unit'))
    data = json.loads(row[3])
    if mutation == 'sql-id':
        row[0] = str(uuid4())
    elif mutation == 'sql-owner':
        row[1] = str(uuid4())
    elif mutation == 'unknown-kind':
        row[2] = 'workspace:future_submit'
    elif mutation == 'extra-json-field':
        data['unexpected'] = 'ignored input must not pass'
    elif mutation == 'missing-json-field':
        del data['ordinal']
    elif mutation == 'bool-integer':
        data['ordinal'] = True
    elif mutation == 'bad-digest':
        data['text_sha256'] = 'Z' * 64
    row[3] = json.dumps(data)
    with pytest.raises(EvidenceError):
        module._validate_table_row('records', tuple(row))


@pytest.mark.parametrize('payload', ['{"id":1,"id":1}', '{"x":NaN}', '{"x":Infinity}',
                                     '{"x":-Infinity}', '{"x":"\\ud800"}', '{"x":"\\udfff"}',
                                     '{} trailing', 'true', 'null', '1'])
def test_strict_json_denies_duplicates_nonfinite_surrogates_and_scalar_roots(payload):
    module = validator()
    with pytest.raises(EvidenceError):
        module._decode_json(payload, module.LogicalBudget())


@pytest.mark.parametrize('table', ['unknown', 'sqlite_schema', 'restore_installation', 'records;DROP TABLE records'])
def test_row_validator_never_accepts_unknown_table_or_sql_identifier(table):
    with pytest.raises(EvidenceError):
        validator()._validate_table_row(table, ())


@pytest.mark.parametrize('payload', ['{}', '[]', '{"unicode":"😀\\r\\n"}'])
def test_strict_json_preserves_valid_unicode_without_normalization(payload):
    result = validator()._decode_json(payload, validator().LogicalBudget())
    assert result == json.loads(payload)


def test_actual_legacy_writer_preserves_archive_and_prior_answer_without_invented_tombstone(rich):
    import base64

    from restore_logical_fixtures import add_legacy_rows

    owner, original = add_legacy_rows(rich)
    rows = read_rows(rich)
    legacy = [row for row in rows['records'] if row[1] == owner]
    archive = json.loads(next(row[3] for row in legacy if row[2] == 'workspace:legacy_archive'))
    assert base64.b64decode(archive['original_bytes_base64'], validate=True) == original
    assert archive['source_sha256'] == sha256(original)
    assert archive['current_approval'] is False
    assert archive['legacy_cloud_consent_not_carried_forward'] is True
    prior = json.loads(next(row[3] for row in legacy if row[2] == 'workspace:interview_archive'))
    assert not any(row[0] == prior['answer']['id'] for row in rows['tombstones'])
    relation = json.loads(next(row[3] for row in legacy if row[2] == 'workspace:legacy_relation'))
    assert relation['legacy_source']['question_id'] == 'common-direction'
    assert not any(row[2] == 'workspace:consent' for row in legacy)
    module = validator()
    for row in legacy:
        assert isinstance(module._validate_table_row('records', row), dict)


@pytest.mark.parametrize('payload', ['{"x":1e999}', '{"x":-1e999}', '{"\\ud800":"value"}',
                                     '[' * 65 + '0' + ']' * 65,
                                     '[' + ','.join('0' for _ in range(100000)) + ']'])
def test_strict_json_finite_depth_and_node_bounds(payload):
    module = validator()
    with pytest.raises(EvidenceError):
        module._decode_json(payload, module.LogicalBudget())


@pytest.mark.parametrize('column', ['metadata', 'facts', 'documents', 'consent'])
@pytest.mark.parametrize('value', [True, -1, 2**63, 1.0, '1', None])
def test_actual_profile_revision_row_never_coerces_invalid_counter(rich, column, value):
    module = validator()
    columns = table_columns()['profile_revisions']
    row = list(next(row for row in read_rows(rich)['profile_revisions'] if row[0] == rich.owner))
    row[columns.index(column)] = value
    with pytest.raises(EvidenceError):
        module._validate_table_row('profile_revisions', tuple(row))


@pytest.mark.parametrize('column,value', [('device', True), ('inode', -1), ('device', 2**63),
                                         ('inode', '1'), ('device', 1.0)])
def test_actual_old_receipt_row_retains_strict_signed_integer_identity(rich, column, value):
    module = validator()
    row = list(read_rows(rich)['upload_receipts'][0])
    row[table_columns()['upload_receipts'].index(column)] = value
    with pytest.raises(EvidenceError):
        module._validate_table_row('upload_receipts', tuple(row))


def test_every_actual_core_and_legacy_record_rejects_unknown_envelope_field(rich):
    from restore_logical_fixtures import add_legacy_rows

    add_legacy_rows(rich)
    module = validator()
    covered = set()
    for row in read_rows(rich)['records']:
        data = json.loads(row[3])
        data['unknown_field_must_not_be_ignored'] = True
        altered = (*row[:3], json.dumps(data))
        with pytest.raises(EvidenceError):
            module._validate_table_row('records', altered)
        covered.add(row[2])
    assert len(covered) == 14


@pytest.mark.parametrize('boundary', ['json-field', 'serialized-table-row'])
def test_oversized_supported_serialized_input_reports_limit_not_corruption(rich, boundary):
    module = validator()
    oversized = '{"text":"' + 'x' * 16777216 + '"}'
    with pytest.raises(EvidenceError) as failure:
        if boundary == 'json-field':
            module._decode_json(oversized, module.LogicalBudget())
        else:
            row = next(row for row in read_rows(rich)['records'] if row[2] == 'unit')
            module._validate_table_row('records', (*row[:3], oversized))
    assert failure.value.code == 'RESTORE_LOGICAL_LIMIT'


def test_exact_serialized_sql_row_escaping_counts_toward_row_byte_limit(rich):
    module = validator()
    serialized = '{"text":"' + '\\\\' * 4_300_000 + '"}'
    assert len(serialized.encode('utf-8')) < 16777216
    original = next(row for row in read_rows(rich)['records'] if row[2] == 'unit')
    row = (*original[:3], serialized)
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    assert sum(len(part.encode('utf-8')) for part in encoder.iterencode(row)) > 16777216
    # Envelope may also be invalid, but the resource cap must be checked first,
    # without accepting/truncating content or approximating escaping overhead.
    with pytest.raises(EvidenceError) as failure:
        module._validate_table_row('records', row)
    assert failure.value.code == 'RESTORE_LOGICAL_LIMIT'


def test_decoder_rejects_node_excess_before_allocating_bulk_child_frontier():
    module = validator()
    previous_trace = sys.gettrace()
    code = module._decode_json.__code__
    observed = []
    payload = '[' + ','.join('0' for _ in range(120000)) + ']'

    def trace(frame, event, argument):
        if frame.f_code is code and event == 'line':
            for name in ('pending', 'frames'):
                working = frame.f_locals.get(name)
                if isinstance(working, list):
                    observed.append(len(working))
                    if len(working) > 100000:
                        raise AssertionError('Decoder materialized an over-budget sibling frontier')
        return trace

    try:
        sys.settrace(trace)
        with pytest.raises(EvidenceError) as failure:
            module._decode_json(payload, module.LogicalBudget())
        assert failure.value.code == 'RESTORE_LOGICAL_LIMIT'
    finally:
        sys.settrace(previous_trace)
    assert observed and max(observed) <= 100000


def test_json_node_allowance_is_shared_by_all_fields_in_one_actual_job_row(rich):
    module = validator()
    job = rich.jobs.enqueue(rich.owner, 'index', {'corpus': 'facts'}, 'node-budget-row')
    row = next(row for row in read_rows(rich)['jobs'] if row[0] == job.id)
    assert isinstance(module._validate_table_row('jobs', row), dict)
    parameters = json.dumps({'corpus': 'facts', 'padding': [0] * 60000})
    dependencies = json.dumps([0] * 60000)
    assert len(parameters) < 16777216 and len(dependencies) < 16777216
    altered = (*row[:4], parameters, dependencies)
    with pytest.raises(EvidenceError) as failure:
        module._validate_table_row('jobs', altered)
    assert failure.value.code == 'RESTORE_LOGICAL_LIMIT'


@pytest.mark.parametrize('mutation', ['consent-revision', 'nested-question-signed64'])
def test_actual_model_writer_rows_reject_out_of_supported_integer_range(rich, mutation):
    module = validator()
    kind = 'workspace:consent' if mutation == 'consent-revision' else 'workspace:application'
    row = next(row for row in read_rows(rich)['records'] if row[2] == kind)
    assert isinstance(module._validate_table_row('records', row), dict)
    data = json.loads(row[3])
    if mutation == 'consent-revision':
        data['revision'] = 9_007_199_254_740_992
    else:
        data['questions'][0]['max_words'] = 2**63
    with pytest.raises(EvidenceError) as failure:
        module._validate_table_row('records', (*row[:3], json.dumps(data)))
    assert failure.value.code == 'RESTORE_LOGICAL_INVALID'


@pytest.mark.parametrize('kind', ['profile', 'source', 'unit', 'fact'])
def test_actual_uuid_tombstones_cannot_use_chunk_digest_identifier(rich, kind):
    module = validator()
    rich.store.delete_profile(rich.owner)
    row = next(row for row in read_rows(rich)['tombstones'] if row[2] == kind)
    assert isinstance(module._validate_table_row('tombstones', row), dict)
    with pytest.raises(EvidenceError) as failure:
        module._validate_table_row('tombstones', ('a' * 64, *row[1:]))
    assert failure.value.code == 'RESTORE_LOGICAL_INVALID'


def test_row_node_allowance_resets_between_rows_without_resetting_snapshot_work(rich):
    module = validator()
    budget = module.LogicalBudget()
    module._decode_json('[' + ','.join('0' for _ in range(99998)) + ']', budget)
    assert budget.row_nodes == 99999
    previous_bytes = budget.json_bytes
    row = next(row for row in read_rows(rich)['records'] if row[2] == 'unit')
    for _ in range(2):
        assert isinstance(module._validate_table_row('records', row, budget=budget), dict)
    assert 0 < budget.row_nodes < 99999
    assert budget.rows == 2 and budget.reads == 2
    assert budget.json_bytes > previous_bytes


def _publish_personal_for_tombstone(bundle, corpus):
    """Self-contained actual fenced writer; no optional graph-test dependency."""
    from copilot.contracts import Chunk, Manifest
    from copilot.generations import Generations
    from copilot.store import digest

    jobs, store = bundle.jobs, bundle.store
    job = jobs.enqueue(bundle.owner, 'index', {'corpus': corpus}, 'index-' + corpus)
    worker = str(uuid4())
    job = jobs.claim(worker, job_id=job.id)
    generations = Generations(store, jobs)
    intent, captured = generations.register(job.id, worker, job.fence, str(uuid4()),
        'a' * 64, 'canonical-offset-v1-220-30')

    def chunks(value):
        return [Chunk(digest(value.id + ':' + value.text), bundle.owner, corpus,
            value.id if corpus == 'facts' else value.source_id,
            None if corpus == 'facts' else value.id, 0, len(value.text), value.text, digest(value.text))]

    revision = getattr(intent.revisions, corpus)
    snapshot = store.chunk_capture(bundle.owner, corpus, revision, captured, chunks)
    assert snapshot.chunks
    generations.stage(intent.generation_id, worker, job.fence, snapshot)
    generations.begin_dense(intent.generation_id, worker, job.fence)
    # Synthetic definite adapter receipt only; not actual dense retrieval proof.
    generations.dense_result(intent.generation_id, job.fence, True)
    manifest = Manifest(intent.generation_id, bundle.owner, corpus, revision,
        intent.model_fingerprint, intent.chunker_version,
        digest('\n'.join(chunk.id for chunk in snapshot.chunks)), len(snapshot.chunks),
        intent.dense_collection, intent.sparse_relpath)
    generations.publish(intent.generation_id, worker, job.fence, manifest)


@pytest.mark.parametrize('corpus', ['facts', 'documents'])
def test_actual_chunk_digest_tombstones_remain_legal_after_index_and_delete(rich, corpus):
    module = validator()
    _publish_personal_for_tombstone(rich, corpus)
    rich.store.delete_profile(rich.owner)
    rows = [row for row in read_rows(rich)['tombstones'] if row[2] == 'chunk:' + corpus]
    assert rows
    for row in rows:
        assert len(row[0]) == 64
        assert isinstance(module._validate_table_row('tombstones', row), dict)


def test_actual_check_invalid_database_row_is_denied_independently_of_native_ro_integrity(rich, tmp_path):
    import sqlite3

    module = validator()
    columns = table_columns()['drafting_admission_heads']
    original = next(row for row in read_rows(rich)['drafting_admission_heads'] if row[0] == rich.owner)
    assert isinstance(module._validate_table_row('drafting_admission_heads', original), dict)
    connection = sqlite3.connect(rich.database)
    try:
        # ONLY our synthetic writer-backed DB: deliberately violate one known
        # CHECK without changing catalog; no writable incoming validation.
        connection.execute('PRAGMA ignore_check_constraints=ON')
        cursor = connection.execute('UPDATE drafting_admission_heads SET revision=-1 WHERE owner=? AND application_id=?',
                                    (rich.owner, rich.application_id))
        assert cursor.rowcount == 1
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(rich.database.as_uri() + '?mode=ro', uri=True)
    try:
        native = connection.execute('PRAGMA integrity_check').fetchall()
        altered = connection.execute('SELECT ' + ','.join(columns)
            + ' FROM drafting_admission_heads WHERE owner=? AND application_id=?',
            (rich.owner, rich.application_id)).fetchone()
    finally:
        connection.close()
    assert altered[columns.index('revision')] == -1
    assert native and all(type(row[0]) is str for row in native)
    (tmp_path / 'native-ro-integrity.json').write_text(json.dumps(native))
    # Native RO may return 'ok': that is not CHECK proof, nor a failed test.
    with pytest.raises(EvidenceError) as failure:
        module._validate_table_row('drafting_admission_heads', tuple(altered))
    assert failure.value.code == 'RESTORE_LOGICAL_INVALID'
