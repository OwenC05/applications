"""Current drafting state is authoritative, isolated and genuinely read-only."""
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_drafting_service import drafting  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_research_integration import setup
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.api import create_app
from copilot.config import Settings
from copilot.contracts import EvidenceError
from copilot.domain.repository import ConsentInput
from copilot.drafting.service import DraftingService


@pytest.fixture
def state(tmp_path):
    store, owner, repo, app = setup(tmp_path)
    service = DraftingService(store)
    return store, owner, repo, app, service


def grant(state, *, provider='openai', granted=True):
    _, owner, repo, _, _ = state
    return repo.consent(owner, ConsentInput(expected_consent_revision=0, provider=provider,
        purposes=('research', 'drafting', 'assessment'), granted=granted))


@pytest.mark.parametrize('has_consent', [False, True])
def test_fixed_dto_in_one_readonly_snapshot(state, monkeypatch, has_consent):
    store, owner, _, app, service = state
    consent = grant(state) if has_consent else None
    statements, snapshots = [], []
    original = store._read

    @contextmanager
    def traced():
        with original() as db:
            snapshots.append(db)
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, '_read', traced)
    monkeypatch.setattr(service.domain, '_singleton', lambda *_: pytest.fail('No singleton writes'))
    monkeypatch.setattr(service.domain, '_all', lambda *_: pytest.fail('No ensure writes'))
    monkeypatch.setattr(service.provider, '_key_supplier', lambda: pytest.fail('No key access'))
    monkeypatch.setattr(service.provider, 'transport', lambda *_: pytest.fail('No transmission'))
    actual = service.current_state(owner, app.application_id)
    assert len(snapshots) == 1
    assert statements and all(sql.lstrip().upper().startswith(('SELECT ', 'ROLLBACK')) for sql in statements)
    with original() as db:
        expected = service.jobs.capture(db, owner, app.application_id).model_dump(mode='json')
    assert actual == dict(schema_version=1, profile_id=owner, application_id=app.application_id,
                          current_revisions=expected, consent=None if consent is None else dict(provider=consent.provider,
                              purposes=list(consent.purposes), granted=True, revision=1))


@pytest.mark.parametrize('provider,granted', [('openai', True), ('other-provider', True), ('openai', False)])
def test_consent_summary_preserves_provider_purposes_and_revocation(state, provider, granted):
    _, owner, _, app, service = state
    consent = grant(state, provider=provider, granted=granted)
    result = service.current_state(owner, app.application_id)
    assert result['consent'] == dict(provider=provider, purposes=list(consent.purposes),
                                      granted=granted, revision=1)
    assert result['current_revisions']['consent'] == 1


@pytest.mark.parametrize('tamper', ['missing', 'duplicate', 'owner', 'id', 'revision',
    'empty_purposes', 'duplicate_purposes', 'unknown_purpose', 'malformed', 'sql_id', 'sql_owner'])
def test_inconsistent_retained_consent_fails_closed(state, tamper):
    store, owner, _, app, service = state
    consent = grant(state)
    payload = consent.model_dump(mode='json')
    changes = {'owner': ('profile_id', str(uuid4())), 'id': ('id', str(uuid4())),
        'revision': ('revision', 2), 'empty_purposes': ('purposes', []),
        'duplicate_purposes': ('purposes', ['drafting', 'drafting']),
        'unknown_purpose': ('purposes', ['submission'])}
    with store._tx() as db:
        if tamper in changes:
            key, value = changes[tamper]
            payload[key] = value
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(payload), consent.id))
        elif tamper == 'missing':
            db.execute('DELETE FROM records WHERE id=?', (consent.id,))
        elif tamper == 'duplicate':
            payload['id'] = str(uuid4())
            db.execute('INSERT INTO records VALUES(?,?,?,?)',
                       (payload['id'], owner, 'workspace:consent', json.dumps(payload)))
        elif tamper == 'malformed':
            db.execute('UPDATE records SET data=? WHERE id=?', ('not-json', consent.id))
        elif tamper == 'sql_id':
            db.execute('UPDATE records SET id=? WHERE id=?', (str(uuid4()), consent.id))
        else:
            db.execute('UPDATE records SET owner=? WHERE id=?', (str(uuid4()), consent.id))
    with pytest.raises(EvidenceError) as exc:
        service.current_state(owner, app.application_id)
    assert exc.value.http_status == 409


@pytest.mark.parametrize('tamper', ['profile_id', 'application_id', 'malformed'])
def test_application_serialized_scope_must_match_sql_and_request(state, tamper):
    store, owner, _, app, service = state
    payload = app.model_dump(mode='json')
    payload[tamper] = str(uuid4())
    with store._tx() as db:
        db.execute('UPDATE records SET data=? WHERE id=?',
                   ('not-json' if tamper == 'malformed' else json.dumps(payload), app.application_id))
    with pytest.raises(EvidenceError) as exc:
        service.current_state(owner, app.application_id)
    assert exc.value.http_status == 409


def test_wrong_owner_and_deleted_application_are_not_found(state):
    store, owner, _, app, service = state
    other = store.create_profile('Friend', ['finance']).id
    for profile in [other, str(uuid4())]:
        with pytest.raises(EvidenceError) as exc:
            service.current_state(profile, app.application_id)
        assert exc.value.http_status == 404
    with store._tx() as db:
        db.execute('DELETE FROM records WHERE id=?', (app.application_id,))
    with pytest.raises(EvidenceError) as exc:
        service.current_state(owner, app.application_id)
    assert exc.value.http_status == 404


def test_current_packet_is_not_authority_for_full_revision_vector(drafting):  # noqa: F811
    service, scope, batch, sent, _ = drafting
    with service.store._tx() as db:
        db.execute('UPDATE profile_revisions SET metadata=metadata+1,documents=documents+1 WHERE owner=?', (scope.profile_id,))
        app = service.domain._get(db, scope.profile_id, scope.application_id, 'application')
        app['output_revision'] += 1
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(app), scope.application_id))
    assert service.packets.detail(scope.profile_id, scope.application_id, batch.id)['current']
    actual = service.current_state(scope.profile_id, scope.application_id)
    with service.store._read() as db:
        expected = service.jobs.capture(db, scope.profile_id, scope.application_id)
    assert actual['current_revisions'] == expected.model_dump(mode='json')
    assert expected.metadata > batch.revisions.metadata and expected.documents > batch.revisions.documents
    assert expected.consent > batch.revisions.consent and expected.application_output > batch.revisions.application_output
    assert not sent


def test_http_state_does_not_initialize_models_or_change_jobs_budgets_attempts(state, tmp_path):
    store, owner, _, app, service = state
    grant(state)
    api = create_app(Settings(store.root, tmp_path / 'absent-models'),
                     lambda *_: pytest.fail('No models initialized'))
    api.state.drafting.provider._key_supplier = lambda: pytest.fail('No key access')
    api.state.drafting.provider.transport = lambda *_: pytest.fail('No provider calls')
    route = f'/api/workspace/profiles/{owner}/applications/{app.application_id}/drafting-state'
    # Bootstrap is outside the endpoint's read-only guarantee.
    with store._read() as db:
        before = list(db.iterdump())
    with TestClient(api, base_url='http://127.0.0.1:3001') as http:
        response = http.get(route)
        assert response.status_code == 200, response.text
        assert response.json() == service.current_state(owner, app.application_id)
        assert http.get(route.replace(app.application_id, str(uuid4()))).status_code == 404
    with store._read() as db:
        assert list(db.iterdump()) == before
    assert not (tmp_path / 'absent-models').exists()


def test_consent_and_revisions_share_snapshot_during_concurrent_update(state, monkeypatch):
    store, owner, repo, app, service = state
    grant(state)
    # Test-only WAL permits a writer to commit while the deferred reader
    # remains open; the endpoint itself never changes journal settings.
    with sqlite3.connect(store.db) as db:
        db.execute('PRAGMA journal_mode=WAL')
    capture = service.jobs.capture
    changed = []

    def concurrent(db, profile, application):
        revisions = capture(db, profile, application)
        changed.append(repo.consent(owner, ConsentInput(expected_consent_revision=1,
            provider='openai', purposes=('drafting',), granted=False)))
        return revisions

    monkeypatch.setattr(service.jobs, 'capture', concurrent)
    actual = service.current_state(owner, app.application_id)
    assert len(changed) == 1 and changed[0].revision == 2
    assert actual['current_revisions']['consent'] == actual['consent']['revision'] == 1
    assert actual['consent']['granted'] is True
    monkeypatch.setattr(service.jobs, 'capture', capture)
    latest = service.current_state(owner, app.application_id)
    assert latest['current_revisions']['consent'] == latest['consent']['revision'] == 2
    assert latest['consent']['granted'] is False


@pytest.mark.parametrize('dimension,value', [
    ('research', -1), ('research', 'malformed'), ('research', 1.5),
    ('metadata', -1), ('metadata', 'malformed'),
    ('facts', 'malformed'), ('documents', 'malformed'), ('consent', 'malformed'),
])
def test_corrupt_sql_revision_counters_fail_closed(state, dimension, value):
    store, owner, _, app, service = state
    with store._tx() as db:
        if dimension == 'research':
            db.execute('INSERT INTO app_revisions(owner,application_id,research) VALUES(?,?,?)',
                       (owner, app.application_id, value))
        else:
            # Dimension is a fixed parametrized identifier, never caller input.
            db.execute(f'UPDATE profile_revisions SET {dimension}=? WHERE owner=?', (value, owner))
    with pytest.raises(EvidenceError) as exc:
        service.current_state(owner, app.application_id)
    assert exc.value.http_status == 409 and exc.value.code == 'CONFLICT'
