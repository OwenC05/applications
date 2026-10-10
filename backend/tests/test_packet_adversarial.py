"""Independent hostile packet cases; synthetic originals and real local Chroma only."""
import json
import sqlite3
from uuid import uuid4

import pytest
from test_packet_service import packets, request, run  # noqa: F401
from test_scoped_generations import second_scope
from test_scoped_retrieval import build, runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain.repository import ApplicationPatch


@pytest.mark.parametrize('corruption', ['missing_stage', 'failed_intent'])
def test_stale_history_does_not_hide_broken_publication_association(packets, corruption):  # noqa: F811
    store, _, _, app, scope, _, service = packets
    batch, job, _ = run(packets)
    store.confirm_fact(scope.profile_id, 'Make immutable history stale first')
    assert not service.detail(scope.profile_id, app.application_id, batch.id)['current']
    with store._tx() as db:
        if corruption == 'missing_stage':
            db.execute('DELETE FROM job_stages WHERE job_id=?', (job.id,))
        else:
            db.execute("UPDATE records SET data=json_set(data,'$.state','failed') WHERE kind='workspace:packet_intent'")
    for inspect in (
        lambda: service.detail(scope.profile_id, app.application_id, batch.id),
        lambda: service.list(scope.profile_id, app.application_id),
        lambda: service.question(scope.profile_id, app.application_id, batch.id, 'writing'),
    ):
        with pytest.raises(EvidenceError) as raised:
            inspect()
        assert raised.value.code == 'CONFLICT'


def test_fact_revocation_scrubs_missing_and_wrong_job_orphans_preserving_other_owner(packets):  # noqa: F811
    store, _, _, _, scope, _, _ = packets
    fact = store.confirm_fact(scope.profile_id, 'PRIVATE-ORPHAN-FACT')
    other = store.create_profile('Other synthetic owner', ['finance']).id
    # Simulate derivatives surviving loss/corruption of the jobs association.
    # Privacy deletion must not depend on successful publication validation.
    preserved = []
    with store._tx() as db:
        for owner in (scope.profile_id, other):
            for kind in ('packet_batch', 'packet_dependency', 'packet_intent'):
                rid = str(uuid4())
                value = dict(id=rid, application_id=scope.application_id,
                             private_text='PRIVATE-ORPHAN-CANARY')
                if kind != 'packet_intent':
                    value['job_id'] = str(uuid4())  # Nonexistent job; intent lacks job_id entirely.
                encoded = json.dumps(value)
                db.execute('INSERT INTO records VALUES(?,?,?,?)',
                           (rid, owner, 'workspace:' + kind, encoded))
                if owner == other:
                    preserved.append((rid, encoded))
    store.revoke_fact(scope.profile_id, fact.id)
    with store._read() as db:
        assert db.execute("SELECT id FROM records WHERE owner=? AND kind IN ('workspace:packet_batch','workspace:packet_dependency','workspace:packet_intent')",
                          (scope.profile_id,)).fetchall() == []
        for rid, encoded in preserved:
            assert db.execute('SELECT data FROM records WHERE id=? AND owner=?', (rid, other)).fetchone() == (encoded,)


def test_same_owner_same_company_packets_are_isolated_and_delete_is_exact(packets):  # noqa: F811
    store, research, repo, app_a, scope_a, evidence, service = packets
    batch_a, _, _ = run(packets)
    scope_b = second_scope(store, research, repo, scope_a.profile_id)
    build(evidence, scope_b)
    body_b = request(service, scope_b, key='same-company-b')
    job_b = service.enqueue(scope_b.profile_id, scope_b.application_id, scope_b.research_run_id, body_b)
    worker = str(uuid4())
    claimed = service.jobs.claim(worker, job_id=job_b.id)
    batch_b = service.run_packets(job_b.id, worker, claimed.fence)
    assert [x['batch']['id'] for x in service.list(scope_a.profile_id, app_a.application_id)['batches']] == [batch_a.id]
    assert [x['batch']['id'] for x in service.list(scope_b.profile_id, scope_b.application_id)['batches']] == [batch_b.id]
    for scope, foreign in ((scope_a, batch_b), (scope_b, batch_a)):
        with pytest.raises(EvidenceError) as raised:
            service.detail(scope.profile_id, scope.application_id, foreign.id)
        assert raised.value.code == 'NOT_FOUND'
    other_owner = store.create_profile('Foreign owner', ['finance']).id
    with pytest.raises(EvidenceError) as raised:
        service.detail(other_owner, scope_a.application_id, batch_a.id)
    assert raised.value.code == 'NOT_FOUND'
    fact = store.confirm_fact(scope_a.profile_id, 'Personal canonical content survives app deletion')
    before_b = service.detail(scope_b.profile_id, scope_b.application_id, batch_b.id)
    repo.delete_application(scope_a.profile_id, app_a.application_id,
                            ApplicationPatch(expected_input_revision=app_a.input_revision,
                                             expected_output_revision=app_a.output_revision))
    assert service.detail(scope_b.profile_id, scope_b.application_id, batch_b.id) == before_b
    assert any(item.id == fact.id for item in store.list_facts(scope_a.profile_id))
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE owner=? AND kind LIKE 'workspace:packet_%' AND json_extract(data,'$.application_id')=?",
                              (scope_a.profile_id, app_a.application_id)).fetchone()


def test_terminal_write_failure_rolls_back_already_written_publication(packets):  # noqa: F811
    store, _, _, _, _, _, service = packets
    with store._tx() as db:
        db.execute("""CREATE TRIGGER reject_packet_completion BEFORE UPDATE OF state ON jobs
            WHEN NEW.state='completed' AND json_extract(NEW.data,'$.kind')='packets'
            BEGIN SELECT RAISE(ABORT, 'terminal packet write rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='terminal packet write rejected'):
        run(packets)
    with store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind IN ('workspace:packet_batch','workspace:packet_dependency')").fetchone()
        assert not db.execute("SELECT 1 FROM job_stages WHERE stage='packets_published'").fetchone()
        intents = db.execute("SELECT data FROM records WHERE kind='workspace:packet_intent'").fetchall()
        assert intents and all(json.loads(row[0])['state'] == 'failed' for row in intents)
        assert not db.execute("SELECT 1 FROM jobs WHERE state='completed' AND json_extract(data,'$.kind')='packets'").fetchone()
