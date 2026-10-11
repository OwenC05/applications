"""Admission is durable history fencing, never a provider permission."""
import pytest
from test_drafting_service import body, capture_draft_launch, drafting, enqueue  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError


def test_state_and_raw_denial_are_read_only(drafting):  # noqa: F811
    service, scope, *_ = drafting
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state == dict(schema_version=1, profile_id=scope.profile_id,
        application_id=scope.application_id, admission_revision=0, eligible=True,
        reason_codes=[], head_job=None)
    with pytest.raises(EvidenceError) as error:
        service.enqueue(scope.profile_id, scope.application_id, body(drafting))
    assert error.value.code == 'ADMISSION_REQUIRED'
    assert service.admission_state(scope.profile_id, scope.application_id) == state


def test_exact_wrapper_replay_precedes_currentness_and_new_key_is_fenced(drafting, monkeypatch):  # noqa: F811
    service, scope, *_ = drafting
    launch = capture_draft_launch(drafting, body(drafting))
    job, _ = enqueue(drafting, launch=launch)
    monkeypatch.setattr(service, '_verify', lambda *a: pytest.fail('replay touched sources'))
    assert service.enqueue(scope.profile_id, scope.application_id, launch).id == job.id
    changed = launch.model_copy(update={'request': launch.request.model_copy(update={'idempotency_key': 'other'})})
    with pytest.raises(EvidenceError) as error:
        service.enqueue(scope.profile_id, scope.application_id, changed)
    assert error.value.code == 'CONFLICT'


@pytest.mark.parametrize('change', [dict(admission_revision=True), dict(extra='forged'), dict(request={'schema_version': 1}), dict(admission_revision=-1)])
def test_unchecked_wrappers_revalidate_before_io(drafting, monkeypatch, change):  # noqa: F811
    service, scope, *_ = drafting
    launch = capture_draft_launch(drafting, body(drafting))
    monkeypatch.setattr(service.store, '_read', lambda: pytest.fail('invalid input opened DB'))
    with pytest.raises(EvidenceError) as error:
        service.enqueue(scope.profile_id, scope.application_id, launch.model_copy(update=change))
    assert error.value.code == 'INVALID_INPUT'


def test_admitted_raw_replay_requires_original_wrapper(drafting):  # noqa: F811
    service, scope, *_ = drafting
    job, request = enqueue(drafting)
    with pytest.raises(EvidenceError) as error:
        service.enqueue(scope.profile_id, scope.application_id, request)
    assert error.value.code == 'ADMISSION_REQUIRED'
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state['reason_codes'] == ['active_operation']
    assert state['head_job']['id'] == job.id


@pytest.mark.parametrize('foreign', [False, True])
def test_malformed_publication_is_owner_isolated(drafting, foreign):  # noqa: F811
    from uuid import uuid4

    service, scope, *_ = drafting
    with service.store._tx() as db:
        db.execute('INSERT INTO records VALUES(?,?,?,?)', (str(uuid4()), str(uuid4()) if foreign else scope.profile_id,
            'workspace:draft_dependency', 'not-json'))
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state['eligible'] is foreign
    assert state['reason_codes'] == ([] if foreign else ['integrity_blocked'])


@pytest.mark.parametrize('tamper', ['id', 'state'])
@pytest.mark.parametrize('kind', ['draft', 'assess'])
def test_attempt_sql_identity_precedes_completed_certificate(drafting, tamper, kind):  # noqa: F811
    from test_drafting_assessment import assess, assessment
    from test_drafting_service import execute

    service, scope, *_ = drafting
    if kind == 'draft':
        _, job, _ = execute(drafting)
    else:
        fixture = assessment.__wrapped__(drafting)
        _, job, _ = assess(fixture)
    with service.store._tx() as db:
        if tamper == 'id':
            db.execute("UPDATE provider_attempts SET id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' WHERE job_id=? AND stage=?", (job.id, 'plan' if kind == 'draft' else 'reassess'))
        else:
            db.execute("UPDATE provider_attempts SET state='definitely_unsent' WHERE job_id=? AND stage=?", (job.id, 'plan' if kind == 'draft' else 'reassess'))
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state['reason_codes'][0] == 'integrity_blocked'


@pytest.mark.parametrize('foreign', ['opaque', 'job_reference', 'draft_reference'])
def test_completed_certificate_defensive_foreign_reference_audit(drafting, foreign):  # noqa: F811
    import json
    from uuid import uuid4

    from test_drafting_service import execute

    service, scope, *_ = drafting
    draft, job, _ = execute(drafting)
    with service.store._tx() as db:
        data = 'not-json' if foreign == 'opaque' else json.dumps({'job_id' if foreign == 'job_reference' else 'draft_id': job.id if foreign == 'job_reference' else draft.id})
        db.execute('INSERT INTO records VALUES(?,?,?,?)', (str(uuid4()), str(uuid4()), 'workspace:draft_dependency', data))
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state['eligible'] is (foreign == 'opaque')


def test_complete_association_revalidated_inside_writer_without_external_io(drafting, monkeypatch):  # noqa: F811
    from test_drafting_service import execute

    service, scope, *_ = drafting
    draft, _, _ = execute(drafting)
    launch = capture_draft_launch(drafting, body(drafting, 'next'))
    association = service._association
    observed = []
    writer = {'active': False}
    transaction = service.store._tx
    from contextlib import contextmanager

    @contextmanager
    def tracked_writer():
        with transaction() as db:
            writer['active'] = True
            try:
                yield db
            finally:
                writer['active'] = False

    monkeypatch.setattr(service.store, '_tx', tracked_writer)

    def inspect(db, owner, app, draft_id, **kwargs):
        if draft_id == draft.id:
            observed.append(writer['active'])
        return association(db, owner, app, draft_id, **kwargs)

    monkeypatch.setattr(service, '_association', inspect)
    monkeypatch.setattr(service, '_verify', lambda capture: None)
    monkeypatch.setattr(service.store, 'verify_scoped_sources', lambda *a: pytest.fail('certificate touched original source'))
    monkeypatch.setattr(service.provider, '_key_supplier', lambda: pytest.fail('certificate read key'))
    enqueue(drafting, launch=launch)
    assert False in observed and True in observed


def test_assessment_certificate_uses_own_request_not_parent_parameters(drafting):  # noqa: F811
    from test_drafting_assessment import assess, assessment

    from copilot.domain import contracts as c
    from copilot.drafting.admission_runtime import DraftAdmission

    service, scope, *_ = drafting
    fixture = assessment.__wrapped__(drafting)
    _, job, _ = assess(fixture)
    with service.store._read() as db:
        runtime = DraftAdmission(service)
        certificate = runtime._completed_publication_certificate(db, scope.profile_id, scope.application_id, job.id)
        import json
        own = json.loads(db.execute('SELECT parameters FROM jobs WHERE id=?', (job.id,)).fetchone()[0])
        assert certificate.request_sha256 == own['request_sha256']
        assert certificate.own_parameters_sha256 == c.canonical_hash(own)
        assert certificate.parent_identity is not None


def test_completed_graph_mutation_between_capture_and_writer_denies_admission(drafting, monkeypatch):  # noqa: F811
    from test_drafting_service import execute

    service, scope, *_ = drafting
    _, completed, _ = execute(drafting)
    launch = capture_draft_launch(drafting, body(drafting, 'after-mutation'))

    def corrupt_after_preflight(capture):
        with service.store._tx() as db:
            db.execute("UPDATE job_stages SET sha256=? WHERE job_id=? AND stage='draft_published'", ('0' * 64, completed.id))

    monkeypatch.setattr(service, '_verify', corrupt_after_preflight)
    with pytest.raises(EvidenceError) as error:
        enqueue(drafting, launch=launch)
    assert error.value.code == 'CONFLICT'
    with service.store._read() as db:
        assert not db.execute('SELECT 1 FROM job_keys WHERE owner=? AND key=?', (scope.profile_id, launch.request.idempotency_key)).fetchone()
        assert db.execute('SELECT revision FROM drafting_admission_heads WHERE owner=? AND application_id=?', (scope.profile_id, scope.application_id)).fetchone() == (1,)


def test_foreign_attempt_linked_owned_job_blocks_both_owners_not_third(drafting):  # noqa: F811
    from uuid import uuid4

    from test_drafting_service import execute

    from copilot.drafting.admission_runtime import DraftAdmission

    service, scope, *_ = drafting
    _, job, _ = execute(drafting)
    foreign, third = str(uuid4()), str(uuid4())
    with service.store._tx() as db:
        db.execute("UPDATE provider_attempts SET owner=? WHERE job_id=? AND stage='plan'", (foreign, job.id))
    with service.store._read() as db:
        runtime = DraftAdmission(service)
        assert runtime._attempts(db, scope.profile_id, scope.application_id)[0]
        assert runtime._attempts(db, foreign, scope.application_id)[0]
        assert runtime._attempts(db, third, scope.application_id) == (False, False)


def test_foreign_unlinked_orphan_json_owner_claim_does_not_block_claimed_owner(drafting):  # noqa: F811
    from uuid import uuid4

    from test_drafting_service import execute

    from copilot.drafting.admission_runtime import DraftAdmission

    service, scope, *_ = drafting
    _, job, _ = execute(drafting)
    foreign, orphan = str(uuid4()), str(uuid4())
    with service.store._tx() as db:
        db.execute("UPDATE provider_attempts SET owner=?,job_id=? WHERE job_id=? AND stage='plan'", (foreign, orphan, job.id))
    with service.store._read() as db:
        runtime = DraftAdmission(service)
        assert runtime._attempts(db, scope.profile_id, scope.application_id) == (False, False)
        assert runtime._attempts(db, foreign, scope.application_id)[0]


def test_admission_state_uses_one_select_only_snapshot(drafting, monkeypatch):  # noqa: F811
    from contextlib import contextmanager

    service, scope, *_ = drafting
    reader = service.store._read
    opened, statements = [], []

    @contextmanager
    def traced():
        with reader() as db:
            opened.append(True)
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(service.store, '_read', traced)
    monkeypatch.setattr(service.store, '_tx', lambda: pytest.fail('GET opened writer'))
    monkeypatch.setattr(service.jobs, 'get', lambda *a: pytest.fail('GET invoked mutable job getter'))
    monkeypatch.setattr(service.jobs, 'list', lambda *a: pytest.fail('GET invoked jobs listing'))
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state['eligible']
    assert len(opened) == 1
    assert all(sql.lstrip().split()[0].upper() in ('SELECT', 'PRAGMA', 'ROLLBACK') for sql in statements)


@pytest.mark.parametrize('state', ['prepared', 'indeterminate', 'completed'])
def test_valid_orphan_attempt_is_app_local_and_missing_usage_is_not_unknown(drafting, state):  # noqa: F811
    import json
    from uuid import uuid4

    from test_drafting_service import execute

    from copilot.drafting.admission_runtime import DraftAdmission

    service, scope, *_ = drafting
    _, job, _ = execute(drafting)
    orphan = str(uuid4())
    with service.store._tx() as db:
        aid, raw = db.execute("SELECT id,data FROM provider_attempts WHERE job_id=? AND stage='plan'", (job.id,)).fetchone()
        data = json.loads(raw)
        data.update(job_id=orphan, state=state, reported_tokens=None)
        db.execute('UPDATE provider_attempts SET job_id=?,state=?,data=? WHERE id=?', (orphan, state, json.dumps(data), aid))
    with service.store._read() as db:
        runtime = DraftAdmission(service)
        assert runtime._attempts(db, scope.profile_id, scope.application_id) == (False, state != 'completed')
        assert runtime._attempts(db, scope.profile_id, str(uuid4())) == (False, False)


def test_fully_forgotten_completed_job_preserves_consumed_head_without_certificate_block(drafting):  # noqa: F811
    from test_drafting_service import execute

    service, scope, *_ = drafting
    _, job, _ = execute(drafting)
    with service.store._tx() as db:
        service.store.forget_jobs(db, scope.profile_id, application_id=scope.application_id)
    state = service.admission_state(scope.profile_id, scope.application_id)
    assert state['eligible'] and not state['reason_codes']
    assert state['admission_revision'] == 2
    assert state['head_job']['id'] == job.id
    assert state['head_job']['consumed_revision'] == 1


def test_partial_scrub_cannot_skip_completed_publication_proof(drafting):  # noqa: F811
    from test_drafting_service import execute

    service, scope, *_ = drafting
    _, job, _ = execute(drafting)
    with service.store._tx() as db:
        db.execute('INSERT INTO tombstones VALUES(?,?,?)', (job.id, scope.profile_id, 'drafting_job_forgotten'))
    assert service.admission_state(scope.profile_id, scope.application_id)['reason_codes'][0] == 'integrity_blocked'


def test_exact_legacy_raw_replay_only_after_real_startup_upgrade(drafting, tmp_path, monkeypatch):  # noqa: F811
    from admission_legacy_helpers import preupgrade_copy

    from copilot.drafting.admission_runtime import DraftAdmission
    from copilot.drafting.runtime_contracts import draft_request_hash
    from copilot.drafting.service import DraftingService
    from copilot.store import Store

    service, scope, *_ = drafting
    launch = capture_draft_launch(drafting, body(drafting))
    job, request = enqueue(drafting, launch=launch)
    target = tmp_path / 'owned-preupgrade-replay'
    with preupgrade_copy(service.store, target) as db:
        db.execute('UPDATE job_keys SET request_sha256=? WHERE job_id=?', (draft_request_hash(scope.profile_id, scope.application_id, request), job.id))
    upgraded = DraftingService(Store(target))
    monkeypatch.setattr(upgraded, '_verify', lambda *a: pytest.fail('legacy replay touched originals'))
    assert upgraded.enqueue(scope.profile_id, scope.application_id, request).id == job.id
    with upgraded.store._read() as db:
        assert DraftAdmission(upgraded)._replay(db, scope.profile_id, scope.application_id, None, request).id == job.id
    with pytest.raises(EvidenceError) as error:
        upgraded.enqueue(scope.profile_id, scope.application_id, launch)
    assert error.value.code == 'CONFLICT'
    with pytest.raises(EvidenceError) as error:
        upgraded.enqueue(scope.profile_id, scope.application_id, request.model_copy(update={'model': 'changed-model'}))
    assert error.value.code == 'CONFLICT'


@pytest.mark.parametrize('foreign', [False, True])
@pytest.mark.parametrize('origin', ['legacy', 'admitted'])
def test_nonhead_orphan_binding_is_scoped_metadata_failure(drafting, tmp_path, foreign, origin):  # noqa: F811
    from uuid import uuid4

    from admission_legacy_helpers import preupgrade_copy

    from copilot.drafting.admission_contracts import DraftLaunch
    from copilot.drafting.runtime_contracts import draft_request_hash
    from copilot.drafting.service import DraftingService
    from copilot.store import Store

    service, scope, *_ = drafting
    job, request = enqueue(drafting)
    service.jobs.cancel(scope.profile_id, job.id)
    if origin == 'legacy':
        target = tmp_path / 'orphan-binding-upgrade'
        with preupgrade_copy(service.store, target) as db:
            db.execute('UPDATE job_keys SET request_sha256=? WHERE job_id=?', (draft_request_hash(scope.profile_id, scope.application_id, request), job.id))
        upgraded = DraftingService(Store(target))
        revision = 1
    else:
        newest, _ = enqueue(drafting, 'newest')
        service.jobs.cancel(scope.profile_id, newest.id)
        upgraded = service
        revision = 2
    fresh = body(drafting, 'after-orphan')
    with upgraded.store._tx() as db:
        if foreign:
            db.execute('UPDATE drafting_admission_bindings SET owner=?,application_id=? WHERE job_id=?', (str(uuid4()), str(uuid4()), job.id))
        db.execute('DELETE FROM job_keys WHERE job_id=?', (job.id,))
        db.execute('DELETE FROM jobs WHERE id=?', (job.id,))
    if foreign:
        assert upgraded.admission_state(scope.profile_id, scope.application_id)['eligible']
        return
    with upgraded.store._read() as db:
        before = (db.execute('SELECT * FROM drafting_admission_heads').fetchall(),
            db.execute('SELECT * FROM drafting_admission_bindings').fetchall(),
            db.execute('SELECT * FROM job_keys').fetchall(), db.execute('SELECT * FROM jobs').fetchall())
    for operation in (lambda: upgraded.admission_state(scope.profile_id, scope.application_id),
            lambda: upgraded.enqueue(scope.profile_id, scope.application_id, DraftLaunch(admission_revision=revision, request=fresh))):
        with pytest.raises(EvidenceError) as error:
            operation()
        assert error.value.code == 'ADMISSION_UNAVAILABLE' and error.value.http_status == 503
    with upgraded.store._read() as db:
        after = (db.execute('SELECT * FROM drafting_admission_heads').fetchall(),
            db.execute('SELECT * FROM drafting_admission_bindings').fetchall(),
            db.execute('SELECT * FROM job_keys').fetchall(), db.execute('SELECT * FROM jobs').fetchall())
    assert after == before


@pytest.mark.parametrize('kind', ['generation', 'assessment'])
@pytest.mark.parametrize('consent_state', ['missing', 'revoked', 'wrong_purpose'])
def test_fresh_admission_requires_current_canonical_purpose_consent(drafting, kind, consent_state):  # noqa: F811
    import json

    from test_drafting_assessment import assessment, assessment_body, capture_assessment_launch

    service, scope, _, sent, _ = drafting
    if kind == 'generation':
        launch = capture_draft_launch(drafting, body(drafting))
        parent = None
    else:
        fixture = assessment.__wrapped__(drafting)
        parent = fixture[2].id
        launch = capture_assessment_launch(service, scope, assessment_body(fixture))
    with service.store._tx() as db:
        row = db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:consent'", (scope.profile_id,)).fetchone()
        if consent_state == 'missing':
            db.execute('DELETE FROM records WHERE id=?', (row[0],))
        else:
            value = json.loads(row[1])
            value.update({'granted': False} if consent_state == 'revoked' else {'purposes': ['research']})
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(value), row[0]))
    with service.store._read() as db:
        before = tuple(db.execute(sql).fetchall() for sql in ('SELECT * FROM jobs', 'SELECT * FROM job_keys', 'SELECT * FROM drafting_admission_bindings', 'SELECT * FROM drafting_admission_heads'))
    sent_before = len(sent)
    with pytest.raises(EvidenceError) as error:
        if parent is None:
            service.enqueue(scope.profile_id, scope.application_id, launch)
        else:
            service.enqueue_assessment(scope.profile_id, scope.application_id, parent, launch)
    assert error.value.code == 'CONSENT_REQUIRED' and error.value.http_status == 403
    with service.store._read() as db:
        assert tuple(db.execute(sql).fetchall() for sql in ('SELECT * FROM jobs', 'SELECT * FROM job_keys', 'SELECT * FROM drafting_admission_bindings', 'SELECT * FROM drafting_admission_heads')) == before
    assert len(sent) == sent_before


def test_owned_binding_job_scope_disagreement_blocks_all_owner_apps_not_other_owner(drafting):  # noqa: F811
    import json
    from uuid import uuid4

    from copilot.domain.repository import ApplicationInput

    service, scope, *_ = drafting
    first, _ = enqueue(drafting)
    service.jobs.cancel(scope.profile_id, first.id)
    second, _ = enqueue(drafting, 'second')
    service.jobs.cancel(scope.profile_id, second.id)
    with service.store._read() as db:
        revision = service.domain.revisions(db, scope.profile_id).metadata
    healthy = service.domain.create_application(scope.profile_id, ApplicationInput(
        expected_metadata_revision=revision, company='Other', role='Intern', sector='finance', vacancy_url='https://example.com/healthy'))
    other_owner = service.store.create_profile('Synthetic other', ['tech']).id
    with service.store._read() as db:
        revision = service.domain.revisions(db, other_owner).metadata
    other = service.domain.create_application(other_owner, ApplicationInput(
        expected_metadata_revision=revision, company='Unrelated', role='Intern', sector='tech', vacancy_url='https://example.com/unrelated'))
    with service.store._tx() as db:
        raw = json.loads(db.execute('SELECT data FROM jobs WHERE id=?', (first.id,)).fetchone()[0])
        raw['application_id'] = str(uuid4())
        db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(raw), first.id))
    assert service.admission_state(scope.profile_id, scope.application_id)['reason_codes'] == ['integrity_blocked']
    assert service.admission_state(scope.profile_id, healthy.application_id)['reason_codes'] == ['integrity_blocked']
    assert service.admission_state(other_owner, other.application_id)['eligible']


@pytest.mark.parametrize('kind', ['generation', 'assessment'])
def test_exact_admitted_replay_remains_read_only_after_purpose_revocation(drafting, monkeypatch, kind):  # noqa: F811
    from test_drafting_assessment import assessment, assessment_body, capture_assessment_launch

    from copilot.domain.repository import ConsentInput

    service, scope, *_ = drafting
    if kind == 'generation':
        launch = capture_draft_launch(drafting, body(drafting))
        parent = None
        job = service.enqueue(scope.profile_id, scope.application_id, launch)
    else:
        fixture = assessment.__wrapped__(drafting)
        parent = fixture[2].id
        launch = capture_assessment_launch(service, scope, assessment_body(fixture))
        job = service.enqueue_assessment(scope.profile_id, scope.application_id, parent, launch)
    service.domain.consent(scope.profile_id, ConsentInput(
        expected_consent_revision=launch.request.expected_revisions.consent,
        provider='openai', purposes=('research',), granted=True))
    monkeypatch.setattr(service, '_verify', lambda *a: pytest.fail('historical replay verified sources'))
    monkeypatch.setattr(service.store, '_tx', lambda: pytest.fail('historical replay opened writer'))
    replay = service.enqueue(scope.profile_id, scope.application_id, launch) if parent is None else service.enqueue_assessment(scope.profile_id, scope.application_id, parent, launch)
    assert replay.id == job.id


@pytest.mark.parametrize(('boundary', 'read'), [('binding', True), ('binding', False), ('key', False)])
def test_stored_job_id_corruption_is_unavailable_not_public_input_error(drafting, tmp_path, boundary, read):  # noqa: F811
    from admission_legacy_helpers import preupgrade_copy

    from copilot.domain.repository import ApplicationInput
    from copilot.drafting.admission_contracts import DraftLaunch
    from copilot.drafting.runtime_contracts import draft_request_hash
    from copilot.drafting.service import DraftingService
    from copilot.store import Store

    service, scope, *_ = drafting
    job, request = enqueue(drafting)
    service.jobs.cancel(scope.profile_id, job.id)
    fresh = body(drafting, 'fresh-after-corruption')
    target = tmp_path / 'stored-id-upgrade'
    with preupgrade_copy(service.store, target) as db:
        db.execute('UPDATE job_keys SET request_sha256=? WHERE job_id=?', (draft_request_hash(scope.profile_id, scope.application_id, request), job.id))
    upgraded = DraftingService(Store(target))
    other_owner = upgraded.store.create_profile('Synthetic unaffected', ['finance']).id
    with upgraded.store._read() as db:
        revision = upgraded.domain.revisions(db, other_owner).metadata
    other = upgraded.domain.create_application(other_owner, ApplicationInput(expected_metadata_revision=revision,
        company='Unaffected', role='Intern', sector='finance', vacancy_url='https://example.com/unaffected'))
    with upgraded.store._tx() as db:
        if boundary == 'binding':
            db.execute("UPDATE drafting_admission_bindings SET job_id='not-a-uuid' WHERE job_id=?", (job.id,))
        else:
            db.execute("UPDATE job_keys SET job_id='not-a-uuid' WHERE job_id=?", (job.id,))
    with upgraded.store._read() as db:
        before = tuple(db.execute(sql).fetchall() for sql in ('SELECT * FROM jobs', 'SELECT * FROM job_keys', 'SELECT * FROM drafting_admission_bindings', 'SELECT * FROM drafting_admission_heads'))
    operations = [lambda: upgraded.enqueue(scope.profile_id, scope.application_id,
        DraftLaunch(admission_revision=1, request=fresh) if boundary == 'binding' else request)]
    if read:
        operations = [lambda: upgraded.admission_state(scope.profile_id, scope.application_id)]
    for operation in operations:
        with pytest.raises(EvidenceError) as error:
            operation()
        assert error.value.code == 'ADMISSION_UNAVAILABLE' and error.value.http_status == 503
    assert upgraded.admission_state(other_owner, other.application_id)['eligible']
    with upgraded.store._read() as db:
        assert tuple(db.execute(sql).fetchall() for sql in ('SELECT * FROM jobs', 'SELECT * FROM job_keys', 'SELECT * FROM drafting_admission_bindings', 'SELECT * FROM drafting_admission_heads')) == before


def test_external_invalid_identifiers_remain_public_input_errors(drafting, monkeypatch):  # noqa: F811
    service, scope, *_ = drafting
    launch = capture_draft_launch(drafting, body(drafting))
    monkeypatch.setattr(service.store, '_read', lambda: pytest.fail('invalid identifier opened database'))
    for operation in (lambda: service.admission_state(scope.profile_id, 'not-a-uuid'),
            lambda: service.enqueue('not-a-uuid', scope.application_id, launch)):
        with pytest.raises(EvidenceError) as error:
            operation()
        assert error.value.code == 'INVALID_INPUT' and error.value.http_status == 422
