"""Local configuration and deterministic cross-process lock exclusion."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from copilot.config import Settings, data_lock


def test_settings_defaults_are_local_and_do_not_read_legacy_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("COPILOT_"):
            monkeypatch.delenv(name)
    (tmp_path / ".env").write_text("COPILOT_PORT=9999\nCOPILOT_DATA_DIR=private-legacy\n")
    settings = Settings.from_env()
    assert settings.port == 3001 and settings.chroma_host == "127.0.0.1"
    assert settings.data_dir == tmp_path / "evidence-data"
    assert settings.model_dir == settings.data_dir / "models"
    assert not settings.data_dir.exists()


def test_settings_use_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path / "chosen"))
    monkeypatch.setenv("COPILOT_MODEL_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("COPILOT_CHROMA_HOST", "chroma")
    monkeypatch.setenv("COPILOT_CHROMA_PORT", "8001")
    monkeypatch.setenv("COPILOT_PORT", "3002")
    assert Settings.from_env() == Settings(tmp_path / "chosen", tmp_path / "cache", "chroma", 8001, 3002)


def test_lock_timeout_excludes_other_process_and_release_allows_reacquisition(tmp_path):
    script = """
import sys
from pathlib import Path
from copilot.config import data_lock
from copilot.contracts import EvidenceError
try:
    with data_lock(Path(sys.argv[1]), timeout=0.15):
        print('acquired')
except EvidenceError as error:
    print(error.code)
"""
    command = [sys.executable, "-c", script, str(tmp_path)]
    with data_lock(tmp_path):
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0 and result.stdout.strip() == "CONFLICT"
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "acquired"


def test_lock_released_after_exception(tmp_path):
    with pytest.raises(RuntimeError), data_lock(tmp_path):
        raise RuntimeError("synthetic")
    with data_lock(tmp_path, timeout=0):
        pass
