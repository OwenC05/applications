from dataclasses import replace

import pytest

from copilot.contracts import Chunk, EvidenceError, Manifest, Span
from copilot.store import Store, digest


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def chunks(obj):
    from copilot.contracts import Fact
    fact = isinstance(obj, Fact)
    return [Chunk(digest(obj.id), obj.profile_id, 'facts' if fact else 'documents',
                  obj.id if fact else obj.source_id, None if fact else obj.id,
                  0, len(obj.text), obj.text, digest(obj.text))] if obj.text else []


def test_fact_correction_and_manifest_cas(store):
    p = store.create_profile('Synthetic', ['tech', 'finance'])
    s = store.add_source(p.id, 'cv.txt', 'text/plain', b'Led 10 people', ['Led 10 people'], 'test')
    u = store.list_units(p.id, s.id)[0]
    f = store.confirm_fact(p.id, 'Led 10 people', Span(u.id, 0, 13))
    old = store.snapshot(p.id, 'facts', chunks)
    corrected = store.confirm_fact(p.id, 'Led 40 people', Span(u.id, 0, 13), f.id)
    assert [x.text for x in store.list_facts(p.id)] == ['Led 40 people']
    snap = store.snapshot(p.id, 'facts', chunks)
    citation = store.citation_for_chunk(p.id, snap.chunks[0])
    assert citation.text_sha256 == corrected.text_sha256
    assert store.resolve_citation(p.id, citation)['excerpt'] == 'Led 40 people'
    with pytest.raises(EvidenceError):
        store.validate_snapshot(p.id, old.revision)
    import uuid
    m = Manifest(str(uuid.uuid4()), p.id, 'facts', snap.revision, 'model', 'v1',
                 digest('\n'.join(c.id for c in snap.chunks)), 1, 'dense', 'sparse')
    store.publish_manifest(m, snap.revision)
    assert store.active_manifest(p.id, 'facts') == m
    store.confirm_fact(p.id, 'Finance experience')
    assert store.active_manifest(p.id, 'facts') is None
    with pytest.raises(EvidenceError):
        store.publish_manifest(replace(m, generation_id=str(uuid.uuid4())), snap.revision)


def test_deletion_persisted_and_raw_fact_distinction(store):
    p = store.create_profile('Synthetic', ['finance'])
    s = store.add_source(p.id, 'cv.txt', 'text/plain', b'Analyst', ['Analyst'], 'test')
    u = store.list_units(p.id, s.id)[0]
    f = store.confirm_fact(p.id, 'Analyst', Span(u.id, 0, 7))
    ticket = store.revoke_fact(p.id, f.id)
    assert store.list_facts(p.id) == []
    assert store.read_source(p.id, s.id) == b'Analyst'
    restarted = Store(store.root)
    assert restarted.pending_cleanup()[0].id == ticket.id
    restarted.complete_cleanup(ticket.id)
    f2 = restarted.confirm_fact(p.id, 'Analyst', Span(u.id, 0, 7))
    deleted = restarted.delete_source(p.id, s.id)
    assert f2.id in deleted.fact_ids
    assert restarted.list_sources(p.id) == restarted.list_facts(p.id) == []
    with pytest.raises(EvidenceError):
        restarted.read_source(p.id, s.id)
    restarted.complete_cleanup(deleted.id)
    assert not restarted._blob(s.id).exists()
    final = restarted.delete_profile(p.id)
    assert restarted.list_profiles() == []
    restarted.complete_cleanup(final.id)


def test_foreign_and_invalid_ids_fail_closed(store):
    p = store.create_profile('A', ['tech'])
    q = store.create_profile('B', ['finance'])
    s = store.add_source(p.id, 'a.txt', 'text/plain', b'text', ['text'], 'test')
    for owner, sid in [(q.id, s.id), (p.id, '../secret'), ('bad', s.id)]:
        with pytest.raises(EvidenceError):
            store.read_source(owner, sid)
    with pytest.raises(EvidenceError):
        store.snapshot(p.id, 'unknown', chunks)
    with pytest.raises(EvidenceError):
        store.add_source(p.id, '../bad.txt', 'text/plain', b'a', ['a'], 'test')


def test_snapshot_validates_canonical_text_and_limit(store):
    p = store.create_profile('A', ['tech'])
    store.confirm_fact(p.id, 'A')
    with pytest.raises(EvidenceError):
        store.snapshot(p.id, 'facts', lambda x: [replace(chunks(x)[0], text='B')])
    with pytest.raises(EvidenceError):
        store.snapshot(p.id, 'facts', lambda x: [replace(chunks(x)[0], id=str(n)) for n in range(1001)])
    assert store.snapshot(p.id, 'facts', chunks).chunks


def test_cleanup_failure_remains_pending_and_symlinks_rejected(store, monkeypatch):
    p = store.create_profile('A', ['tech'])
    s = store.add_source(p.id, 'a.txt', 'text/plain', b'a', ['a'], 'test')
    ticket = store.delete_source(p.id, s.id)
    from pathlib import Path
    original = Path.unlink
    def refused(path, *args, **kwargs):
        if path.name == s.id:
            raise OSError('private diagnostic must not escape')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', refused)
    with pytest.raises(EvidenceError) as exc:
        store.complete_cleanup(ticket.id)
    assert exc.value.code == 'CLEANUP_PENDING'
    assert 'diagnostic' not in str(exc.value)
    with pytest.raises(EvidenceError):
        Store(store.root)  # Readiness cannot succeed while orphan cleanup is blocked.
    monkeypatch.setattr(Path, 'unlink', original)
    assert Store(store.root).pending_cleanup()[0].id == ticket.id
    store.complete_cleanup(ticket.id)
    store.complete_cleanup(ticket.id)
    assert store.pending_cleanup() == []


def test_crash_after_blob_write_before_commit_is_reconciled(store):
    import os
    import subprocess
    import sys
    from pathlib import Path

    p = store.create_profile('Synthetic crash owner', ['tech'])
    other = store.create_profile('Other owner', ['finance'])
    surviving = store.add_source(other.id, 'surviving.txt', 'text/plain', b'keep', ['keep'], 'test')
    program = '''
import os
import sys
from pathlib import Path
from copilot.store import Store
s = Store(Path(sys.argv[1]))
original = s._put
def crash(db, owner, kind, value):
    if kind == 'source':
        os._exit(73)
    return original(db, owner, kind, value)
s._put = crash
s.add_source(sys.argv[2], 'synthetic.txt', 'text/plain', b'private synthetic orphan', ['private synthetic orphan'], 'test')
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]))
    process = subprocess.run([sys.executable, '-c', program, str(store.root), p.id], env=env, check=False)
    assert process.returncode == 73
    assert len(list(store.blobs.iterdir())) == 2
    restarted = Store(store.root)
    assert [x.name for x in restarted.blobs.iterdir()] == [surviving.id]
    assert restarted.read_source(other.id, surviving.id) == b'keep'
    restarted.complete_cleanup(restarted.delete_profile(p.id).id)
    restarted.complete_cleanup(restarted.delete_profile(other.id).id)
    assert list(restarted.blobs.iterdir()) == []


def test_reconciliation_refuses_unknown_paths_and_symlinks(store):
    import uuid

    unknown = store.blobs / 'not-owned.txt'
    unknown.write_bytes(b'leave untouched')
    with pytest.raises(EvidenceError) as exc:
        store.reconcile_blobs()
    assert exc.value.code == 'CLEANUP_PENDING'
    assert unknown.read_bytes() == b'leave untouched'
    unknown.unlink()
    outside = store.root / 'outside.txt'
    outside.write_bytes(b'outside')
    link = store.blobs / str(uuid.uuid4())
    link.symlink_to(outside)
    with pytest.raises(EvidenceError):
        Store(store.root)
    assert link.is_symlink() and outside.read_bytes() == b'outside'
    link.unlink()
    orphan = store.blobs / str(uuid.uuid4())
    orphan.write_bytes(b'orphan')
    store.reconcile_blobs()
    assert not orphan.exists()


def test_complete_cleanup_reconciles_later_orphan(store):
    import uuid

    p = store.create_profile('Synthetic', ['tech'])
    ticket = store.delete_profile(p.id)
    orphan = store.blobs / str(uuid.uuid4())
    orphan.write_bytes(b'synthetic orphan')
    store.complete_cleanup(ticket.id)
    assert not orphan.exists()


def test_reconciliation_waits_for_upload_writer_commit(store, monkeypatch):
    import threading

    p = store.create_profile('Synthetic writer', ['tech'])
    blob_written = threading.Event()
    allow_commit = threading.Event()
    reconciliation_done = threading.Event()
    results = []
    errors = []
    original = store._put

    def pause(db, owner, kind, value):
        if kind == 'source':
            blob_written.set()
            if not allow_commit.wait(5):
                raise RuntimeError('test coordination timeout')
        return original(db, owner, kind, value)

    monkeypatch.setattr(store, '_put', pause)

    def upload():
        try:
            results.append(store.add_source(p.id, 'race.txt', 'text/plain', b'keep', ['keep'], 'test'))
        except Exception as exc:
            errors.append(exc)

    def reconcile():
        try:
            store.reconcile_blobs()
        except Exception as exc:
            errors.append(exc)
        finally:
            reconciliation_done.set()

    writer = threading.Thread(target=upload)
    cleaner = threading.Thread(target=reconcile)
    writer.start()
    assert blob_written.wait(5)
    cleaner.start()
    try:
        assert not reconciliation_done.wait(0.1)
    finally:
        allow_commit.set()
        writer.join(5)
        cleaner.join(5)
    assert not writer.is_alive() and not cleaner.is_alive()
    assert not errors
    assert store.read_source(p.id, results[0].id) == b'keep'
