"""Real competing local writes/admission, ordered before the final writer."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_drafting_edits import request as edit_request
from test_drafting_reviews import review_body, reviewing  # noqa: F401
from test_drafting_service import body, capture_draft_launch, execute
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError


@pytest.mark.parametrize('outer,winner', [('review', 'edit'), ('edit', 'review'),
                                          ('review', 'admission'), ('admission', 'review')])
def test_actual_competing_review_edit_and_admission_winner_orders(reviewing, monkeypatch, outer, winner):  # noqa: F811
    service, scope, _, sent, _ = reviewing
    parent, *_ = execute(reviewing)
    review = review_body(service, scope, parent, key='race-review')
    edit = edit_request(service, scope, parent, 'Competing exact local edit', key='race-edit')
    launch = capture_draft_launch(reviewing, body(reviewing, key='race-admission'))
    with service.store._read() as db:
        attempts_before = db.execute('SELECT * FROM provider_attempts ORDER BY id').fetchall()
        head_before = db.execute('SELECT * FROM drafting_admission_heads ORDER BY owner,application_id').fetchall()
        selected_head_before = db.execute('SELECT revision,last_job_id FROM drafting_admission_heads '
                                          'WHERE owner=? AND application_id=?',
                                          (scope.profile_id, scope.application_id)).fetchone()
    service.provider._key_supplier = lambda: pytest.fail('Competing local operations read a key')
    service.provider.transport = lambda *a, **k: pytest.fail('Competing local operations sent provider data')
    calls = {
        'review': lambda: service.review(scope.profile_id, scope.application_id, parent.id, review),
        'edit': lambda: service.edit(scope.profile_id, scope.application_id, parent.id, edit),
        'admission': lambda: service.enqueue(scope.profile_id, scope.application_id, launch),
    }
    captured = threading.Event()
    release = threading.Event()
    outer_thread = []
    original = service._verify

    def verified(capture):
        original(capture)
        if threading.get_ident() in outer_thread:
            captured.set()
            assert release.wait(30), 'Competing operation did not release the harness barrier'

    def pending_outer():
        outer_thread.append(threading.get_ident())
        return calls[outer]()

    monkeypatch.setattr(service, '_verify', verified)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(pending_outer)
        try:
            assert captured.wait(30), 'Outer operation did not reach source verification'
            committed = calls[winner]()
        finally:
            release.set()
        with pytest.raises(EvidenceError) as error:
            pending.result(timeout=30)
        assert error.value.code == 'CONFLICT'
    output = service.domain.application(scope.profile_id, scope.application_id).output_revision
    assert output == parent.revisions.application_output + (winner != 'admission')
    with service.store._read() as db:
        assert db.execute('SELECT * FROM provider_attempts ORDER BY id').fetchall() == attempts_before
        reviews = db.execute("SELECT id FROM records WHERE owner=? AND kind='workspace:draft_review'",
                             (scope.profile_id,)).fetchall()
        assert len(reviews) == (winner == 'review')
        assert db.execute("SELECT count(*) FROM records WHERE owner=? AND kind='workspace:draft_dependency' "
                          "AND json_extract(data,'$.origin')='edit'", (scope.profile_id,)).fetchone()[0] == (winner == 'edit')
        inserted = db.execute('SELECT job_id FROM job_keys WHERE owner=? AND key=?',
                              (scope.profile_id, 'race-admission')).fetchall()
        head_after = db.execute('SELECT * FROM drafting_admission_heads ORDER BY owner,application_id').fetchall()
        selected_head_after = db.execute('SELECT revision,last_job_id FROM drafting_admission_heads '
                                         'WHERE owner=? AND application_id=?',
                                         (scope.profile_id, scope.application_id)).fetchone()
    if winner == 'admission':
        assert inserted == [(committed.id,)] and committed.state == 'queued'
        assert selected_head_after == (selected_head_before[0] + 1, committed.id)
        assert service.admission_state(scope.profile_id, scope.application_id)['reason_codes'] == ['active_operation']
    else:
        assert inserted == [] and head_after == head_before
        detail = service.detail(scope.profile_id, scope.application_id, committed.id)
        assert detail['current'] and detail['publication']['origin'] == winner
        assert detail['reviewed_current'] is (winner == 'review')
    assert len(sent) == 3
