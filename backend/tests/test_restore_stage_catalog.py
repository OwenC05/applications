"""Actual trusted initialization and inactive physical-observer acceptance."""
import dataclasses
import os
import sys
from pathlib import Path

import pytest
from restore_stage_catalog_fixtures import (
    archive_database,
    capture_baseline,
    create_trusted_database,
    expected_catalog,
    read_trusted_catalog,
    sha256,
)

from copilot import restore


def observer():
    function = getattr(restore, '_observe_archive_catalog', None)
    assert callable(function), 'Inactive owned-stage catalog observer is not implemented'
    return function


def test_current_trusted_store_jobs_catalog_matches_independent_frozen_fixture(tmp_path):
    baseline = capture_baseline(tmp_path / 'trusted')
    assert baseline['catalog'] == expected_catalog()
    assert baseline['reinitialized_catalog_identical'] is True
    assert baseline['loaded_forbidden_modules'] == []
    assert baseline['catalog']['table_count'] == 26
    assert baseline['catalog']['object_count'] == 57
    indexes = [row for row in baseline['catalog']['objects'] if row[0] == 'index']
    assert len([row for row in indexes if row[4] is None]) == 29
    job_keys = next(row[4] for row in baseline['catalog']['objects'] if row[1] == 'job_keys')
    assert 'application_id TEXT, kind TEXT, PRIMARY KEY(owner,key)' in job_keys


def test_actual_trusted_database_passes_real_owned_stage_catalog_observation(tmp_path):
    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    expected = read_trusted_catalog(database)
    payload = database.read_bytes()
    selected = archive_database(tmp_path / 'selected.tar', payload,
                                source_schema_sha256=expected['catalog_sha256'])
    before = selected.read_bytes()
    result = function(selected, sha256(before))
    assert result.archive_sha256 == sha256(before)
    assert result.canonical_database_sha256 == sha256(payload)
    assert result.catalog_sha256 == expected['catalog_sha256']
    assert result.source_schema_sha256 == expected['catalog_sha256']
    assert result.schema_version == expected['schema_version']
    assert result.table_count == 26
    assert result.object_count == 57
    assert selected.read_bytes() == before
    assert database.read_bytes() == payload
    assert set(p.name for p in tmp_path.iterdir()) == {'trusted', 'selected.tar'}
    assert not {'copilot.api', 'copilot.worker', 'chromadb'} & sys.modules.keys()


def test_observer_is_not_a_restore_or_path_bearing_capability(tmp_path):
    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    result = function(selected, sha256(selected.read_bytes()))
    assert dataclasses.is_dataclass(result)
    clone = dataclasses.replace(result)
    assert clone == result
    assert all(type(getattr(result, field.name)) in {str, int}
               for field in dataclasses.fields(result))
    assert not any(hasattr(restore, name) for name in (
        'ValidatedCapture', 'inspect_archive', 'prepare_destination'))


def test_observer_preserves_parent_cwd_and_environment(tmp_path):
    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    cwd, environment = Path.cwd(), dict(os.environ)
    function(selected, sha256(selected.read_bytes()))
    assert Path.cwd() == cwd
    assert dict(os.environ) == environment


def test_observer_does_not_accept_caller_fabricated_inventory_or_stage(tmp_path):
    function = observer()
    with pytest.raises(TypeError):
        function(object(), '0' * 64, stage=tmp_path, inventory={})


def test_catalog_success_deliberately_does_not_validate_application_rows(tmp_path):
    import sqlite3

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database)
    try:
        # Usable physical schema, deliberately invalid application envelope/identity.
        connection.execute('INSERT INTO profiles(id,data) VALUES(?,?)',
                           ('not-a-canonical-owner', 'not JSON and not a valid profile'))
        connection.commit()
    finally:
        connection.close()
    selected = archive_database(tmp_path / 'selected.tar', database.read_bytes(),
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    result = function(selected, sha256(selected.read_bytes()))
    assert result.catalog_sha256 == expected_catalog()['catalog_sha256']
    assert not hasattr(result, 'validated_capture')


class ConnectionObservation:
    """Test-only transparent spy around an owned real SQLite connection."""
    def __init__(self, connection):
        self.connection = connection
        self.events = []
        self.progress = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def setconfig(self, name, value):
        self.events.append(('setconfig', name, value))
        return self.connection.setconfig(name, value)

    def getconfig(self, name):
        self.events.append(('getconfig', name))
        return self.connection.getconfig(name)

    def setlimit(self, name, value):
        self.events.append(('setlimit', name, value))
        return self.connection.setlimit(name, value)

    def getlimit(self, name):
        self.events.append(('getlimit', name))
        return self.connection.getlimit(name)

    def set_authorizer(self, callback):
        self.events.append(('authorizer',))
        return self.connection.set_authorizer(callback)

    def set_progress_handler(self, callback, interval):
        self.events.append(('progress', interval))
        self.progress = callback
        return self.connection.set_progress_handler(callback, interval)

    def execute(self, sql):
        self.events.append(('sql', sql))
        return self.connection.execute(sql)


def test_real_connection_installs_all_controls_before_exact_catalog_and_metadata(tmp_path):
    import sqlite3
    import time

    assert callable(getattr(restore, '_install_sqlite_controls', None))
    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, autocommit=True,
                                 timeout=0, cached_statements=0)
    spy = ConnectionObservation(connection)
    policy = restore._CatalogPolicy(database.stat().st_size // 4096, time.monotonic() + 5)
    try:
        restore._install_sqlite_controls(spy, policy)
        assert not any(event[0] == 'sql' for event in spy.events)
        assert sum(event[0] == 'getconfig' for event in spy.events) == 5
        assert sum(event[0] == 'getlimit' for event in spy.events) == 12
        assert spy.events[-2:] == [('authorizer',), ('progress', 1000)]
        result = restore._read_supported_catalog(spy, policy)
        statements = [event[1] for event in spy.events if event[0] == 'sql']
        assert statements[0] == 'SELECT type,name,tbl_name,rootpage,sql FROM main.sqlite_schema'
        assert all(sql.startswith('PRAGMA main.') for sql in statements[1:])
        assert not any('integrity_check' in sql or 'quick_check' in sql for sql in statements)
        assert result == expected_catalog()['catalog_sha256']
        assert policy.phase == 'metadata'
    finally:
        connection.close()


@pytest.mark.parametrize('sql', [
    'SELECT data FROM profiles', 'SELECT lower(\'x\')', 'SELECT load_extension(\'owned-missing\')',
    "ATTACH DATABASE ':memory:' AS other", 'CREATE TABLE extra(id TEXT)',
    'CREATE TEMP TABLE extra(id TEXT)', 'DELETE FROM profiles', 'PRAGMA main.table_xinfo(profiles)',
    'PRAGMA main.writable_schema=ON',
])
def test_real_catalog_authorizer_denies_application_reads_functions_and_mutations(tmp_path, sql):
    import sqlite3
    import time

    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, autocommit=True,
                                 timeout=0, cached_statements=0)
    policy = restore._CatalogPolicy(database.stat().st_size // 4096, time.monotonic() + 5)
    try:
        restore._install_sqlite_controls(connection, policy)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(sql).fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize('fault', ['wrong-config-readback', 'wrong-limit-readback', 'missing-config'])
def test_unsupported_controls_deny_before_any_sql(tmp_path, fault):
    import sqlite3
    import time

    from copilot.contracts import EvidenceError

    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, autocommit=True)

    class Broken(ConnectionObservation):
        def getconfig(self, name):
            return not super().getconfig(name) if fault == 'wrong-config-readback' else super().getconfig(name)

        def getlimit(self, name):
            return 2**31 - 1 if fault == 'wrong-limit-readback' else super().getlimit(name)

        def setconfig(self, name, value):
            if fault == 'missing-config':
                raise AttributeError('synthetic missing runtime control')
            return super().setconfig(name, value)

    spy = Broken(connection)
    try:
        with pytest.raises(EvidenceError) as failure:
            restore._install_sqlite_controls(spy, restore._CatalogPolicy(100, time.monotonic() + 5))
        assert failure.value.code == 'RESTORE_SQLITE_UNSUPPORTED'
        assert not any(event[0] == 'sql' for event in spy.events)
    finally:
        connection.close()


def test_expired_sqlite_work_deadline_denies_before_first_statement(tmp_path):
    import sqlite3
    import time

    from copilot.contracts import EvidenceError

    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, autocommit=True)
    spy = ConnectionObservation(connection)
    policy = restore._CatalogPolicy(database.stat().st_size // 4096, time.monotonic() - 1)
    try:
        restore._install_sqlite_controls(spy, policy)
        with pytest.raises(EvidenceError) as failure:
            restore._read_supported_catalog(spy, policy)
        assert failure.value.code == 'RESTORE_SQLITE_LIMIT'
        assert not any(event[0] == 'sql' for event in spy.events)
    finally:
        connection.close()


@pytest.mark.parametrize('page_size', [512, 65536])
def test_supported_nondefault_sqlite_page_sizes_use_actual_header_page_bounds(tmp_path, page_size):
    import sqlite3

    function = observer()
    database = create_trusted_database(tmp_path / 'trusted')
    connection = sqlite3.connect(database)
    try:
        connection.execute(f'PRAGMA page_size={page_size}')
        connection.execute('VACUUM')
    finally:
        connection.close()
    payload = database.read_bytes()
    encoded = int.from_bytes(payload[16:18], 'big')
    assert (65536 if encoded == 1 else encoded) == page_size
    selected = archive_database(tmp_path / 'selected.tar', payload,
                                source_schema_sha256=expected_catalog()['catalog_sha256'])
    result = function(selected, sha256(selected.read_bytes()))
    assert result.catalog_sha256 == expected_catalog()['catalog_sha256']
