"""Explicit local paths; never reads the legacy app's .env or .data."""
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .contracts import EvidenceError


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_dir: Path
    chroma_host: str = "127.0.0.1"
    chroma_port: int = 8000
    port: int = 3001

    @classmethod
    def from_env(cls):
        data = Path(os.environ.get("COPILOT_DATA_DIR", "evidence-data")).resolve()
        return cls(data, Path(os.environ.get("COPILOT_MODEL_DIR", str(data / "models"))).resolve(),
                   os.environ.get("COPILOT_CHROMA_HOST", "127.0.0.1"),
                   int(os.environ.get("COPILOT_CHROMA_PORT", "8000")),
                   int(os.environ.get("COPILOT_PORT", "3001")))


@contextmanager
def data_lock(data_dir: Path, timeout: float = 5):
    """One local API process and CLI build serialize access across processes."""
    import fcntl

    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".evidence.lock").open("a") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EvidenceError("CONFLICT", "Evidence is busy indexing. Retry shortly.", 409) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
