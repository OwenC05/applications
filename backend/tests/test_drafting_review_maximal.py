"""Actual maximal synthetic review graph, uncached/default-recursion qualification."""
import asyncio
import json
import sys
from threading import Lock
from time import monotonic

import httpx
import pytest
from test_drafting_edits import request as edit_request
from test_drafting_reviews import preview_body, review_body, reviewing  # noqa: F401
from test_drafting_service import execute
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.api import create_app
from copilot.config import Settings
from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ConsentInput
from copilot.drafting.admission_runtime import DraftAdmission
from copilot.drafting.assessments import DraftAssessments
from copilot.drafting.review_contracts import ReviewRequest
from copilot.drafting.service import DraftingService
from copilot.jobs import Jobs
from copilot.provider import Provider
from copilot.worker import Worker


@pytest.fixture
def maximal_observations(monkeypatch, record_property):
    """Observe unchanged calls; never classify, suppress or replace failures."""
    summary = {'capture': {'calls': 0, 'seconds': 0.0, 'max_seconds': 0.0},
               'first_guard_error': None, 'first_nonterminal_guard_error': None,
               'guard_errors': [], 'guard_errors_total': 0,
               'first_provider_error': None}
    observation_lock = Lock()
    capture = DraftAssessments._capture
    assert_current = Jobs.assert_current
    send = Provider.send

    def emit(label, value):
        try:
            print(label + json.dumps(value), flush=True)
        except Exception:
            # Diagnostic output cannot replace an original product exception.
            pass

    def observed_capture(self, *args, **kwargs):
        started = monotonic()
        try:
            return capture(self, *args, **kwargs)
        finally:
            elapsed = monotonic() - started
            with observation_lock:
                timings = summary['capture']
                timings['calls'] += 1
                timings['seconds'] += elapsed
                timings['max_seconds'] = max(timings['max_seconds'], elapsed)

    def observed_assert_current(self, db, job_id, worker_id, fence):
        try:
            return assert_current(self, db, job_id, worker_id, fence)
        except EvidenceError as error:
            observation = {'code': error.code, 'requested_fence': fence}
            try:
                observation['observed_at'] = self._now().isoformat()
                retained, _, _ = self._job(db, job_id)
                observation.update(state=retained.state, stage=retained.stage,
                    fence=retained.fence, attempt_count=retained.attempt_count,
                    cancellation_requested=retained.cancellation_requested,
                    lease_owner_matches=retained.lease_owner == worker_id,
                    lease_expires_at=(retained.lease_expires_at.isoformat()
                                      if retained.lease_expires_at else None))
            except Exception:
                observation['diagnostic_status'] = 'unavailable'
            with observation_lock:
                summary['guard_errors_total'] += 1
                if len(summary['guard_errors']) < 128:
                    summary['guard_errors'].append(observation)
                if summary['first_guard_error'] is None:
                    summary['first_guard_error'] = observation
                if (summary['first_nonterminal_guard_error'] is None
                        and observation.get('state') in ('queued', 'running')):
                    summary['first_nonterminal_guard_error'] = observation
            emit('maximal guarded error: ', observation)
            raise

    def observed_send(self, *args, **kwargs):
        try:
            return send(self, *args, **kwargs)
        except Exception as error:
            observation = {'exception_chain': []}
            try:
                pending = [('raised', error)]
                seen = set()
                while pending and len(observation['exception_chain']) < 8:
                    relation, current = pending.pop(0)
                    if id(current) in seen:
                        continue
                    seen.add(id(current))
                    item = {'relation': relation, 'type': type(current).__name__}
                    if isinstance(current, EvidenceError):
                        item['code'] = current.code
                    observation['exception_chain'].append(item)
                    for name in ('__cause__', '__context__'):
                        linked = getattr(current, name)
                        if linked is not None:
                            pending.append((name, linked))
            except Exception:
                observation['diagnostic_status'] = 'unavailable'
            with observation_lock:
                if summary['first_provider_error'] is None:
                    summary['first_provider_error'] = observation
                    emit('maximal first provider error: ', observation)
            raise

    monkeypatch.setattr(DraftAssessments, '_capture', observed_capture)
    monkeypatch.setattr(Jobs, 'assert_current', observed_assert_current)
    monkeypatch.setattr(Provider, 'send', observed_send)
    try:
        yield summary
    finally:
        record_property('actual_193_link_guard_observations', json.dumps(summary))
        emit('maximal guard observations: ', summary)


def test_actual_193_link_history_uncached_detail_export_replay_and_denial(
        reviewing, record_property, tmp_path, maximal_observations):  # noqa: F811
    service, scope, _, sent, settings = reviewing
    initial_recursion_limit = sys.getrecursionlimit()
    started = monotonic()
    with service.store._read() as db:
        consent = service.domain._singleton(db, scope.profile_id, 'consent', c.ConsentRecord)
    service.domain.consent(scope.profile_id, ConsentInput(expected_consent_revision=consent.revision,
        provider='openai', purposes=('research', 'drafting', 'assessment'), granted=True))
    generated, *_ = execute(reviewing)
    body = review_body(service, scope, generated, key='max-review-0')
    current = service.review(scope.profile_id, scope.application_id, generated.id, body)
    replay_parent = generated
    replay_body = body
    for index in range(1, 65):
        edited = service.edit(scope.profile_id, scope.application_id, current.id,
            edit_request(service, scope, current, 'Exact Café e\u0301 😀\r\n', key=f'max-edit-{index}'))
        assessed, _, _ = _assess(service, scope, edited, sent, settings, index)
        body = review_body(service, scope, assessed, key=f'max-review-{index}')
        current = service.review(scope.profile_id, scope.application_id, assessed.id, body)
        replay_parent, replay_body = assessed, body
        if index % 16 == 0:
            print(f'maximal graph: {index} edits, {index} assessments, {index + 1} reviews; '
                  f'{monotonic() - started:.2f}s', flush=True)
    setup_seconds = monotonic() - started
    record_property('actual_193_link_setup_seconds', setup_seconds)
    assert len(sent) == 67 and sys.getrecursionlimit() == initial_recursion_limit
    # A new service has no warm invocation context. Each public call independently
    # reconstructs retained history at the unchanged interpreter recursion limit.
    restarted = DraftingService(service.store)
    verification_started = monotonic()
    detail = restarted.detail(scope.profile_id, scope.application_id, current.id)
    assert detail['current'] and detail['reviewed_current'] and not detail['review_eligible']
    assert detail['review_snapshot']['draft_id'] == current.id and not detail['browser_eligible']
    assert current.answers[0].text in restarted.export(scope.profile_id, scope.application_id, current.id)
    assert restarted.review(scope.profile_id, scope.application_id, replay_parent.id, replay_body) == current
    with restarted.store._read() as db:
        counts = restarted._lineage_counts(db, scope.profile_id, scope.application_id, current.id)
        assert (counts.edits, counts.assessments, counts.reviews, counts.total) == (64, 64, 65, 193)
        assert db.execute("SELECT count(*) FROM records WHERE kind='workspace:draft_dependency' "
                          "AND json_extract(data,'$.origin')='review'").fetchone() == (65,)
    next_edit = edit_request(restarted, scope, current, 'Not accepted', key='max-edit-65')
    with pytest.raises(EvidenceError):
        restarted.edit(scope.profile_id, scope.application_id, current.id, next_edit)
    next_preview = preview_body(restarted, scope, current)
    with pytest.raises(EvidenceError):
        restarted.review_preview(scope.profile_id, scope.application_id, current.id, next_preview)
    next_review = ReviewRequest(**next_preview.model_dump(), idempotency_key='max-review-66',
                               disclosure_sha256='0' * 64, acknowledged=True)
    with pytest.raises(EvidenceError):
        restarted.review(scope.profile_id, scope.application_id, current.id, next_review)
    assert restarted.domain.application(scope.profile_id, scope.application_id).output_revision == current.revisions.application_output
    assert len(sent) == 67 and sys.getrecursionlimit() == initial_recursion_limit
    record_property('actual_193_link_uncached_verification_seconds', monotonic() - verification_started)
    _actual_http_responsiveness(service, scope, current, tmp_path, record_property)
    # Both depth authority and cycles are checked on a fresh traversal, not a memo
    # warmed by the successful maximum qualification above. Restore exact bytes
    # between faults so each proves its own failure independently.
    with restarted.store._read() as db:
        row = db.execute("SELECT id,data FROM records WHERE kind='workspace:draft_dependency' "
                         "AND json_extract(data,'$.draft_id')=?", (current.id,)).fetchone()
    for changes in ({'edit_depth': 0}, {'parent_draft_id': current.id}):
        raw = json.loads(row[1]) | changes
        with restarted.store._tx() as db:
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), row[0]))
        try:
            with pytest.raises(EvidenceError):
                DraftingService(service.store).detail(scope.profile_id, scope.application_id, current.id)
        finally:
            with restarted.store._tx() as db:
                db.execute('UPDATE records SET data=? WHERE id=?', (row[1], row[0]))
    assert DraftingService(service.store).detail(scope.profile_id, scope.application_id, current.id)['reviewed_current']


def _assess(service, scope, parent, sent, settings, index):
    from test_drafting_assessment import assessment_body, capture_assessment_launch

    local = service, scope, parent, sent, settings
    body = assessment_body(local, key=f'max-assess-{index}')
    launch = capture_assessment_launch(service, scope, body)
    job = service.enqueue_assessment(scope.profile_id, scope.application_id, parent.id, launch)
    worker = Worker(service.store, lambda: pytest.fail('assessment loaded retrieval service'),
                    drafting_factory=lambda *_: service)
    assert (worker.lease_seconds, worker.interval) == (30, 10)
    outcome = worker.run_once(job.id, recovery=False)
    finished_at = service.jobs._now()
    assert outcome == {'job_id': job.id, 'state': 'completed'}
    assert worker.last_outcome['heartbeat'] in (
        {'state': 'stopped'}, {'state': 'authority_lost', 'code': 'AUTHORITY_LOST'})
    assert worker.last_outcome['recovery'] == {'state': 'not_requested'}
    with service.store._read() as db:
        terminal, _, _ = service.jobs._job(db, job.id, scope.profile_id)
        assert (terminal.id, terminal.profile_id, terminal.application_id, terminal.kind,
                terminal.stage, terminal.state, terminal.lease_owner, terminal.fence,
                terminal.attempt_count, terminal.cancellation_requested) == (
                    job.id, scope.profile_id, scope.application_id, 'assess',
                    'assessment_published', 'completed', worker.worker_id, job.fence + 1, 1, False)
        # authority_lost is acceptable only as a postterminal renewal race on an
        # exactly certified completed operation whose retained lease was still
        # live when run_once returned. Expiry/failed execution never qualifies.
        assert terminal.lease_expires_at > finished_at
        certificate = DraftAdmission(service)._completed_publication_certificate(
            db, scope.profile_id, scope.application_id, job.id)
        assert (certificate.job_id, certificate.owner, certificate.application_id,
                certificate.kind, certificate.fence) == (
                    job.id, scope.profile_id, scope.application_id, 'assess', terminal.fence)
        child = service.domain._get(db, scope.profile_id, certificate.draft_id, 'draft', c.DraftRevision)
        assert (child.id, child.profile_id, child.application_id, child.revisions) == (
            certificate.draft_id, scope.profile_id, scope.application_id,
            parent.revisions.model_copy(update={'application_output': parent.revisions.application_output + 1}))
    return child, job, body


def _actual_http_responsiveness(service, scope, current, tmp_path, record_property):
    api = create_app(Settings(service.store.root, tmp_path / 'absent-models'),
                     lambda *_: pytest.fail('read-only HTTP loaded retrieval models'))
    base = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{current.id}'

    async def scenario():
        observations = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                     base_url='http://127.0.0.1:3001') as client:
            status = await client.get('/api/workspace/status')
            assert status.status_code == 200
            client.headers['x-evidence-token'] = status.json()['boot_token']

            async def measured(path):
                start = monotonic()
                response = await client.get(path)
                return response, monotonic() - start

            detail = asyncio.create_task(measured(base))
            export = asyncio.create_task(measured(base + '/export'))
            # Yield scheduling only; no sleeps/delays/fakes inside product code.
            await asyncio.sleep(0)
            for _ in range(3):
                response, elapsed = await measured('/health')
                assert response.status_code == 200
                observations.append(dict(seconds=elapsed,
                    graph_requests_pending=not detail.done() or not export.done()))
            (detail_response, detail_seconds), (export_response, export_seconds) = await asyncio.gather(detail, export)
            assert detail_response.status_code == 200, detail_response.text
            assert detail_response.json()['reviewed_current']
            assert export_response.status_code == 200, export_response.text
            assert current.answers[0].text in export_response.text
            assert any(item['graph_requests_pending'] for item in observations)
            return dict(detail_seconds=detail_seconds, export_seconds=export_seconds, health=observations)

    observations = asyncio.run(scenario())
    record_property('actual_193_link_asgi_observations', json.dumps(observations))
    print('maximal ASGI observations: ' + json.dumps(observations), flush=True)
