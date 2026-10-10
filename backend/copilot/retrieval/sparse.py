"""Persisted BM25 with canonical ID/file checksums; filter before ranking."""
import hashlib
import json
from pathlib import Path

import bm25s
import numpy as np

from copilot.contracts import EvidenceError
from copilot.retrieval.dense import ids_checksum, scope_metadata


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
    scope = {"profile_id": chunks[0].profile_id, "corpus": chunks[0].corpus,
             "generation_id": directory.name}
    if chunks[0].corpus == "employer":
        scope.update(application_id=chunks[0].application_id,
                     research_run_id=chunks[0].research_run_id)
    (directory / "manifest.json").write_text(json.dumps({**scope, "ids": ids,
        "records": [c.record_id for c in chunks], "fingerprint": fingerprint,
        "ids_checksum": ids_checksum(ids), "files": files}, sort_keys=True))


def load(directory: Path, ids, fingerprint, *, scope=None, generation_id=None, records=None):
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if (manifest["ids"] != ids or manifest["fingerprint"] != fingerprint
                or manifest["ids_checksum"] != ids_checksum(ids)):
            raise ValueError("manifest mismatch")
        if scope is not None and any(manifest.get(k) != v
                for k, v in scope_metadata(scope, generation_id).items()):
            raise ValueError("scope mismatch")
        if records is not None and manifest["records"] != records:
            raise ValueError("canonical record mismatch")
        if len(manifest["records"]) != len(ids):
            raise ValueError("record association mismatch")
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
