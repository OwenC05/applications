"""Explicit selected-byte uploads, durable intent and local producer quiescence."""
import threading
from uuid import uuid4

import pytest

from copilot.contracts import EvidenceError
from copilot.store import Store


def test_live_upload_has_intent_before_blob_write_and_does_not_hold_sqlite(tmp_path, monkeypatch):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    original = store._write_blob
    def blocked(path, content):
        with store._tx() as db:
            assert db.execute('SELECT state FROM upload_intents WHERE id=?', (path.name,)).fetchone()[0] == 'writing'
        original(path, content)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(store, '_write_blob', blocked)
    def upload():
        try:
            results.append(store.add_source(owner, 'notes.txt', 'text/plain', b'private synthetic', ['private synthetic'], 'test'))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=upload)
    thread.start()
    assert entered.wait(5)
    try:
        reopened = Store(tmp_path)
        assert reopened.list_profiles()
        assert reopened.reconcile_blobs()['active_uploads'] == 1
        store.confirm_fact(owner, 'Independent fact while bytes writer blocked')
        assert list(store.blobs.iterdir())
    finally:
        release.set()
        thread.join(5)
    assert not errors and not thread.is_alive()
    assert store.read_source(owner, results[0].id) == b'private synthetic'


def test_profile_delete_blocks_live_upload_commit_and_keeps_ticket_pending(tmp_path, monkeypatch):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = store._write_blob
    def blocked(path, content):
        original(path, content)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(store, '_write_blob', blocked)
    def upload():
        try:
            store.add_source(owner, 'notes.txt', 'text/plain', b'private synthetic', ['private synthetic'], 'test')
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=upload)
    thread.start()
    assert entered.wait(5)
    try:
        ticket = store.delete_profile(owner)
        assert ticket.source_ids
        with pytest.raises(EvidenceError) as caught:
            store.complete_cleanup(ticket.id)
        assert caught.value.code == 'CLEANUP_PENDING'
        assert store.pending_cleanup()
    finally:
        release.set()
        thread.join(5)
    assert errors and not thread.is_alive()
    store.reconcile_blobs()
    store.complete_cleanup(ticket.id)
    assert not store.pending_cleanup() and not list(store.blobs.iterdir())


def test_unknown_legacy_blob_is_not_deleted_by_startup_or_ticket_cleanup(tmp_path):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    unknown = store.blobs / str(uuid4())
    unknown.write_bytes(b'Unknown selected-by-nobody canary')
    ticket = store.delete_profile(owner)
    reopened = Store(tmp_path)
    assert unknown.exists()
    report = reopened.reconcile_blobs()
    assert report['unknown_blobs'] == 1
    reopened.complete_cleanup(ticket.id)
    assert unknown.read_bytes() == b'Unknown selected-by-nobody canary'


def test_server_id_collision_never_adopts_or_deletes_unknown_blob(tmp_path, monkeypatch):
    import uuid

    import copilot.store as module
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    collision = uuid.uuid4()
    unknown = store.blobs / str(collision)
    unknown.write_bytes(b'Unknown original bytes')
    ids = iter((collision, uuid.uuid4()))
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: next(ids))
    with pytest.raises(EvidenceError) as caught:
        store.add_source(owner, 'notes.txt', 'text/plain', b'New bytes', ['New bytes'], 'test')
    assert caught.value.code == 'CONFLICT'
    assert unknown.read_bytes() == b'Unknown original bytes'
    with store._tx() as db:
        assert not db.execute('SELECT 1 FROM upload_intents').fetchone()


def test_unknown_blob_racing_exclusive_open_survives_exception_and_restart(tmp_path, monkeypatch):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    original = store._write_blob
    collided = []
    def race(path, content):
        path.write_bytes(b'Unknown racing canary')
        collided.append(path)
        return original(path, content)
    monkeypatch.setattr(store, '_write_blob', race)
    with pytest.raises(FileExistsError):
        store.add_source(owner, 'notes.txt', 'text/plain', b'New upload', ['New upload'], 'test')
    assert collided[0].read_bytes() == b'Unknown racing canary'
    restarted = Store(tmp_path)
    restarted.reconcile_blobs()
    assert collided[0].read_bytes() == b'Unknown racing canary'


def test_crash_before_exclusive_creation_cannot_adopt_later_unknown_bytes(tmp_path):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    reserved = str(uuid4())
    with store._tx() as db:
        db.execute('INSERT INTO upload_intents VALUES(?,?,?,?)', (reserved, owner, 'writing', '0' * 64))
    path = store.blobs / reserved
    path.write_bytes(b'Unknown bytes after crashed reservation')
    assert Store(tmp_path).reconcile_blobs()['cleanup_pending'] == 1
    assert path.read_bytes() == b'Unknown bytes after crashed reservation'


def test_cleaned_absent_uploads_are_not_rewritten_each_poll(tmp_path, monkeypatch):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    source = store.add_source(owner, 'notes.txt', 'text/plain', b'Owned', ['Owned'], 'test')
    store.complete_cleanup(store.delete_source(owner, source.id).id)
    def unexpected(_source_id):
        pytest.fail('Terminal absent upload must not enter mutation cleanup on each poll')
    monkeypatch.setattr(store, 'cleanup_upload', unexpected)
    assert store.reconcile_blobs()['cleanup_pending'] == 0
    assert store.reconcile_blobs()['cleanup_pending'] == 0


def test_unreceipted_absence_is_not_a_file_ownership_proof(tmp_path):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    reserved = str(uuid4())
    with store._tx() as db:
        db.execute('INSERT INTO upload_intents VALUES(?,?,?,?)', (reserved, owner, 'writing', '0' * 64))
    assert not (store.blobs / reserved).exists()
    assert store.reconcile_blobs()['cleanup_pending'] == 1
    with store._tx() as db:
        assert db.execute('SELECT state FROM upload_intents WHERE id=?', (reserved,)).fetchone()[0] != 'cleaned'


def test_nonownership_exclusion_survives_profile_forget_without_deleting_collision(tmp_path, monkeypatch):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    original = store._write_blob
    paths = []
    def race(path, content):
        path.write_bytes(b'Unknown racing bytes')
        paths.append(path)
        return original(path, content)
    monkeypatch.setattr(store, '_write_blob', race)
    with pytest.raises(FileExistsError):
        store.add_source(owner, 'notes.txt', 'text/plain', b'New', ['New'], 'test')
    ticket = store.delete_profile(owner)
    store.complete_cleanup(ticket.id)
    restarted = Store(tmp_path)
    assert restarted.reconcile_blobs()['unknown_blobs'] == 1
    assert paths[0].read_bytes() == b'Unknown racing bytes'
    assert not restarted.pending_cleanup()


def test_acquisition_receipt_cannot_delete_replaced_unrelated_inode(tmp_path, monkeypatch):
    store = Store(tmp_path)
    owner = store.create_profile('Synthetic', ['tech']).id
    source = store.add_source(owner, 'notes.txt', 'text/plain', b'Owned', ['Owned'], 'test')
    path = store.blobs / source.id
    # Atomic replacement guarantees a different live inode, not inode reuse speculation.
    replacement = tmp_path / 'outside-replacement'
    replacement.write_bytes(b'Unrelated replacement canary')
    replacement.replace(path)
    ticket = store.delete_source(owner, source.id)
    with pytest.raises(EvidenceError) as caught:
        store.complete_cleanup(ticket.id)
    assert caught.value.code == 'CLEANUP_PENDING'
    assert path.read_bytes() == b'Unrelated replacement canary'
    assert Store(tmp_path).reconcile_blobs()['cleanup_pending'] == 1
