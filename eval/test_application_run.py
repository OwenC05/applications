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
                                    cwd=temp, env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(Path(__file__).parent.resolve()), str(Path(__file__).resolve().parents[1] / 'backend')])},
                                    capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_rejects_ambient_transport_before_chroma_import(self):
        code = """
import sys
from application_run import run
try:
    run([], __import__('pathlib').Path('.'), None)
except Exception as error:
    assert getattr(error, 'code', None) == 'INDEX_NOT_READY'
else:
    raise AssertionError('ambient transport was accepted')
assert 'chromadb' not in sys.modules
"""
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], cwd=temp,
                env={**os.environ, 'CHROMA_API_IMPL': 'chromadb.api.fastapi.FastAPI',
                     'PYTHONPATH': os.pathsep.join([str(Path(__file__).parent.resolve()), str(Path(__file__).resolve().parents[1] / 'backend')])}, capture_output=True, text=True, check=False)
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
except Exception as error:
    assert getattr(error, 'code', None) == 'INDEX_NOT_READY'
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
            self.assertIn('INDEX_NOT_READY', result.stderr)
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
import subprocess
from copilot import process_boundary as boundary
original = subprocess.Popen
injected = '''
import application_run
from test_application_run import ApplicationRunnerTests
def worker(command, workspace):
    case = ApplicationRunnerTests()
    case.setUp()
    case._assert_owned_wording(workspace)
application_run._worker_main = worker
'''
def spawn(command, **kwargs):
    command[3] = command[3].replace('from copilot.process_boundary import _child_entry;',
        injected + '\\nfrom copilot.process_boundary import _child_entry;')
    return original(command, **kwargs)
boundary.subprocess.Popen = spawn
from pathlib import Path
assert boundary.launch({'schema_version': 1, 'kind': 'application_eval',
    'dataset': str(Path('unused').resolve()), 'models': str(Path('unused').resolve()),
    'output': str(Path('unused').resolve()), 'offline': True, 'seed': 17, 'split': 'tune'}) == 0
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
import application_run
original_load = application_run.load_cases
application_run.load_cases = lambda path: original_load(path)[:1]
'''
def checked_popen(command, **kwargs):
    assert command[1:3] == ['-I', '-c']
    assert set(kwargs['env']) <= {'PATH', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE',
        'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'ANONYMIZED_TELEMETRY'}
    assert not pathlib.Path(kwargs['cwd'], '.env').exists()
    command[3] = command[3].replace('from copilot.process_boundary import _child_entry;',
                                  injected + '\\nfrom copilot.process_boundary import _child_entry;')
    return real_popen(command, **kwargs)
subprocess.Popen = checked_popen
sys.argv = ['application_run.py', '--dataset', DATASET, '--models', 'absent-models',
            '--offline', '--output', OUTPUT]
try:
    application_run.main()
except SystemExit as error:
    assert error.code == 0
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
    def test_parent_sigterm_reaps_child_before_separate_workspace_cleanup(self):
        for forced in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                ready = Path(temp, 'ready.json')
                output = Path(temp, 'never-published.json')
                foreign = Path(temp, 'foreign-sibling')
                foreign.write_text('owned foreign sibling')
                injected = f"""
    import json, os, pathlib, signal, time
    import application_run
    def run(cases, root, models, seed):
        if {forced!r}: signal.signal(signal.SIGTERM, signal.SIG_IGN)
        root.joinpath('owned-storage').write_text('owned synthetic storage')
        pathlib.Path({str(ready)!r}).write_text(json.dumps({{'pid': os.getpid(), 'cwd': os.getcwd(),
            'workspace': str(root), 'cwd_entries': os.listdir('.')}}))
        while True: time.sleep(0.02)
    application_run.run = run
    """
                injected = textwrap.dedent(injected)
                code = f"""
    import json, os, pathlib, signal, subprocess, sys, threading, time
    import application_run
    from copilot import process_boundary as boundary
    boundary._GRACE['application_eval'] = 0.1 if {forced!r} else 5
    original = subprocess.Popen
    children = []
    handlers = {{n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}}
    def spawn(command, **kwargs):
        command[3] = command[3].replace('from copilot.process_boundary import _child_entry;',
            {injected!r} + '\\nfrom copilot.process_boundary import _child_entry;')
        child = original(command, **kwargs)
        children.append(child)
        return child
    boundary.subprocess.Popen = spawn
    def interrupt():
        deadline = time.monotonic() + 10
        while not pathlib.Path({str(ready)!r}).exists() and time.monotonic() < deadline: time.sleep(0.02)
        if pathlib.Path({str(ready)!r}).exists(): os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=interrupt, daemon=True).start()
    sys.argv = ['application_run.py', '--dataset', {str(Path(__file__).with_name('applications-frozen.jsonl'))!r},
        '--models', 'unused', '--offline', '--output', {str(output)!r}]
    try: application_run.main()
    except SystemExit as error: assert error.code == 143
    info = json.loads(pathlib.Path({str(ready)!r}).read_text())
    assert not pathlib.Path(info['cwd']).exists()
    assert not pathlib.Path(info['workspace']).exists()
    assert info['cwd_entries'] == []
    assert not pathlib.Path(info['workspace']).is_relative_to(info['cwd'])
    assert not pathlib.Path({str(output)!r}).exists()
    assert pathlib.Path({str(foreign)!r}).read_text() == 'owned foreign sibling'
    assert {{n: signal.getsignal(n) for n in handlers}} == handlers
    try: os.waitpid(children[0].pid, os.WNOHANG)
    except ChildProcessError: pass
    else: raise AssertionError('child not reaped')
    """
                self._subprocess_assertions(temp, code)

    def _assert_owned_wording(self, root):
        from application_run import run

        case = copy.deepcopy(self.cases[0])
        case['gold']['label_basis'] = 'MODELGOLDCANARY'
        case['category'] = 'MODELCATEGORYCANARY'
        case['pending_proposals'] = [{'text': 'UNCONFIRMEDCANARY'}]
        models = SyntheticModels()
        report = run([case], Path(root), models)
        observed = '\n'.join(models.observed)
        for canary in ('MODELGOLDCANARY', 'MODELCATEGORYCANARY', 'UNCONFIRMEDCANARY'):
            self.assertNotIn(canary, observed)
        self.assertFalse(report['summary']['release_qualified'])
        self.assertGreater(report['results'][0]['citation_integrity_checked'], 0)


if __name__ == '__main__':
    unittest.main()
