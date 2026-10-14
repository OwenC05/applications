"""Synthetic trusted catalog capture; executable unchanged in both target runtimes.

Run from an owned empty CWD with explicit repository PYTHONPATH. CLI creates its
own fresh /tmp installation; output must not exist. No service/SDK/model imports.
"""
import argparse
import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = 'copilot-physical-schema-v1'
SOURCE_PATHS = (
    'backend/copilot/store.py', 'backend/copilot/jobs.py',
    'backend/copilot/admission_state.py', 'backend/copilot/generations.py',
    'backend/copilot/restore.py',
)
CATALOG_SQL = 'SELECT type,name,tbl_name,rootpage,sql FROM main.sqlite_schema'


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def catalog_projection(rows):
    return {'version': SCHEMA_VERSION,
            'objects': sorted([[row[0], row[1], row[2], row[4]] for row in rows])}


def quoted_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def read_trusted_catalog(database):
    """Observe only a fresh TEST-created trusted schema, never an incoming DB."""
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        rows = sorted([list(row) for row in connection.execute(CATALOG_SQL)])
        tables = [row[1] for row in rows if row[0] == 'table']
        metadata = {}
        for table in tables:
            name = quoted_identifier(table)
            indexes = [list(row) for row in connection.execute(f'PRAGMA main.index_list({name})')]
            metadata[table] = {
                'table_xinfo': [list(row) for row in connection.execute(f'PRAGMA main.table_xinfo({name})')],
                'index_list': indexes,
                'foreign_key_list': [list(row) for row in connection.execute(f'PRAGMA main.foreign_key_list({name})')],
                'index_xinfo': {
                    index[1]: [list(row) for row in connection.execute(
                        f'PRAGMA main.index_xinfo({quoted_identifier(index[1])})')]
                    for index in indexes
                },
            }
        return {'objects': rows, 'metadata': metadata,
                'catalog_sha256': sha256(canonical_bytes(catalog_projection(rows))),
                'schema_version': SCHEMA_VERSION,
                'table_count': len(tables), 'object_count': len(rows)}
    finally:
        connection.close()


def create_trusted_database(root):
    """Fresh synthetic installation only, deliberately no caller existing root."""
    if root.exists():
        raise ValueError('Synthetic installation root must not exist')
    from copilot.jobs import Jobs
    from copilot.store import Store

    store = Store(root)
    Jobs(store)
    return store.db


def capture_baseline(root):
    database = create_trusted_database(root)
    first = read_trusted_catalog(database)
    from copilot.jobs import Jobs
    from copilot.store import Store

    Jobs(Store(root))
    second = read_trusted_catalog(database)
    if first != second:
        raise AssertionError('Trusted initialization changed its physical catalog')
    payload = database.read_bytes()
    if payload[18:20] != b'\x01\x01':
        raise AssertionError('Synthetic canonical database unexpectedly uses WAL format')
    repository = Path(__file__).resolve().parents[2]
    return {
        'format': 'copilot-trusted-catalog-test-baseline-v1',
        'runtime': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                    'sqlite_source_id': _source_id(database)},
        'source_sha256': {name: sha256((repository / name).read_bytes()) for name in SOURCE_PATHS},
        'catalog': first, 'reinitialized_catalog_identical': True,
        'database': {'sha256': sha256(payload), 'size': len(payload),
                     'page_size': int.from_bytes(payload[16:18], 'big') or 65536,
                     'header_read_version': payload[19], 'header_write_version': payload[18]},
        'loaded_forbidden_modules': sorted(name for name in sys.modules
            if name == 'chromadb' or name.startswith('chromadb.')
            or name in {'copilot.api', 'copilot.worker', 'copilot.retrieval.service'}),
    }


def _source_id(database):
    connection = sqlite3.connect(database)
    try:
        return connection.execute('SELECT sqlite_source_id()').fetchone()[0]
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description='Capture only a fresh synthetic Store+Jobs catalog')
    parser.add_argument('--capture', required=True, type=Path)
    args = parser.parse_args()
    # Own /tmp stage retained for audit, never inspect or remove caller data.
    stage = Path(tempfile.mkdtemp(prefix='copilot-r2a-baseline-', dir='/tmp'))
    os.chmod(stage, 0o700)
    baseline = capture_baseline(stage / 'installation')
    baseline['synthetic_root'] = str(stage)
    output = canonical_bytes(baseline) + b'\n'
    descriptor = os.open(args.capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(output)
    print(json.dumps({'capture_sha256': sha256(output), 'catalog_sha256': baseline['catalog']['catalog_sha256'],
                      'table_count': baseline['catalog']['table_count'],
                      'object_count': baseline['catalog']['object_count'], 'runtime': baseline['runtime'],
                      'loaded_forbidden_modules': baseline['loaded_forbidden_modules']}))


if __name__ == '__main__':
    main()


def archive_database(target, payload, *, source_schema_sha256, manifest_edit=None):
    """Independent stdlib USTAR writer for one opaque DB payload, no originals."""
    import io
    import tarfile

    database_sha = sha256(payload)
    manifest = {
        'format': 'copilot-backup', 'version': 1,
        'backup_id': '11111111-1111-4111-8111-111111111111',
        'source_schema_sha256': source_schema_sha256,
        'application_compatibility': 'application-copilot-evidence-0.2.0',
        'model_lock_sha256': 'a' * 64,
        'installation_id': '22222222-2222-4222-8222-222222222222',
        'entries': [{'path': 'evidence.sqlite3', 'type': 'file', 'size': len(payload),
                     'sha256': database_sha}],
        'originals': [],
        'snapshot': {'method': 'offline-stage-backup-v1', 'raw_capture_sha256': 'b' * 64,
                     'canonical_database_sha256': database_sha},
    }
    if manifest_edit:
        manifest_edit(manifest)
    encoded = canonical_bytes(manifest)
    with tarfile.open(target, 'w', format=tarfile.USTAR_FORMAT) as writer:
        for name, content in [('manifest.json', encoded), ('evidence.sqlite3', payload)]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            info.uid = info.gid = info.mtime = 0
            info.type = tarfile.REGTYPE
            writer.addfile(info, io.BytesIO(content))
    return target


def expected_catalog():
    return json.loads((Path(__file__).parent / 'fixtures' / 'restore-schema-v1.json').read_text())


def actual_child_case(tmp_path, database, *, fault=None, request_bytes=None):
    """Instrumented actual isolated four-FD child; no parent resource changes.

    Audit observes limits/loaded modules at first sqlite3 import. Fault injection
    is confined to explicitly owned FDs/request bytes, never production source.
    """
    import subprocess

    from copilot import restore

    stage = tmp_path / 'child-stage'
    stage.mkdir(mode=0o700)
    staged = stage / 'evidence.sqlite3'
    staged.write_bytes(database.read_bytes())
    staged.chmod(0o600)
    descriptors = []
    process = None
    audit_path = tmp_path / 'child-import-audit.json'
    try:
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(stage_fd)
        database_fd = os.open(staged, os.O_RDWR if fault == 'writable-database' else os.O_RDONLY)
        descriptors.append(database_fd)
        request_read, request_write = os.pipe()
        response_read, response_write = os.pipe()
        descriptors.extend((request_read, request_write, response_read, response_write))
        root_info, db_info = os.fstat(stage_fd), os.fstat(database_fd)
        request = {'version': 1, 'parent_pid': os.getpid(), 'child_pid': 0,
                   'stage_identity': {'device': root_info.st_dev, 'inode': root_info.st_ino,
                                      'uid': root_info.st_uid, 'mode': 0o700},
                   'database_identity': {'device': db_info.st_dev, 'inode': db_info.st_ino,
                       'uid': db_info.st_uid, 'mode': 0o600, 'nlink': 1, 'size': db_info.st_size,
                       'sha256': sha256(staged.read_bytes())},
                   'declared_schema_sha256': expected_catalog()['catalog_sha256']}
        selected_request_fd = request_read
        if fault == 'regular-request':
            selected_request_fd = os.open(tmp_path / 'request.json', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            descriptors.append(selected_request_fd)
        selected_response_fd = response_read if fault == 'read-only-response' else response_write
        code = ('import sys,os,json,resource\n'
                f'audit_path={str(audit_path)!r}\n'
                'def audit(event,args):\n'
                '    if event == "import" and args[0] == "sqlite3":\n'
                '        record={"address_space":resource.getrlimit(resource.RLIMIT_AS),'
                '"core":resource.getrlimit(resource.RLIMIT_CORE),'
                '"forbidden":[n for n in sys.modules if n=="chromadb" or n.startswith("chromadb.")]}\n'
                '        with open(audit_path,"x") as output: json.dump(record,output)\n'
                'sys.addaudithook(audit)\n')
        if fault == 'regular-request':
            code += (f'request={request!r}\nrequest["child_pid"]=os.getpid()\n'
                     f'os.write({selected_request_fd},json.dumps(request).encode())\n'
                     f'os.lseek({selected_request_fd},0,0)\n')
        backend = str(Path(restore.__file__).resolve().parents[1])
        code += (f'sys.path.insert(0,{backend!r})\nfrom copilot.restore import _catalog_child\n'
                 f'_catalog_child({stage_fd},{database_fd},{selected_request_fd},{selected_response_fd})\n')
        passed = (stage_fd, database_fd, selected_request_fd, selected_response_fd)
        assert len(set(passed)) == 4
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', code],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=passed)
        request['child_pid'] = process.pid
        if fault == 'wrong-parent-pid':
            request['parent_pid'] += 1
        elif fault == 'wrong-child-pid':
            request['child_pid'] += 1
        elif fault == 'wrong-database-hash':
            request['database_identity']['sha256'] = '0' * 64
        if fault != 'regular-request':
            os.write(request_write, canonical_bytes(request) if request_bytes is None else request_bytes)
        for fd in (request_write, response_write):
            os.close(fd)
            descriptors.remove(fd)
        process.wait(timeout=5)
        response = os.read(response_read, 4097)
        assert len(response) <= 4096
        return {'response': json.loads(response) if response else None,
                'exit': process.returncode, 'pid': process.pid,
                'audit': json.loads(audit_path.read_text()) if audit_path.exists() else None}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for fd in descriptors:
            os.close(fd)
