"""Explicit model provisioning; normal loading is strictly local and hash checked."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from copilot.contracts import EvidenceError

LOCK = Path(__file__).resolve().parents[1] / "models.lock.json"
COMMON_FILES = {"config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"}
EMBEDDING_FILES = COMMON_FILES | {"modules.json", "config_sentence_transformers.json", "sentence_bert_config.json", "1_Pooling/config.json"}
ALLOWED = sorted(EMBEDDING_FILES)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_directory(data_dir: Path) -> Path:
    return Path(os.environ.get("COPILOT_MODEL_DIR", str(data_dir / "models")))


def setup(directory: Path) -> None:
    from huggingface_hub import snapshot_download

    spec = json.loads(LOCK.read_text())
    directory.mkdir(parents=True, exist_ok=True)
    records = {}
    for kind in ("embedding", "reranker"):
        entry = spec[kind]
        target = directory / kind
        snapshot_download(entry["repo"], revision=entry["revision"], local_dir=target,
                          allow_patterns=ALLOWED)
        files = sorted(p for p in target.rglob("*") if p.is_file() and str(p.relative_to(target)) in ALLOWED)
        if not any(p.suffix == ".safetensors" for p in files):
            raise EvidenceError("MODEL_NOT_READY", "Pinned safetensors model is unavailable", 503)
        records[kind] = {str(p.relative_to(target)): sha(p) for p in files}
        required = EMBEDDING_FILES if kind == "embedding" else COMMON_FILES
        if not required <= set(records[kind]):
            raise EvidenceError("MODEL_NOT_READY", "Pinned required model files are missing", 503)
    manifest = {"spec": spec, "files": records}
    tmp = directory / "verified.json.tmp"
    tmp.write_text(json.dumps(manifest, sort_keys=True))
    tmp.replace(directory / "verified.json")


class LocalModels:
    def __init__(self, directory: Path):
        self.directory = directory
        self._loaded = False

    def _verified(self):
        directory = self.directory
        try:
            manifest = json.loads((directory / "verified.json").read_text())
            if manifest["spec"] != json.loads(LOCK.read_text()):
                raise ValueError("pin mismatch")
            for kind, records in manifest["files"].items():
                required = EMBEDDING_FILES if kind == "embedding" else COMMON_FILES
                if kind not in ("embedding", "reranker") or not required <= set(records) or not set(records) <= set(ALLOWED):
                    raise ValueError("missing files")
                for relpath, expected in records.items():
                    path = (directory / kind / relpath).resolve()
                    if not path.is_relative_to((directory / kind).resolve()) or sha(path) != expected:
                        raise ValueError("file integrity mismatch")
            if set(manifest["files"]) != {"embedding", "reranker"}:
                raise ValueError("missing model")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise EvidenceError("MODEL_NOT_READY", "Run explicit model setup; local files failed verification", 503) from exc
        return manifest

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self._verified(), sort_keys=True).encode()).hexdigest()

    @property
    def ready(self):
        try:
            self._verified()
            return True
        except EvidenceError:
            return False

    def _ensure(self):
        if self._loaded:
            return
        self._verified()
        directory = self.directory
        import torch
        torch.set_num_threads(2)
        from sentence_transformers import CrossEncoder, SentenceTransformer
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(str(directory / "embedding"),
                                                      local_files_only=True, trust_remote_code=False)
        self.embedding = SentenceTransformer(str(directory / "embedding"), device="cpu",
                                            local_files_only=True, trust_remote_code=False,
                                            model_kwargs={"use_safetensors": True})
        self.embedding.max_seq_length = 256
        self.cross = CrossEncoder(str(directory / "reranker"), device="cpu", max_length=512,
                                  local_files_only=True, trust_remote_code=False,
                                  model_kwargs={"use_safetensors": True})
        if self.embedding.get_sentence_embedding_dimension() != 384:
            raise EvidenceError("MODEL_NOT_READY", "Unexpected embedding dimension", 503)
        self._loaded = True

    def tokenize_offsets(self, text: str) -> list[tuple[int, int]]:
        self._ensure()
        result = self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                                truncation=False)
        return [(int(a), int(b)) for a, b in result["offset_mapping"] if b > a]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        if any(len(self.tokenize_offsets(text)) + 2 > 256 for text in texts):
            raise EvidenceError("LIMIT_EXCEEDED", "Embedding text exceeds pinned token limit")
        return self.embedding.encode(texts, normalize_embeddings=True, show_progress_bar=False,
                                     batch_size=16).tolist()

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        self._ensure()
        if any(len(self.cross.tokenizer(query, text, truncation=False)["input_ids"]) > 512 for text in texts):
            raise EvidenceError("LIMIT_EXCEEDED", "Reranking pair exceeds 512 tokens; shorten query")
        return [float(v) for v in self.cross.predict([(query, text) for text in texts],
                                                    show_progress_bar=False, batch_size=8)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit pinned public model download")
    parser.add_argument("command", choices=["setup"])
    from copilot.config import Settings
    parser.add_argument("--directory", type=Path, default=Settings.from_env().model_dir)
    args = parser.parse_args()
    setup(args.directory)
    print("Pinned local models provisioned and file hashes recorded.")


if __name__ == "__main__":
    main()
