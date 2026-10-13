"""Owned process-lifetime SDK CWD. This is not a filesystem or environment sandbox."""
import json
import os
import secrets
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .contracts import EvidenceError

_MESSAGE = 'Start the evidence service through the supported isolated launcher.'
_LIMIT = 65536
_HANDSHAKE = 5
_GRACE = {'serve': 8, 'application_eval': 5}
_STATE = None
_APP = {'serve', 'worker', 'index', 'cleanup'}
_EVAL = {'retrieval_eval', 'application_eval'}


def _deny():
    raise EvidenceError('INDEX_NOT_READY', _MESSAGE, 503)


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _deny()


def _integer(value, low=0, high=2**64 - 1):
    if type(value) is not int or not low <= value <= high:
        _deny()


def _string(value, maximum, minimum=1):
    if type(value) is not str or '\0' in value or not minimum <= len(value.encode('utf-8')) <= maximum:
        _deny()


def _path(value):
    _string(value, 4096)
    if not Path(value).is_absolute() or str(Path(value)) != value or '..' in Path(value).parts:
        _deny()


def validate_command(command):
    """Closed non-secret wire schema; no user-selected executable or environment."""
    if type(command) is not dict:
        _deny()
    kind = command.get('kind')
    fields = {'serve': {'settings', 'container'}, 'worker': {'settings', 'once'},
              'index': {'settings', 'profile', 'corpus'}, 'cleanup': {'settings'},
              'models_setup': {'directory'}, 'retrieval_eval': {'models', 'output'},
              'application_eval': {'dataset', 'models', 'output', 'offline', 'seed', 'split'}}
    if type(kind) is not str or kind not in fields:
        _deny()
    _keys(command, {'schema_version', 'kind'} | fields[kind])
    _integer(command['schema_version'], 1, 1)
    if kind in _APP:
        settings = command['settings']
        _keys(settings, {'data_dir', 'model_dir', 'chroma_host', 'chroma_port', 'port'})
        for field in ('data_dir', 'model_dir'):
            _path(settings[field])
        _string(settings['chroma_host'], 253)
        for field in ('chroma_port', 'port'):
            _integer(settings[field], 1, 65535)
    for field in ('container', 'once', 'offline'):
        if field in command and type(command[field]) is not bool:
            _deny()
    if kind == 'index':
        _string(command['profile'], 200)
        if command['corpus'] not in ('facts', 'documents'):
            _deny()
    if kind == 'models_setup':
        _path(command['directory'])
    if kind in _EVAL:
        _path(command['models'])
        if command['output'] is not None:
            _path(command['output'])
    if kind == 'application_eval':
        _path(command['dataset'])
        _path(command['output'])
        _integer(command['seed'], -(2**63), 2**63 - 1)
        if command['offline'] is not True or command['split'] not in ('tune', 'heldout', 'all'):
            _deny()
    return command


def settings_command(kind, settings, **fields):
    """Resolve approved caller-relative settings before creating the SDK child."""
    return validate_command({'schema_version': 1, 'kind': kind, 'settings': {
        'data_dir': str(settings.data_dir.resolve()), 'model_dir': str(settings.model_dir.resolve()),
        'chroma_host': settings.chroma_host, 'chroma_port': settings.chroma_port,
        'port': settings.port}, **fields})


def _identity(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        _deny()
    return info


def _same_path(path, fd):
    actual, expected = os.stat(path, follow_symlinks=False), _identity(fd)
    if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino, actual.st_uid,
            stat.S_IMODE(actual.st_mode)) != (expected.st_dev, expected.st_ino, expected.st_uid, 0o700):
        _deny()


def _check_sdk_boundary():
    """Refuse default embedding, inherited state and changed lifetime roots."""
    if _STATE is None or _STATE['pid'] != os.getpid():
        _deny()
    info = _identity(_STATE['fd'])
    current = os.stat('.')
    if (info.st_dev, info.st_ino) != _STATE['identity'] or (
            current.st_dev, current.st_ino) != _STATE['identity']:
        _deny()
    _same_path(_STATE['path'], _STATE['fd'])
    # Non-following metadata, never open a dotenv file/FIFO/symlink.
    try:
        os.stat('.env', dir_fd=_STATE['fd'], follow_symlinks=False)
    except FileNotFoundError:
        return
    _deny()



def require_sdk_boundary():
    try:
        _check_sdk_boundary()
    except EvidenceError:
        raise
    except Exception:
        _deny()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _deny()
        result[key] = value
    return result


def _read_envelope(fd):
    deadline, chunks, size = time.monotonic() + _HANDSHAKE, [], 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            _deny()
        block = os.read(fd, min(8192, _LIMIT + 1 - size))
        if not block:
            break
        chunks.append(block)
        size += len(block)
        if size > _LIMIT:
            _deny()
    return json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=lambda _value: _deny())



def _require_workspace_disjoint(workspace, command):
    # Command paths are already validated absolute strings. No caller FS reads.
    for field in ('models', 'output', 'dataset'):
        value = command.get(field)
        if value is not None:
            caller = Path(value)
            if workspace.is_relative_to(caller) or caller.is_relative_to(workspace):
                _deny()


def _workspace(envelope, root):
    workspace = envelope['eval_workspace']
    if envelope['command']['kind'] not in _EVAL:
        if workspace is not None:
            _deny()
        return None
    _keys(workspace, {'path', 'dev', 'ino', 'owner_uid', 'mode'})
    _path(workspace['path'])
    _require_workspace_disjoint(Path(workspace['path']), envelope['command'])
    for field in ('dev', 'ino', 'owner_uid', 'mode'):
        _integer(workspace[field])
    info = os.stat(workspace['path'], follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or workspace['mode'] != 0o700 or
            (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) !=
            (workspace['dev'], workspace['ino'], os.geteuid(), 0o700) or
            workspace['owner_uid'] != os.geteuid() or
            Path(workspace['path']).is_relative_to(root) or root.is_relative_to(workspace['path'])):
        _deny()
    return Path(workspace['path'])


def _activate(root_fd, envelope):
    global _STATE
    _keys(envelope, {'schema_version', 'parent_pid', 'child_pid', 'root_dev', 'root_ino',
                     'owner_uid', 'capability', 'command', 'eval_workspace'})
    _integer(envelope['schema_version'], 1, 1)
    for field in ('parent_pid', 'child_pid', 'root_dev', 'root_ino', 'owner_uid'):
        _integer(envelope[field], 1 if field.endswith('pid') else 0)
    capability = envelope['capability']
    if (type(capability) is not str or len(capability) != 64 or
            any(c not in '0123456789abcdef' for c in capability)):
        _deny()
    command = validate_command(envelope['command'])
    info = _identity(root_fd)
    current = os.stat('.')
    if (_STATE is not None or any(key == 'chromadb' or key.startswith('chromadb.') for key in sys.modules)
            or envelope['parent_pid'] != os.getppid() or envelope['child_pid'] != os.getpid()
            or envelope['owner_uid'] != os.geteuid()
            or (info.st_dev, info.st_ino) != (envelope['root_dev'], envelope['root_ino'])
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or os.listdir(root_fd)):
        _deny()
    root = Path.cwd()
    workspace = _workspace(envelope, root)
    _STATE = {'pid': os.getpid(), 'fd': root_fd, 'path': root,
              'identity': (info.st_dev, info.st_ino), 'capability': capability}
    require_sdk_boundary()
    return command, workspace


def _child_entry(root_fd, config_fd):
    """Fixed private FD bootstrap. Root FD intentionally lives until process exit."""
    try:
        try:
            envelope = _read_envelope(config_fd)
        finally:
            os.close(config_fd)
        command, workspace = _activate(root_fd, envelope)
        kind = command['kind']
        if kind in _APP:
            from .__main__ import _dispatch
            _dispatch(command)
        elif kind == 'models_setup':
            from .models import setup
            setup(Path(command['directory']))
            print('Pinned local models provisioned and file hashes recorded.')
        elif kind == 'retrieval_eval':
            from run import _run
            _run(Path(command['models']), Path(command['output']) if command['output'] else None, workspace)
        else:
            from application_run import _worker_main
            _worker_main(command, workspace)
    except SystemExit:
        raise
    except BaseException:
        print(json.dumps({'error': {'code': 'INDEX_NOT_READY', 'message': _MESSAGE}}), file=sys.stderr)
        raise SystemExit(1) from None


def _new_root(prefix):
    path = Path(tempfile.mkdtemp(prefix=prefix, dir='/tmp'))
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _identity(fd)
        _same_path(path, fd)
        return path, fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        # Identity validation failed: retain rather than delete an unproved path.
        print('Isolated runtime setup incomplete; owned directory retained.', file=sys.stderr)
        raise


def _mount_id(fd):
    # Linux fd metadata, not user configuration. Missing proof retains workspace.
    with open(f'/proc/self/fdinfo/{fd}', encoding='ascii') as stream:
        metadata = stream.read(8193)
    if len(metadata) > 8192:
        _deny()
    ids = [line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.startswith('mnt_id:')]
    if len(ids) == 1 and ids[0].isascii() and ids[0].isdigit():
        return int(ids[0])
    _deny()


def _clear_workspace(fd, device, mount):
    """Descriptor-relative deletion; no symlink following or device crossing."""
    for name in os.listdir(fd):
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if before.st_dev != device:
            _deny()
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                info = os.fstat(child_fd)
                if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or _mount_id(child_fd) != mount:
                    _deny()
                _clear_workspace(child_fd, device, mount)
                after = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                    _deny()
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child_fd)
        else:
            os.unlink(name, dir_fd=fd)


def _cleanup(path, fd, recursive=False):
    try:
        _same_path(path, fd)
        if recursive:
            _clear_workspace(fd, os.fstat(fd).st_dev, _mount_id(fd))
            _same_path(path, fd)
        os.rmdir(path)
    except BaseException:
        print('Isolated runtime cleanup incomplete; no operation rollback or retry is implied.', file=sys.stderr)
    finally:
        os.close(fd)


def _group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _signal_child(child, signum):
    # A dead leader must never grant authority over a reused group number.
    try:
        if child.poll() is None and os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _write_envelope(fd, envelope, pending=None):
    payload = json.dumps(envelope, allow_nan=False).encode('utf-8')
    if len(payload) > _LIMIT:
        _deny()
    deadline = time.monotonic() + _HANDSHAKE
    os.set_blocking(fd, False)
    while payload:
        stop_deadline = pending() if pending is not None else None
        remaining = deadline - time.monotonic()
        if stop_deadline is not None:
            remaining = min(remaining, stop_deadline - time.monotonic())
        if remaining <= 0:
            _deny()
        if not select.select([], [fd], [], min(remaining, 0.05))[1]:
            continue
        try:
            written = os.write(fd, payload)
            payload = payload[written:]
        except BlockingIOError:
            continue


def launch(command):
    """Launch exactly one owned SDK child, preserving the caller's CWD and env."""
    roots, pipe, child, handlers = [], [], None, {}
    requested, sent, deadline = None, False, None
    kill_deadline = None
    reaped, quiescent = False, False

    def request(signum, _frame):
        nonlocal requested
        if requested is None:
            requested = signum

    def force_reap():
        nonlocal kill_deadline
        if kill_deadline is None:
            _signal_child(child, signal.SIGKILL)
            kill_deadline = time.monotonic() + 1
        return child.wait(timeout=max(0, kill_deadline - time.monotonic()))

    def forward_pending():
        nonlocal sent, deadline
        if requested is not None and not sent:
            _signal_child(child, signal.SIGTERM if command['kind'] == 'application_eval' else requested)
            sent = True
            deadline = time.monotonic() + _GRACE.get(command['kind'], 58)
        return deadline

    try:
        validate_command(command)
        if threading.current_thread() is not threading.main_thread():
            _deny()
        # signal.signal rejects non-main-thread embedding before any spawn.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            signal.signal(signum, request)
            handlers[signum] = previous
        root, root_fd = _new_root('copilot-sdk-cwd-')
        roots.append((root, root_fd, False))
        workspace = None
        if command['kind'] in _EVAL:
            work, work_fd = _new_root('copilot-sdk-eval-')
            roots.append((work, work_fd, True))
            _require_workspace_disjoint(work, command)
            info = os.fstat(work_fd)
            workspace = {'path': str(work), 'dev': info.st_dev, 'ino': info.st_ino,
                         'owner_uid': info.st_uid, 'mode': 0o700}
        read_fd, write_fd = os.pipe()
        pipe.extend((read_fd, write_fd))
        repo = Path(__file__).resolve().parents[2]
        bootstrap = (f'import sys; sys.path[:0] = {[str(repo / "backend"), str(repo / "eval")]!r}; '
                     'from copilot.process_boundary import _child_entry; '
                     '_child_entry(int(sys.argv[1]), int(sys.argv[2]))')
        env = None
        if command['kind'] == 'application_eval':
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE'}}
            env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                       ANONYMIZED_TELEMETRY='False')
        child = subprocess.Popen([str(Path(sys.executable).absolute()), '-I', '-c', bootstrap,
                                  str(root_fd), str(read_fd)], cwd=root, env=env,
                                 close_fds=True, pass_fds=(root_fd, read_fd), start_new_session=True)
        forward_pending()
        os.close(read_fd)
        pipe.remove(read_fd)
        info = os.fstat(root_fd)
        try:
            _write_envelope(write_fd, {'schema_version': 1, 'parent_pid': os.getpid(),
                'child_pid': child.pid, 'root_dev': info.st_dev, 'root_ino': info.st_ino,
                'owner_uid': info.st_uid, 'capability': secrets.token_hex(32),
                'command': command, 'eval_workspace': workspace}, forward_pending)
        finally:
            os.close(write_fd)
            pipe.remove(write_fd)
        while child.poll() is None:
            forward_pending()
            if deadline is not None and time.monotonic() >= deadline:
                force_reap()
                break
            time.sleep(0.02)
        code = child.wait(timeout=1)
        reaped = True
        quiescent = not _group_alive(child.pid)
        if not quiescent:
            _deny()
        return 128 + requested if requested is not None else (128 - code if code < 0 else code)
    except BaseException:
        if child is not None and not reaped:
            try:
                _signal_child(child, signal.SIGTERM)
                child.wait(timeout=max(0, deadline - time.monotonic()) if deadline is not None
                           else _GRACE.get(command.get('kind'), 58))
            except subprocess.TimeoutExpired:
                try:
                    force_reap()
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
            reaped = child.poll() is not None
            quiescent = reaped and not _group_alive(child.pid)
        print(json.dumps({'error': {'code': 'INDEX_NOT_READY', 'message': _MESSAGE}}), file=sys.stderr)
        return 128 + requested if requested is not None and reaped else 1
    finally:
        for fd in pipe:
            os.close(fd)
        if child is None or (reaped and quiescent):
            for path, fd, recursive in reversed(roots):
                _cleanup(path, fd, recursive)
        else:
            for _path_name, fd, _recursive in roots:
                os.close(fd)
            print('Isolated runtime retained: child/group termination was not proved.', file=sys.stderr)
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
