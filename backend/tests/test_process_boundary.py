"""Lifetime SDK startup regressions; every hostile file below is synthetic."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def isolated_probe(cwd, code):
    bootstrap = f'import sys; sys.path.insert(0, {str(BACKEND)!r});\n'
    return subprocess.run([sys.executable, '-I', '-c', bootstrap + code], cwd=cwd,
        env={'PATH': os.environ.get('PATH', ''), 'HF_HUB_OFFLINE': '1',
             'TRANSFORMERS_OFFLINE': '1', 'ANONYMIZED_TELEMETRY': 'False',
             'PYTHON_DOTENV_DISABLED': '1'}, capture_output=True, text=True, timeout=30)


def test_default_embedding_denies_before_actual_sdk_dotenv_open(tmp_path):
    canary = tmp_path / '.env'
    canary.write_text('CHROMA_SERVER_HOST=owned-canary.invalid\n')
    result = isolated_probe(tmp_path, '''
import json, pathlib, sys
opened = []
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        if pathlib.Path(args[0]).name == '.env':
            opened.append(str(args[0]))
sys.addaudithook(audit)
from copilot.api import make_service
from copilot.config import Settings
try:
    make_service(None, Settings(pathlib.Path('/tmp/unused-data'), pathlib.Path('/tmp/unused-models')))
except Exception as error:
    print(json.dumps({'code': getattr(error, 'code', None), 'opens': opened,
                      'imported': 'chromadb' in sys.modules}))
''')
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {'code': 'INDEX_NOT_READY', 'opens': [], 'imported': False}


def test_legacy_eval_help_does_not_import_sdk_or_open_dotenv(tmp_path):
    (tmp_path / '.env').write_text('CHROMA_SERVER_HOST=owned-canary.invalid\n')
    evaluation = BACKEND.parent / 'eval'
    result = isolated_probe(tmp_path, f'''
import pathlib, sys
sys.path.insert(0, {str(evaluation)!r})
opened = []
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)) and pathlib.Path(args[0]).name == '.env':
        opened.append(str(args[0]))
sys.addaudithook(audit)
import run
sys.argv = ['run.py', '--help']
try:
    run.main()
except SystemExit as error:
    assert error.code == 0
assert not opened, opened
assert 'chromadb' not in sys.modules
''')
    assert result.returncode == 0, result.stderr


def test_imports_are_pure_and_default_dispatch_has_no_storage_effect(tmp_path):
    result = isolated_probe(tmp_path, f'''
import pathlib, sys
import copilot, copilot.api
from copilot.__main__ import _dispatch
from copilot.config import Settings
from copilot.process_boundary import settings_command
root = pathlib.Path({str(tmp_path / 'never-created')!r})
try:
    _dispatch(settings_command('cleanup', Settings(root, root / 'models')))
except Exception as error:
    assert error.code == 'INDEX_NOT_READY'
else:
    raise AssertionError('unguarded dispatch')
assert not root.exists()
assert 'chromadb' not in sys.modules
''')
    assert result.returncode == 0, result.stderr


def launch_probe(tmp_path, injected, parent=''):
    """Test-only bootstrap interception; genuine transport and activation remain."""
    code = f'''
import json, os, pathlib, signal, subprocess
from copilot import process_boundary as boundary
from copilot.config import Settings
real_popen = subprocess.Popen
roots = []
def spawn(argv, **kwargs):
    assert argv[1:3] == ['-I', '-c']
    assert kwargs['env'] is None
    assert kwargs['start_new_session'] is True
    roots.append(str(kwargs['cwd']))
    argv[3] = argv[3].replace('from copilot.process_boundary import _child_entry;',
        {injected!r} + '\\nfrom copilot.process_boundary import _child_entry;')
    return real_popen(argv, **kwargs)
boundary.subprocess.Popen = spawn
command = boundary.settings_command('cleanup', Settings(pathlib.Path('data').resolve(), pathlib.Path('models').resolve()))
{parent}
code = boundary.launch(command)
print(json.dumps({{'exit': code, 'roots': roots, 'cwd': os.getcwd()}}))
'''
    return isolated_probe(tmp_path, code)


def test_actual_activation_preserves_paths_env_and_parent_cwd(tmp_path):
    observation = tmp_path / 'child.json'
    injected = f'''
import json, os, pathlib
import copilot.__main__ as main
from copilot.process_boundary import require_sdk_boundary
def dispatch(command):
    require_sdk_boundary()
    pathlib.Path({str(observation)!r}).write_text(json.dumps({{'cwd': os.getcwd(), 'command': command,
        'key': os.environ.get('SYNTHETIC_INHERITED'), 'entries': os.listdir('.')}}))
main._dispatch = dispatch
'''
    result = launch_probe(tmp_path, injected, "os.environ['SYNTHETIC_INHERITED'] = 'owned-value'")
    assert result.returncode == 0, result.stderr
    parent, child = json.loads(result.stdout), json.loads(observation.read_text())
    assert parent['exit'] == 0 and parent['cwd'] == str(tmp_path)
    assert child['cwd'] != str(tmp_path) and child['entries'] == []
    assert child['command']['settings']['data_dir'] == str(tmp_path / 'data')
    assert child['key'] == 'owned-value'
    assert all(not Path(root).exists() for root in parent['roots'])


def test_corruptions_deny_without_reading_dotenv(tmp_path):
    observation = tmp_path / 'corruption.json'
    injected = f'''
import json, os, pathlib, sys
import copilot.__main__ as main
from copilot import process_boundary as boundary
def dispatch(command):
    root = pathlib.Path.cwd()
    outcomes = []
    for kind in ('file', 'symlink', 'fifo', 'mode', 'cwd', 'pid'):
        old = boundary._STATE['pid']
        if kind == 'file': (root / '.env').write_text('owned')
        elif kind == 'symlink': (root / '.env').symlink_to({str(tmp_path / 'foreign')!r})
        elif kind == 'fifo': os.mkfifo(root / '.env')
        elif kind == 'mode': root.chmod(0o755)
        elif kind == 'cwd': os.chdir({str(tmp_path)!r})
        elif kind == 'pid': boundary._STATE['pid'] = -1
        try:
            boundary.require_sdk_boundary()
        except Exception as error:
            outcomes.append(error.code)
        else: raise AssertionError('corruption accepted: ' + kind)
        finally:
            os.chdir(root)
            root.chmod(0o700)
            boundary._STATE['pid'] = old
            if kind in ('file', 'symlink', 'fifo'): (root / '.env').unlink()
    child = os.fork()
    if child == 0:
        try: boundary.require_sdk_boundary()
        except Exception: os._exit(0)
        os._exit(1)
    assert os.waitpid(child, 0)[1] == 0
    boundary.require_sdk_boundary()
    pathlib.Path({str(observation)!r}).write_text(json.dumps(outcomes))
main._dispatch = dispatch
'''
    result = launch_probe(tmp_path, injected)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['exit'] == 0
    assert json.loads(observation.read_text()) == ['INDEX_NOT_READY'] * 6


def test_child_exit_mapping_and_signal_handlers_restored(tmp_path):
    injected = '''
import copilot.__main__ as main
def dispatch(command): raise SystemExit(17)
main._dispatch = dispatch
'''
    parent = "previous = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}"
    result = launch_probe(tmp_path, injected, parent)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['exit'] == 17
    assert all(not Path(root).exists() for root in observed['roots'])


def test_strict_command_schema_rejects_coercion_unknown_fields_and_relative_paths():
    import pytest

    from copilot.config import Settings
    from copilot.contracts import EvidenceError
    from copilot.process_boundary import settings_command, validate_command
    base = settings_command('worker', Settings(Path('/tmp/data'), Path('/tmp/models')), once=True)
    for mutate in (lambda c: c.update(extra=1), lambda c: c.update(schema_version=True),
                   lambda c: c.update(once=1),
                   lambda c: c['settings'].update(data_dir='relative'),
                   lambda c: c['settings'].update(port=True),
                   lambda c: c['settings'].update(chroma_port=65536)):
        candidate = json.loads(json.dumps(base))
        mutate(candidate)
        with pytest.raises(EvidenceError):
            validate_command(candidate)


def test_spawn_failure_is_safe_and_restores_handlers(tmp_path, monkeypatch, capsys):
    import signal

    from copilot import process_boundary as boundary
    from copilot.config import Settings
    roots, original = [], boundary._new_root
    def create(prefix):
        result = original(prefix)
        roots.append(result[0])
        return result
    def fail(*_args, **_kwargs):
        raise OSError('PRIVATE-SPAWN-CANARY')
    previous = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(boundary, '_new_root', create)
    monkeypatch.setattr(boundary.subprocess, 'Popen', fail)
    assert boundary.launch(boundary.settings_command('cleanup', Settings(tmp_path, tmp_path / 'models'))) == 1
    assert all(not root.exists() for root in roots)
    assert {n: signal.getsignal(n) for n in previous} == previous
    assert 'PRIVATE-' not in capsys.readouterr().err


def test_private_json_rejects_duplicates_and_oversize(tmp_path):
    import pytest

    from copilot.process_boundary import _read_envelope
    for payload in (b'{"x":1,"x":2}', b'{} trailing', b'{"x":NaN}', b'x' * 65537):
        read_fd, write_fd = os.pipe()
        # Avoid pipe-capacity deadlock: use a regular owned file descriptor.
        os.close(read_fd)
        os.close(write_fd)
        source = tmp_path / 'envelope'
        source.write_bytes(payload)
        with source.open('rb') as stream, pytest.raises(Exception):
            _read_envelope(stream.fileno())


def test_forced_signal_reaps_child_and_preserves_unfinished_synthetic_job(tmp_path):
    observation = tmp_path / 'ready.json'
    injected = f'''
import json, os, pathlib, signal, time
import copilot.__main__ as main
from copilot.store import Store
from copilot.jobs import Jobs
from copilot.process_boundary import require_sdk_boundary
def dispatch(command):
    require_sdk_boundary()
    store = Store(pathlib.Path(command['settings']['data_dir']))
    owner = store.create_profile('Synthetic forced interruption', ['tech']).id
    jobs = Jobs(store)
    job = jobs.enqueue(owner, 'index', {{'corpus': 'facts'}}, 'owned-forced')
    jobs.claim(str(__import__('uuid').uuid4()), job_id=job.id)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path({str(observation)!r}).write_text(json.dumps({{'pid': os.getpid(), 'owner': owner, 'job': job.id}}))
    while True: time.sleep(0.02)
main._dispatch = dispatch
'''
    parent = f'''
import threading, time
boundary._GRACE['cleanup'] = 0.1
previous = signal.getsignal(signal.SIGTERM)
def interrupt():
    deadline = time.monotonic() + 10
    while not pathlib.Path({str(observation)!r}).exists() and time.monotonic() < deadline: time.sleep(0.02)
    os.kill(os.getpid(), signal.SIGTERM)
    os.kill(os.getpid(), signal.SIGTERM)
thread = threading.Thread(target=interrupt, daemon=True)
thread.start()
'''
    result = launch_probe(tmp_path, injected, parent)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['exit'] == 143
    assert all(not Path(root).exists() for root in observed['roots'])
    ready = json.loads(observation.read_text())
    import pytest

    from copilot.store import Store
    with pytest.raises(ProcessLookupError):
        os.kill(ready['pid'], 0)
    with Store(tmp_path / 'data')._tx() as db:
        job = json.loads(db.execute('SELECT data FROM jobs').fetchone()[0])
        assert job['state'] != 'completed'
        assert db.execute('SELECT COUNT(*) FROM provider_attempts').fetchone()[0] == 0


def test_pending_signal_forwarded_before_handshake(tmp_path, monkeypatch):
    import signal

    from copilot import process_boundary as boundary
    from copilot.config import Settings
    events = []
    class Child:
        pid = 99999991
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
    def spawn(*_args, **_kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return Child()
    def forward(_child, _signal):
        events.append('forwarded')
    def write(*_args, **_kwargs):
        assert events == ['forwarded'], 'pending terminal signal delayed until after handshake'
        events.append('handshake')
    monkeypatch.setattr(boundary.subprocess, 'Popen', spawn)
    monkeypatch.setattr(boundary, '_signal_child', forward)
    monkeypatch.setattr(boundary, '_write_envelope', write)
    monkeypatch.setattr(boundary, '_group_alive', lambda _pid: False)
    assert boundary.launch(boundary.settings_command('serve', Settings(tmp_path, tmp_path / 'models'), container=False)) == 143
    assert events == ['forwarded', 'handshake']


def test_workspace_mount_boundary_retains_foreign_contents(tmp_path, monkeypatch):
    from copilot import process_boundary as boundary
    root = tmp_path / 'workspace'
    root.mkdir(mode=0o700)
    mounted = root / 'foreign-mount'
    mounted.mkdir()
    canary = mounted / 'never-delete'
    canary.write_text('owned foreign fixture')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    # Synthetic mount IDs emulate a same-device bind mount without CAP_SYS_ADMIN.
    monkeypatch.setattr(boundary, '_mount_id', lambda fd: 1 if fd == root_fd else 2, raising=False)
    boundary._cleanup(root, root_fd, True)
    assert canary.read_text() == 'owned foreign fixture'
    assert root.exists()


def test_initial_root_validation_failure_closes_fd_and_retains_path(tmp_path, monkeypatch):
    import pytest

    from copilot import process_boundary as boundary
    created = tmp_path / 'owned-runtime'
    created.mkdir(mode=0o700)
    captured = []
    original_open = os.open
    def opened(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        captured.append(fd)
        return fd
    monkeypatch.setattr(boundary.tempfile, 'mkdtemp', lambda **_kwargs: str(created))
    monkeypatch.setattr(boundary.os, 'open', opened)
    def deny(_fd):
        raise OSError('owned metadata failure')
    monkeypatch.setattr(boundary, '_identity', deny)
    with pytest.raises(OSError):
        boundary._new_root('owned-')
    assert created.exists()
    with pytest.raises(OSError):
        os.fstat(captured[0])


def test_non_callable_injection_denies_before_storage(tmp_path):
    import pytest

    from copilot.__main__ import _dispatch
    from copilot.config import Settings
    from copilot.contracts import EvidenceError
    from copilot.process_boundary import settings_command
    data = tmp_path / 'never-created'
    with pytest.raises(EvidenceError):
        _dispatch(settings_command('cleanup', Settings(data, tmp_path / 'models')), service_factory=object())
    assert not data.exists()


def test_thread_embedding_fails_safely_before_spawn(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from copilot import process_boundary as boundary
    from copilot.config import Settings
    def fail(*_args, **_kwargs):
        raise AssertionError('must not spawn in embedding thread')
    monkeypatch.setattr(boundary.subprocess, 'Popen', fail)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(boundary.launch, boundary.settings_command(
            'cleanup', Settings(tmp_path, tmp_path / 'models'))).result()
    assert result == 1


def test_cleanup_retains_substituted_root_and_foreign_sibling(tmp_path):
    from copilot import process_boundary as boundary
    root = tmp_path / 'root'
    root.mkdir(mode=0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    retained = tmp_path / 'retained-original'
    root.rename(retained)
    root.mkdir(mode=0o700)
    foreign = root / 'foreign'
    foreign.write_text('owned foreign canary')
    boundary._cleanup(root, fd, True)
    assert foreign.read_text() == 'owned foreign canary'
    assert retained.exists()


def test_completed_command_cleanup_warning_does_not_request_retry(tmp_path):
    injected = '''
import pathlib
import copilot.__main__ as main
from copilot.process_boundary import require_sdk_boundary
def dispatch(command):
    require_sdk_boundary()
    pathlib.Path('unexpected-owned-file').write_text('retain root')
main._dispatch = dispatch
'''
    result = launch_probe(tmp_path, injected)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['exit'] == 0 and 'no operation rollback or retry' in result.stderr
    # Exact test-created retained root only; production intentionally retains it.
    for name in observed['roots']:
        root = Path(name)
        (root / 'unexpected-owned-file').unlink()
        root.rmdir()


def test_all_public_wrappers_reach_real_sdk_without_caller_dotenv(tmp_path):
    """Expensive command bodies are injected; real SDK import/defaults are not."""
    # Deliberately retain one test function to share the bounded subprocess recipe.
    kinds = ('serve', 'worker', 'index', 'cleanup', 'models_setup', 'retrieval_eval', 'application_eval')
    for kind, nested in [(kind, nested) for kind in kinds for nested in (False, True)]:
        ancestor = tmp_path / (kind + ('-nested' if nested else '-direct'))
        ancestor.mkdir()
        (ancestor / '.env').write_text('CHROMA_SERVER_HOST=owned-ancestor.invalid\n')
        caller = ancestor / 'child' if nested else ancestor
        if nested:
            caller.mkdir()
        (caller / '.env').write_text('CHROMA_SERVER_HOST=owned-canary.invalid\n')
        artifact = caller / 'observed.json'
        injected = f'''
import json, os, pathlib, sys
opens = []
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)) and pathlib.Path(args[0]).name == '.env':
        opens.append(str(args[0]))
sys.addaudithook(audit)
def observed(*args):
    from copilot.process_boundary import require_sdk_boundary
    require_sdk_boundary()
    import chromadb
    from chromadb.config import Settings
    settings = Settings()
    assert settings.chroma_server_host != 'owned-canary.invalid'
    pathlib.Path({str(artifact)!r}).write_text(json.dumps({{'cwd': os.getcwd(), 'opens': opens,
        'sdk': chromadb.__version__, 'args': str(args)}}))
import copilot.__main__ as application
import copilot.models as models
import run, application_run
application._dispatch = observed
models.setup = observed
run._run = observed
application_run._worker_main = observed
'''
        arguments = {'serve': ['serve'], 'worker': ['worker', '--once'],
            'index': ['index', '--profile', 'owned-profile', '--corpus', 'facts'],
            'cleanup': ['cleanup'], 'models_setup': ['setup', '--directory', 'relative models'],
            'retrieval_eval': ['--models', 'relative models', '--output', 'output.json'],
            'application_eval': ['--dataset', 'dataset.jsonl', '--models', 'relative models',
                                 '--offline', '--output', 'output.json']}
        module = 'copilot.__main__' if kind in ('serve', 'worker', 'index', 'cleanup') else {
            'models_setup': 'copilot.models', 'retrieval_eval': 'run', 'application_eval': 'application_run'}[kind]
        code = f'''
import importlib, os, pathlib, subprocess, sys
parent_opens = []
def parent_audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)) and pathlib.Path(args[0]).name == '.env':
        parent_opens.append(str(args[0]))
sys.addaudithook(parent_audit)
sys.path.insert(0, {str(BACKEND.parent / 'eval')!r})
from copilot import process_boundary as boundary
original = subprocess.Popen
roots = []
def spawn(command, **kwargs):
    roots.append(str(kwargs['cwd']))
    assert kwargs['env'] is None if {kind!r} != 'application_eval' else kwargs['env']['HF_HUB_OFFLINE'] == '1'
    command[3] = command[3].replace('from copilot.process_boundary import _child_entry;',
        {injected!r} + '\\nfrom copilot.process_boundary import _child_entry;')
    return original(command, **kwargs)
boundary.subprocess.Popen = spawn
sys.argv = [{module!r}, *{arguments[kind]!r}]
try: importlib.import_module({module!r}).main()
except SystemExit as error: assert error.code == 0, error.code
assert roots and all(not __import__('pathlib').Path(root).exists() for root in roots)
assert not parent_opens, parent_opens
'''
        result = isolated_probe(caller, code)
        assert result.returncode == 0, f'{kind}: {result.stderr}'
        observed = json.loads(artifact.read_text())
        assert observed['opens'] == [] and observed['sdk'] == '1.5.9'
        assert observed['cwd'] != str(caller)
        if kind in ('models_setup', 'retrieval_eval', 'application_eval'):
            assert str(caller / 'relative models') in observed['args']


def test_unreaped_child_uses_one_post_kill_budget_and_retains_root(tmp_path, monkeypatch):
    import signal

    from copilot import process_boundary as boundary
    from copilot.config import Settings
    waits, roots = [], []
    clock = [0.0]
    original_root = boundary._new_root
    class Child:
        pid = 99999992
        def poll(self): return None
        def wait(self, timeout=None):
            waits.append(timeout)
            clock[0] += timeout or 0
            raise subprocess.TimeoutExpired('owned synthetic', timeout)
    def spawn(*_args, **_kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return Child()
    def root(prefix):
        pair = original_root(prefix)
        roots.append(pair[0])
        return pair
    monkeypatch.setattr(boundary, '_new_root', root)
    monkeypatch.setattr(boundary.subprocess, 'Popen', spawn)
    monkeypatch.setattr(boundary, '_signal_child', lambda *_args: None)
    monkeypatch.setattr(boundary, '_write_envelope', lambda *_args: None)
    monkeypatch.setattr(boundary.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(boundary.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setitem(boundary._GRACE, 'serve', 0)
    assert boundary.launch(boundary.settings_command('serve', Settings(tmp_path, tmp_path / 'models'), container=False)) == 1
    assert sum(waits) <= 1, waits
    assert all(root.exists() for root in roots)
    for path in roots:
        path.rmdir()


def test_public_setup_errors_are_safe_and_help_does_not_prepare_paths(tmp_path):
    evaluation = BACKEND.parent / 'eval'
    for module, arguments in (
        ('copilot.__main__', ['cleanup']), ('copilot.models', ['setup']),
        ('run', ['--models', 'relative models']),
        ('application_run', ['--dataset', 'dataset', '--models', 'models', '--offline', '--output', 'output'])):
        for help_only in (False, True):
            result = isolated_probe(tmp_path, f'''
import importlib, os, pathlib, sys
sys.path.insert(0, {str(evaluation)!r})
module = importlib.import_module({module!r})
lost = pathlib.Path('PRIVATE-DELETED-CWD-CANARY')
lost.mkdir()
os.chdir(lost)
os.rmdir('../PRIVATE-DELETED-CWD-CANARY')
sys.argv = [{module!r}, *{['--help'] if help_only else arguments!r}]
try: module.main()
except SystemExit as error: assert error.code == {0 if help_only else 1}, error.code
assert 'chromadb' not in sys.modules
''')
            assert result.returncode == 0, result.stderr
            assert 'PRIVATE-' not in result.stdout + result.stderr
            if not help_only:
                assert 'INDEX_NOT_READY' in result.stdout + result.stderr


def test_production_launch_does_not_query_provider_key_supplier(tmp_path):
    injected = '''
import copilot.__main__ as main
from copilot.process_boundary import require_sdk_boundary
def dispatch(command): require_sdk_boundary()
main._dispatch = dispatch
'''
    parent = '''
class Environment(dict):
    def get(self, key, default=None):
        assert key != 'OPENAI_API_KEY', 'startup consulted provider secret'
        return super().get(key, default)
    def __getitem__(self, key):
        assert key != 'OPENAI_API_KEY', 'startup consulted provider secret'
        return super().__getitem__(key)
os.environ = Environment({'OPENAI_API_KEY': 'SYNTHETIC-NOT-A-KEY'})
'''
    result = launch_probe(tmp_path, injected, parent)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['exit'] == 0
    assert 'SYNTHETIC-NOT-A-KEY' not in result.stdout + result.stderr


_EVAL_PATH_FIELDS = [('retrieval_eval', 'models'), ('retrieval_eval', 'output'),
                     ('application_eval', 'models'), ('application_eval', 'output'),
                     ('application_eval', 'dataset')]


def _eval_command_fixture(parent, kind):
    command = {'schema_version': 1, 'kind': kind,
               'models': str(parent / 'caller-models'), 'output': str(parent / 'caller-output')}
    if kind == 'application_eval':
        command.update(dataset=str(parent / 'caller-dataset'), offline=True, seed=17, split='all')
    return command


def _overlapping_path(workspace, relation):
    return {'equal': workspace, 'ancestor': workspace.parent,
            'descendant': workspace / 'nested-caller-path'}[relation]


@pytest.mark.parametrize('kind,field', _EVAL_PATH_FIELDS)
@pytest.mark.parametrize('relation', ['equal', 'ancestor', 'descendant'])
def test_parent_rejects_workspace_overlap_before_spawn(tmp_path, monkeypatch, kind, field, relation):
    from copilot import process_boundary as boundary
    workspace = tmp_path / 'new-owned-workspace'
    root = tmp_path / 'new-owned-cwd'
    foreign = tmp_path / 'foreign-sibling'
    foreign.write_text('owned foreign canary')
    command = _eval_command_fixture(tmp_path, kind)
    command[field] = str(_overlapping_path(workspace, relation))
    attempts = []
    def new_root(prefix):
        path = workspace if prefix == 'copilot-sdk-eval-' else root
        path.mkdir(mode=0o700)
        return path, os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    def spawn(*_args, **_kwargs):
        attempts.append(True)
        raise OSError('must deny before spawn')
    monkeypatch.setattr(boundary, '_new_root', new_root)
    monkeypatch.setattr(boundary.subprocess, 'Popen', spawn)
    assert boundary.launch(command) == 1
    assert attempts == [], 'overlapping caller path reached subprocess launch'
    assert not root.exists() and not workspace.exists()
    assert foreign.read_text() == 'owned foreign canary'


@pytest.mark.parametrize('kind,field', _EVAL_PATH_FIELDS)
@pytest.mark.parametrize('relation', ['equal', 'ancestor', 'descendant'])
def test_child_rejects_forged_workspace_caller_overlap(tmp_path, kind, field, relation):
    from copilot import process_boundary as boundary
    from copilot.contracts import EvidenceError
    workspace = tmp_path / 'owned-workspace'
    workspace.mkdir(mode=0o700)
    info = workspace.stat()
    command = _eval_command_fixture(tmp_path, kind)
    command[field] = str(_overlapping_path(workspace, relation))
    envelope = {'command': command, 'eval_workspace': {'path': str(workspace),
        'dev': info.st_dev, 'ino': info.st_ino, 'owner_uid': os.geteuid(), 'mode': 0o700}}
    with pytest.raises(EvidenceError) as caught:
        boundary._workspace(envelope, tmp_path / 'separate-lifetime-cwd')
    assert caught.value.code == 'INDEX_NOT_READY'
    assert workspace.exists()


@pytest.mark.parametrize('kind', ['retrieval_eval', 'application_eval'])
def test_workspace_caller_siblings_are_valid_without_resolving_inputs(tmp_path, monkeypatch, kind):
    from copilot import process_boundary as boundary
    workspace, root = tmp_path / 'new-owned-workspace', tmp_path / 'new-owned-cwd'
    command = _eval_command_fixture(tmp_path, kind)
    attempts = []
    def new_root(prefix):
        path = workspace if prefix == 'copilot-sdk-eval-' else root
        path.mkdir(mode=0o700)
        return path, os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    def spawn(*_args, **_kwargs):
        info = workspace.stat()
        assert boundary._workspace({'command': command, 'eval_workspace': {
            'path': str(workspace), 'dev': info.st_dev, 'ino': info.st_ino,
            'owner_uid': os.geteuid(), 'mode': 0o700}}, root) == workspace
        attempts.append(True)
        raise OSError('synthetic spawn failure after valid admission')
    monkeypatch.setattr(boundary, '_new_root', new_root)
    monkeypatch.setattr(boundary.subprocess, 'Popen', spawn)
    assert boundary.launch(command) == 1
    assert attempts == [True]
    assert not root.exists() and not workspace.exists()
    assert not any(Path(command[field]).exists() for field in ('models', 'output'))
