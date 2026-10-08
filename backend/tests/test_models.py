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
