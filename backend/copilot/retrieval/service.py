"""Fail-closed snapshot publication and final canonical eligibility checks."""
import hashlib
import json
import math
import shutil
import uuid
from pathlib import Path

from copilot.contracts import Chunk, EvidenceError, Fact, Hit, Manifest
from copilot.retrieval import sparse
from copilot.retrieval.dense import ids_checksum

CHUNKER_VERSION = "canonical-offset-v1-220-30"


class EvidenceService:
    def __init__(self, store, index_root: Path, dense, models):
        self.store, self.index_root, self.dense, self.models = store, index_root, dense, models
        index_root.mkdir(parents=True, exist_ok=True)

    def _chunk(self, record):
        offsets = self.models.tokenize_offsets(record.text)
        result = []
        corpus = "facts" if isinstance(record, Fact) else "documents"
        for index in range(0, len(offsets), 190):
            start = offsets[index][0]
            end = offsets[min(index + 220, len(offsets)) - 1][1]
            text = record.text[start:end]
            digest = hashlib.sha256(text.encode()).hexdigest()
            unit_id = None if corpus == "facts" else record.id
            record_id = record.id if corpus == "facts" else record.source_id
            identity = [record.profile_id, corpus, record_id, unit_id, start, end,
                        digest, CHUNKER_VERSION]
            chunk_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            result.append(Chunk(chunk_id, record.profile_id, corpus, record_id, unit_id,
                                start, end, text, digest))
            if index + 220 >= len(offsets):
                break
        return result

    def build(self, profile_id, corpus):
        if corpus not in ("facts", "documents"):
            raise EvidenceError("INVALID_INPUT", "Choose facts or documents")
        snapshot = self.store.snapshot(profile_id, corpus, self._chunk)
        chunks = sorted(snapshot.chunks, key=lambda c: c.id)
        if len(chunks) > 1000:
            raise EvidenceError("LIMIT_EXCEEDED", "Milestone index limit is 1000 chunks")
        generation = str(uuid.uuid4())
        collection = "evidence_" + generation.replace("-", "")
        relpath = profile_id + "/" + generation
        directory = self.index_root / relpath
        manifest = Manifest(generation, profile_id, corpus, snapshot.revision,
                            self.models.fingerprint, CHUNKER_VERSION,
                            ids_checksum([c.id for c in chunks]), len(chunks), collection, relpath)
        try:
            if chunks:
                vectors = self.models.embed([c.text for c in chunks])
                self._validate_vectors(vectors, len(chunks))
                self.dense.create(collection, chunks, vectors, self.models.fingerprint,
                                  profile_id, generation)
                sparse.create(directory, chunks, self.models.fingerprint)
                self.dense.verify(collection, [c.id for c in chunks], self.models.fingerprint)
                sparse.load(directory, [c.id for c in chunks], self.models.fingerprint)
            else:
                directory.mkdir(parents=True)
                (directory / "empty.json").write_text(json.dumps({"generation": generation}))
            self.store.publish_manifest(manifest, expected_revision=snapshot.revision)
        except Exception as original:
            cleanup_failed = False
            # Independent attempts: an unavailable dense service must not leave
            # a removable sparse generation behind (or hide its own failure).
            try:
                self.dense.delete(collection)
            except Exception:
                cleanup_failed = True
            try:
                if directory.exists():
                    shutil.rmtree(directory)
                if directory.exists():
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            if cleanup_failed:
                # Unpublished UUID-named artifacts remain discoverable by
                # recover(); no manifest or successful index is advertised.
                raise EvidenceError("CLEANUP_PENDING",
                    "Index build was not published; staging cleanup is pending. Restart recovery required.",
                    503) from original
            raise
        return manifest

    @staticmethod
    def _validate_vectors(vectors, count):
        if (len(vectors) != count or any(len(v) != 384 for v in vectors)
                or any(not math.isfinite(float(x)) for v in vectors for x in v)):
            raise EvidenceError("MODEL_NOT_READY", "Embedding output failed validation", 503)

    def search(self, profile_id, corpus, query, source_ids=None, limit=8):
        if corpus not in ("facts", "documents") or not isinstance(query, str) or not query.strip():
            raise EvidenceError("INVALID_INPUT", "Choose a corpus and enter a query")
        if not 1 <= limit <= 8:
            raise EvidenceError("INVALID_INPUT", "Result limit must be 1 to 8")
        if corpus == "documents":
            if not source_ids:
                raise EvidenceError("INVALID_INPUT", "Select at least one source document")
            available = {s.id for s in self.store.list_sources(profile_id) if s.state == "active"}
            if not set(source_ids) <= available:
                raise EvidenceError("NOT_FOUND", "Source not found", 404)
        elif source_ids is not None:
            raise EvidenceError("INVALID_INPUT", "Fact search does not take source selection")
        manifest = self.store.active_manifest(profile_id, corpus)
        if manifest is None:
            raise EvidenceError("INDEX_NOT_READY", "Build the selected corpus index first", 503)
        self.store.validate_snapshot(profile_id, manifest.revision)
        if (manifest.model_fingerprint != self.models.fingerprint
                or manifest.chunker_version != CHUNKER_VERSION):
            raise EvidenceError("INDEX_NOT_READY", "Model or chunker changed; rebuild required", 503)
        chunks = self.store.eligible_chunks(profile_id, corpus, self._ids(manifest), None)
        chunks = sorted(chunks, key=lambda c: c.id)
        ids = [c.id for c in chunks]
        if len(ids) != manifest.chunk_count or ids_checksum(ids) != manifest.chunk_ids_sha256:
            raise EvidenceError("INDEX_NOT_READY", "Canonical index snapshot mismatch", 503)
        if not chunks:
            self.store.validate_snapshot(profile_id, manifest.revision)
            return []
        self.dense.verify(manifest.dense_collection, ids, self.models.fingerprint)
        engine, meta = sparse.load(self.index_root / manifest.sparse_relpath, ids,
                                   self.models.fingerprint)
        offsets = self.models.tokenize_offsets(query)
        if len(offsets) > 128:
            # Reject instead of silently truncate: response contract has no truncation field.
            raise EvidenceError("LIMIT_EXCEEDED", "Query exceeds 128 tokens; shorten it")
        vector = self.models.embed([query])
        self._validate_vectors(vector, 1)
        allowed = [c for c in chunks if source_ids is None or c.record_id in source_ids]
        if not allowed:
            self.store.validate_snapshot(profile_id, manifest.revision)
            return []
        dense_ids = self.dense.query(manifest.dense_collection, vector[0], min(30, len(allowed)), source_ids)
        sparse_ids = sparse.query(engine, meta, query, 30, source_ids)
        allowed_ids = {c.id for c in allowed}
        if not set(dense_ids + sparse_ids) <= allowed_ids:
            raise EvidenceError("INDEX_NOT_READY", "Index returned ineligible evidence", 503)
        dr = {key: rank for rank, key in enumerate(dense_ids, 1)}
        br = {key: rank for rank, key in enumerate(sparse_ids, 1)}
        scores = {key: (1 / (60 + dr[key]) if key in dr else 0)
                       + (1 / (60 + br[key]) if key in br else 0) for key in set(dr) | set(br)}
        fused = sorted(scores, key=lambda key: (-scores[key], key))[:40]
        mapping = {c.id: c for c in chunks}
        rerank_scores = self.models.rerank(query, [mapping[key].text for key in fused])
        if len(rerank_scores) != len(fused) or any(not math.isfinite(v) for v in rerank_scores):
            raise EvidenceError("MODEL_NOT_READY", "Reranker output failed validation", 503)
        ranked = sorted(zip(fused, rerank_scores, strict=True), key=lambda item: (-item[1], item[0]))[:limit]
        final_ids = [key for key, _ in ranked]
        final = self.store.eligible_chunks(profile_id, corpus, final_ids, source_ids)
        if {c.id for c in final} != set(final_ids):
            raise EvidenceError("CONFLICT", "Evidence changed during search; rebuild required", 409)
        hits = [Hit(mapping[key], self.store.citation_for_chunk(profile_id, mapping[key]),
                    dr.get(key), br.get(key), scores[key], score) for key, score in ranked]
        self.store.validate_snapshot(profile_id, manifest.revision)
        return hits

    def _ids(self, manifest):
        directory = self.index_root / manifest.sparse_relpath
        if manifest.chunk_count == 0:
            if not (directory / "empty.json").is_file():
                raise EvidenceError("INDEX_NOT_READY", "Empty manifest missing; rebuild required", 503)
            return []
        try:
            return json.loads((directory / "manifest.json").read_text())["ids"]
        except (OSError, ValueError, KeyError) as exc:
            raise EvidenceError("INDEX_NOT_READY", "Sparse manifest missing; rebuild required", 503) from exc

    def cleanup(self, ticket):
        for name in self.dense.owned_names(ticket.profile_id):
            self.dense.delete(name)
        directory = self.index_root / ticket.profile_id
        if directory.exists():
            shutil.rmtree(directory)
        if directory.exists() or self.dense.owned_names(ticket.profile_id):
            raise EvidenceError("CLEANUP_PENDING", "External index cleanup is pending", 503)
        self.store.complete_cleanup(ticket.id)

    def recover(self):
        for ticket in self.store.pending_cleanup():
            self.cleanup(ticket)
        active_names, active_paths = set(), set()
        for profile in self.store.list_profiles():
            for corpus in ("facts", "documents"):
                manifest = self.store.active_manifest(profile.id, corpus)
                if manifest:
                    active_names.add(manifest.dense_collection)
                    active_paths.add((self.index_root / manifest.sparse_relpath).resolve())
        for name in self.dense.names():
            if name.startswith("evidence_") and name not in active_names:
                self.dense.delete(name)
        for profile_path in self.index_root.iterdir():
            if profile_path.is_dir():
                for generation_path in profile_path.iterdir():
                    if generation_path.is_dir() and generation_path.resolve() not in active_paths:
                        shutil.rmtree(generation_path)
