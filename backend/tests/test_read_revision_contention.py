"""Revision authority reads must not upgrade deferred SQLite snapshots."""
import json
import sqlite3
from uuid import uuid4

import pytest
from test_packet_service import packets, queued  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import DomainRepository
from copilot.retrieval.packet_runtime_contracts import PacketParameters
from copilot.store import Store


@pytest.mark.parametrize('operation', ['domain', 'corpus', 'job', 'packet', 'reference'])
def test_packet_authority_on_query_only_connection(packets, operation):  # noqa: F811
    store, _, _, _, scope, evidence, service = packets
    job, _ = queued(packets)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    result = evidence.search_scoped(scope, ['finance'])
    with store._read() as db:
        db.execute('PRAGMA query_only=ON')
        if operation == 'domain':
            service.domain.revisions(db, scope.profile_id)
        elif operation == 'corpus':
            store._corpus_revision(db, scope.profile_id, 'facts')
        elif operation == 'job':
            service.jobs.assert_current(db, job.id, worker, claimed.fence)
        elif operation == 'packet':
            raw = service.jobs._job(db, job.id)[1]
            service._capture(db, PacketParameters.model_validate_json(json.dumps(raw)))
        else:
            assert result.references
            store.resolve_evidence_reference(db, result.binding, result.references[0], service.jobs._now())


def test_packet_capture_during_independent_heartbeat_writer(packets):  # noqa: F811
    store, _, _, _, scope, _, service = packets
    job, _ = queued(packets)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    writer = sqlite3.connect(store.db, timeout=.1)
    try:
        with store._read() as db:
            db.execute('SELECT data FROM jobs WHERE id=?', (job.id,)).fetchone()
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('UPDATE jobs SET state=state WHERE id=?', (job.id,))
            current, raw = service.jobs.assert_current(db, job.id, worker, claimed.fence)
            assert current.id == job.id
            service._capture(db, PacketParameters.model_validate_json(json.dumps(raw)))
            writer.rollback()
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize('kind', ['legacy', 'workspace'])
def test_profile_creation_initializes_revision_row_atomically(tmp_path, kind):
    store = Store(tmp_path / 'data')
    repo = DomainRepository(store)
    if kind == 'legacy':
        owner = store.create_profile('Synthetic', ['tech']).id
    else:
        owner = repo.create_profile(c.ProfileCreate(name='Synthetic', sectors=('finance',))).profile.profile_id
    with store._read() as db:
        db.execute('PRAGMA query_only=ON')
        assert db.execute('SELECT metadata,facts,documents,consent FROM profile_revisions WHERE owner=?',
                          (owner,)).fetchone() == (0, 0, 0, 0)
        assert repo.revisions(db, owner) == c.RevisionVector()
        assert store._corpus_revision(db, owner, 'facts') == 0


@pytest.mark.parametrize('operation', ['domain', 'corpus'])
def test_missing_revision_row_fails_closed_without_implicit_reset(tmp_path, operation):
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    store.confirm_fact(owner, 'A confirmed synthetic fact')
    with store._tx() as db:
        db.execute('DELETE FROM profile_revisions WHERE owner=?', (owner,))
    with store._read() as db:
        with pytest.raises(EvidenceError) as failure:
            if operation == 'domain':
                DomainRepository(store).revisions(db, owner)
            else:
                store._corpus_revision(db, owner, 'facts')
        assert failure.value.code == 'CONFLICT'
        assert not db.execute('SELECT 1 FROM profile_revisions WHERE owner=?', (owner,)).fetchone()


def test_startup_backfills_legacy_and_preserves_existing_revisions(tmp_path):
    store = Store(tmp_path / 'data')
    legacy = store.create_profile('Legacy synthetic', ['tech']).id
    current = store.create_profile('Current synthetic', ['finance']).id
    store.confirm_fact(legacy, 'A synthetic fact')
    with store._tx() as db:
        db.execute('DELETE FROM profile_revisions WHERE owner=?', (legacy,))
        db.execute('UPDATE profile_revisions SET metadata=7,facts=3,documents=5,consent=2 WHERE owner=?', (current,))
    reopened = Store(store.root)
    with reopened._read() as db:
        assert db.execute('SELECT metadata,facts,documents,consent FROM profile_revisions WHERE owner=?',
                          (legacy,)).fetchone() == (0, 1, 1, 0)
        assert db.execute('SELECT metadata,facts,documents,consent FROM profile_revisions WHERE owner=?',
                          (current,)).fetchone() == (7, 3, 5, 2)
