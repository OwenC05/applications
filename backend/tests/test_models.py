import json

import pytest

from copilot.contracts import EvidenceError
from copilot.models import COMMON_FILES, EMBEDDING_FILES, LOCK, LocalModels, sha


def test_missing_models_fail_closed(tmp_path):
    model = LocalModels(tmp_path)
    assert not model.ready
    with pytest.raises(EvidenceError, match="setup"):
        model.embed(["synthetic"])


def test_modified_local_file_not_ready(tmp_path):
    files = {}
    for kind in ("embedding", "reranker"):
        files[kind] = {}
        for name in (EMBEDDING_FILES if kind == "embedding" else COMMON_FILES):
            path = tmp_path / kind / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture, never loaded as model")
            files[kind][name] = sha(path)
    manifest = {"spec": json.loads(LOCK.read_text()), "files": files}
    (tmp_path / "verified.json").write_text(json.dumps(manifest))
    model = LocalModels(tmp_path)
    assert model.ready
    (tmp_path / "embedding/model.safetensors").write_bytes(b"altered")
    assert not model.ready
    with pytest.raises(EvidenceError):
        _ = model.fingerprint


def test_malformed_manifest_is_actionable_not_uncaught(tmp_path):
    (tmp_path / "verified.json").write_text('{"spec": null, "files": []}')
    model = LocalModels(tmp_path)
    assert not model.ready
    with pytest.raises(EvidenceError) as caught:
        _ = model.fingerprint
    assert caught.value.code == "MODEL_NOT_READY"


@pytest.mark.parametrize("override", [False, True])
def test_setup_default_directory_matches_api_without_download(tmp_path, monkeypatch, override):
    import sys

    from copilot import models
    from copilot.config import Settings
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COPILOT_DATA_DIR", raising=False)
    monkeypatch.delenv("COPILOT_MODEL_DIR", raising=False)
    if override:
        monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "custom-data"))
        monkeypatch.setenv("COPILOT_MODEL_DIR", str(tmp_path / "custom-models"))
    captured = []
    monkeypatch.setattr(models, "setup", captured.append)
    monkeypatch.setattr(sys, "argv", ["copilot.models", "setup"])
    models.main()
    assert captured == [Settings.from_env().model_dir]


@pytest.mark.parametrize('failure', [False, True])
def test_cold_model_initialization_is_single_flight_and_publishes_no_partial_state(tmp_path, monkeypatch, failure):
    import sys
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    calls = []
    entered, release = threading.Event(), threading.Event()
    class Tokenizer:
        @staticmethod
        def from_pretrained(*_args, **_kwargs):
            calls.append('tokenizer')
            entered.set()
            assert release.wait(5)
            return object()
    class Embedding:
        def __init__(self, *_args, **_kwargs):
            calls.append('embedding')
        def get_sentence_embedding_dimension(self):
            return 384
    class Cross:
        def __init__(self, *_args, **_kwargs):
            calls.append('cross')
            if failure:
                raise RuntimeError('PRIVATE-MODEL-FAILURE-CANARY')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(set_num_threads=lambda _: None))
    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(SentenceTransformer=Embedding, CrossEncoder=Cross))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=Tokenizer))
    model = LocalModels(tmp_path)
    monkeypatch.setattr(model, '_verified', lambda: {})
    def ensure():
        try:
            model._ensure()
            return None
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(ensure) for _ in range(6)]
        assert entered.wait(5)
        time.sleep(0.1)
        assert not hasattr(model, 'embedding')  # No partially published model bundle.
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert calls == ['tokenizer', 'embedding', 'cross']
    if failure:
        assert not model._loaded and not hasattr(model, 'tokenizer') and not hasattr(model, 'embedding')
        assert all(isinstance(result, EvidenceError) for result in results)
        assert all('PRIVATE-' not in str(result) for result in results)
        # A later explicit request may retry after repair; failed waiters do not stampede.
        failure = False
        model._ensure()
        assert model._loaded
    else:
        assert model._loaded and results == [None] * 6


@pytest.mark.parametrize('changed_during_initialization', [False, True])
def test_loaded_bundle_cannot_be_relabelled_by_changed_verified_files(tmp_path, monkeypatch, changed_during_initialization):
    import sys
    from types import SimpleNamespace

    bundle = {'version': 'first'}

    class Tokenizer:
        @staticmethod
        def from_pretrained(*_args, **_kwargs):
            return object()

    class Embedding:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_sentence_embedding_dimension(self):
            return 384

    class Cross:
        def __init__(self, *_args, **_kwargs):
            if changed_during_initialization:
                bundle['version'] = 'second'

    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(set_num_threads=lambda _: None))
    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(SentenceTransformer=Embedding, CrossEncoder=Cross))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=Tokenizer))
    model = LocalModels(tmp_path)
    monkeypatch.setattr(model, '_verified', lambda: dict(bundle))
    first = model.fingerprint
    if changed_during_initialization:
        with pytest.raises(EvidenceError) as caught:
            model._ensure()
        assert caught.value.code == 'MODEL_NOT_READY'
        assert not model._loaded and not hasattr(model, 'embedding')
        return

    model._ensure()
    assert model.fingerprint == first and model.ready
    bundle['version'] = 'second'
    with pytest.raises(EvidenceError) as caught:
        _ = model.fingerprint
    assert caught.value.code == 'MODEL_NOT_READY'
    assert not model.ready
    # Explicitly recreate the service/model instance; never silently relabel the
    # memory-resident first bundle with the second bundle's metadata.
    recreated = LocalModels(tmp_path)
    monkeypatch.setattr(recreated, '_verified', lambda: dict(bundle))
    recreated._ensure()
    assert recreated.ready and recreated.fingerprint != first
