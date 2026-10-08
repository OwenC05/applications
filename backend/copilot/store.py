"""SQLite is authoritative; index manifests contain no applicant text."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    Chunk,
    Citation,
    CleanupTicket,
    EvidenceError,
    Fact,
    Manifest,
    Profile,
    Snapshot,
    Source,
    Span,
    Unit,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def fail(code='NOT_FOUND', message='Evidence record not found', status=404):
    raise EvidenceError(code, message, status)


def identifier(value):
    try:
        if str(uuid.UUID(value)) != value:
            fail()
    except (ValueError, TypeError, AttributeError):
        fail()


def corpus_name(value):
    if value not in ('facts', 'documents'):
        fail('INVALID_INPUT', 'Invalid corpus', 400)


class Store:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / 'blobs'
        self.blobs.mkdir(exist_ok=True)
        if self.blobs.is_symlink() or self.blobs.resolve().parent != self.root:
            fail('INVALID_INPUT', 'Invalid owned storage directory', 400)
        self.db = self.root / 'evidence.sqlite3'
        with self._tx() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_revisions(owner TEXT PRIMARY KEY,
                    metadata INTEGER NOT NULL DEFAULT 0, facts INTEGER NOT NULL DEFAULT 0,
                    documents INTEGER NOT NULL DEFAULT 0, consent INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS fact_metadata(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS record_owner ON records(owner,kind);
                CREATE TABLE IF NOT EXISTS manifests(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    corpus TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generation_intents(id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL, corpus TEXT NOT NULL, state TEXT NOT NULL,
                    data TEXT NOT NULL, captured TEXT);
                CREATE TABLE IF NOT EXISTS dense_mutations(generation_id TEXT PRIMARY KEY,
                    fence INTEGER NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generation_chunks(generation_id TEXT NOT NULL,
                    id TEXT NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(generation_id,id));
                CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tombstones(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    kind TEXT NOT NULL);
            ''')
        # executescript commits implicitly; reconcile in a fresh writer transaction.
        with self._tx() as db:
            for owner, serialized in db.execute('SELECT id,data FROM profiles').fetchall():
                revision = json.loads(serialized)['revision']
                db.execute('INSERT OR IGNORE INTO profile_revisions(owner,facts,documents) VALUES(?,?,?)',
                           (owner, revision, revision))
        # Legacy published manifests prove a completed dual-index publication, not a
        # fabricated producer lease. Unjournaled staging cannot prove no dispatch.
        with self._tx() as db:
            db.execute("""INSERT OR IGNORE INTO dense_mutations
                SELECT g.id,json_extract(g.data,'$.fence'),
                CASE WHEN EXISTS(SELECT 1 FROM manifests m WHERE m.id=g.id)
                THEN 'acknowledged' ELSE 'indeterminate' END FROM generation_intents g""")
        self.reconcile_blobs()

    def _reconcile_blobs(self, db):
        """Writer transaction fences uploads while uncommitted UUID blobs are removed."""
        referenced = {r[0] for r in db.execute("SELECT id FROM records WHERE kind='source'")}
        paths = list(self.blobs.iterdir())
        # Never follow symlinks or remove arbitrary files, including unknown names.
        for path in paths:
            identifier(path.name)
            if path.is_symlink() or not path.is_file() or path.resolve().parent != self.blobs:
                raise OSError()
        for path in paths:
            if path.name not in referenced:
                path.unlink()
                if path.exists():
                    raise OSError()

    def reconcile_blobs(self):
        """Reconcile this store's UUID blobs against all committed source owners."""
        with self._tx() as db:
            try:
                self._reconcile_blobs(db)
            except (OSError, EvidenceError):
                fail('CLEANUP_PENDING', 'Owned blob reconciliation remains pending', 503)

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.db, timeout=30)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _profile(self, db, owner):
        identifier(owner)
        row = db.execute('SELECT data FROM profiles WHERE id=?', (owner,)).fetchone()
        if not row:
            fail()
        return Profile(**json.loads(row[0]))

    def _record(self, db, owner, record, kind):
        identifier(record)
        self._profile(db, owner)
        row = db.execute('SELECT data FROM records WHERE id=? AND owner=? AND kind=?',
                         (record, owner, kind)).fetchone()
        if not row:
            fail()
        return json.loads(row[0])

    def _put(self, db, owner, kind, value):
        db.execute('INSERT INTO records VALUES(?,?,?,?)',
                   (value.id, owner, kind, json.dumps(asdict(value))))

    def _bump(self, db, owner, corpus=None):
        p = self._profile(db, owner)
        db.execute('INSERT OR IGNORE INTO profile_revisions(owner) VALUES(?)', (owner,))
        if corpus in ('facts', 'documents'):
            db.execute(f'UPDATE profile_revisions SET {corpus}={corpus}+1 WHERE owner=?', (owner,))
        updated = Profile(p.id, p.name, p.sectors, p.revision + 1)
        db.execute('UPDATE profiles SET data=? WHERE id=?', (json.dumps(asdict(updated)), owner))
        if corpus in ('facts', 'documents'):
            db.execute("UPDATE manifests SET state='retired' WHERE owner=? AND corpus=?", (owner, corpus))
        else:
            db.execute("UPDATE manifests SET state='retired' WHERE owner=?", (owner,))
        return updated

    def _blob(self, source_id):
        identifier(source_id)
        path = self.blobs / source_id
        if path.is_symlink() or path.resolve().parent != self.blobs.resolve():
            fail('INVALID_INPUT', 'Invalid blob path', 400)
        return path

    def create_profile(self, name, sectors):
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            fail('INVALID_INPUT', 'Name must contain 1–200 characters', 400)
        if not isinstance(sectors, list) or not sectors or any(s not in ('tech', 'finance') for s in sectors):
            fail('INVALID_INPUT', 'Choose tech or finance sectors', 400)
        p = Profile(str(uuid.uuid4()), name.strip(), list(dict.fromkeys(sectors)), 0)
        with self._tx() as db:
            db.execute('INSERT INTO profiles VALUES(?,?)', (p.id, json.dumps(asdict(p))))
        return p

    def list_profiles(self):
        with self._tx() as db:
            return [Profile(**json.loads(r[0])) for r in db.execute('SELECT data FROM profiles ORDER BY id')]

    def _list(self, owner, kind, cls):
        with self._tx() as db:
            self._profile(db, owner)
            values = [json.loads(r[0]) for r in db.execute(
                'SELECT data FROM records WHERE owner=? AND kind=? ORDER BY id', (owner, kind))]
            if cls is Fact:
                for v in values:
                    if v['origin']:
                        v['origin'] = Span(**v['origin'])
            return [cls(**v) for v in values if v.get('state', 'active') == 'active']

    def list_sources(self, profile_id):
        return self._list(profile_id, 'source', Source)

    def list_facts(self, profile_id):
        return self._list(profile_id, 'fact', Fact)

    def list_units(self, profile_id, source_id):
        with self._tx() as db:
            self._record(db, profile_id, source_id, 'source')
            values = (json.loads(r[0]) for r in db.execute(
                "SELECT data FROM records WHERE owner=? AND kind='unit'", (profile_id,)))
            return sorted((Unit(**value) for value in values if value['source_id'] == source_id),
                          key=lambda unit: unit.ordinal)

    def read_source(self, profile_id, source_id):
        with self._tx() as db:
            source = self._record(db, profile_id, source_id, 'source')
            try:
                content = self._blob(source_id).read_bytes()
            except OSError:
                fail('NOT_FOUND', 'Original source unavailable', 404)
            if hashlib.sha256(content).hexdigest() != source['blob_sha256']:
                fail('CONFLICT', 'Original source integrity mismatch', 409)
            return content

    def add_source(self, profile_id, filename, media_type, original, units, parser_version):
        if not isinstance(original, bytes) or len(original) > 10 * 1024 * 1024:
            fail('LIMIT_EXCEEDED', 'Upload limit is 10 MiB', 413)
        if not isinstance(filename, str) or not filename or len(filename) > 200 or '/' in filename or '\\' in filename or any(ord(c) < 32 for c in filename):
            fail('INVALID_INPUT', 'Invalid filename', 400)
        if media_type not in ('text/plain', 'text/markdown', 'application/pdf') or not units or len(units) > 100 or any(not isinstance(u, str) for u in units) or sum(map(len, units)) > 1_000_000 or not any(u.strip() for u in units):
            fail('INVALID_INPUT', 'Invalid extracted document', 400)
        source = Source(str(uuid.uuid4()), profile_id, filename, media_type,
                        hashlib.sha256(original).hexdigest(), parser_version)
        path = self._blob(source.id)
        try:
            with self._tx() as db:
                self._profile(db, profile_id)
                with path.open('xb') as out:
                    out.write(original)
                self._put(db, profile_id, 'source', source)
                for n, text in enumerate(units):
                    text = text.replace('\r\n', '\n').replace('\r', '\n')
                    unit = Unit(str(uuid.uuid4()), profile_id, source.id, n,
                                n + 1 if media_type == 'application/pdf' else None, text, digest(text))
                    self._put(db, profile_id, 'unit', unit)
                self._bump(db, profile_id, 'documents')
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return source

    def confirm_fact(self, profile_id, text, origin=None, supersedes=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            fail('INVALID_INPUT', 'Confirmed text must contain 1–20000 characters', 400)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        with self._tx() as db:
            self._profile(db, profile_id)
            if origin:
                if not isinstance(origin, Span):
                    fail('INVALID_INPUT', 'Invalid origin', 400)
                unit = self._record(db, profile_id, origin.unit_id, 'unit')
                self._span(unit['text'], origin.start, origin.end)
            if supersedes:
                old = self._record(db, profile_id, supersedes, 'fact')
                if old['state'] != 'active':
                    fail('CONFLICT', 'Fact is no longer active', 409)
                old['state'] = 'superseded'
                db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(old), supersedes))
            fact = Fact(str(uuid.uuid4()), profile_id, text, digest(text),
                        datetime.now(timezone.utc).isoformat(),
                        'document_derived' if origin else 'manual', origin)
            self._put(db, profile_id, 'fact', fact)
            self._bump(db, profile_id, 'facts')
        return fact

    @staticmethod
    def _span(text, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            fail('INVALID_INPUT', 'Invalid canonical span', 400)

    def _corpus_revision(self, db, owner, corpus):
        corpus_name(corpus)
        p = self._profile(db, owner)
        db.execute('INSERT OR IGNORE INTO profile_revisions(owner,facts,documents) VALUES(?,?,?)',
                   (owner, p.revision, p.revision))
        return db.execute(f'SELECT {corpus} FROM profile_revisions WHERE owner=?', (owner,)).fetchone()[0]

    def capture_sources(self, db, profile_id, corpus):
        """Immutable canonical input capture; callers own a short transaction."""
        corpus_name(corpus)
        self._profile(db, profile_id)
        kind = 'fact' if corpus == 'facts' else 'unit'
        values = []
        for row in db.execute('SELECT data FROM records WHERE owner=? AND kind=? ORDER BY id', (profile_id, kind)):
            value = json.loads(row[0])
            if value.get('state', 'active') == 'active':
                values.append(value)
        return self._corpus_revision(db, profile_id, corpus), values

    def chunk_capture(self, profile_id, corpus, revision, values, chunker):
        """Pure validation/tokenization: no SQLite transaction or canonical writes."""
        chunks = []
        for serialized in values:
            value = dict(serialized)
            if corpus == 'facts' and value['origin']:
                value['origin'] = Span(**value['origin'])
            obj = Fact(**value) if corpus == 'facts' else Unit(**value)
            for c in chunker(obj):
                self._span(obj.text, c.start, c.end)
                if (c.profile_id != profile_id or c.corpus != corpus
                        or c.record_id != (obj.id if corpus == 'facts' else obj.source_id)
                        or c.unit_id != (None if corpus == 'facts' else obj.id)
                        or c.text != obj.text[c.start:c.end] or c.text_sha256 != digest(c.text)):
                    fail('INVALID_INPUT', 'Chunk does not match canonical text', 400)
                chunks.append(c)
                if len(chunks) > 1000:
                    fail('LIMIT_EXCEEDED', 'Snapshot limit is 1000 chunks', 413)
        if len({c.id for c in chunks}) != len(chunks):
            fail('INVALID_INPUT', 'Duplicate chunk IDs', 400)
        return Snapshot(profile_id, corpus, revision, tuple(sorted(chunks, key=lambda c: c.id)))

    def snapshot(self, profile_id, corpus, chunker):
        with self._tx() as db:
            revision, values = self.capture_sources(db, profile_id, corpus)
        return self.chunk_capture(profile_id, corpus, revision, values, chunker)

    def validate_snapshot(self, profile_id, revision, corpus=None):
        with self._tx() as db:
            current = self._corpus_revision(db, profile_id, corpus) if corpus else self._profile(db, profile_id).revision
            if current != revision:
                fail('CONFLICT', 'Evidence changed; rebuild required', 409)

    def publish_manifest(self, manifest, expected_revision, *, transaction=None, jobs=None, worker_id=None, fence=None):
        """Internal terminal-job callback only; never an unfenced publication API."""
        if transaction is None or jobs is None:
            fail('CONFLICT', 'Index publication requires a fenced job transaction', 409)
        db = transaction
        corpus_name(manifest.corpus)
        identifier(manifest.generation_id)
        p = self._profile(db, manifest.profile_id)
        intent = db.execute('SELECT owner,state,data FROM generation_intents WHERE id=?',
                            (manifest.generation_id,)).fetchone()
        if not intent or intent[0] != p.id or intent[1] != 'ready':
            fail('CONFLICT', 'Generation is not ready for publication', 409)
        captured = json.loads(intent[2])
        job, parameters = jobs.assert_current(db, captured['job_id'], worker_id, fence)
        if (captured['fence'] != fence or job.kind != 'index'
                or job.profile_id != p.id or parameters['corpus'] != manifest.corpus
                or captured['corpus'] != manifest.corpus
                or captured['model_fingerprint'] != manifest.model_fingerprint
                or captured['chunker_version'] != manifest.chunker_version
                or captured['dense_collection'] != manifest.dense_collection
                or captured['sparse_relpath'] != manifest.sparse_relpath):
            fail('CONFLICT', 'Generation publication authority mismatch', 409)
        rows = db.execute('SELECT id,data FROM generation_chunks WHERE generation_id=? ORDER BY id',
                          (manifest.generation_id,)).fetchall()
        ids = [r[0] for r in rows]
        mutation = db.execute('SELECT fence,state FROM dense_mutations WHERE generation_id=?', (manifest.generation_id,)).fetchone()
        required_state = 'acknowledged' if ids else 'not_attempted'
        if mutation != (fence, required_state):
            fail('CONFLICT', 'Dense generation completion has not been acknowledged', 409)
        if self._corpus_revision(db, p.id, manifest.corpus) != expected_revision or manifest.revision != expected_revision:
            fail('CONFLICT', 'Evidence changed; rebuild required', 409)
        if manifest.chunk_count != len(ids) or manifest.chunk_ids_sha256 != digest('\n'.join(ids)):
            fail('CONFLICT', 'Index chunk set mismatch', 409)
        db.execute("DELETE FROM records WHERE owner=? AND kind=?", (p.id, 'chunk:' + manifest.corpus))
        for _, data in rows:
            self._put(db, p.id, 'chunk:' + manifest.corpus, Chunk(**json.loads(data)))
        db.execute("UPDATE manifests SET state='retired' WHERE owner=? AND corpus=?", (p.id, manifest.corpus))
        db.execute('INSERT INTO manifests VALUES(?,?,?,?,?)', (manifest.generation_id, p.id, manifest.corpus, 'active', json.dumps(asdict(manifest))))

    def active_manifest(self, profile_id, corpus):
        corpus_name(corpus)
        with self._tx() as db:
            p = self._profile(db, profile_id)
            row = db.execute("SELECT data FROM manifests WHERE owner=? AND corpus=? AND state='active'", (p.id, corpus)).fetchone()
            if not row:
                return None
            value = Manifest(**json.loads(row[0]))
            return value if value.revision == self._corpus_revision(db, p.id, corpus) else None

    def eligible_chunks(self, profile_id, corpus, chunk_ids, source_ids=None, generation_id=None):
        corpus_name(corpus)
        with self._tx() as db:
            self._profile(db, profile_id)
            if source_ids is not None:
                if corpus != 'documents' or not source_ids:
                    fail('INVALID_INPUT', 'Select document sources', 400)
                for sid in source_ids:
                    self._record(db, profile_id, sid, 'source')
            result = []
            for cid in chunk_ids:
                if generation_id is not None:
                    row = db.execute('SELECT c.data FROM generation_chunks c JOIN generation_intents g ON g.id=c.generation_id WHERE c.id=? AND c.owner=? AND g.id=? AND g.corpus=? AND g.state=?',
                                     (cid, profile_id, generation_id, corpus, 'published')).fetchone()
                    if not db.execute('SELECT 1 FROM generation_intents WHERE id=?', (generation_id,)).fetchone():
                        # Honest legacy read path: no invented lease or job metadata.
                        row = db.execute('SELECT data FROM records WHERE id=? AND owner=? AND kind=?', (cid, profile_id, 'chunk:' + corpus)).fetchone()
                else:
                    row = db.execute('SELECT data FROM records WHERE id=? AND owner=? AND kind=?', (cid, profile_id, 'chunk:' + corpus)).fetchone()
                if not row:
                    continue
                c = Chunk(**json.loads(row[0]))
                if source_ids is not None and c.record_id not in source_ids:
                    continue
                record = self._record(db, profile_id, c.record_id, 'fact' if corpus == 'facts' else 'source')
                if record.get('state', 'active') == 'active':
                    result.append(c)
            return result

    def citation_for_chunk(self, profile_id, chunk):
        corpus_name(chunk.corpus)
        if chunk.profile_id != profile_id:
            fail()
        with self._tx() as db:
            obj = self._citation_record(db, profile_id, chunk.corpus, chunk.record_id, chunk.unit_id)
            self._span(obj['text'], chunk.start, chunk.end)
            if obj['text'][chunk.start:chunk.end] != chunk.text or digest(chunk.text) != chunk.text_sha256:
                fail('CONFLICT', 'Chunk integrity mismatch', 409)
            return Citation(profile_id, chunk.corpus, chunk.record_id, chunk.unit_id,
                            chunk.start, chunk.end, obj['text_sha256'], chunk.text)

    def _citation_record(self, db, owner, corpus, record_id, unit_id):
        corpus_name(corpus)
        if corpus == 'facts':
            if unit_id is not None:
                fail('INVALID_INPUT', 'Fact citation cannot reference a source unit', 400)
            obj = self._record(db, owner, record_id, 'fact')
            if obj['state'] != 'active':
                fail()
        else:
            self._record(db, owner, record_id, 'source')
            obj = self._record(db, owner, unit_id, 'unit')
            if obj['source_id'] != record_id:
                fail()
        return obj

    def resolve_citation(self, profile_id, citation):
        if citation.profile_id != profile_id:
            fail()
        with self._tx() as db:
            obj = self._citation_record(db, profile_id, citation.corpus, citation.record_id, citation.unit_id)
            self._span(obj['text'], citation.start, citation.end)
            if obj['text_sha256'] != citation.text_sha256 or digest(obj['text']) != obj['text_sha256'] or obj['text'][citation.start:citation.end] != citation.excerpt:
                fail('CONFLICT', 'Citation integrity mismatch', 409)
            result = {'integrity': 'verified', 'excerpt': citation.excerpt, 'canonical_text': obj['text'], 'start': citation.start, 'end': citation.end, 'record_id': citation.record_id, 'unit_id': citation.unit_id, 'semantic_support': 'not_assessed'}
            if citation.corpus == 'documents':
                result.update(page=obj['page'], line_start=obj['text'].count('\n', 0, citation.start) + 1, line_end=obj['text'].count('\n', 0, citation.end - 1) + 1)
            else:
                result.update(provenance=obj['provenance'], confirmed_at=obj['confirmed_at'], origin=obj['origin'], origin_label='proposal origin, not semantic proof')
            return result

    @staticmethod
    def _workspace_state(db, owner):
        return db.execute("SELECT id,data FROM records WHERE owner=? AND kind LIKE 'workspace:%' ORDER BY id", (owner,)).fetchall()

    @staticmethod
    def _metadata_revision(db, owner):
        row = db.execute('SELECT metadata FROM profile_revisions WHERE owner=?', (owner,)).fetchone()
        return row[0] if row else 0

    def _invalidate_workspace_changes(self, db, owner, before, revision):
        # Tokens are monotonic, not a promise of one increment per compound action.
        # Nested forgetting must invalidate once before the outer operation returns.
        if self._workspace_state(db, owner) != before and self._metadata_revision(db, owner) == revision:
            db.execute('INSERT OR IGNORE INTO profile_revisions(owner) VALUES(?)', (owner,))
            db.execute('UPDATE profile_revisions SET metadata=metadata+1 WHERE owner=?', (owner,))

    def forget_proposal_origin(self, db, owner, proposal, legacy_source=None):
        """Forget only source-linked raw lineage; never another interview question."""
        before = self._workspace_state(db, owner)
        revision = self._metadata_revision(db, owner)
        source = legacy_source or {'kind': proposal['origin'], 'id': proposal.get('origin_id')}
        relations = [(rid, json.loads(text)) for rid, text in db.execute(
            "SELECT id,data FROM records WHERE owner=? AND kind='workspace:legacy_relation'", (owner,))]
        for rid, relation in relations:
            if relation['proposal_id'] == proposal['id']:
                source = relation['legacy_source']
                db.execute('DELETE FROM records WHERE id=? AND owner=?', (rid, owner))
        origin_id = source.get('id')
        if source['kind'] == 'interview' and origin_id:
            question_id = source.get('question_id')
            answers = [(rid, json.loads(text)) for rid, text in db.execute(
                "SELECT id,data FROM records WHERE owner=? AND kind='workspace:interview_answer'", (owner,))]
            archives = [(rid, json.loads(text)) for rid, text in db.execute(
                "SELECT id,data FROM records WHERE owner=? AND kind='workspace:interview_archive'", (owner,))]
            for rid, answer in answers:
                if rid == origin_id:
                    question_id = answer['question_id']
            for _, archive in archives:
                if archive['answer']['id'] == origin_id:
                    question_id = archive['answer']['question_id']
            removed_ids = {origin_id}
            for rid, answer in answers:
                if rid == origin_id or question_id and answer['question_id'] == question_id:
                    removed_ids.add(rid)
                    db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (rid, owner, 'workspace:interview_answer'))
                    db.execute('DELETE FROM records WHERE id=? AND owner=?', (rid, owner))
            for rid, archive in archives:
                if archive['answer']['id'] == origin_id or question_id and archive['answer']['question_id'] == question_id:
                    removed_ids.add(archive['answer']['id'])
                    db.execute('DELETE FROM records WHERE id=? AND owner=?', (rid, owner))
            for rid, text in db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:proposal'", (owner,)).fetchall():
                value = json.loads(text)
                if value['status'] == 'pending' and value.get('origin_id') in removed_ids and value['origin'] == 'interview':
                    db.execute('DELETE FROM records WHERE id=? AND owner=?', (rid, owner))
            for relation_id, relation in relations:
                related_source = relation['legacy_source']
                if (related_source.get('kind') == 'interview' and question_id
                        and related_source.get('question_id') == question_id):
                    row = db.execute("SELECT data FROM records WHERE id=? AND owner=? AND kind='workspace:proposal'",
                                     (relation['proposal_id'], owner)).fetchone()
                    if row and json.loads(row[0])['status'] == 'pending':
                        db.execute('DELETE FROM records WHERE id=? AND owner=?', (relation['proposal_id'], owner))
                        db.execute('DELETE FROM records WHERE id=? AND owner=?', (relation_id, owner))
            for rid, text in db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:interview_progress'", (owner,)).fetchall():
                value = json.loads(text)
                value['answer_ids'] = [answer_id for answer_id in value['answer_ids'] if answer_id not in removed_ids]
                db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(value), rid))
        if source['kind'] in ('application_feedback', 'application') and origin_id:
            db.execute("DELETE FROM records WHERE owner=? AND kind='workspace:history' AND (id=? OR json_extract(data,'$.application_id')=?)", (owner, origin_id, origin_id))
        if proposal['origin'] == 'legacy' or source['kind'] in ('interview', 'application', 'application_feedback'):
            db.execute("DELETE FROM records WHERE owner=? AND kind='workspace:legacy_archive'", (owner,))
        self._invalidate_workspace_changes(db, owner, before, revision)

    def _delete(self, owner, source_id=None, fact_id=None, profile=False, transaction=None):
        with nullcontext(transaction) if transaction is not None else self._tx() as db:
            self._profile(db, owner)
            workspace_before = self._workspace_state(db, owner)
            metadata_before = self._metadata_revision(db, owner)
            if source_id:
                self._record(db, owner, source_id, 'source')
            if fact_id:
                self._record(db, owner, fact_id, 'fact')
            sources = []
            facts = []
            units = set()
            rows = [(r[0], r[1], json.loads(r[2])) for r in db.execute('SELECT id,kind,data FROM records WHERE owner=?', (owner,))]
            for rid, kind, obj in rows:
                if kind == 'source' and (profile or rid == source_id):
                    sources.append(rid)
                if kind == 'unit' and (profile or obj['source_id'] == source_id):
                    units.add(rid)
            for rid, kind, obj in rows:
                if kind == 'fact' and (profile or rid == fact_id or (obj['origin'] and obj['origin']['unit_id'] in units)):
                    facts.append(rid)
            if source_id:
                for rid, serialized in db.execute('SELECT id,data FROM fact_metadata WHERE owner=?', (owner,)):
                    metadata = json.loads(serialized)
                    if any(span.get('unit_id') in units for span in metadata.get('source_spans', [])) and rid not in facts:
                        facts.append(rid)
            affected = {'facts', 'documents'} if profile else ({'documents'} if source_id else {'facts'})
            if facts:
                affected.add('facts')
            generations = [r[0] for r in db.execute('SELECT id,corpus FROM manifests WHERE owner=?', (owner,)) if r[1] in affected]
            for gid, corpus, serialized in db.execute('SELECT id,corpus,data FROM generation_intents WHERE owner=?', (owner,)).fetchall():
                if corpus not in affected:
                    continue
                if gid not in generations:
                    generations.append(gid)
                intent = json.loads(serialized)
                intent['state'] = 'cleanup_pending'
                db.execute('UPDATE generation_intents SET state=?,data=?,captured=NULL WHERE id=?',
                           ('cleanup_pending', json.dumps(intent), gid))
                db.execute('DELETE FROM generation_chunks WHERE generation_id=?', (gid,))
                # Index cancellation is atomic with source/fact/profile forgetting.
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                    job_row = db.execute('SELECT data FROM jobs WHERE id=?', (intent['job_id'],)).fetchone()
                    if job_row:
                        job = json.loads(job_row[0])
                        if job['state'] in ('queued', 'running'):
                            job.update(state='cancelled', cancellation_requested=True, stage='scope_forgotten')
                            db.execute('UPDATE jobs SET state=?,data=? WHERE id=?', ('cancelled', json.dumps(job), intent['job_id']))

            for rid, kind, obj in rows:
                if profile or rid in sources or rid in facts or rid in units or kind in {'chunk:' + corpus for corpus in affected}:
                    db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (rid, owner, kind))
                    db.execute('DELETE FROM records WHERE id=?', (rid,))
            for rid in facts:
                metadata = db.execute('SELECT data FROM fact_metadata WHERE id=? AND owner=?', (rid, owner)).fetchone()
                if metadata:
                    proposal_id = json.loads(metadata[0])['proposal_id']
                    proposal_row = db.execute('SELECT data FROM records WHERE id=? AND owner=? AND kind=?',
                                              (proposal_id, owner, 'workspace:proposal')).fetchone()
                    if proposal_row:
                        proposal = json.loads(proposal_row[0])
                        self.forget_proposal_origin(db, owner, proposal, json.loads(metadata[0]).get('legacy_source'))
                    db.execute('DELETE FROM records WHERE id=? AND owner=? AND kind=?',
                               (proposal_id, owner, 'workspace:proposal'))
                db.execute('DELETE FROM fact_metadata WHERE id=? AND owner=?', (rid, owner))
            # Document-derived pending proposals cannot survive loss of their sources.
            for rid, kind, obj in rows:
                if kind == 'workspace:proposal' and any(span.get('unit_id') in units for span in obj.get('source_spans', [])):
                    db.execute('DELETE FROM records WHERE id=? AND owner=?', (rid, owner))
            if facts or sources:
                # Immutable uploaded legacy backups can contain revoked factual text;
                # remove the whole owned backup rather than edit original bytes.
                db.execute("DELETE FROM records WHERE owner=? AND kind='workspace:legacy_archive'", (owner,))
            self._invalidate_workspace_changes(db, owner, workspace_before, metadata_before)
            self._bump(db, owner, 'documents' if source_id else 'facts')
            if source_id and facts:
                db.execute('UPDATE profile_revisions SET facts=facts+1 WHERE owner=?', (owner,))
                db.execute("UPDATE manifests SET state='retired' WHERE owner=? AND corpus='facts'", (owner,))
            if profile:
                db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (owner, owner, 'profile'))
                db.execute('DELETE FROM fact_metadata WHERE owner=?', (owner,))
                db.execute('DELETE FROM profile_revisions WHERE owner=?', (owner,))
                db.execute('DELETE FROM profiles WHERE id=?', (owner,))
            ticket = CleanupTicket(str(uuid.uuid4()), owner, generations, sources, facts)
            ticket_data = asdict(ticket)
            db.execute('INSERT INTO tickets VALUES(?,?)', (ticket.id, json.dumps(ticket_data)))
            return ticket

    def revoke_fact(self, profile_id, fact_id):
        return self._delete(profile_id, fact_id=fact_id)

    def delete_source(self, profile_id, source_id):
        return self._delete(profile_id, source_id=source_id)

    def delete_profile(self, profile_id):
        return self._delete(profile_id, profile=True)

    def cleanup_manifests(self, ticket):
        with self._tx() as db:
            result = []
            for generation_id in ticket.generation_ids:
                identifier(generation_id)
                row = db.execute('SELECT data FROM manifests WHERE id=? AND owner=?',
                                 (generation_id, ticket.profile_id)).fetchone()
                if row:
                    result.append(Manifest(**json.loads(row[0])))
            return result

    def pending_cleanup(self):
        with self._tx() as db:
            return [CleanupTicket(**v) for r in db.execute('SELECT data FROM tickets ORDER BY id') if (v := json.loads(r[0]))['state'] == 'pending']

    def complete_cleanup(self, ticket_id):
        identifier(ticket_id)
        with self._tx() as db:
            row = db.execute('SELECT data FROM tickets WHERE id=?', (ticket_id,)).fetchone()
            if not row:
                fail()
            ticket = CleanupTicket(**json.loads(row[0]))
            try:
                self._reconcile_blobs(db)
                if ticket.state == 'complete':
                    return
                for sid in ticket.source_ids:
                    tombstone = db.execute('SELECT owner,kind FROM tombstones WHERE id=?', (sid,)).fetchone()
                    if tombstone != (ticket.profile_id, 'source'):
                        raise OSError()
                    self._blob(sid).unlink(missing_ok=True)
                    if self._blob(sid).exists():
                        raise OSError()
                for rid in ticket.source_ids + ticket.fact_ids:
                    if db.execute('SELECT 1 FROM records WHERE id=?', (rid,)).fetchone():
                        raise OSError()
                for row in db.execute('SELECT kind,data FROM records WHERE owner=?', (ticket.profile_id,)):
                    obj = json.loads(row[1])
                    if (row[0] == 'unit' and obj['source_id'] in ticket.source_ids) or (row[0].startswith('chunk:') and obj['record_id'] in ticket.source_ids + ticket.fact_ids):
                        raise OSError()
            except (OSError, EvidenceError):
                fail('CLEANUP_PENDING', 'Owned data cleanup remains pending', 503)
            for gid in ticket.generation_ids:
                intent = db.execute('SELECT state FROM generation_intents WHERE id=? AND owner=?', (gid, ticket.profile_id)).fetchone()
                if intent and intent[0] != 'cleaned':
                    fail('CLEANUP_PENDING', 'Generation cleanup remains pending', 503)
                mutation = db.execute('SELECT state FROM dense_mutations WHERE generation_id=?', (gid,)).fetchone()
                if mutation and mutation[0] in ('inflight', 'indeterminate'):
                    fail('CLEANUP_PENDING', 'External dense write completion remains unknown', 503)
                db.execute('DELETE FROM manifests WHERE id=? AND owner=?', (gid, ticket.profile_id))
            done = CleanupTicket(ticket.id, ticket.profile_id, ticket.generation_ids, ticket.source_ids, ticket.fact_ids, 'complete')
            db.execute('UPDATE tickets SET data=? WHERE id=?', (json.dumps(asdict(done)), ticket.id))
