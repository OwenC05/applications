"""Runner integrity only; synthetic test doubles cannot qualify model quality."""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from application_run import evaluation_summary, load_cases, retrieval_input, rrf


class SyntheticModels:
    fingerprint = hashlib.sha256(b'runner-test-double').hexdigest()

    def __init__(self):
        self.observed = []

    def tokenize_offsets(self, text):
        self.observed.append(text)
        return [match.span() for match in re.finditer(r'\S+', text)]

    def embed(self, texts):
        self.observed.extend(texts)
        return [[float('Python' in text), 1.0] + [0.0] * 382 for text in texts]

    def rerank(self, query, texts):
        self.observed.extend([query, *texts])
        return [float(len(text)) for text in texts]


class ApplicationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases(Path(__file__).with_name('applications-frozen.jsonl'))

    def test_import_is_pure_even_with_hostile_dotenv(self):
        code = """
import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'chromadb', 'dotenv', 'copilot', 'torch'}:
            raise AssertionError('runtime import before CLI isolation: ' + fullname)
sys.meta_path.insert(0, Deny())
import application_run
assert application_run.metrics(['a'], ['a'])['recall_at_8'] == 1
"""
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, '.env').write_text('CHROMA_API_IMPL=chromadb.api.fastapi.FastAPI\n')
            result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)],
                                    cwd=temp, env={**os.environ, 'PYTHONPATH': str(Path(__file__).parent.resolve())},
                                    capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_rejects_ambient_transport_before_chroma_import(self):
        code = """
import sys
from application_run import run
try:
    run([], __import__('pathlib').Path('.'), None)
except RuntimeError as error:
    assert 'isolated' in str(error)
else:
    raise AssertionError('ambient transport was accepted')
assert 'chromadb' not in sys.modules
"""
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], cwd=temp,
                env={**os.environ, 'CHROMA_API_IMPL': 'chromadb.api.fastapi.FastAPI',
                     'PYTHONPATH': str(Path(__file__).parent.resolve())}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_rejects_cwd_dotenv_without_importing_chroma(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, '.env').write_text('CHROMA_SERVER_HOST=example.invalid\n')
            self._subprocess_assertions(temp, """
import sys
from pathlib import Path
from application_run import run
try:
    run([], Path('.'), None)
except RuntimeError as error:
    assert 'isolated' in str(error)
else:
    raise AssertionError('dotenv cwd accepted')
assert 'chromadb' not in sys.modules
""")

    def test_cli_child_failure_does_not_publish_a_success_report(self):
        with tempfile.TemporaryDirectory() as temp:
            dataset = Path(temp, 'bad.jsonl')
            dataset.write_text('{}')
            output = Path(temp, 'report.json')
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('application_run.py').resolve()),
                '--dataset', str(dataset), '--models', str(Path(temp, 'absent-models')),
                '--offline', '--output', str(output)], cwd=temp, env={'PATH': os.environ.get('PATH', '')},
                capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Only the exact frozen synthetic dataset is accepted', result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertFalse(output.exists())

    def test_only_frozen_synthetic_bytes_are_accepted(self):
        self.assertEqual(len(self.cases), 60)
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'changed.jsonl'
            path.write_text(json.dumps(self.cases[0]))
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_presentation_is_seeded_and_never_carries_labels_or_pending(self):
        case = self.cases[0]
        value = retrieval_input(case, seed=17)
        self.assertEqual(value, retrieval_input(case, seed=17))
        self.assertEqual(set(value), {'question', 'facts', 'criteria'})
        for source in value['facts']:
            self.assertEqual(set(source), {'id', 'text'})
        self.assertNotIn('gold', json.dumps(value))
        self.assertNotIn('support', json.dumps(value))
        changed = copy.deepcopy(case)
        changed.update(answerable=not case['answerable'], split='label-canary',
                       category='label-canary', gold={'label-canary': True},
                       pending_proposals=[{'text': 'unconfirmed-canary'}])
        self.assertEqual(value, retrieval_input(changed, seed=17))

    def test_wrong_owner_or_unconfirmed_fact_is_never_loaded(self):
        for field, value in [('owner_id', 'wrong'), ('state', 'pending')]:
            case = copy.deepcopy(self.cases[0])
            case['confirmed_fact_bank'][0][field] = value
            with self.assertRaises(ValueError):
                retrieval_input(case, seed=17)

    def test_wrong_application_employer_never_becomes_a_criterion(self):
        case = copy.deepcopy(self.cases[0])
        case['employer_excerpts'][0]['application_id'] = 'other'
        with self.assertRaises(ValueError):
            retrieval_input(case, seed=17)

    def test_rrf_deduplicates_inside_each_variant_and_has_stable_ties(self):
        self.assertEqual(rrf([['b', 'b', 'a'], ['a', 'b']]), ['a', 'b'])
        self.assertEqual(rrf([['b'], ['a']]), ['a', 'b'])
        self.assertEqual(rrf([]), [])

    def test_summary_never_turns_unknown_semantics_into_passed_abstention(self):
        rows = [{'split': 'heldout', 'sector': 'tech', 'answerable': False,
                 'metrics': {'dense': None, 'bm25': None, 'hybrid_reranked': None}}]
        value = evaluation_summary(rows)
        self.assertEqual(value['heldout']['unanswerable'], 1)
        self.assertIsNone(value['heldout']['branches']['dense']['recall_at_8'])
        self.assertEqual(value['semantic_abstention'], 'not_assessed')
        self.assertFalse(value['release_qualified'])

    def test_injected_models_real_indexes_use_only_owned_confirmed_wording(self):
        with tempfile.TemporaryDirectory() as temp:
            self._subprocess_assertions(temp, """
from test_application_run import ApplicationRunnerTests
case = ApplicationRunnerTests()
case.setUp()
case._assert_owned_wording()
""")

    def _subprocess_assertions(self, cwd, code, env=None):
        repo = Path(__file__).resolve().parents[1]
        bootstrap = f'import sys; sys.path[:0] = {[str(repo / "eval"), str(repo / "backend")]!r}; '
        result = subprocess.run([sys.executable, '-I', '-c', bootstrap + textwrap.dedent(code)],
                                cwd=cwd, env=env or {'PATH': os.environ.get('PATH', '')},
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_cli_isolates_dotenv_and_hostile_environment(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, '.env').write_text('CHROMA_API_IMPL=chromadb.api.fastapi.FastAPI\n'
                                         'CHROMA_SERVER_HOST=example.invalid\n')
            output = Path(temp, 'report.json')
            code = """
import pathlib, subprocess, sys
import application_run
real_popen = subprocess.Popen
# Test-only child injection, never a production flag.
injected = '''
import os, socket
assert not any(k.startswith('CHROMA_') for k in os.environ)
assert 'HTTPS_PROXY' not in os.environ and 'PYTHONPATH' not in os.environ
assert os.environ['HF_HUB_OFFLINE'] == '1'
assert not __import__('pathlib').Path('.env').exists()
def deny(*args, **kwargs):
    raise AssertionError('network attempted')
socket.socket.connect = deny
socket.create_connection = deny
socket.getaddrinfo = deny
import copilot.models
from test_application_run import SyntheticModels
copilot.models.LocalModels = lambda directory: SyntheticModels()
import chromadb
original_client = chromadb.PersistentClient
def local_client(*args, **kwargs):
    settings = kwargs['settings']
    assert settings.chroma_api_impl == 'chromadb.api.rust.RustBindingsAPI'
    assert settings.anonymized_telemetry is False
    client = original_client(*args, **kwargs)
    assert client.get_settings().chroma_api_impl == 'chromadb.api.rust.RustBindingsAPI'
    return client
chromadb.PersistentClient = local_client
import application_run
original_load = application_run.load_cases
application_run.load_cases = lambda path: original_load(path)[:1]
'''
def checked_popen(command, **kwargs):
    assert command[1:3] == ['-I', '-c']
    assert set(kwargs['env']) <= {'PATH', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE',
        'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'ANONYMIZED_TELEMETRY'}
    assert not pathlib.Path(kwargs['cwd'], '.env').exists()
    command[3] = command[3].replace('from application_run import _worker_main;',
                                  injected + '\\nfrom application_run import _worker_main;')
    return real_popen(command, **kwargs)
subprocess.Popen = checked_popen
sys.argv = ['application_run.py', '--dataset', DATASET, '--models', 'absent-models',
            '--offline', '--output', OUTPUT]
application_run.main()
""".replace('DATASET', repr(str(repo / 'eval/applications-frozen.jsonl'))).replace('OUTPUT', repr(str(output)))
            self._subprocess_assertions(temp, code, {'PATH': os.environ.get('PATH', ''),
                'CHROMA_API_IMPL': 'chromadb.api.fastapi.FastAPI',
                'CHROMA_SERVER_HOST': 'example.invalid', 'HTTPS_PROXY': 'http://example.invalid',
                'PYTHONPATH': '/not-used'})
            report = json.loads(output.read_text())
            self.assertEqual(len(report['results']), 1)
            self.assertFalse(report['summary']['release_qualified'])
            self.assertGreater(report['results'][0]['citation_integrity_checked'], 0)

    @unittest.skipUnless(os.name == 'posix', 'POSIX SIGTERM lifecycle')
    def test_handler_restored_after_success_launch_failure_and_launch_sigterm(self):
        with tempfile.TemporaryDirectory() as temp:
            self._subprocess_assertions(temp, """
import pathlib, signal, sys
from unittest.mock import patch
import application_run
previous = signal.getsignal(signal.SIGTERM)
sys.argv = ['application_run.py', '--dataset', 'unused', '--models', 'unused',
            '--offline', '--output', 'unused']
for mode in ('success', 'launch_failure', 'launch_sigterm'):
    roots = []
    class Child:
        args = ['synthetic']
        terminated = False
        def wait(self, timeout=None):
            return 0
        def poll(self):
            return None if mode == 'launch_sigterm' and not self.terminated else 0
        def terminate(self):
            self.terminated = True
    child = Child()
    def launch(*args, **kwargs):
        roots.append(pathlib.Path(kwargs['cwd']))
        if mode == 'launch_failure':
            raise OSError('synthetic launch failure')
        if mode == 'launch_sigterm':
            # TERM while Popen has not returned must not lose child ownership.
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return child
    with patch.object(application_run.subprocess, 'Popen', launch):
        try:
            application_run.main()
        except OSError:
            assert mode == 'launch_failure'
        except SystemExit as error:
            assert mode == 'launch_sigterm' and error.code == 143
        else:
            assert mode == 'success'
    assert signal.getsignal(signal.SIGTERM) == previous
    assert roots and all(not root.exists() for root in roots)
    if mode == 'launch_sigterm':
        assert child.terminated
""")

    @unittest.skipUnless(os.name == 'posix', 'POSIX SIGTERM lifecycle')
    def test_parent_sigterm_reaps_child_and_removes_nested_storage(self):
        import shutil
        import signal
        import time

        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            ready = Path(temp, 'ready.json')
            cleanup = Path(temp, 'cleanup.json')
            output = Path(temp, 'late-report.json')
            injected = f'''
import os, pathlib, time
import copilot.models
copilot.models.LocalModels = lambda path: None
import application_run
def synthetic_run(cases, root, models, seed):
    nested = root / 'canary'
    nested.write_text('owned storage')
    pathlib.Path({str(ready)!r}).write_text(__import__('json').dumps(
        dict(pid=os.getpid(), cwd=os.getcwd(), nested=str(nested))))
    time.sleep(1.5)
    return {{'summary': {{'release_qualified': False}}}}
application_run.run = synthetic_run
'''
            code = f'''
import json, os, signal, subprocess, sys
import application_run
original = subprocess.Popen
children = []
previous = signal.getsignal(signal.SIGTERM)
def tracked_popen(command, **kwargs):
    command[3] = command[3].replace('from application_run import _worker_main;',
        {injected!r} + '\\nfrom application_run import _worker_main;')
    child = original(command, **kwargs)
    children.append(child)
    return child
subprocess.Popen = tracked_popen
sys.argv = ['application_run.py', '--dataset', {str(repo / 'eval/applications-frozen.jsonl')!r},
    '--models', 'absent-models', '--offline', '--output', {str(output)!r}]
try:
    application_run.main()
finally:
    reaped = False
    if children:
        try:
            os.waitpid(children[0].pid, os.WNOHANG)
        except ChildProcessError:
            reaped = True
    __import__('pathlib').Path({str(cleanup)!r}).write_text(json.dumps(dict(
        reaped=reaped, restored=signal.getsignal(signal.SIGTERM) == previous)))
'''
            bootstrap = f'import sys; sys.path[:0] = {[str(repo / "eval"), str(repo / "backend")]!r}; '
            parent = subprocess.Popen([sys.executable, '-I', '-c', bootstrap + code],
                                      cwd=temp, env={'PATH': os.environ.get('PATH', '')},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            info = None
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), 'child never reached ready handshake')
                info = json.loads(ready.read_text())
                parent.send_signal(signal.SIGTERM)
                _, stderr = parent.communicate(timeout=10)
                time.sleep(1.7)  # A leaked child would now publish its late report.
                self.assertFalse(output.exists(), 'child survived parent termination')
                self.assertFalse(Path(info['cwd']).exists(), 'parent worker cwd leaked')
                self.assertFalse(Path(info['nested']).exists(), 'nested storage leaked')
                self.assertTrue(Path(info['nested']).is_relative_to(Path(info['cwd'])))
                self.assertEqual(parent.returncode, 128 + signal.SIGTERM, stderr)
                self.assertEqual(json.loads(cleanup.read_text()), {'reaped': True, 'restored': True})
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.communicate()
                if info:
                    try:
                        os.kill(info['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    shutil.rmtree(info['cwd'], ignore_errors=True)
                    shutil.rmtree(str(Path(info['nested']).parent), ignore_errors=True)

    def _assert_owned_wording(self):
        from application_run import run

        case = copy.deepcopy(self.cases[0])
        case['gold']['label_basis'] = 'MODELGOLDCANARY'
        case['category'] = 'MODELCATEGORYCANARY'
        case['pending_proposals'] = [{'text': 'UNCONFIRMEDCANARY'}]
        models = SyntheticModels()
        with tempfile.TemporaryDirectory() as root:
            report = run([case], Path(root), models)
        observed = '\n'.join(models.observed)
        for canary in ('MODELGOLDCANARY', 'MODELCATEGORYCANARY', 'UNCONFIRMEDCANARY'):
            self.assertNotIn(canary, observed)
        self.assertFalse(report['summary']['release_qualified'])
        self.assertGreater(report['results'][0]['citation_integrity_checked'], 0)


if __name__ == '__main__':
    unittest.main()
