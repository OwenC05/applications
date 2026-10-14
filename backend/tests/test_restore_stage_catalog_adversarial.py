"""Unrestricted synthetic negative controls, then real isolated observer denial."""
import sqlite3

import pytest
from restore_stage_catalog_fixtures import (
    archive_database,
    create_trusted_database,
    expected_catalog,
    read_trusted_catalog,
    sha256,
)

from copilot import restore
from copilot.contracts import EvidenceError


def observer():
    function = getattr(restore, '_observe_archive_catalog', None)
    assert callable(function), 'Inactive owned-stage catalog observer is not implemented'
    return function


def denied(function, selected, expected_codes=('RESTORE_SQLITE_INVALID',)):
    payload = selected.read_bytes()
    with pytest.raises(EvidenceError) as failure:
        function(selected, sha256(payload))
    assert failure.value.code in expected_codes
    assert selected.read_bytes() == payload
    assert selected.name not in str(failure.value)
    assert 'CREATE ' not in str(failure.value)


@pytest.mark.parametrize('declaration', ['hostile-fingerprint', 'supported-fingerprint'])
@pytest.mark.parametrize('statements', [
    ['CREATE TABLE extra(id TEXT)'],
    ['CREATE INDEX extra ON profiles(data)'],
    ['CREATE VIEW extra AS SELECT data FROM profiles'],
    ['CREATE TRIGGER extra AFTER INSERT ON profiles BEGIN DELETE FROM profiles; END'],
    ['DROP INDEX record_owner'],
    ['DROP TABLE budget_limits'],
    ['DROP TABLE profiles', 'CREATE TABLE profiles(id TEXT PRIMARY KEY, data TEXT)'],
    ['DROP TABLE profiles', 'CREATE TABLE profiles(id TEXT PRIMARY KEY, data BLOB NOT NULL)'],
    ['DROP TABLE profiles', 'CREATE TABLE profiles(data TEXT NOT NULL, id TEXT PRIMARY KEY)'],
    ['DROP TABLE profiles', 'CREATE TABLE profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT \'x\')'],
    ['DROP TABLE profiles', 'CREATE TABLE profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL CHECK(length(data)<10))'],
    ['DROP TABLE profiles', 'CREATE TABLE profiles(id TEXT PRIMARY KEY COLLATE NOCASE, data TEXT NOT NULL)'],
    ['DROP TABLE profiles', 'CREATE TABLE profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL, generated TEXT GENERATED ALWAYS AS (data) VIRTUAL)'],
    ['DROP TABLE profiles', 'CREATE VIEW profiles AS SELECT id,data FROM records'],
    ['CREATE UNIQUE INDEX extra ON profiles(data)'],
    ['CREATE INDEX extra ON profiles(data) WHERE id IS NOT NULL'],
    ['CREATE INDEX extra ON profiles(lower(data))'],
    ['ANALYZE'],
])
def test_real_usable_hostile_schema_rejected_despite_manifest_matching_its_fingerprint(tmp_path, statements, declaration):
    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
        # Actual unrestricted negative control: schema is loaded and queryable.
        assert connection.execute('SELECT type,name,sql FROM sqlite_schema').fetchall()
    finally:
        connection.close()
    changed = read_trusted_catalog(database)
    assert changed['catalog_sha256'] != expected_catalog()['catalog_sha256']
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=(changed['catalog_sha256'] if declaration == 'hostile-fingerprint'
                                                      else expected_catalog()['catalog_sha256']))
    denied(observer(), selected)


def test_matching_supported_schema_with_wrong_declared_fingerprint_rejected(tmp_path):
    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256='0' * 64)
    denied(function, selected)


@pytest.mark.parametrize('kind', ['magic', 'truncated', 'write-wal', 'read-wal', 'page-size',
                                  'schema-format', 'encoding', 'partial-page'])
def test_canonical_header_policy_rejects_unsupported_or_wal_dependent_member(tmp_path, kind):
    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    payload = bytearray(database.read_bytes())
    if kind == 'magic':
        payload[:16] = b'X' * 16
    elif kind == 'truncated':
        payload = payload[:99]
    elif kind == 'write-wal':
        payload[18] = 2
    elif kind == 'read-wal':
        payload[19] = 2
    elif kind == 'page-size':
        payload[16:18] = b'\x00\x03'
    elif kind == 'schema-format':
        payload[44:48] = (9).to_bytes(4, 'big')
    elif kind == 'encoding':
        payload[56:60] = (2).to_bytes(4, 'big')
    elif kind == 'partial-page':
        payload += b'X'
    selected = archive_database(tmp_path / 'selected.tar', bytes(payload),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    denied(function, selected)


@pytest.mark.parametrize('declaration', ['hostile-fingerprint', 'supported-fingerprint'])
def test_actual_known_name_virtual_table_is_usable_control_but_never_supported_catalog(tmp_path, declaration):
    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database)
    try:
        connection.execute('DROP TABLE profiles')
        connection.execute('CREATE VIRTUAL TABLE profiles USING fts5(id,data)')
        connection.execute('INSERT INTO profiles(id,data) VALUES(?,?)', ('control', 'synthetic canary'))
        assert connection.execute("SELECT id FROM profiles WHERE profiles MATCH 'canary'").fetchall() == [('control',)]
        connection.commit()
    finally:
        connection.close()
    changed = read_trusted_catalog(database)
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=(changed['catalog_sha256'] if declaration == 'hostile-fingerprint'
                                                      else expected_catalog()['catalog_sha256']))
    denied(observer(), selected)


@pytest.mark.parametrize('kind', ['many-objects', 'oversized-sql', 'equivalent-ddl'])
def test_closed_catalog_denies_resource_excess_and_equivalent_unapproved_ddl(tmp_path, kind):
    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database)
    try:
        if kind == 'many-objects':
            for number in range(130):
                connection.execute(f'CREATE TABLE extra_{number}(id TEXT)')
        elif kind == 'oversized-sql':
            connection.execute('CREATE TABLE extra(id TEXT' + ' ' * 70000 + ')')
        elif kind == 'equivalent-ddl':
            connection.execute('DROP INDEX record_owner')
            connection.execute('CREATE INDEX record_owner ON records(owner, kind)')
        connection.commit()
        assert connection.execute('SELECT count(*) FROM sqlite_schema').fetchone()[0] >= 57
    finally:
        connection.close()
    changed = read_trusted_catalog(database)
    assert changed['catalog_sha256'] != expected_catalog()['catalog_sha256']
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    denied(observer(), selected, ('RESTORE_SQLITE_INVALID', 'RESTORE_SQLITE_LIMIT'))


def test_partial_store_only_schema_is_not_silently_migrated_or_accepted(tmp_path):
    from copilot.store import Store

    database = Store(tmp_path / 'partial').db
    partial = read_trusted_catalog(database)
    assert partial['table_count'] < 26
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=partial['catalog_sha256'])
    denied(observer(), selected)


def test_non_main_thread_denied_before_any_archive_or_stage_resources(tmp_path, monkeypatch):
    import threading

    function = observer()
    acquired = []

    def forbidden(*args, **kwargs):
        acquired.append(True)
        raise AssertionError('Resource acquisition preceded thread rejection')

    monkeypatch.setattr(restore, '_selected_archive', forbidden)
    caught = []

    def run():
        try:
            function(tmp_path / 'does-not-exist.tar', '0' * 64)
        except BaseException as error:
            caught.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert acquired == []
    assert len(caught) == 1
    assert isinstance(caught[0], EvidenceError)


@pytest.mark.parametrize('fault', ['short-write', 'zero-write', 'disk-full', 'fsync'])
def test_stage_copy_handles_short_writes_and_failures_without_source_mutation(tmp_path, monkeypatch, fault):
    import errno
    import os
    import stat
    import tempfile

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    payload = database.read_bytes()
    selected = archive_database(tmp_path / 'selected.tar', payload,
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    created = []
    original_mkdtemp, original_write, original_fsync = tempfile.mkdtemp, os.write, os.fsync

    def create(*args, **kwargs):
        root = original_mkdtemp(*args, **kwargs)
        created.append(root)
        return root

    def write(fd, value):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            if fault == 'short-write':
                return original_write(fd, value[:max(1, len(value) // 2)])
            if fault == 'zero-write':
                return 0
            if fault == 'disk-full':
                raise OSError(errno.ENOSPC, 'synthetic private disk full detail')
        return original_write(fd, value)

    def fsync(fd):
        if fault == 'fsync' and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'synthetic private fsync detail')
        return original_fsync(fd)

    monkeypatch.setattr(tempfile, 'mkdtemp', create)
    monkeypatch.setattr(os, 'write', write)
    monkeypatch.setattr(os, 'fsync', fsync)
    if fault == 'short-write':
        assert function(selected, sha256(selected.read_bytes())).catalog_sha256 == expected_catalog()['catalog_sha256']
    else:
        denied(function, selected, ('RESTORE_STAGE_IO', 'RESTORE_STAGE_CLEANUP'))
    assert database.read_bytes() == payload
    assert created
    from pathlib import Path

    assert all(not Path(root).exists() for root in created)


@pytest.mark.parametrize('fault', ['wrong-selection-hash', 'invalid-final-tail'])
def test_entire_archive_must_validate_before_sqlite_child_starts(tmp_path, monkeypatch, fault):
    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    raw = selected.read_bytes()
    expected = sha256(raw)
    if fault == 'wrong-selection-hash':
        expected = '0' * 64
    else:
        selected.write_bytes(raw[:-1] + b'X')
        expected = sha256(selected.read_bytes())
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('SQLite child started before complete archive validation')

    monkeypatch.setattr(restore, '_exchange_catalog_child', forbidden)
    with pytest.raises(EvidenceError) as failure:
        function(selected, expected)
    assert failure.value.code in {'RESTORE_ARCHIVE_HASH', 'RESTORE_ARCHIVE_INVALID'}
    assert calls == []


@pytest.mark.parametrize('signal_name,when', [('SIGINT', 'before-spawn'), ('SIGTERM', 'before-spawn'),
                                             ('SIGINT', 'after-spawn'), ('SIGTERM', 'after-spawn')])
def test_real_child_is_reaped_and_previous_handlers_restored_on_spawn_boundary_signal(tmp_path, monkeypatch, signal_name, when):
    import os
    import signal
    import subprocess

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    children = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        if when == 'before-spawn':
            os.kill(os.getpid(), getattr(signal, signal_name))
        process = original(*args, **kwargs)
        children.append(process)
        if when == 'after-spawn':
            os.kill(os.getpid(), getattr(signal, signal_name))
        return process

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    try:
        with pytest.raises(EvidenceError):
            function(selected, sha256(selected.read_bytes()))
    finally:
        assert {number: signal.getsignal(number) for number in previous} == previous
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
                pytest.fail('Observer failed to reap its exact child after terminal interruption')
    assert children


@pytest.mark.parametrize('payload', [b'', b'[]', b'null', b'true', b'\xff',
                                     b'{"version":1,"version":1}', b'{"x":NaN}',
                                     b'{"x":Infinity}', b'{} trailing', b'{}{}', b' ' * 4097])
def test_child_json_transport_parser_rejects_duplicate_trailing_nonfinite_and_oversize(payload):
    helper = getattr(restore, '_protocol_object', None)
    assert callable(helper), 'Bounded child protocol parser is not implemented'
    with pytest.raises(EvidenceError) as failure:
        helper(payload)
    assert failure.value.code in {'RESTORE_SQLITE_INVALID', 'RESTORE_SQLITE_LIMIT'}


def successful_child_response():
    return {'version': 1, 'status': 'ok', 'database_sha256': 'd' * 64,
            'catalog_sha256': expected_catalog()['catalog_sha256'],
            'declared_schema_sha256': expected_catalog()['catalog_sha256'],
            'schema_version': expected_catalog()['schema_version'], 'table_count': 26,
            'object_count': 57}


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('status', 'OK'), ('database_sha256', 'e' * 64),
    ('catalog_sha256', '0' * 64), ('declared_schema_sha256', '0' * 64),
    ('schema_version', 'other'), ('table_count', True), ('table_count', 25),
    ('object_count', 56), ('object_count', 57.0), ('unknown', 'private detail'),
])
def test_child_response_exact_scalars_scope_and_closed_shape(field, value):
    helper = getattr(restore, '_check_response', None)
    assert callable(helper), 'Child response validator is not implemented'
    response = successful_child_response()
    response[field] = value
    request = {'database_identity': {'sha256': 'd' * 64},
               'declared_schema_sha256': expected_catalog()['catalog_sha256']}
    with pytest.raises(EvidenceError) as failure:
        helper(response, request)
    assert failure.value.code == 'RESTORE_SQLITE_INVALID'


@pytest.mark.parametrize('response', [
    {'version': True, 'status': 'error', 'code': 'RESTORE_SQLITE_INVALID'},
    {'version': 1, 'status': 'error', 'code': 'arbitrary private error'},
    {'version': 1, 'status': 'error', 'code': []},
    {'version': 1, 'status': 'error', 'code': 'RESTORE_SQLITE_INVALID', 'detail': 'private'},
])
def test_child_error_transport_never_admits_arbitrary_messages(response):
    helper = getattr(restore, '_check_response', None)
    assert callable(helper), 'Child response validator is not implemented'
    with pytest.raises(EvidenceError) as failure:
        helper(response, {})
    assert failure.value.code == 'RESTORE_SQLITE_INVALID'
    assert 'private' not in str(failure.value)


def test_valid_response_eof_wait_polls_for_terminal_interruption_instead_of_blocking_deadline(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original = subprocess.Popen
    normal_wait_timeouts, children = [], []

    def spawn(args, **kwargs):
        command = list(args)
        command[-1] += ';import time;time.sleep(60)'
        child = original(command, **kwargs)
        children.append(child)
        original_wait = child.wait

        def wait(timeout=None):
            if not normal_wait_timeouts:
                normal_wait_timeouts.append(timeout)
                os.kill(os.getpid(), signal.SIGTERM)
                raise subprocess.TimeoutExpired(command, timeout)
            return original_wait(timeout=timeout)

        child.wait = wait
        return child

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    try:
        with pytest.raises(EvidenceError):
            function(selected, sha256(selected.read_bytes()))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
                pytest.fail('Exact owned child was not reaped after EOF/signal')
    assert len(normal_wait_timeouts) == 1
    assert 0 < normal_wait_timeouts[0] <= 0.1


def test_unsupported_platform_denies_before_archive_stage_or_child(tmp_path, monkeypatch):
    import sys

    function = observer()
    acquired = []

    def forbidden(*args, **kwargs):
        acquired.append(True)
        raise AssertionError('Unsupported platform acquired an archive resource')

    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(restore, '_selected_archive', forbidden)
    with pytest.raises(EvidenceError) as failure:
        function(tmp_path / 'missing.tar', '0' * 64)
    assert failure.value.code == 'RESTORE_SQLITE_UNSUPPORTED'
    assert acquired == []


def test_terminal_signal_during_handler_restoration_cannot_return_success(tmp_path, monkeypatch):
    import os
    import signal

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    original = signal.signal
    delivered = []

    def install(number, handler):
        result = original(number, handler)
        if number == signal.SIGINT and handler == previous[number] and not delivered:
            delivered.append(True)
            # SIGTERM's temporary observer handler is still installed here.
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(signal, 'signal', install)
    with pytest.raises(EvidenceError):
        function(selected, sha256(selected.read_bytes()))
    assert delivered == [True]
    assert {number: signal.getsignal(number) for number in previous} == previous


def test_resource_limit_oserror_is_unsupported_not_generic_stage_io(tmp_path, monkeypatch):
    import subprocess

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original = subprocess.Popen

    def spawn(args, **kwargs):
        command = list(args)
        prefix = ('import resource\n'
                  'def denied_limit(*args):\n'
                  '    raise OSError("synthetic unsupported resource control")\n'
                  'resource.setrlimit=denied_limit\n')
        command[-1] = prefix + command[-1]
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    denied(function, selected, ('RESTORE_SQLITE_UNSUPPORTED',))


def test_instrumented_actual_child_limits_precede_sqlite_import_and_catalog_success(tmp_path):
    from restore_stage_catalog_fixtures import actual_child_case

    database = create_trusted_database(tmp_path / 'trusted')
    result = actual_child_case(tmp_path, database)
    assert result['exit'] == 0
    assert result['response']['status'] == 'ok'
    assert result['audit'] == {'address_space': [512 * 1024**2, 512 * 1024**2],
                               'core': [0, 0], 'forbidden': []}


@pytest.mark.parametrize('fault', ['writable-database', 'regular-request', 'read-only-response',
                                   'wrong-parent-pid', 'wrong-child-pid', 'wrong-database-hash'])
def test_actual_child_rejects_descriptor_direction_type_pid_and_hash_before_sqlite_import(tmp_path, fault):
    from restore_stage_catalog_fixtures import actual_child_case

    database = create_trusted_database(tmp_path / 'trusted')
    result = actual_child_case(tmp_path, database, fault=fault)
    assert result['audit'] is None, 'SQLite was imported before actual FD/request denial'
    assert result['response'] is None or result['response']['status'] == 'error'


@pytest.mark.parametrize('payload', [b'{}{}', b'{"version":1,"version":1}', b'X' * 4097])
def test_actual_child_protocol_requires_strict_bounded_json_and_eof(tmp_path, payload):
    from restore_stage_catalog_fixtures import actual_child_case

    database = create_trusted_database(tmp_path / 'trusted')
    result = actual_child_case(tmp_path, database, request_bytes=payload)
    assert result['audit'] is None
    assert result['response']['status'] == 'error'


@pytest.mark.parametrize('fault', ['nonzero-exit', 'missing-eof', 'oversize', 'trailing-json',
                                   'duplicate-key', 'unknown-field', 'bool-count'])
def test_parent_rejects_hostile_real_process_transport_and_always_reaps(tmp_path, monkeypatch, fault):
    import signal
    import subprocess
    import time

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original = subprocess.Popen
    children = []
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

    def spawn(args, **kwargs):
        _, _, request_fd, response_fd = kwargs['pass_fds']
        code = ('import os,json,sys,time\n'
                'data=b""\n'
                'while True:\n'
                f'    part=os.read({request_fd},4096)\n'
                '    if not part: break\n'
                '    data+=part\n'
                'request=json.loads(data)\n'
                'response={"version":1,"status":"ok",'
                '"database_sha256":request["database_identity"]["sha256"],'
                f'"catalog_sha256":{expected_catalog()["catalog_sha256"]!r},'
                '"declared_schema_sha256":request["declared_schema_sha256"],'
                f'"schema_version":{expected_catalog()["schema_version"]!r},'
                '"table_count":26,"object_count":57}\n')
        if fault == 'unknown-field':
            code += 'response["unknown"]="private"\n'
        elif fault == 'bool-count':
            code += 'response["table_count"]=True\n'
        code += 'encoded=json.dumps(response).encode()\n'
        if fault == 'oversize':
            code += 'encoded=b"X"*4097\n'
        elif fault == 'trailing-json':
            code += 'encoded+=b"{}"\n'
        elif fault == 'duplicate-key':
            code += 'encoded=encoded.replace(b"{",b\'{"version":1,\',1)\n'
        code += f'os.write({response_fd},encoded)\n'
        if fault == 'missing-eof':
            code += 'time.sleep(60)\n'
        else:
            code += f'os.close({response_fd})\n'
        if fault == 'nonzero-exit':
            code += 'sys.exit(7)\n'
        command = list(args)
        command[-1] = code
        process = original(command, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    started = time.monotonic()
    try:
        denied(function, selected, ('RESTORE_SQLITE_INVALID', 'RESTORE_SQLITE_LIMIT'))
    finally:
        assert {number: signal.getsignal(number) for number in previous} == previous
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
                pytest.fail('Parent did not reap exact hostile transport child')
    assert children
    if fault == 'missing-eof':
        assert time.monotonic() - started < 13


@pytest.mark.parametrize('fault', ['unknown-member', 'replace-database', 'replace-root',
                                   'sidecar', 'unproved-reap', 'cleanup-unlink'])
def test_post_child_stage_changes_deny_success_and_never_sweep_unknown_files(tmp_path, monkeypatch, fault):
    import os

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    payload = database.read_bytes()
    selected = archive_database(tmp_path / 'selected.tar', payload,
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original = restore._exchange_catalog_child
    stages = []
    original_unlink = os.unlink

    def exchange(stage, database_fd, request, interruption):
        result = original(stage, database_fd, request, interruption)
        stages.append(stage.root)
        if fault == 'unknown-member':
            (stage.root / 'unknown-canary').write_bytes(b'owned unknown bytes must survive')
        elif fault == 'replace-database':
            replacement = stage.root / 'replacement'
            replacement.write_bytes(payload)
            replacement.chmod(0o600)
            os.replace(replacement, stage.root / 'evidence.sqlite3')
        elif fault == 'replace-root':
            stage.root.rename(stage.root.with_name(stage.root.name + '-original'))
            stage.root.mkdir(mode=0o700)
        elif fault == 'sidecar':
            sidecar = stage.root / 'evidence.sqlite3-journal'
            sidecar.write_bytes(b'owned synthetic sidecar')
            sidecar.chmod(0o600)
        elif fault == 'unproved-reap':
            # The real child was reaped; inject only the parent's uncertainty flag.
            stage.reaped = False
        return result

    def unlink(path, *args, **kwargs):
        if fault == 'cleanup-unlink' and path == 'evidence.sqlite3' and 'dir_fd' in kwargs:
            raise OSError('owned synthetic cleanup failure')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(restore, '_exchange_catalog_child', exchange)
    monkeypatch.setattr(os, 'unlink', unlink)
    denied(function, selected, ('RESTORE_STAGE_CLEANUP', 'RESTORE_SQLITE_INVALID'))
    assert len(stages) == 1
    root = stages[0]
    if fault == 'sidecar':
        assert not root.exists()
    else:
        assert root.exists()
        if fault == 'unknown-member':
            assert (root / 'unknown-canary').read_bytes() == b'owned unknown bytes must survive'
            assert (root / 'evidence.sqlite3').exists()
        if fault == 'replace-database':
            assert (root / 'evidence.sqlite3').read_bytes() == payload
        # Retain all uncertain synthetic roots; no recursive test cleanup either.
        (tmp_path / 'retained-stage.txt').write_text(str(root))
    assert database.read_bytes() == payload


def test_selected_archive_replaced_during_real_sqlite_work_is_rejected(tmp_path, monkeypatch):
    import os

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original = restore._exchange_catalog_child
    archive_bytes = selected.read_bytes()

    def exchange(*args):
        result = original(*args)
        replacement = tmp_path / 'replacement.tar'
        replacement.write_bytes(archive_bytes)
        os.replace(replacement, selected)
        return result

    monkeypatch.setattr(restore, '_exchange_catalog_child', exchange)
    denied(function, selected, ('RESTORE_ARCHIVE_CHANGED',))


def test_missing_selected_archive_preserves_r1_archive_io_category(tmp_path):
    with pytest.raises(EvidenceError) as failure:
        observer()(tmp_path / 'missing.tar', '0' * 64)
    assert failure.value.code == 'RESTORE_ARCHIVE_IO'


@pytest.mark.parametrize('phase', ['stream-read', 'final-source-rehash'])
def test_selected_source_io_failure_preserves_archive_not_stage_error(tmp_path, monkeypatch, phase):
    import os

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    identity = selected.stat()
    original_read, original_pread = os.read, os.pread
    original_exchange = restore._exchange_catalog_child
    enabled = [phase == 'stream-read']

    def is_selected(fd):
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)

    def read(fd, size):
        if phase == 'stream-read' and enabled[0] and is_selected(fd):
            raise OSError('synthetic private source stream detail')
        return original_read(fd, size)

    def pread(fd, size, offset):
        if phase == 'final-source-rehash' and enabled[0] and is_selected(fd):
            raise OSError('synthetic private source pread detail')
        return original_pread(fd, size, offset)

    def exchange(*args):
        result = original_exchange(*args)
        enabled[0] = True
        return result

    monkeypatch.setattr(os, 'read', read)
    monkeypatch.setattr(os, 'pread', pread)
    monkeypatch.setattr(restore, '_exchange_catalog_child', exchange)
    denied(function, selected, ('RESTORE_ARCHIVE_IO',))


def test_repeated_terminal_signals_do_not_reset_term_kill_or_reap_budgets(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    import time

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original = subprocess.Popen
    waits, children = [], []

    def spawn(args, **kwargs):
        command = list(args)
        command[-1] = ('import signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                        + command[-1] + ';import time;time.sleep(60)')
        child = original(command, **kwargs)
        children.append(child)
        original_wait = child.wait

        def wait(timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                os.kill(os.getpid(), signal.SIGTERM)
                raise subprocess.TimeoutExpired(command, timeout)
            if len(waits) == 2:
                os.kill(os.getpid(), signal.SIGINT)
                os.kill(os.getpid(), signal.SIGTERM)
            return original_wait(timeout=timeout)

        child.wait = wait
        return child

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    started = time.monotonic()
    try:
        with pytest.raises(EvidenceError):
            function(selected, sha256(selected.read_bytes()))
    finally:
        assert {number: signal.getsignal(number) for number in previous} == previous
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
                pytest.fail('Observer did not kill/reap exact TERM-ignoring child')
    assert len(waits) == 3
    assert 0 < waits[0] <= 0.1
    assert waits[1:] == [1, 1]
    assert children[0].returncode == -signal.SIGKILL
    assert time.monotonic() - started < 3.5


def test_missing_request_eof_is_bounded_without_extra_child_descriptors(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    import tempfile
    import time
    from pathlib import Path

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original_pipe2, original_spawn = os.pipe2, subprocess.Popen
    original_mkdtemp = tempfile.mkdtemp
    pairs, held, children, roots = [], [], [], []

    def pipe2(flags):
        pair = original_pipe2(flags)
        pairs.append(pair)
        return pair

    def create(*args, **kwargs):
        root = original_mkdtemp(*args, **kwargs)
        roots.append(Path(root))
        return root

    def spawn(args, **kwargs):
        assert len(kwargs['pass_fds']) == 4
        assert len(pairs) == 2
        # This extra reference stays exclusively in the parent. close_fds plus
        # exact four pass_fds prevents the child inheriting this adversarial FD.
        duplicate = os.dup(pairs[0][1])
        assert duplicate not in kwargs['pass_fds']
        held.append(duplicate)
        child = original_spawn(args, **kwargs)
        children.append(child)
        return child

    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(os, 'pipe2', pipe2)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(tempfile, 'mkdtemp', create)
    started = time.monotonic()
    try:
        denied(function, selected, ('RESTORE_SQLITE_LIMIT',))
    finally:
        for descriptor in held:
            os.close(descriptor)
        assert {number: signal.getsignal(number) for number in previous} == previous
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
                pytest.fail('Missing request EOF stranded an exact owned child')
    assert len(children) == 1
    assert roots and all(not root.exists() for root in roots)
    assert time.monotonic() - started < 13


def test_owned_stage_creation_oserror_has_stage_not_archive_category(tmp_path, monkeypatch):
    import tempfile

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    original = tempfile.mkdtemp
    calls = []

    def create(*args, **kwargs):
        if kwargs.get('prefix') == 'copilot-restore-catalog-':
            calls.append(True)
            raise OSError('synthetic private owned-stage creation detail')
        return original(*args, **kwargs)

    monkeypatch.setattr(tempfile, 'mkdtemp', create)
    denied(function, selected, ('RESTORE_STAGE_IO',))
    assert calls == [True]


@pytest.mark.parametrize('boundary', ['zero-exit-wait', 'closed-response-validation'])
def test_real_child_completion_after_original_absolute_deadline_never_succeeds(tmp_path, monkeypatch, boundary):
    import os
    import signal
    import subprocess
    import tempfile
    import time
    from pathlib import Path

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    database_bytes = database.read_bytes()
    selected = archive_database(tmp_path / 'selected.tar', database_bytes,
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    archive_bytes = selected.read_bytes()
    original_clock, original_spawn = time.monotonic, subprocess.Popen
    original_response, original_create = restore._check_response, tempfile.mkdtemp
    offset = [0.0]
    children, roots, waits, teardowns, advanced = [], [], [], [], []
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    parent_cwd, parent_environment = Path.cwd(), dict(os.environ)

    def clock():
        return original_clock() + offset[0]

    def advance():
        if not advanced:
            advanced.append(boundary)
            offset[0] = 11.0  # Cross the ORIGINAL 10-second deadline; no new budget.

    def create(*args, **kwargs):
        root = original_create(*args, **kwargs)
        roots.append(Path(root))
        return root

    def spawn(args, **kwargs):
        # Unmodified trusted bootstrap, real child/core/closed response, four FDs.
        assert len(kwargs['pass_fds']) == 4
        child = original_spawn(args, **kwargs)
        children.append(child)
        original_wait, original_terminate, original_kill = child.wait, child.terminate, child.kill

        def wait(timeout=None):
            status = original_wait(timeout=timeout)
            waits.append((timeout, status))
            if boundary == 'zero-exit-wait' and status == 0:
                advance()  # Actual zero exit observed just before returning to parent.
            return status

        def terminate():
            teardowns.append('TERM')
            return original_terminate()

        def kill():
            teardowns.append('KILL')
            return original_kill()

        child.wait, child.terminate, child.kill = wait, terminate, kill
        return child

    def validate(response, request):
        result = original_response(response, request)
        if boundary == 'closed-response-validation':
            advance()  # Strict valid actual response passed; clock then crosses deadline.
        return result

    monkeypatch.setattr(time, 'monotonic', clock)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(tempfile, 'mkdtemp', create)
    monkeypatch.setattr(restore, '_check_response', validate)
    try:
        with pytest.raises(EvidenceError) as failure:
            function(selected, sha256(archive_bytes))
        assert failure.value.code == 'RESTORE_SQLITE_LIMIT'
    finally:
        assert {number: signal.getsignal(number) for number in previous} == previous
        assert Path.cwd() == parent_cwd
        assert dict(os.environ) == parent_environment
        assert selected.read_bytes() == archive_bytes
        assert database.read_bytes() == database_bytes
        assert roots and all(not root.exists() for root in roots)
        assert len(children) == 1 and children[0].poll() == 0
        assert teardowns == []  # Already reaped: no redundant TERM/KILL/reap budget.
    assert advanced == [boundary]
    assert waits and all(status == 0 for _, status in waits)


def test_actual_installed_progress_callback_interrupts_at_exact_twenty_thousand_boundary(tmp_path):
    import time

    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True,
                                 autocommit=True, timeout=0, cached_statements=0)
    captured = []

    class ConnectionDelegate:
        def __getattr__(self, name):
            return getattr(connection, name)

        def set_progress_handler(self, callback, interval):
            captured.append((callback, interval))
            return connection.set_progress_handler(callback, interval)

    policy = restore._CatalogPolicy(database.stat().st_size // 4096, time.monotonic() + 5)
    try:
        # Actual SQLite configuration/limit set+readbacks and callback install;
        # only capture the real installed callback, no replacement SQL response.
        restore._install_sqlite_controls(ConnectionDelegate(), policy)
        assert len(captured) == 1
        callback, interval = captured[0]
        assert interval == 1000
        policy.progress_calls = 19998
        assert callback() == 0
        assert policy.progress_calls == 19999
        assert callback() == 1
        assert policy.progress_calls == 20000
    finally:
        connection.close()
