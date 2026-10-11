"""Real process boundaries and SQLite rollback; no runtime fault switches."""
import multiprocessing
from contextlib import contextmanager
from uuid import uuid4

import pytest
from test_drafting_service import body, capture_draft_launch, drafting  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError


def _admit(root, scope, launch, barrier, outcomes):
    from copilot.drafting.service import DraftingService
    from copilot.store import Store

    service = DraftingService(Store(root))
    service._verify = lambda capture: barrier.wait(timeout=30)
    try:
        job = service.enqueue(scope.profile_id, scope.application_id, launch)
        outcomes.put(('ok', job.id))
    except EvidenceError as exc:
        outcomes.put(('error', exc.code))


def test_distinct_keys_same_capture_process_race_one_commit(drafting):  # noqa: F811
    service, scope, *_ = drafting
    launches = [capture_draft_launch(drafting, body(drafting, str(uuid4()))) for _ in range(2)]
    ctx = multiprocessing.get_context('spawn')
    barrier, outcomes = ctx.Barrier(2), ctx.Queue()
    children = [ctx.Process(target=_admit, args=(service.store.root, scope, launch, barrier, outcomes)) for launch in launches]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(45)
        assert all(child.exitcode == 0 for child in children)
        results = [outcomes.get(timeout=5) for _ in children]
        assert sorted(item[0] for item in results) == ['error', 'ok']
        assert next(item[1] for item in results if item[0] == 'error') == 'CONFLICT'
        state = service.admission_state(scope.profile_id, scope.application_id)
        assert state['admission_revision'] == 1
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(5)
        outcomes.close()
        outcomes.join_thread()


@pytest.mark.parametrize('statement', ['INSERT INTO jobs', 'INSERT INTO job_keys', 'INSERT INTO drafting_admission_bindings', 'UPDATE drafting_admission_heads'])
def test_failure_after_each_write_rolls_back_all_four(drafting, monkeypatch, statement):  # noqa: F811
    service, scope, *_ = drafting
    launch = capture_draft_launch(drafting, body(drafting))
    transaction = service.store._tx

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            result = self.connection.execute(sql, parameters)
            if sql.startswith(statement):
                raise RuntimeError('synthetic post-write crash')
            return result

        def __getattr__(self, key):
            return getattr(self.connection, key)

    @contextmanager
    def fail_transaction():
        with transaction() as db:
            yield FailingConnection(db)

    monkeypatch.setattr(service.store, '_tx', fail_transaction)
    with pytest.raises(RuntimeError, match='post-write'):
        service.enqueue(scope.profile_id, scope.application_id, launch)
    with service.store._read() as db:
        assert db.execute('SELECT revision,last_job_id FROM drafting_admission_heads WHERE owner=? AND application_id=?', (scope.profile_id, scope.application_id)).fetchone() == (0, None)
        assert not db.execute('SELECT 1 FROM job_keys WHERE owner=? AND key=?', (scope.profile_id, launch.request.idempotency_key)).fetchone()
        assert not db.execute("SELECT 1 FROM jobs WHERE owner=? AND json_extract(data,'$.kind')='draft'", (scope.profile_id,)).fetchone()
        assert not db.execute('SELECT 1 FROM drafting_admission_bindings WHERE owner=?', (scope.profile_id,)).fetchone()


def test_delayed_loser_remains_stale_after_winner_cancelled(drafting, monkeypatch):  # noqa: F811
    service, scope, *_ = drafting
    winner = capture_draft_launch(drafting, body(drafting, 'winner'))
    loser = capture_draft_launch(drafting, body(drafting, 'loser'))
    job = service.enqueue(scope.profile_id, scope.application_id, winner)
    service.jobs.cancel(scope.profile_id, job.id)
    monkeypatch.setattr(service, '_verify', lambda capture: pytest.fail('stale request touched originals'))
    with pytest.raises(EvidenceError) as error:
        service.enqueue(scope.profile_id, scope.application_id, loser)
    assert error.value.code == 'CONFLICT'
    assert service.admission_state(scope.profile_id, scope.application_id)['admission_revision'] == 1


def test_commit_restart_replays_original_capture_without_source_io(drafting, monkeypatch):  # noqa: F811
    from copilot.drafting.service import DraftingService

    service, scope, *_ = drafting
    launch = capture_draft_launch(drafting, body(drafting))
    job = service.enqueue(scope.profile_id, scope.application_id, launch)
    restarted = DraftingService(service.store)
    monkeypatch.setattr(restarted, '_verify', lambda capture: pytest.fail('restart replay touched originals'))
    assert restarted.enqueue(scope.profile_id, scope.application_id, launch).id == job.id


def _uncommitted_admit(root, scope, launch, ready):
    from copilot.drafting.service import DraftingService
    from copilot.store import Store

    service = DraftingService(Store(root))
    transaction = service.store._tx

    @contextmanager
    def stop_before_commit():
        with transaction() as db:
            yield db
            ready.send('uncommitted')
            ready.recv()

    service.store._tx = stop_before_commit
    service.enqueue(scope.profile_id, scope.application_id, launch)


def test_process_termination_before_commit_reopens_with_zero_partial_state(drafting):  # noqa: F811
    from copilot.store import Store

    service, scope, *_ = drafting
    launch = capture_draft_launch(drafting, body(drafting))
    ctx = multiprocessing.get_context('spawn')
    parent, child_pipe = ctx.Pipe()
    child = ctx.Process(target=_uncommitted_admit, args=(service.store.root, scope, launch, child_pipe))
    try:
        child.start()
        assert parent.poll(45)
        assert parent.recv() == 'uncommitted'
        child.terminate()
        child.join(10)
        assert child.exitcode is not None
        reopened = Store(service.store.root)
        with reopened._read() as db:
            assert db.execute('SELECT revision,last_job_id FROM drafting_admission_heads WHERE owner=? AND application_id=?', (scope.profile_id, scope.application_id)).fetchone() == (0, None)
            assert not db.execute('SELECT 1 FROM job_keys WHERE owner=? AND key=?', (scope.profile_id, launch.request.idempotency_key)).fetchone()
            assert not db.execute('SELECT 1 FROM drafting_admission_bindings WHERE owner=?', (scope.profile_id,)).fetchone()
    finally:
        if child.is_alive():
            child.terminate()
        child.join(10)
        parent.close()
        child_pipe.close()


@pytest.mark.parametrize('kind', ['generation', 'assessment'])
@pytest.mark.parametrize('privacy', ['delete', 'invalidate'])
def test_privacy_commits_after_outside_verification_before_admission_writer(drafting, monkeypatch, kind, privacy):  # noqa: F811
    from test_drafting_assessment import (
        assessment,
        assessment_body,
        capture_assessment_launch,
    )

    from copilot.admission_state import invalidate_scope

    service, scope, _, sent, _ = drafting
    if kind == 'generation':
        launch = capture_draft_launch(drafting, body(drafting))
        def operation():
            return service.enqueue(scope.profile_id, scope.application_id, launch)
    else:
        fixture = assessment.__wrapped__(drafting)
        parent = fixture[2]
        launch = capture_assessment_launch(service, scope, assessment_body(fixture))
        def operation():
            return service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, launch)
    verify = service._verify
    after_privacy = []

    def snapshot():
        with service.store._read() as db:
            return tuple(db.execute(query).fetchall() for query in (
                'SELECT * FROM jobs ORDER BY id', 'SELECT * FROM job_keys ORDER BY owner,key',
                'SELECT * FROM drafting_admission_bindings ORDER BY job_id',
                'SELECT * FROM drafting_admission_heads ORDER BY owner,application_id',
                'SELECT * FROM job_stages ORDER BY job_id,stage'))

    def privacy_between_capture_and_writer(capture):
        verify(capture)
        with service.store._tx() as db:
            if privacy == 'delete':
                service.store.forget_jobs(db, scope.profile_id, application_id=scope.application_id)
                db.execute('INSERT INTO tombstones VALUES(?,?,?)', (scope.application_id, scope.profile_id, 'workspace:application'))
                db.execute("DELETE FROM records WHERE id=? AND owner=? AND kind='workspace:application'", (scope.application_id, scope.profile_id))
            else:
                invalidate_scope(db, scope.profile_id, scope.application_id)
        after_privacy.append(snapshot())

    monkeypatch.setattr(service, '_verify', privacy_between_capture_and_writer)
    monkeypatch.setattr(service.provider, '_key_supplier', lambda: pytest.fail('admission read provider key'))
    sent_before = len(sent)
    with pytest.raises(EvidenceError) as error:
        operation()
    assert error.value.code == ('NOT_FOUND' if privacy == 'delete' else 'CONFLICT')
    assert len(after_privacy) == 1 and snapshot() == after_privacy[0]
    assert len(sent) == sent_before


@pytest.mark.parametrize('kind', ['generation', 'assessment'])
def test_purpose_revocation_between_fresh_snapshots_denies_without_consuming_head(drafting, monkeypatch, kind):  # noqa: F811
    from test_drafting_assessment import assessment, assessment_body, capture_assessment_launch

    from copilot.domain.repository import ConsentInput

    service, scope, _, sent, _ = drafting
    if kind == 'generation':
        launch = capture_draft_launch(drafting, body(drafting))
        parent = None
    else:
        fixture = assessment.__wrapped__(drafting)
        parent = fixture[2].id
        launch = capture_assessment_launch(service, scope, assessment_body(fixture))
    verify = service._verify

    def revoke_after_verification(capture):
        verify(capture)
        service.domain.consent(scope.profile_id, ConsentInput(
            expected_consent_revision=launch.request.expected_revisions.consent,
            provider='openai', purposes=('research',), granted=True))

    with service.store._read() as db:
        before = tuple(db.execute(sql).fetchall() for sql in ('SELECT * FROM jobs', 'SELECT * FROM job_keys', 'SELECT * FROM drafting_admission_bindings', 'SELECT * FROM drafting_admission_heads', 'SELECT * FROM job_stages'))
    sent_before = len(sent)
    monkeypatch.setattr(service, '_verify', revoke_after_verification)
    with pytest.raises(EvidenceError) as error:
        if parent is None:
            service.enqueue(scope.profile_id, scope.application_id, launch)
        else:
            service.enqueue_assessment(scope.profile_id, scope.application_id, parent, launch)
    assert error.value.code == 'CONSENT_REQUIRED'
    with service.store._read() as db:
        assert tuple(db.execute(sql).fetchall() for sql in ('SELECT * FROM jobs', 'SELECT * FROM job_keys', 'SELECT * FROM drafting_admission_bindings', 'SELECT * FROM drafting_admission_heads', 'SELECT * FROM job_stages')) == before
    assert len(sent) == sent_before
