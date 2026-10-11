"""Actual Store/domain privacy hooks, synthetic SQLite with no provider traffic."""
import json
import sqlite3
from uuid import uuid4

import pytest
from admission_legacy_helpers import preupgrade_copy

from copilot import admission_state as a
from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import (
    ApplicationInput,
    ApplicationPatch,
    DomainRepository,
    ProposalDelete,
    ProposalInput,
)
from copilot.jobs import Jobs
from copilot.store import Store


@pytest.fixture
def activated(tmp_path):
    store = Store(tmp_path / 'data')
    owner = store.create_profile('Synthetic', ['tech']).id
    domain = DomainRepository(store)
    app = domain.create_application(owner, ApplicationInput(
        expected_metadata_revision=0, company='Synthetic', role='Engineer',
        sector='tech', vacancy_url='https://example.test/job'))
    return store, owner, domain, app


def head(store, owner, app):
    with store._read() as db:
        return a.read_head(db, owner, app.application_id)


def legacy_paid(activated, tmp_path, *, kind='draft'):
    store, owner, _, app = activated
    jobs = Jobs(store)
    with store._read() as db:
        revisions = jobs.capture(db, owner, app.application_id)
    job = c.DurableJob(id=str(uuid4()), profile_id=owner,
        application_id=app.application_id, kind=kind, stage='queued',
        idempotency_key='synthetic-legacy', state='queued', revisions=revisions, fence=0)
    target = tmp_path / ('legacy-' + kind)
    with preupgrade_copy(store, target) as db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
            (job.id, owner, job.state, job.model_dump_json(), '{}', '[]'))
        db.execute('INSERT INTO job_keys VALUES(?,?,?,?,?,?)',
            (owner, job.idempotency_key, job.id, 'a' * 64, app.application_id, kind))
    return Store(target), job


def test_fresh_store_bootstraps_in_fresh_writer_before_jobs(tmp_path):
    store = Store(tmp_path / 'empty')
    with store._read() as db:
        assert db.execute('PRAGMA user_version').fetchone() == (1,)
        assert db.execute(f'SELECT * FROM {a.HEADS}').fetchall() == []
        assert db.execute(f'SELECT * FROM {a.BINDINGS}').fetchall() == []
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
    Store(store.root)
    Jobs(store)
    Store(store.root)


def test_new_application_has_zero_head_and_updates_never_reseed(activated):
    store, owner, domain, app = activated
    assert head(store, owner, app) == a.AdmissionHead(owner, app.application_id, 0, None)
    with store._tx() as db:
        a.invalidate_scope(db, owner, app.application_id)
    domain.patch_application(owner, app.application_id, ApplicationPatch(
        expected_input_revision=0, expected_output_revision=0, role='Analyst'))
    assert head(store, owner, app).revision == 1
    with store._tx() as db:
        db.execute(f'DELETE FROM {a.HEADS} WHERE application_id=?', (app.application_id,))
    with pytest.raises(EvidenceError) as raised:
        domain.patch_application(owner, app.application_id, ApplicationPatch(
            expected_input_revision=1, expected_output_revision=1, role='Other'))
    assert raised.value.code == 'ADMISSION_UNAVAILABLE'
    with store._read() as db:
        assert not db.execute(f'SELECT 1 FROM {a.HEADS}').fetchone()


def test_application_record_and_head_creation_rollback_together(activated, monkeypatch):
    store, owner, domain, _ = activated
    original = a.register_new_application
    def interrupted(db, application):
        original(db, application)
        raise RuntimeError('synthetic failure after both inserts')
    monkeypatch.setattr(a, 'register_new_application', interrupted)
    with pytest.raises(RuntimeError):
        domain.create_application(owner, ApplicationInput(expected_metadata_revision=1,
            company='Other', role='Analyst', sector='tech', vacancy_url='https://example.test/2'))
    with store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='workspace:application'").fetchone() == (1,)
        assert db.execute(f'SELECT COUNT(*) FROM {a.HEADS}').fetchone() == (1,)
        assert domain.revisions(db, owner).metadata == 1


def test_bootstrap_migrates_old_paid_jobs_without_ack_or_lease(activated, tmp_path):
    store, owner, _, app = activated
    upgraded, job = legacy_paid(activated, tmp_path)
    with upgraded._read() as db:
        assert db.execute(f'SELECT origin,captured_revision,consumed_revision,request_sha256,envelope_sha256 FROM {a.BINDINGS} WHERE job_id=?', (job.id,)).fetchone() == ('legacy', None, None, None, None)
        assert db.execute('SELECT data,parameters FROM jobs WHERE id=?', (job.id,)).fetchone() == (job.model_dump_json(), '{}')
    assert head(upgraded, owner, app).revision == 1
    Store(upgraded.root)
    assert head(upgraded, owner, app).revision == 1
    with store._read() as db:
        assert not db.execute('SELECT 1 FROM jobs WHERE id=?', (job.id,)).fetchone()


@pytest.mark.parametrize('kind', ['draft', 'assess'])
def test_forgetting_marks_full_paid_scrub_preserves_binding_and_restart(activated, tmp_path, kind):
    _, owner, _, app = activated
    store, job = legacy_paid(activated, tmp_path, kind=kind)
    fact = store.confirm_fact(owner, 'Synthetic fact')
    store.revoke_fact(owner, fact.id)
    with store._read() as db:
        assert db.execute('SELECT owner,kind FROM tombstones WHERE id=?', (job.id,)).fetchone() == (owner, 'drafting_job_forgotten')
        record, parameters = db.execute('SELECT data,parameters FROM jobs WHERE id=?', (job.id,)).fetchone()
        assert json.loads(record)['idempotency_key'] == 'forgotten'
        assert json.loads(parameters) == {}
        assert not db.execute('SELECT 1 FROM job_keys WHERE job_id=?', (job.id,)).fetchone()
        assert db.execute(f'SELECT origin FROM {a.BINDINGS} WHERE job_id=?', (job.id,)).fetchone() == ('legacy',)
    assert head(store, owner, app).revision == 2
    Store(store.root)
    assert head(store, owner, app).revision == 2


def test_fact_forget_invalidates_all_owned_heads_not_other_owner(activated):
    store, owner, _, app = activated
    other = store.create_profile('Other', ['finance']).id
    other_app = DomainRepository(store).create_application(other, ApplicationInput(
        expected_metadata_revision=0, company='Other', role='Analyst', sector='finance',
        vacancy_url='https://example.test/other'))
    fact = store.confirm_fact(owner, 'Synthetic fact')
    store.revoke_fact(owner, fact.id)
    assert head(store, owner, app).revision == 1
    assert head(store, other, other_app).revision == 0


def test_pending_proposal_deletion_invalidates_without_forgetting_intact_jobs(activated, tmp_path):
    _, owner, _, app = activated
    store, job = legacy_paid(activated, tmp_path)
    domain = DomainRepository(store)
    proposal = domain.propose(owner, ProposalInput(expected_metadata_revision=1, text='Pending synthetic fact'))
    with store._read() as db:
        before = db.execute('SELECT * FROM jobs WHERE id=?', (job.id,)).fetchone()
        metadata = domain.revisions(db, owner).metadata
    domain.delete_proposal(owner, proposal.id, ProposalDelete(expected_metadata_revision=metadata, expected_facts_revision=0))
    assert head(store, owner, app).revision == 2
    with store._read() as db:
        assert domain.revisions(db, owner).metadata == metadata + 1
        assert db.execute('SELECT * FROM jobs WHERE id=?', (job.id,)).fetchone() == before
        assert not db.execute('SELECT 1 FROM tombstones WHERE id=?', (job.id,)).fetchone()


def test_privacy_saturates_max_without_resetting_or_refunding(activated):
    store, owner, _, app = activated
    with store._tx() as db:
        db.execute(f'UPDATE {a.HEADS} SET revision=?', (a.MAX_REVISION,))
    fact = store.confirm_fact(owner, 'Synthetic fact')
    store.revoke_fact(owner, fact.id)
    assert head(store, owner, app).revision == a.MAX_REVISION


def test_profile_delete_validates_live_heads_before_removing_app_records(activated):
    store, owner, _, app = activated
    with store._tx() as db:
        db.execute(f'DELETE FROM {a.HEADS} WHERE application_id=?', (app.application_id,))
    with pytest.raises(EvidenceError) as raised:
        store.delete_profile(owner)
    assert raised.value.code == 'ADMISSION_UNAVAILABLE'
    with store._read() as db:
        assert db.execute('SELECT 1 FROM profiles WHERE id=?', (owner,)).fetchone()
        assert db.execute('SELECT 1 FROM records WHERE id=?', (app.application_id,)).fetchone()
        assert not db.execute('SELECT 1 FROM tombstones WHERE id IN (?,?)', (owner, app.application_id)).fetchone()


@pytest.mark.parametrize('mismatch', ['owner', 'key'])
def test_application_save_binds_caller_scope_before_registration(activated, mismatch):
    store, owner, domain, app = activated
    new = app.model_copy(update={'application_id': str(uuid4())})
    with pytest.raises(EvidenceError):
        with store._tx() as db:
            domain._save(db, str(uuid4()) if mismatch == 'owner' else owner,
                         'application', new, key=str(uuid4()) if mismatch == 'key' else None)
    with store._read() as db:
        assert not db.execute('SELECT 1 FROM records WHERE id=?', (new.application_id,)).fetchone()
        assert db.execute(f'SELECT COUNT(*) FROM {a.HEADS}').fetchone() == (1,)


@pytest.mark.parametrize('wrong_marker', ['foreign', 'wrong_kind'])
def test_forgetting_conflicting_marker_rolls_back_everything(activated, tmp_path, wrong_marker):
    _, owner, _, app = activated
    store, job = legacy_paid(activated, tmp_path)
    fact = store.confirm_fact(owner, 'Synthetic fact')
    with store._tx() as db:
        db.execute('INSERT INTO tombstones VALUES(?,?,?)', (job.id,
            str(uuid4()) if wrong_marker == 'foreign' else owner,
            'drafting_job_forgotten' if wrong_marker == 'foreign' else 'workspace:draft'))
        baseline = {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('records', 'jobs', 'job_keys', a.HEADS, a.BINDINGS, 'tombstones')}
    with pytest.raises(EvidenceError):
        store.revoke_fact(owner, fact.id)
    with store._read() as db:
        assert {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                for table in baseline} == baseline
    assert head(store, owner, app).revision == 1


def test_marker_precedes_key_scrub_and_failure_rolls_back_receipt(activated, tmp_path):
    _, owner, _, app = activated
    store, job = legacy_paid(activated, tmp_path)
    fact = store.confirm_fact(owner, 'Synthetic fact')
    with store._tx() as db:
        db.execute("""CREATE TRIGGER require_receipt BEFORE DELETE ON job_keys
            WHEN OLD.job_id='%s' BEGIN
            SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM tombstones
                WHERE id=OLD.job_id AND owner=OLD.owner AND kind='drafting_job_forgotten')
                THEN RAISE(ABORT,'receipt missing before key scrub') END;
            SELECT RAISE(ABORT,'synthetic interruption after receipt'); END""" % job.id)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic interruption after receipt'):
        store.revoke_fact(owner, fact.id)
    with store._read() as db:
        assert not db.execute('SELECT 1 FROM tombstones WHERE id=?', (job.id,)).fetchone()
        assert db.execute('SELECT 1 FROM job_keys WHERE job_id=?', (job.id,)).fetchone()
        assert db.execute('SELECT 1 FROM records WHERE id=?', (fact.id,)).fetchone()
    assert head(store, owner, app).revision == 1


def test_legacy_invalid_parameters_do_not_prevent_paid_scope_forgetting(activated, tmp_path):
    _, owner, _, app = activated
    store, job = legacy_paid(activated, tmp_path)
    with store._tx() as db:
        db.execute('UPDATE jobs SET parameters=? WHERE id=?', ('{invalid JSON', job.id))
    fact = store.confirm_fact(owner, 'Synthetic fact')
    store.revoke_fact(owner, fact.id)
    Store(store.root)
    with store._read() as db:
        assert db.execute('SELECT parameters FROM jobs WHERE id=?', (job.id,)).fetchone() == ('{}',)
        assert db.execute('SELECT owner,kind FROM tombstones WHERE id=?', (job.id,)).fetchone() == (owner, 'drafting_job_forgotten')
    assert head(store, owner, app).revision == 2
