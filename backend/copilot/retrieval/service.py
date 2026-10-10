"""Fail-closed snapshot publication and final canonical eligibility checks."""
import hashlib
import json
import math
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from copilot.contracts import (
    Chunk,
    EmployerChunk,
    EmployerManifest,
    EmployerUnit,
    EvidenceError,
    Fact,
    Hit,
    Manifest,
    decode_chunk,
    decode_manifest,
)
from copilot.domain.contracts import EvidenceReference, SourceSpan
from copilot.generations import Generations, new_generation
from copilot.jobs import Jobs
from copilot.retrieval import sparse
from copilot.retrieval.dense import ids_checksum
from copilot.retrieval.packet_contracts import CorpusScope, GenerationBinding
from copilot.store import identifier

CHUNKER_VERSION = "canonical-offset-v1-220-30"


@dataclass(frozen=True)
class ScopedSearchResult:
    binding: GenerationBinding
    hits: tuple[dict, ...]
    references: tuple[EvidenceReference, ...]


class EvidenceService:
    def __init__(self, store, index_root: Path, dense, models):
        self.store, self.index_root, self.dense, self.models = store, index_root, dense, models
        index_root.mkdir(parents=True, exist_ok=True)
        self._index_anchor = index_root.resolve()
        self.jobs = Jobs(store)
        self.generations = Generations(store, self.jobs)

    def _chunk(self, record):
        offsets = self.models.tokenize_offsets(record.text)
        result = []
        corpus = "facts" if isinstance(record, Fact) else "employer" if isinstance(record, EmployerUnit) else "documents"
        scoped = dict(application_id=record.application_id, research_run_id=record.research_run_id) if corpus == "employer" else {}
        for index in range(0, len(offsets), 190):
            start = offsets[index][0]
            end = offsets[min(index + 220, len(offsets)) - 1][1]
            text = record.text[start:end]
            digest = hashlib.sha256(text.encode()).hexdigest()
            unit_id = None if corpus == "facts" else record.id
            record_id = record.id if corpus == "facts" else record.source_id
            identity = [record.profile_id, corpus, record_id, unit_id, start, end,
                        digest, CHUNKER_VERSION, *scoped.values()]
            chunk_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            projection = EmployerChunk if corpus == "employer" else Chunk
            result.append(projection(chunk_id, record.profile_id, corpus, record_id, unit_id,
                                     start, end, text, digest, **scoped))
            if index + 220 >= len(offsets):
                break
        return result

    def build(self, profile_id, corpus):
        job = self.jobs.enqueue(profile_id, 'index', {'corpus': corpus}, str(uuid.uuid4()))
        worker_id = str(uuid.uuid4())
        leased = self.jobs.claim(worker_id, lease_seconds=300, job_id=job.id)
        if leased is None:
            raise EvidenceError('CONFLICT', 'Index job could not be claimed', 409)
        return self.run_index(leased.id, worker_id, leased.fence)

    def run_index(self, job_id, worker_id, fence):
        generation_id = new_generation()
        registered = False
        try:
            with self.generations.producer(generation_id):
                # Fingerprint verification may touch model files, never a writer transaction.
                fingerprint = self.models.fingerprint
                intent, values = self.generations.register(job_id, worker_id, fence,
                    generation_id, fingerprint, CHUNKER_VERSION)
                registered = True
                snapshot = self.store.chunk_capture(intent.profile_id, intent.corpus,
                    self.generations.revision(intent), values, self._chunk)
                self.generations.stage(generation_id, worker_id, fence, snapshot)
                chunks = list(snapshot.chunks)
                projection = EmployerManifest if intent.corpus == "employer" else Manifest
                scoped = dict(application_id=intent.application_id, research_run_id=intent.research_run_id) if intent.corpus == "employer" else {}
                manifest = projection(generation_id, intent.profile_id, intent.corpus, snapshot.revision,
                    fingerprint, CHUNKER_VERSION, ids_checksum([c.id for c in chunks]),
                    len(chunks), intent.dense_collection, intent.sparse_relpath, **scoped)
                directory = self._generation_path(intent)
                if chunks:
                    vectors = self.models.embed([c.text for c in chunks])
                    self._validate_vectors(vectors, len(chunks))
                    if self.models.fingerprint != fingerprint:
                        raise EvidenceError("MODEL_NOT_READY", "Model changed during indexing", 503)
                    self.generations.guard(generation_id, worker_id, fence)
                    self.generations.begin_dense(generation_id, worker_id, fence)
                    try:
                        self.dense.create(intent.dense_collection, chunks, vectors, fingerprint,
                                          intent.profile_id, generation_id)
                    except BaseException:
                        self.generations.dense_result(generation_id, fence, False)
                        raise
                    self.generations.dense_result(generation_id, fence, True)
                    self.generations.guard(generation_id, worker_id, fence)
                    sparse.create(directory, chunks, fingerprint)
                    if intent.corpus == "employer":
                        scope = self.generations.scope(intent)
                        self.dense.verify(intent.dense_collection, [c.id for c in chunks], fingerprint,
                                          scope=scope, generation_id=generation_id,
                                          records={c.id: c.record_id for c in chunks})
                        sparse.load(directory, [c.id for c in chunks], fingerprint,
                                    scope=scope, generation_id=generation_id,
                                    records=[c.record_id for c in chunks])
                    else:
                        self.dense.verify(intent.dense_collection, [c.id for c in chunks], fingerprint)
                        sparse.load(directory, [c.id for c in chunks], fingerprint)
                else:
                    self.generations.guard(generation_id, worker_id, fence)
                    directory.mkdir()
                    (directory / 'empty.json').write_text(json.dumps({'generation': generation_id}))
                if self.models.fingerprint != fingerprint:
                    raise EvidenceError("MODEL_NOT_READY", "Model changed during indexing", 503)
                return self.generations.publish(generation_id, worker_id, fence, manifest)
        except Exception as original:
            try:
                self.jobs.finish(job_id, worker_id, fence, 'failed')
            except EvidenceError:
                pass  # Lost authority must never change the newer producer's job.
            if registered:
                self.generations.abandon(generation_id)
                try:
                    self.cleanup_generation(generation_id)
                except Exception:
                    raise EvidenceError('CLEANUP_PENDING',
                        'Index was not published; registered generation cleanup is pending', 503) from original
            raise

    def _anchored_path(self, relative, expected, code='CLEANUP_PENDING'):
        relative, expected = Path(relative), Path(expected)
        directory = self.index_root / relative
        if (relative != expected or relative.is_absolute() or self.index_root.is_symlink()
                or self.index_root.resolve() != self._index_anchor
                or directory.is_symlink() or directory.parent.is_symlink()
                or directory.resolve() != self._index_anchor / expected):
            raise EvidenceError(code, 'Generation path ownership is unsafe', 503)
        return directory

    def _generation_path(self, intent):
        # New intents use one UUID token; legacy manifests have a separate reader.
        return self._anchored_path(intent.sparse_relpath, intent.generation_id)

    def cleanup_generation(self, generation_id):
        intent = next((i for i in self.generations.list() if i.generation_id == generation_id), None)
        if intent is None:
            raise EvidenceError('CLEANUP_PENDING', 'Generation ownership is unavailable', 503)
        with self.generations.producer(generation_id):
            # Lock is quiescence evidence, not authority; current durable inputs decide eligibility.
            with self.store._tx() as db:
                row = db.execute('SELECT state FROM manifests WHERE id=? AND owner=?',
                                 (generation_id, intent.profile_id)).fetchone()
                profile = db.execute('SELECT 1 FROM profiles WHERE id=?', (intent.profile_id,)).fetchone()
                if row and row[0] == 'active' and profile:
                    try:
                        revision = self.store.scope_revision(db, self.generations.scope(intent), self.jobs._now())
                    except EvidenceError as exc:
                        if exc.code not in ('CONFLICT', 'NOT_FOUND'):
                            raise
                    else:
                        if revision == self.generations.revision(intent):
                            return False
            if self.generations.live(intent):
                return False
            directory = self._generation_path(intent)
            expected = 'evidence_' + generation_id.replace('-', '')
            if intent.dense_collection != expected:
                raise EvidenceError('CLEANUP_PENDING', 'Generation dense ownership is unsafe', 503)
            if expected in self.dense.names():
                metadata = self.dense.get(expected).metadata or {}
                if (metadata.get('profile_id') != intent.profile_id
                        or metadata.get('generation_id') != generation_id):
                    raise EvidenceError('CLEANUP_PENDING', 'Generation dense ownership is unsafe', 503)
            failure = None
            try:
                self.dense.delete(expected)
            except Exception as exc:
                failure = exc
            try:
                if directory.exists():
                    shutil.rmtree(directory)
            except Exception as exc:
                failure = exc
            if failure or directory.exists() or expected in self.dense.names():
                raise EvidenceError('CLEANUP_PENDING', 'Generation cleanup is not verified', 503) from failure
            if self.generations.dense_pending(generation_id):
                self.generations.abandon(generation_id)
                raise EvidenceError('CLEANUP_PENDING', 'External dense write completion remains unknown', 503)
            self.generations.cleaned(generation_id)
            return True

    @staticmethod
    def _validate_vectors(vectors, count):
        if (len(vectors) != count or any(len(v) != 384 for v in vectors)
                or any(not math.isfinite(float(x)) for v in vectors for x in v)):
            raise EvidenceError("MODEL_NOT_READY", "Embedding output failed validation", 503)

    def search_scoped(self, scope, query_variants, *, binding=None, source_ids=None, limit=8):
        """One canonical generation for every bounded variant, never cross-scope ranking."""
        if not isinstance(scope, CorpusScope) or scope.corpus not in ('facts', 'employer'):
            raise EvidenceError('INVALID_INPUT', 'Scoped retrieval requires facts or employer scope')
        if (not isinstance(query_variants, (list, tuple)) or not 1 <= len(query_variants) <= 4
                or any(not isinstance(q, str) or not q.strip() for q in query_variants)
                or type(limit) is not int or not 1 <= limit <= 8 or source_ids is not None):
            raise EvidenceError('INVALID_INPUT', 'Provide 1 to 4 queries and a result limit of 1 to 8')
        # Freeze caller-owned input before any blocking tokenizer/model work.
        variants = tuple(query_variants)
        for query in variants:
            if len(self.models.tokenize_offsets(query)) > 128:
                raise EvidenceError('LIMIT_EXCEEDED', 'Query exceeds 128 tokens; shorten it')
        fingerprint = self.models.fingerprint
        with self.store._read() as db:
            manifest = self.store.active_scoped_manifest(db, scope, self.jobs._now())
            if manifest is None:
                raise EvidenceError('INDEX_NOT_READY', 'Build the selected corpus index first', 503)
            captured = GenerationBinding(generation_id=manifest.generation_id, scope=scope,
                revision=manifest.revision, model_fingerprint=manifest.model_fingerprint,
                chunker_version=manifest.chunker_version, chunk_checksum=manifest.chunk_ids_sha256)
            if binding is not None and binding != captured:
                raise EvidenceError('CONFLICT', 'Requested generation is no longer current', 409)
            self.store.require_generation_binding(db, captured, self.jobs._now())
            revision, values = self.store.capture_scoped_sources(db, scope, self.jobs._now())
            rows = db.execute('SELECT data FROM generation_chunks WHERE generation_id=? AND owner=? ORDER BY id',
                              (captured.generation_id, scope.profile_id)).fetchall()
            if not rows and scope.corpus == 'facts' and not db.execute(
                    'SELECT 1 FROM generation_intents WHERE id=?', (captured.generation_id,)).fetchone():
                rows = db.execute("SELECT data FROM records WHERE owner=? AND kind='chunk:facts' ORDER BY id",
                                  (scope.profile_id,)).fetchall()
            chunks = [decode_chunk(json.loads(row[0])) for row in rows]
        self.store.verify_scoped_sources(scope, values)
        if fingerprint != manifest.model_fingerprint or manifest.chunker_version != CHUNKER_VERSION:
            raise EvidenceError('INDEX_NOT_READY', 'Model or chunker changed; rebuild required', 503)
        canonical = {(v['unit']['source_id'], v['unit']['id']): v['unit'] for v in values} if scope.corpus == 'employer' else {(v['id'], None): v for v in values}
        ids = [c.id for c in chunks]
        if len(ids) != manifest.chunk_count or ids_checksum(ids) != manifest.chunk_ids_sha256:
            raise EvidenceError('INDEX_NOT_READY', 'Canonical index snapshot mismatch', 503)
        for chunk in chunks:
            record = canonical.get((chunk.record_id, chunk.unit_id))
            if (record is None or chunk.profile_id != scope.profile_id or chunk.corpus != scope.corpus
                    or (scope.corpus == 'employer' and (chunk.application_id, chunk.research_run_id)
                        != (scope.application_id, scope.research_run_id))
                    or not 0 <= chunk.start < chunk.end <= len(record['text'])
                    or chunk.text != record['text'][chunk.start:chunk.end]
                    or hashlib.sha256(chunk.text.encode()).hexdigest() != chunk.text_sha256):
                raise EvidenceError('INDEX_NOT_READY', 'Canonical chunk scope/integrity mismatch', 503)
        directory = self._manifest_path(manifest)
        scores, dense_ranks, sparse_ranks = {}, {}, {}
        if chunks:
            # Verify BOTH complete branch identities before either branch may rank.
            self.dense.verify(manifest.dense_collection, ids, fingerprint,
                              scope=scope, generation_id=captured.generation_id,
                              records={c.id: c.record_id for c in chunks})
            engine, meta = sparse.load(directory, ids, fingerprint,
                                      scope=scope, generation_id=captured.generation_id,
                                      records=[c.record_id for c in chunks])
            vectors = self.models.embed(list(variants))
            self._validate_vectors(vectors, len(variants))
            self.store.verify_scoped_sources(scope, values)
            for query, vector in zip(variants, vectors, strict=True):
                dense_ids = self.dense.query(manifest.dense_collection, vector, min(30, len(ids)),
                                            scope=scope, generation_id=captured.generation_id)
                sparse_ids = sparse.query(engine, meta, query, 30)
                if not set(dense_ids + sparse_ids) <= set(ids):
                    raise EvidenceError('INDEX_NOT_READY', 'Index returned ineligible evidence', 503)
                for branch, ranks in ((dense_ids, dense_ranks), (sparse_ids, sparse_ranks)):
                    for rank, key in enumerate(branch, 1):
                        ranks[key] = min(ranks.get(key, rank), rank)
                        scores[key] = scores.get(key, 0.) + 1 / (60 + rank)
        else:
            self._ids(manifest)  # Empty generations still require their registered artifact.
        mapping = {c.id: c for c in chunks}
        fused = sorted(scores, key=lambda key: (-scores[key], key))[:40]
        rerank = {key: 0. for key in fused}
        for query in variants:
            if not fused:
                break
            result = self.models.rerank(query, [mapping[key].text for key in fused])
            if len(result) != len(fused) or any(not math.isfinite(float(v)) for v in result):
                raise EvidenceError('MODEL_NOT_READY', 'Reranker output failed validation', 503)
            for key, score in zip(fused, result, strict=True):
                rerank[key] += float(score) / len(variants)
        ranked = sorted(fused, key=lambda key: (-rerank[key], -scores[key], key))[:limit]
        references = []
        hits = []
        for key in ranked:
            chunk = mapping[key]
            reference = EvidenceReference(profile_id=scope.profile_id,
                kind='confirmed_fact' if scope.corpus == 'facts' else 'employer',
                application_id=scope.application_id, research_run_id=scope.research_run_id,
                generation_id=captured.generation_id,
                span=SourceSpan(record_id=chunk.record_id, unit_id=chunk.unit_id,
                    start=chunk.start, end=chunk.end, excerpt=chunk.text,
                    text_sha256=canonical[(chunk.record_id, chunk.unit_id)]['text_sha256']))
            references.append(reference)
            hits.append({'chunk': chunk, 'reference': reference, 'dense_rank': dense_ranks.get(key),
                         'bm25_rank': sparse_ranks.get(key), 'rrf_score': scores[key],
                         'rerank_score': rerank[key]})
        # IO receipt verification is outside the final writer; equality recapture,
        # live eligibility and reference resolution are one final atomic check.
        if chunks:
            self.dense.verify(manifest.dense_collection, ids, fingerprint,
                              scope=scope, generation_id=captured.generation_id,
                              records={c.id: c.record_id for c in chunks})
            sparse.load(directory, ids, fingerprint, scope=scope, generation_id=captured.generation_id,
                        records=[c.record_id for c in chunks])
        self.store.verify_scoped_sources(scope, values)
        if self.models.fingerprint != fingerprint:
            raise EvidenceError('INDEX_NOT_READY', 'Model changed during retrieval', 503)
        with self.store._tx() as db:
            now = self.jobs._now()
            self.store.require_generation_binding(db, captured, now)
            current_revision, current_values = self.store.capture_scoped_sources(db, scope, now)
            if current_revision != revision or current_values != values:
                raise EvidenceError('CONFLICT', 'Canonical sources changed during retrieval', 409)
            for reference in references:
                self.store.resolve_evidence_reference(db, captured, reference, now)
        return ScopedSearchResult(captured, tuple(hits), tuple(references))

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
        self.store.validate_snapshot(profile_id, manifest.revision, corpus)
        if (manifest.model_fingerprint != self.models.fingerprint
                or manifest.chunker_version != CHUNKER_VERSION):
            raise EvidenceError("INDEX_NOT_READY", "Model or chunker changed; rebuild required", 503)
        chunks = self.store.eligible_chunks(profile_id, corpus, self._ids(manifest), None, generation_id=manifest.generation_id)
        chunks = sorted(chunks, key=lambda c: c.id)
        ids = [c.id for c in chunks]
        if len(ids) != manifest.chunk_count or ids_checksum(ids) != manifest.chunk_ids_sha256:
            raise EvidenceError("INDEX_NOT_READY", "Canonical index snapshot mismatch", 503)
        if not chunks:
            self.store.validate_snapshot(profile_id, manifest.revision, corpus)
            return []
        self.dense.verify(manifest.dense_collection, ids, self.models.fingerprint)
        engine, meta = sparse.load(self._manifest_path(manifest), ids,
                                   self.models.fingerprint)
        offsets = self.models.tokenize_offsets(query)
        if len(offsets) > 128:
            # Reject instead of silently truncate: response contract has no truncation field.
            raise EvidenceError("LIMIT_EXCEEDED", "Query exceeds 128 tokens; shorten it")
        vector = self.models.embed([query])
        self._validate_vectors(vector, 1)
        allowed = [c for c in chunks if source_ids is None or c.record_id in source_ids]
        if not allowed:
            self.store.validate_snapshot(profile_id, manifest.revision, corpus)
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
        final = self.store.eligible_chunks(profile_id, corpus, final_ids, source_ids, generation_id=manifest.generation_id)
        if {c.id for c in final} != set(final_ids):
            raise EvidenceError("CONFLICT", "Evidence changed during search; rebuild required", 409)
        hits = [Hit(mapping[key], self.store.citation_for_chunk(profile_id, mapping[key]),
                    dr.get(key), br.get(key), scores[key], score) for key, score in ranked]
        self.store.validate_snapshot(profile_id, manifest.revision, corpus)
        return hits

    def _manifest_path(self, manifest):
        identifier(manifest.generation_id)
        identifier(manifest.profile_id)
        with self.store._tx() as db:
            row = db.execute('SELECT data FROM generation_intents WHERE id=?', (manifest.generation_id,)).fetchone()
            if row:
                intent, _ = self.generations._load(db, manifest.generation_id)
                if (intent.profile_id != manifest.profile_id or intent.corpus != manifest.corpus
                        or intent.state != 'published' or intent.sparse_relpath != manifest.sparse_relpath
                        or intent.dense_collection != manifest.dense_collection):
                    raise EvidenceError('INDEX_NOT_READY', 'Generation read scope mismatch', 503)
            else:
                intent = None
        if intent is not None:
            return self._generation_path(intent)
        if manifest.corpus == "employer":
            raise EvidenceError("INDEX_NOT_READY", "Employer registration is unavailable", 503)
        # Version-1 manifests predate intents; recognize only their original nested path.
        expected = Path(manifest.profile_id) / manifest.generation_id
        return self._anchored_path(manifest.sparse_relpath, expected, 'INDEX_NOT_READY')

    def _ids(self, manifest):
        directory = self._manifest_path(manifest)
        if manifest.chunk_count == 0:
            try:
                if json.loads((directory / "empty.json").read_text()) != {'generation': manifest.generation_id}:
                    raise ValueError('empty generation mismatch')
            except (OSError, ValueError) as exc:
                raise EvidenceError("INDEX_NOT_READY", "Empty manifest missing or mismatched; rebuild required", 503) from exc
            return []
        try:
            return json.loads((directory / "manifest.json").read_text())["ids"]
        except (OSError, ValueError, KeyError) as exc:
            raise EvidenceError("INDEX_NOT_READY", "Sparse manifest missing; rebuild required", 503) from exc

    def cleanup(self, ticket):
        # Tickets name captured generations, never an owner's current entire tree.
        intents = {i.generation_id: i for i in self.generations.list()}
        for generation_id in ticket.generation_ids:
            if generation_id in intents:
                if not self.cleanup_generation(generation_id):
                    raise EvidenceError('CLEANUP_PENDING', 'Generation remains eligible or live', 503)
        for manifest in self.store.cleanup_manifests(ticket):
            if manifest.generation_id in intents:
                continue
            if manifest.corpus == 'employer':
                raise EvidenceError('CLEANUP_PENDING', 'Employer generation registration is unavailable', 503)
            expected_name = 'evidence_' + manifest.generation_id.replace('-', '')
            relative = Path(manifest.sparse_relpath)
            expected_path = Path(ticket.profile_id) / manifest.generation_id
            if (manifest.dense_collection != expected_name or relative != expected_path
                    or relative.is_absolute()):
                raise EvidenceError('CLEANUP_PENDING', 'Captured index ownership could not be verified', 503)
            directory = self._anchored_path(relative, expected_path)
            if manifest.dense_collection in self.dense.names():
                metadata = self.dense.get(manifest.dense_collection).metadata or {}
                if (metadata.get('profile_id') != ticket.profile_id
                        or metadata.get('generation_id') != manifest.generation_id):
                    raise EvidenceError('CLEANUP_PENDING', 'Captured dense ownership could not be verified', 503)
            self.dense.delete(manifest.dense_collection)
            if directory.exists():
                shutil.rmtree(directory)
            if directory.exists() or manifest.dense_collection in self.dense.names():
                raise EvidenceError('CLEANUP_PENDING', 'Captured index cleanup is pending', 503)
        self.store.complete_cleanup(ticket.id)

    def recover(self):
        pending = 0
        for ticket in self.store.pending_cleanup():
            try:
                self.cleanup(ticket)
            except EvidenceError as exc:
                if exc.code != 'CLEANUP_PENDING':
                    raise
                pending += 1
        intents = self.generations.list()
        for intent in intents:
            if self.generations.live(intent):
                continue
            if intent.state in ('registered', 'building', 'ready'):
                self.generations.abandon(intent.generation_id)
            try:
                self.cleanup_generation(intent.generation_id)
            except EvidenceError as exc:
                if exc.code != 'CLEANUP_PENDING':
                    raise
                pending += 1
        # Unknown/unregistered artifacts have no proven deletion authority.
        with self.store._tx() as db:
            manifests = [decode_manifest(json.loads(row[0])) for row in db.execute('SELECT data FROM manifests')]
        known_names = {i.dense_collection for i in intents} | {m.dense_collection for m in manifests}
        known_paths = {i.sparse_relpath for i in intents} | {m.sparse_relpath for m in manifests}
        unknown_names = sum(name.startswith('evidence_') and name not in known_names for name in self.dense.names())
        unknown_paths = 0
        for path in self.index_root.iterdir():
            if path.is_dir() and path.name not in known_paths:
                # Legacy nested owner directories are known only through existing manifests.
                if not any(Path(value).parts[0] == path.name for value in known_paths):
                    unknown_paths += 1
        return {'cleanup_pending': pending, 'unknown_dense': unknown_names, 'unknown_sparse': unknown_paths}
