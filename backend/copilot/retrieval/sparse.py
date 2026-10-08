"""Persisted BM25 with canonical ID/file checksums; filter before ranking."""
import hashlib
import json
from pathlib import Path

import bm25s
import numpy as np

from copilot.contracts import EvidenceError
from copilot.retrieval.dense import ids_checksum


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create(directory: Path, chunks, fingerprint):
    directory.mkdir(parents=True)
    ids = [c.id for c in chunks]
    engine = bm25s.BM25()
    engine.index(bm25s.tokenize([c.text for c in chunks], stopwords="en", show_progress=False),
                 show_progress=False)
    engine.save(str(directory))
    files = {p.name: file_hash(p) for p in directory.iterdir() if p.is_file()}
    (directory / "manifest.json").write_text(json.dumps({"ids": ids,
        "records": [c.record_id for c in chunks], "fingerprint": fingerprint,
        "ids_checksum": ids_checksum(ids), "files": files}, sort_keys=True))


def load(directory: Path, ids, fingerprint):
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if (manifest["ids"] != ids or manifest["fingerprint"] != fingerprint
                or manifest["ids_checksum"] != ids_checksum(ids)):
            raise ValueError("manifest mismatch")
        for filename, digest in manifest["files"].items():
            path = (directory / filename).resolve()
            if not path.is_relative_to(directory.resolve()) or file_hash(path) != digest:
                raise ValueError("file mismatch")
        return bm25s.BM25.load(str(directory), mmap=False), manifest
    except Exception as exc:
        raise EvidenceError("INDEX_NOT_READY", "Sparse index unavailable or corrupted; rebuild required", 503) from exc


def query(engine, manifest, text, limit, source_ids=None):
    tokens = bm25s.tokenize([text], stopwords="en", return_ids=False, show_progress=False)[0]
    scores = (np.asarray(engine.get_scores(tokens)) if tokens
              else np.zeros(len(manifest["ids"])))
    candidates = [(manifest["ids"][i], float(score)) for i, score in enumerate(scores)
                  if source_ids is None or manifest["records"][i] in source_ids]
    return [item[0] for item in sorted(candidates, key=lambda pair: (-pair[1], pair[0]))[:limit]]
