"""Hostile local writing-review histories; synthetic transport, no paid IO."""
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from test_drafting_reviews import preview_body, review_body, reviewing  # noqa: F401
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.admission_state import MAX_REVISION
from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ApplicationInput, ApplicationPatch
from copilot.drafting.review_contracts import review_request_hash
from copilot.drafting.service import DraftingService


def publication_row(service, owner, draft_id):
    with service.store._read() as db:
        rows = db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:draft_dependency' "
                          "AND json_extract(data,'$.draft_id')=?", (owner, draft_id)).fetchall()
    assert len(rows) == 1
    return rows[0][0], json.loads(rows[0][1])


def write_record(db, identity, value):
    db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(value), identity))


def stale(service, scope):
    app = service.domain.application(scope.profile_id, scope.application_id)
    service.domain.patch_application(scope.profile_id, scope.application_id,
        ApplicationPatch(expected_input_revision=app.input_revision,
                         expected_output_revision=app.output_revision, location='Changed location'))


def accounting(service):
    with service.store._read() as db:
        return {table: tuple(db.execute(f'SELECT * FROM {table} ORDER BY 1,2').fetchall())
                for table in ('drafting_admission_heads', 'drafting_admission_bindings',
                              'jobs', 'job_keys', 'job_stages', 'provider_attempts', 'budget_limits')}


def reviewed(local):
    service, scope, *_ = local
    parent, *_ = execute(local)
    body = review_body(service, scope, parent)
    child = service.review(scope.profile_id, scope.application_id, parent.id, body)
    return parent, body, child


@pytest.mark.parametrize('tamper', ['disclosure_text', 'disclosure_warnings', 'snapshot_vector',
    'snapshot_time', 'snapshot_parent', 'clone_text', 'publication_parent', 'request_ack'])
def test_rehashed_review_corruption_fails_before_stale_source_bypass(reviewing, monkeypatch, tamper):  # noqa: F811
    service, scope, *_ = reviewing
    parent, body, child = reviewed(reviewing)
    pub_id, publication = publication_row(service, scope.profile_id, child.id)
    stale(service, scope)
    monkeypatch.setattr(service.store, 'verify_scoped_sources',
                        lambda *a: pytest.fail('stale retained history read original bytes'))
    assert not service.detail(scope.profile_id, scope.application_id, child.id)['current']
    with service.store._tx() as db:
        snapshot_id = publication['review_snapshot_id']
        snapshot = json.loads(db.execute('SELECT data FROM records WHERE id=?', (snapshot_id,)).fetchone()[0])
        if tamper.startswith('disclosure_'):
            if tamper == 'disclosure_text':
                publication['disclosure']['cover_letter'] += ' FORGED'
            else:
                publication['disclosure']['warnings'] = publication['disclosure']['warnings'][::-1]
            publication['request']['disclosure_sha256'] = c.canonical_hash(publication['disclosure'])
            snapshot['disclosure_sha256'] = publication['request']['disclosure_sha256']
        elif tamper.startswith('snapshot_'):
            if tamper == 'snapshot_vector':
                snapshot['revisions']['application_output'] += 1
            elif tamper == 'snapshot_time':
                snapshot['reviewed_at'] = '2020-01-01T00:00:00Z'
            else:
                snapshot['draft_id'] = parent.id
        elif tamper == 'clone_text':
            raw = child.model_dump(mode='json')
            raw['answers'][0]['text'] += ' FORGED'
            forged = c.DraftRevision.model_validate_json(json.dumps(raw))
            write_record(db, child.id, raw)
            publication['text_sha256'] = forged.text_sha256
            snapshot['text_sha256'] = forged.text_sha256
        elif tamper == 'publication_parent':
            publication['parent_draft_id'] = child.id
        else:
            publication['request']['acknowledged'] = 1
        # Rehash every outer binding the attacker can update. Relational reconstruction
        # must reject the corruption rather than trusting consistent hash strings.
        publication['request_sha256'] = c.canonical_hash(dict(operation='human_writing_review',
            profile_id=scope.profile_id, application_id=scope.application_id,
            parent_draft_id=publication['parent_draft_id'], request=publication['request']))
        write_record(db, snapshot_id, snapshot)
        publication['review_snapshot_sha256'] = c.canonical_hash(snapshot)
        write_record(db, pub_id, publication)
    with pytest.raises(EvidenceError):
        service.detail(scope.profile_id, scope.application_id, child.id)
    with pytest.raises(EvidenceError):
        service.export(scope.profile_id, scope.application_id, child.id)
    with pytest.raises(EvidenceError):
        service.review(scope.profile_id, scope.application_id, parent.id, body)


@pytest.mark.parametrize('tamper', ['duplicate', 'duplicate_wrong_app', 'wrong_sql_identity', 'wrong_app'])
def test_snapshot_cardinality_and_scope_are_checked_before_application_filter(reviewing, tamper):  # noqa: F811
    service, scope, *_ = reviewing
    _, _, child = reviewed(reviewing)
    _, publication = publication_row(service, scope.profile_id, child.id)
    with service.store._tx() as db:
        identity = publication['review_snapshot_id']
        raw = json.loads(db.execute('SELECT data FROM records WHERE id=?', (identity,)).fetchone()[0])
        if tamper.startswith('duplicate'):
            duplicate_id = str(uuid4())
            raw['id'] = duplicate_id
            if tamper == 'duplicate_wrong_app':
                raw['application_id'] = str(uuid4())
            db.execute('INSERT INTO records VALUES(?,?,?,?)',
                (duplicate_id, scope.profile_id, 'workspace:draft_review', json.dumps(raw)))
        elif tamper == 'wrong_sql_identity':
            db.execute('UPDATE records SET id=? WHERE id=?', (str(uuid4()), identity))
        else:
            raw['application_id'] = str(uuid4())
            write_record(db, identity, raw)
    stale(service, scope)
    with pytest.raises(EvidenceError):
        service.detail(scope.profile_id, scope.application_id, child.id)


def test_exact_restart_replay_survives_staleness_without_restoring_current_approval(reviewing, monkeypatch):  # noqa: F811
    service, scope, _, sent, _ = reviewing
    parent, body, child = reviewed(reviewing)
    stale(service, scope)
    restarted = DraftingService(service.store)
    monkeypatch.setattr(restarted, '_verify', lambda *a: pytest.fail('retained replay verified originals'))
    assert restarted.review(scope.profile_id, scope.application_id, parent.id, body) == child
    detail = restarted.detail(scope.profile_id, scope.application_id, child.id)
    assert not detail['reviewed_current'] and detail['review_snapshot']['draft_id'] == child.id
    assert not detail['browser_eligible'] and len(sent) == 3


def test_review_replay_key_is_scoped_to_parent_and_exact_body(reviewing):  # noqa: F811
    service, scope, *_ = reviewing
    parent, body, child = reviewed(reviewing)
    for forged in (body.model_copy(update={'disclosure_sha256': '0' * 64}),
                   body.model_copy(update={'parent_text_sha256': '0' * 64})):
        with pytest.raises(EvidenceError):
            service.review(scope.profile_id, scope.application_id, parent.id, forged)
    with pytest.raises(EvidenceError):
        service.review(scope.profile_id, scope.application_id, child.id, body)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == child.revisions.application_output
    assert body and review_request_hash(scope.profile_id, scope.application_id, parent.id, body)


def test_same_key_racing_review_returns_single_child_and_snapshot(reviewing, monkeypatch):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    original = service._verify
    winner = []

    def verify(capture):
        original(capture)
        if not winner:
            # Disable this one-shot seam before entering the competing operation.
            monkeypatch.setattr(service, '_verify', original)
            winner.append(service.review(scope.profile_id, scope.application_id, parent.id, body))

    monkeypatch.setattr(service, '_verify', verify)
    assert service.review(scope.profile_id, scope.application_id, parent.id, body) == winner[0]
    with service.store._read() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='workspace:draft_review'").fetchone() == (1,)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output + 1


@pytest.mark.parametrize('mutation', ['input', 'output', 'consent', 'parent_delete', 'forget'])
def test_change_after_outside_verification_cannot_publish_review(reviewing, monkeypatch, mutation):  # noqa: F811
    service, scope, _, sent, _ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    original = service._verify
    changed = []

    def verify(capture):
        original(capture)
        if changed:
            return
        changed.append(True)
        if mutation == 'forget':
            app = service.domain.application(scope.profile_id, scope.application_id)
            service.domain.delete_application(scope.profile_id, scope.application_id,
                ApplicationPatch(expected_input_revision=app.input_revision, expected_output_revision=app.output_revision))
        else:
            with service.store._tx() as db:
                if mutation == 'parent_delete':
                    db.execute('DELETE FROM records WHERE id=?', (parent.id,))
                elif mutation == 'consent':
                    service.domain._bump(db, scope.profile_id, 'consent')
                else:
                    app = service.domain._get(db, scope.profile_id, scope.application_id, 'application', c.ApplicationRecord)
                    field = 'input_revision' if mutation == 'input' else 'output_revision'
                    service.domain._save(db, scope.profile_id, 'application',
                        app.model_copy(update={field: getattr(app, field) + 1}))

    monkeypatch.setattr(service, '_verify', verify)
    with pytest.raises(EvidenceError):
        service.review(scope.profile_id, scope.application_id, parent.id, body)
    assert changed and len(sent) == 3
    with service.store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:draft_review'").fetchone()
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:draft_dependency' AND json_extract(data,'$.origin')='review'").fetchone()


def test_successful_local_review_preserves_every_paid_accounting_byte(reviewing, monkeypatch):  # noqa: F811
    service, scope, _, sent, _ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    before = accounting(service)
    monkeypatch.setattr(service.provider, '_key_supplier', lambda: pytest.fail('review read a cloud key'))
    monkeypatch.setattr(service.provider, 'transport', lambda *a: pytest.fail('review sent a provider request'))
    child = service.review(scope.profile_id, scope.application_id, parent.id, body)
    assert accounting(service) == before and len(sent) == 3
    assert child.revisions.application_output == parent.revisions.application_output + 1


def test_admission_maximum_alone_does_not_block_no_cost_review(reviewing):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    with service.store._tx() as db:
        db.execute('UPDATE drafting_admission_heads SET revision=? WHERE owner=? AND application_id=?',
                   (MAX_REVISION, scope.profile_id, scope.application_id))
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state['reason_codes'] == ['admission_exhausted']
    before = accounting(service)
    body = review_body(service, scope, parent)
    child = service.review(scope.profile_id, scope.application_id, parent.id, body)
    assert service.detail(scope.profile_id, scope.application_id, child.id)['reviewed_current']
    assert accounting(service) == before


@pytest.mark.parametrize('after_kind', ['draft', 'draft_dependency', 'draft_review', 'application'])
def test_failure_after_each_review_write_rolls_back_child_snapshot_and_output(reviewing, monkeypatch, after_kind):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    transaction = service.store._tx
    before = accounting(service)
    with service.store._read() as db:
        records = tuple(db.execute('SELECT * FROM records ORDER BY id').fetchall())

    class FailingConnection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, parameters=()):
            result = self.db.execute(sql, parameters)
            if sql.startswith(('INSERT INTO records', 'UPDATE records')) and f'workspace:{after_kind}' in parameters:
                raise RuntimeError('synthetic failure after local review write')
            return result

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def failing_transaction():
        with transaction() as db:
            yield FailingConnection(db)

    monkeypatch.setattr(service.store, '_tx', failing_transaction)
    with pytest.raises(RuntimeError, match='after local review write'):
        service.review(scope.profile_id, scope.application_id, parent.id, body)
    with service.store._read() as db:
        assert tuple(db.execute('SELECT * FROM records ORDER BY id').fetchall()) == records
    assert accounting(service) == before


def test_forgetting_application_erases_orphan_review_plaintext_but_preserves_foreign_canary(reviewing):  # noqa: F811
    service, scope, *_ = reviewing
    parent, _, child = reviewed(reviewing)
    with service.store._read() as db:
        metadata = service.domain.revisions(db, scope.profile_id).metadata
    other = service.domain.create_application(scope.profile_id,
        ApplicationInput(expected_metadata_revision=metadata, company='Foreign synthetic', role='Canary',
                         sector='tech', vacancy_url='https://example.com/foreign'))
    foreign_id = str(uuid4())
    with service.store._tx() as db:
        # Exercise cleanup without the normal generation/job linkage.
        db.execute('DELETE FROM records WHERE id=?', (parent.id,))
        db.execute('INSERT INTO records VALUES(?,?,?,?)', (foreign_id, scope.profile_id,
            'workspace:draft_review', json.dumps(dict(id=foreign_id, application_id=other.application_id,
                plaintext='FOREIGN_REVIEW_CANARY'))))
    app = service.domain.application(scope.profile_id, scope.application_id)
    service.domain.delete_application(scope.profile_id, scope.application_id,
        ApplicationPatch(expected_input_revision=app.input_revision, expected_output_revision=app.output_revision))
    with service.store._read() as db:
        assert db.execute('SELECT data FROM records WHERE id=?', (foreign_id,)).fetchone()
        retained = db.execute('SELECT id,data FROM records WHERE owner=?', (scope.profile_id,)).fetchall()
    assert all(identity != child.id and child.id not in data for identity, data in retained)
    assert all(scope.application_id not in data for _, data in retained)


@pytest.mark.parametrize('history', ['active', 'prepared', 'unknown', 'integrity'])
def test_fresh_review_history_denial_preserves_accounting_and_inspection(reviewing, history):  # noqa: F811
    from test_drafting_service import enqueue

    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    if history == 'integrity':
        with service.store._tx() as db:
            db.execute('INSERT INTO records VALUES(?,?,?,?)',
                (str(uuid4()), scope.profile_id, 'workspace:draft_dependency',
                 json.dumps(dict(origin='edit', draft_id=str(uuid4())))))
        reason = 'integrity_blocked'
    else:
        job, _ = enqueue(reviewing, key='later-paid-operation')
        reason = 'active_operation'
        if history in ('prepared', 'unknown'):
            worker = str(uuid4())
            lease = service.jobs.claim(worker, job_id=job.id)
            attempt = service.provider.reserve(job.id, worker, lease.fence, 'plan', 1000, 'synthetic-model')
            if history == 'unknown':
                service.provider._unknown(attempt.id, job.id)
            reason = 'provider_outcome_unresolved'
    before = accounting(service)
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    assert detail['writing_review_ready'] and not detail['review_eligible']
    assert parent.answers[0].text in service.export(scope.profile_id, scope.application_id, parent.id)
    assert reason in service.admission_state(scope.profile_id, scope.application_id)['reason_codes']
    with pytest.raises(EvidenceError) as error:
        service.review(scope.profile_id, scope.application_id, parent.id, body)
    assert error.value.http_status == 409
    assert accounting(service) == before


def test_unavailable_admission_metadata_only_disables_permission_not_valid_writing(reviewing):  # noqa: F811
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    preview = preview_body(service, scope, parent)
    with service.store._tx() as db:
        db.execute('DELETE FROM drafting_admission_heads WHERE owner=? AND application_id=?',
                   (scope.profile_id, scope.application_id))
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    assert detail['writing_review_ready'] and not detail['review_eligible']
    assert parent.answers[0].text in service.export(scope.profile_id, scope.application_id, parent.id)
    for operation, request in ((service.review_preview, preview), (service.review, body)):
        with pytest.raises(EvidenceError) as error:
            operation(scope.profile_id, scope.application_id, parent.id, request)
        assert error.value.http_status == 503


def test_corrupt_original_detected_by_outside_verifier_denies_review_without_partial_child(reviewing):  # noqa: F811
    service, scope, batch, sent, _ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    with service.store._read() as db:
        _, values = service.store.capture_scoped_sources(db, batch.employer_generation.scope, service.jobs._now())
    service.store._blob(values[0]['source']['id']).write_bytes(b'corrupted synthetic original')
    before = accounting(service)
    with pytest.raises(EvidenceError):
        service.review(scope.profile_id, scope.application_id, parent.id, body)
    with service.store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:draft_review'").fetchone()
    assert accounting(service) == before and len(sent) == 3


@pytest.mark.parametrize('forget', ['application', 'profile', 'source'])
def test_orphan_review_erasure_retains_unknown_paid_reservation_and_foreign_owner(reviewing, forget):  # noqa: F811
    from test_drafting_service import enqueue

    service, scope, *_ = reviewing
    parent, _, child = reviewed(reviewing)
    job, _ = enqueue(reviewing, key='unknown-after-review')
    worker = str(uuid4())
    lease = service.jobs.claim(worker, job_id=job.id)
    attempt = service.provider.reserve(job.id, worker, lease.fence, 'plan', 1000, 'synthetic-model')
    service.provider._unknown(attempt.id, job.id)
    foreign = service.store.create_profile('Foreign synthetic owner', ['finance'])
    foreign_id = str(uuid4())
    foreign_data = json.dumps(dict(id=foreign_id, private_text='FOREIGN_OWNER_REVIEW_CANARY'))
    with service.store._tx() as db:
        db.execute('DELETE FROM records WHERE id=?', (parent.id,))
        db.execute('INSERT INTO records VALUES(?,?,?,?)',
            (foreign_id, foreign.id, 'workspace:draft_review', foreign_data))
        before = db.execute('SELECT state,data,day,dispatched,request_sha256 FROM provider_attempts WHERE id=?',
                            (attempt.id,)).fetchone()
        erased = [row[0] for row in db.execute("SELECT id FROM records WHERE owner=? "
            "AND kind IN ('workspace:draft','workspace:draft_dependency','workspace:draft_review') "
            "AND json_extract(data,'$.application_id')=?", (scope.profile_id, scope.application_id))]
    assert child.id in erased and before[0] == 'indeterminate'
    if forget == 'profile':
        service.store.delete_profile(scope.profile_id)
    elif forget == 'source':
        source = service.store.add_source(scope.profile_id, 'synthetic-review.txt', 'text/plain',
                                         b'Synthetic source', ['Synthetic source'], 'test')
        service.store.delete_source(scope.profile_id, source.id)
    else:
        app = service.domain.application(scope.profile_id, scope.application_id)
        service.domain.delete_application(scope.profile_id, scope.application_id,
            ApplicationPatch(expected_input_revision=app.input_revision, expected_output_revision=app.output_revision))
    with service.store._read() as db:
        assert db.execute('SELECT state,data,day,dispatched,request_sha256 FROM provider_attempts WHERE id=?',
                          (attempt.id,)).fetchone() == before
        assert db.execute('SELECT response FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone() == (None,)
        assert db.execute('SELECT data FROM records WHERE id=? AND owner=?',
                          (foreign_id, foreign.id)).fetchone() == (foreign_data,)
        assert all(not db.execute('SELECT 1 FROM records WHERE id=?', (identity,)).fetchone() for identity in erased)
        assert all(db.execute('SELECT 1 FROM tombstones WHERE id=?', (identity,)).fetchone() for identity in erased)
        assert db.execute('SELECT owner,kind FROM tombstones WHERE id=?', (job.id,)).fetchone() == (
            scope.profile_id, 'drafting_job_forgotten')
