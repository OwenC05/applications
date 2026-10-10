"""A rejected unpaid packet job fails promptly, without widening publication authority."""
from datetime import timedelta
from uuid import uuid4

import pytest
from test_packet_service import packets, queued  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.retrieval.packet_service import PacketService
from copilot.worker import Worker


@pytest.mark.parametrize('phase', ['before', 'tokenize'])
def test_stale_binding_worker_failure_is_terminal_not_reclaimed(packets, monkeypatch, phase):  # noqa: F811
    store, _, _, _, scope, evidence, service = packets
    job, _ = queued(packets)
    if phase == 'before':
        evidence.build(scope.profile_id, 'facts')
    else:
        original = evidence.models.tokenize_offsets
        changed = False
        def replace(text):
            nonlocal changed
            if not changed:
                changed = True
                evidence.build(scope.profile_id, 'facts')
            return original(text)
        monkeypatch.setattr(evidence.models, 'tokenize_offsets', replace)
    worker = Worker(store, lambda: pytest.fail('Unexpected model service initialization'),
                    packet_factory=lambda s, jobs: PacketService(s, evidence, jobs=jobs))
    assert worker.run_once(job.id, recovery=False)['error'] == 'CONFLICT'
    failed = worker.jobs.get(scope.profile_id, job.id)
    assert failed.state == 'failed' and failed.stage == 'packet_failed' and failed.attempt_count == 1
    worker.jobs.clock = lambda: failed.lease_expires_at + timedelta(seconds=1)
    assert worker.jobs.claim(str(uuid4()), job_id=job.id) is None
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:packet_batch'").fetchone()
        assert not db.execute('SELECT 1 FROM job_stages WHERE job_id=?', (job.id,)).fetchone()


@pytest.mark.parametrize('loss', ['worker', 'fence', 'expiry', 'reclaim', 'cancel', 'application', 'profile', 'completed'])
def test_failure_terminalization_never_overwrites_lost_authority(packets, loss):  # noqa: F811
    store, _, repo, app, scope, _, service = packets
    job, _ = queued(packets)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    fence = claimed.fence
    if loss == 'worker':
        worker = str(uuid4())
    elif loss == 'fence':
        fence += 1
    elif loss in ('expiry', 'reclaim'):
        service.jobs.clock = lambda: claimed.lease_expires_at + timedelta(seconds=1)
        if loss == 'reclaim':
            assert service.jobs.claim(str(uuid4()), job_id=job.id).fence == fence + 1
    elif loss == 'cancel':
        service.jobs.cancel(scope.profile_id, job.id)
    elif loss == 'application':
        from copilot.domain.repository import ApplicationPatch
        repo.delete_application(scope.profile_id, app.application_id, ApplicationPatch(
            expected_input_revision=app.input_revision, expected_output_revision=app.output_revision))
    elif loss == 'profile':
        store.delete_profile(scope.profile_id)
    else:
        service.run_packets(job.id, worker, fence)
    with store._read() as db:
        before = db.execute('SELECT state,data,parameters,dependencies FROM jobs WHERE id=?', (job.id,)).fetchone()
    with pytest.raises(EvidenceError):
        service.jobs.fail_packet(job.id, worker, fence)
    with store._read() as db:
        assert db.execute('SELECT state,data,parameters,dependencies FROM jobs WHERE id=?', (job.id,)).fetchone() == before


@pytest.mark.parametrize('state', ['prepared', 'indeterminate'])
def test_packet_failure_refuses_uncertain_provider_attempt_without_mutation(packets, state):  # noqa: F811
    store, _, _, _, scope, _, service = packets
    job, _ = queued(packets)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    # Deliberately malformed synthetic row: uncertainty must block even decoding,
    # rather than treating packet kind as a shortcut around paid-attempt protection.
    with store._tx() as db:
        db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
            (str(uuid4()), scope.profile_id, job.id, state, '{"synthetic_reserved_tokens":5000}',
             1, '2026-10-10', 'synthetic-response', 'synthetic', 'a' * 64))
        before = db.execute('SELECT * FROM provider_attempts WHERE job_id=?', (job.id,)).fetchall()
    with pytest.raises(EvidenceError) as failure:
        service.jobs.fail_packet(job.id, worker, claimed.fence)
    assert failure.value.code == 'CONFLICT'
    with store._read() as db:
        assert db.execute('SELECT * FROM provider_attempts WHERE job_id=?', (job.id,)).fetchall() == before
    assert service.jobs.get(scope.profile_id, job.id).state == 'running'


def test_packet_failure_cannot_terminalize_non_packet_job(packets):  # noqa: F811
    _, _, _, _, scope, _, service = packets
    job = service.jobs.enqueue(scope.profile_id, 'index', {'corpus': 'facts'}, 'foreign-kind')
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job.id)
    with pytest.raises(EvidenceError):
        service.jobs.fail_packet(job.id, worker, claimed.fence)
    assert service.jobs.get(scope.profile_id, job.id).state == 'running'
