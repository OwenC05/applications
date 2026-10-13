"""Fresh-interpreter, real-SDK privacy proofs using only owned synthetic fixtures."""
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _environment(home):
    return {
        'HOME': str(home),
        'PATH': f'{Path(sys.executable).parent}:/usr/bin:/bin',
        'ANONYMIZED_TELEMETRY': 'False',
        'HF_HUB_OFFLINE': '1',
        'TRANSFORMERS_OFFLINE': '1',
        'PYTHON_DOTENV_DISABLED': '1',
    }


def _audit_prelude(observation_fd, allowed_port=None):
    # Hooks must be installed inside exec: parent hooks do not survive exec.
    return f'''
import json, os, sys
_observation_fd = {observation_fd}
def _observe(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = os.path.abspath(os.fsdecode(args[0]))
        if os.path.basename(path) == '.env':
            os.write(_observation_fd, (json.dumps({{'event': 'open', 'pid': os.getpid(), 'path': path, 'mode': str(args[1])}}) + '\\n').encode())
    if event == 'socket.connect':
        address = args[1]
        if not (isinstance(address, tuple) and len(address) == 2
                and address == ('127.0.0.1', {allowed_port!r})):
            raise RuntimeError('unexpected network during SDK probe')
sys.addaudithook(_observe)
def witness(stage, **fields):
    os.write(_observation_fd, (json.dumps({{'event': stage, 'pid': os.getpid(), **fields}}) + '\\n').encode())
witness('pre-bootstrap')
'''


def _fresh_probe(cwd, artifact, source):
    home = artifact.parent / 'owned-empty-home'
    home.mkdir(exist_ok=True)
    with artifact.open('wb') as observations:
        process = subprocess.run(
            [sys.executable, '-I', '-c', _audit_prelude(observations.fileno())
             + f'\nsys.path.insert(0, {str(BACKEND)!r})\n' + source],
            cwd=cwd, env=_environment(home), pass_fds=(observations.fileno(),),
            capture_output=True, text=True, timeout=60, check=False,
        )
    records = [json.loads(line) for line in artifact.read_text().splitlines()]
    return process, records


@pytest.mark.parametrize('nested', [False, True])
def test_unmodified_sdk_positive_control_observes_owned_dotenv_despite_disable_flag(tmp_path, nested):
    parent = tmp_path / 'hostile'
    parent.mkdir()
    cwd = parent / 'nested' if nested else parent
    cwd.mkdir(exist_ok=True)
    canary = cwd / '.env'
    canary.write_text('CHROMA_SERVER_HOST=owned-sdk-canary.invalid\n')
    process, records = _fresh_probe(cwd, tmp_path / 'control.jsonl', '''
import chromadb
from chromadb.config import Settings
settings = Settings()
assert chromadb.__version__ == '1.5.9'
assert settings.chroma_server_host == 'owned-sdk-canary.invalid'
witness('actual-sdk-settings', version=chromadb.__version__)
''')
    assert process.returncode == 0, process.stderr
    assert any(row['event'] == 'actual-sdk-settings' for row in records)
    assert any(row['event'] == 'open' and row['path'] == str(canary) for row in records)


def test_default_embedding_denies_before_real_sdk_import_or_owned_dotenv_open(tmp_path):
    caller = tmp_path / 'hostile'
    caller.mkdir()
    (caller / '.env').write_text('CHROMA_SERVER_HOST=owned-sdk-canary.invalid\n')
    process, records = _fresh_probe(caller, tmp_path / 'embedding.jsonl', '''
from pathlib import Path
from copilot.api import make_service
from copilot.config import Settings
from copilot.contracts import EvidenceError
try:
    make_service(None, Settings(Path('/unused-synthetic-data'), Path('/unused-synthetic-models')))
except EvidenceError as exc:
    assert exc.code == 'INDEX_NOT_READY'
    assert 'chromadb' not in sys.modules
    witness('embedding-denied')
else:
    raise AssertionError('default embedding unexpectedly acquired SDK authority')
''')
    assert process.returncode == 0, process.stderr
    assert any(row['event'] == 'embedding-denied' for row in records)
    assert not [row for row in records if row['event'] == 'open']


@pytest.fixture(scope='module')
def owned_chroma_server(tmp_path_factory):
    import socket

    root = tmp_path_factory.mktemp('owned-chroma-server')
    cwd = root / 'empty-cwd'
    cwd.mkdir(mode=0o700)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    home = root / 'owned-empty-home'
    home.mkdir()
    environment = _environment(home) | {'RAYON_NUM_THREADS': '2'}
    with (root / 'server.log').open('wb') as log:
        process = subprocess.Popen(
            [str(Path(sys.executable).with_name('chroma')), 'run', '--host', '127.0.0.1',
             '--port', str(port), '--path', str(root / 'synthetic-store')],
            cwd=cwd, env=environment, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, (root / 'server.log').read_text()
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
                try:
                    connection.request('GET', '/api/v2/heartbeat')
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 200 and 'nanosecond heartbeat' in json.loads(body):
                        break
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
                assert time.monotonic() < deadline, 'owned Chroma did not become ready'
                time.sleep(0.05)
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            assert process.poll() is not None
    cwd.rmdir()
    with socket.socket() as check:
        check.settimeout(0.5)
        assert check.connect_ex(('127.0.0.1', port)) != 0
    (root / 'server-lifecycle.json').write_text(json.dumps({
        'pid': process.pid, 'port': port, 'returncode': process.returncode,
        'reaped': process.poll() is not None, 'empty_cwd_removed': not cwd.exists(),
        'loopback_listener_absent': True,
    }, indent=2))


@pytest.mark.parametrize('nested', [False, True])
def test_actual_launch_keeps_real_sdk_and_production_httpclient_clear_of_owned_dotenv(
    tmp_path, monkeypatch, owned_chroma_server, nested,
):
    parent = tmp_path / 'hostile'
    parent.mkdir()
    (parent / '.env').write_text('CHROMA_SERVER_HOST=owned-parent-canary.invalid\n')
    caller = parent / 'nested' if nested else parent
    caller.mkdir(exist_ok=True)
    if nested:
        (caller / '.env').write_text('CHROMA_SERVER_HOST=owned-child-canary.invalid\n')
    data = tmp_path / 'synthetic-data'
    models = tmp_path / 'synthetic-models'
    persistent = tmp_path / 'synthetic-persistent-sdk'
    home = tmp_path / 'owned-empty-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    artifact = tmp_path / 'actual-child.jsonl'
    parent_records = []
    observing = [True]

    def parent_observer(event, args):
        if observing[0] and event == 'open' and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if os.path.basename(path) == '.env':
                parent_records.append({'pid': os.getpid(), 'event': 'open', 'path': path})

    sys.addaudithook(parent_observer)
    # Prove the parent observer works on an owned positive-control open before GREEN.
    with (parent / '.env').open() as control:
        assert control.read().startswith('CHROMA_SERVER_HOST=owned-parent-canary')
    assert len(parent_records) == 1
    parent_positive_control = list(parent_records)
    parent_records.clear()
    from copilot import process_boundary

    original_popen = subprocess.Popen
    spawned = []
    with artifact.open('wb') as observations:
        def audited_spawn(args, **kwargs):
            assert args[0] == sys.executable and args[1:3] == ['-I', '-c']
            assert kwargs.get('env') is None
            assert kwargs['start_new_session'] is True
            original_fds = kwargs['pass_fds']
            assert len(original_fds) == 2
            prelude = _audit_prelude(observations.fileno(), owned_chroma_server)
            prelude += f"""
import atexit
sys.path.insert(0, {str(BACKEND)!r})
from copilot import __main__ as application_main
from copilot.process_boundary import require_sdk_boundary
def actual_probe(command, *, service_factory=None):
    require_sdk_boundary()
    witness('boundary-activated', cwd=os.getcwd())
    import chromadb
    witness('actual-sdk-import', version=chromadb.__version__)
    from chromadb.config import Settings as ChromaSettings
    later = ChromaSettings()
    assert later.chroma_server_host is None
    witness('actual-later-settings')
    client = chromadb.PersistentClient(path={str(persistent)!r})
    assert client.heartbeat() > 0
    witness('actual-default-persistent-client')
    from pathlib import Path
    from copilot.api import make_service
    from copilot.config import Settings
    from copilot.store import Store
    settings = Settings(Path({str(data)!r}), Path({str(models)!r}),
                        '127.0.0.1', {owned_chroma_server})
    service = make_service(Store(settings.data_dir), settings)
    assert service.dense.client.heartbeat() > 0
    assert service.dense.client.list_collections() == []
    witness('actual-production-httpclient', service_type=type(service).__name__)
    assert os.listdir('.') == []
    return 0
application_main._dispatch = actual_probe
atexit.register(lambda: witness('shutdown', cwd=os.getcwd()))
"""
            replacement = list(args)
            replacement[3] = prelude + '\n' + args[3]
            kwargs['pass_fds'] = (*original_fds, observations.fileno())
            process = original_popen(replacement, **kwargs)
            spawned.append((process, kwargs['cwd']))
            return process

        monkeypatch.setattr(process_boundary.subprocess, 'Popen', audited_spawn)
        monkeypatch.chdir(caller)
        try:
            result = process_boundary.launch({
                'schema_version': 1, 'kind': 'cleanup',
                'settings': {'data_dir': str(data), 'model_dir': str(models),
                             'chroma_host': '127.0.0.1', 'chroma_port': owned_chroma_server,
                             'port': 3001},
            })
        finally:
            observing[0] = False
    records = [json.loads(line) for line in artifact.read_text().splitlines()]
    (tmp_path / 'parent-observations.json').write_text(json.dumps({
        'positive_control': parent_positive_control, 'green_opens': parent_records,
        'observer_pid': os.getpid(),
    }, indent=2))
    assert result == 0
    assert len(spawned) == 1
    child, clean_root = spawned[0]
    assert child.poll() == 0
    assert not Path(clean_root).exists()
    (tmp_path / 'child-lifecycle.json').write_text(json.dumps({
        'pid': child.pid, 'returncode': child.returncode, 'reaped': True,
        'clean_root': str(clean_root), 'clean_root_removed': True,
        'parent_cwd_preserved': str(Path.cwd()) == str(caller),
    }, indent=2))
    assert Path.cwd() == caller
    assert not parent_records
    assert not [row for row in records if row['event'] == 'open']
    assert {row['pid'] for row in records} == {child.pid}
    assert [row['event'] for row in records] == [
        'pre-bootstrap', 'boundary-activated', 'actual-sdk-import', 'actual-later-settings',
        'actual-default-persistent-client', 'actual-production-httpclient', 'shutdown',
    ]
    assert next(row for row in records if row['event'] == 'actual-sdk-import')['version'] == '1.5.9'
    assert records[1]['cwd'] == records[-1]['cwd'] == str(clean_root)
