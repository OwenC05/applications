"""SQLite is authoritative; index manifests contain no applicant text."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
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
                CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS record_owner ON records(owner,kind);
                CREATE TABLE IF NOT EXISTS manifests(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    corpus TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tombstones(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    kind TEXT NOT NULL);
            ''')
        # executescript commits implicitly; reconcile in a fresh writer transaction.
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

    def _bump(self, db, owner):
        p = self._profile(db, owner)
        updated = Profile(p.id, p.name, p.sectors, p.revision + 1)
        db.execute('UPDATE profiles SET data=? WHERE id=?', (json.dumps(asdict(updated)), owner))
        db.execute("UPDATE manifests SET state='retired' WHERE owner=?", (owner,))
        return updated

    def _blob(self, source_id):
        identifier(source_id)
        path = self.blobs / source_id
        if path.is_symlink() or path.resolve().parent != self.blobs.resolve():
            fail('INVALID_INPUT', 'Invalid blob path', 400)
        return path

    def create_profile(self, name, sectors):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            fail('INVALID_INPUT', 'Name must contain 1–100 characters', 400)
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
                self._bump(db, profile_id)
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
            self._bump(db, profile_id)
        return fact

    @staticmethod
    def _span(text, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            fail('INVALID_INPUT', 'Invalid canonical span', 400)

    def snapshot(self, profile_id, corpus, chunker):
        corpus_name(corpus)
        with self._tx() as db:
            p = self._profile(db, profile_id)
            values = []
            kind = 'fact' if corpus == 'facts' else 'unit'
            for row in db.execute('SELECT data FROM records WHERE owner=? AND kind=? ORDER BY id', (profile_id, kind)):
                value = json.loads(row[0])
                if value.get('state', 'active') != 'active':
                    continue
                if kind == 'fact' and value['origin']:
                    value['origin'] = Span(**value['origin'])
                obj = Fact(**value) if kind == 'fact' else Unit(**value)
                for c in chunker(obj):
                    self._span(obj.text, c.start, c.end)
                    if (c.profile_id != profile_id or c.corpus != corpus or c.record_id != (obj.id if kind == 'fact' else obj.source_id) or c.unit_id != (None if kind == 'fact' else obj.id) or c.text != obj.text[c.start:c.end] or c.text_sha256 != digest(c.text)):
                        fail('INVALID_INPUT', 'Chunk does not match canonical text', 400)
                    values.append(c)
                    if len(values) > 1000:
                        fail('LIMIT_EXCEEDED', 'Snapshot limit is 1000 chunks', 413)
            if len({c.id for c in values}) != len(values):
                fail('INVALID_INPUT', 'Duplicate chunk IDs', 400)
            db.execute("DELETE FROM records WHERE owner=? AND kind=?", (profile_id, 'chunk:' + corpus))
            for c in values:
                self._put(db, profile_id, 'chunk:' + corpus, c)
            return Snapshot(profile_id, corpus, p.revision, tuple(sorted(values, key=lambda c: c.id)))

    def validate_snapshot(self, profile_id, revision):
        with self._tx() as db:
            if self._profile(db, profile_id).revision != revision:
                fail('CONFLICT', 'Evidence changed; rebuild required', 409)

    def publish_manifest(self, manifest, expected_revision):
        corpus_name(manifest.corpus)
        identifier(manifest.generation_id)
        with self._tx() as db:
            p = self._profile(db, manifest.profile_id)
            ids = sorted(r[0] for r in db.execute('SELECT id FROM records WHERE owner=? AND kind=?', (p.id, 'chunk:' + manifest.corpus)))
            # Retrieval contract uses newline-joined sorted IDs.
            if p.revision != expected_revision or manifest.revision != expected_revision:
                fail('CONFLICT', 'Evidence changed; rebuild required', 409)
            if manifest.chunk_count != len(ids) or manifest.chunk_ids_sha256 != digest('\n'.join(ids)):
                fail('CONFLICT', 'Index chunk set mismatch', 409)
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
            return value if value.revision == p.revision else None

    def eligible_chunks(self, profile_id, corpus, chunk_ids, source_ids=None):
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

    def _delete(self, owner, source_id=None, fact_id=None, profile=False):
        with self._tx() as db:
            self._profile(db, owner)
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
            generations = [r[0] for r in db.execute('SELECT id FROM manifests WHERE owner=?', (owner,))]
            for rid, kind, obj in rows:
                if profile or rid in sources or rid in facts or rid in units or kind.startswith('chunk:'):
                    db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (rid, owner, kind))
                    db.execute('DELETE FROM records WHERE id=?', (rid,))
            self._bump(db, owner)
            if profile:
                db.execute('DELETE FROM profiles WHERE id=?', (owner,))
            ticket = CleanupTicket(str(uuid.uuid4()), owner, generations, sources, facts)
            db.execute('INSERT INTO tickets VALUES(?,?)', (ticket.id, json.dumps(asdict(ticket))))
            return ticket

    def revoke_fact(self, profile_id, fact_id):
        return self._delete(profile_id, fact_id=fact_id)

    def delete_source(self, profile_id, source_id):
        return self._delete(profile_id, source_id=source_id)

    def delete_profile(self, profile_id):
        return self._delete(profile_id, profile=True)

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
                db.execute('DELETE FROM manifests WHERE id=? AND owner=?', (gid, ticket.profile_id))
            done = CleanupTicket(ticket.id, ticket.profile_id, ticket.generation_ids, ticket.source_ids, ticket.fact_ids, 'complete')
            db.execute('UPDATE tickets SET data=? WHERE id=?', (json.dumps(asdict(done)), ticket.id))
