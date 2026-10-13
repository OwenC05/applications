"""Invocation-local owner discovery must not weaken selected graph validation."""
import json
from uuid import uuid4

import pytest
from test_drafting_edits import request as edit_request
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError


def discovery_queries(statements):
    return [sql for sql in statements if sql.startswith('SELECT id,data FROM records WHERE owner=')
            and "kind='workspace:draft_dependency'" in sql]


def test_one_owner_discovery_for_real_edit_chain_including_generation_leaf(drafting):  # noqa: F811
    service, scope, _, sent, _ = drafting
    generated, *_ = execute(drafting)
    current = generated
    for index in range(3):
        current = service.edit(scope.profile_id, scope.application_id, current.id,
            edit_request(service, scope, current, f'Exact edit {index}', key=f'discovery-{index}'))
    statements = []
    with service.store._read() as db:
        db.set_trace_callback(statements.append)
        context = service._association_context(db, scope.profile_id, scope.application_id)
        associated = service._association(db, scope.profile_id, scope.application_id, current.id, context=context)
        assert associated[0] == current
        service._association(db, scope.profile_id, scope.application_id, generated.id, context=context)
        counts = service._lineage_counts(db, scope.profile_id, scope.application_id, current.id, context=context)
        assert (counts.edits, counts.assessments, counts.reviews, counts.total) == (3, 0, 0, 3)
        assert len(discovery_queries(statements)) == 1
    assert current.answers[0].text in service.export(scope.profile_id, scope.application_id, current.id)
    assert len(sent) == 3


def insert_publication(db, owner, payload):
    identifier = str(uuid4())
    db.execute('INSERT INTO records VALUES(?,?,?,?)',
        (identifier, owner, 'workspace:draft_dependency', payload))
    return identifier


@pytest.mark.parametrize('payload', ['null', '[]', '42', '"string"',
    '{"draft_id":42}', '{"draft_id":null}', '{"draft_id":"unrelated","origin":"unknown"}'])
def test_well_formed_unrelated_json_is_not_promoted_to_selected_publication(drafting, payload):  # noqa: F811
    service, scope, *_ = drafting
    generated, *_ = execute(drafting)
    with service.store._tx() as db:
        insert_publication(db, scope.profile_id, payload)
    with service.store._read() as db:
        assert service._association(db, scope.profile_id, scope.application_id, generated.id)[0] == generated


@pytest.mark.parametrize('foreign', [False, True])
def test_malformed_json_fails_owner_wide_but_foreign_rows_are_ignored(drafting, foreign):  # noqa: F811
    service, scope, *_ = drafting
    generated, *_ = execute(drafting)
    with service.store._tx() as db:
        insert_publication(db, str(uuid4()) if foreign else scope.profile_id, '{malformed')
    with service.store._read() as db:
        if foreign:
            assert service._association(db, scope.profile_id, scope.application_id, generated.id)[0] == generated
        else:
            with pytest.raises(ValueError):
                service._association(db, scope.profile_id, scope.application_id, generated.id)


@pytest.mark.parametrize('corruption', ['duplicate', 'sql_identity', 'origin'])
def test_selected_publication_duplicate_identity_and_origin_still_fail(drafting, corruption):  # noqa: F811
    service, scope, *_ = drafting
    generated, *_ = execute(drafting)
    with service.store._tx() as db:
        identifier, serialized = db.execute("SELECT id,data FROM records WHERE owner=? "
            "AND kind='workspace:draft_dependency'", (scope.profile_id,)).fetchone()
        raw = json.loads(serialized)
        if corruption == 'duplicate':
            insert_publication(db, scope.profile_id, serialized)
        else:
            raw['id' if corruption == 'sql_identity' else 'origin'] = str(uuid4())
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), identifier))
    with service.store._read() as db:
        with pytest.raises(EvidenceError):
            service._association(db, scope.profile_id, scope.application_id, generated.id)


@pytest.mark.parametrize('failure', ['malformed', 'write_during_decode'])
def test_discovery_install_is_atomic_and_requires_unchanged_transaction(drafting, monkeypatch, failure):  # noqa: F811
    service, scope, *_ = drafting
    generated, *_ = execute(drafting)
    with service.store._tx() as db:
        if failure == 'malformed':
            insert_publication(db, scope.profile_id, '{malformed')
        context = service._association_context(db, scope.profile_id, scope.application_id)
        if failure == 'write_during_decode':
            original = json.loads
            written = False

            def decoding(*args, **kwargs):
                nonlocal written
                result = original(*args, **kwargs)
                if not written:
                    written = True
                    db.execute('UPDATE profiles SET data=data WHERE id=?', (scope.profile_id,))
                return result

            monkeypatch.setattr('copilot.drafting.service.json.loads', decoding)
        with pytest.raises((ValueError, EvidenceError)):
            service._association_publication_rows(db, scope.profile_id, scope.application_id,
                                                 generated.id, context=context)
        assert context.discovery_rows is None


@pytest.mark.parametrize('mismatch', ['owner', 'application', 'connection', 'write', 'ended', 'closed'])
def test_complete_discovery_never_consumed_outside_original_read_scope(drafting, mismatch):  # noqa: F811
    service, scope, *_ = drafting
    generated, *_ = execute(drafting)
    with service.store._tx() as db:
        context = service._association_context(db, scope.profile_id, scope.application_id)
        selected = service._association_publication_rows(db, scope.profile_id, scope.application_id,
                                                       generated.id, context=context)
        assert isinstance(selected, tuple) and len(selected) == 1
        assert all(isinstance(row, tuple) and len(row) == 2 and all(isinstance(v, str) for v in row)
                   for row in selected)
        if mismatch == 'closed':
            pass
        elif mismatch == 'connection':
            with service.store._read() as other:
                with pytest.raises(EvidenceError):
                    service._association_publication_rows(other, scope.profile_id, scope.application_id,
                                                         generated.id, context=context)
        else:
            owner = str(uuid4()) if mismatch == 'owner' else scope.profile_id
            app = str(uuid4()) if mismatch == 'application' else scope.application_id
            if mismatch == 'write':
                db.execute('UPDATE profiles SET data=data WHERE id=?', (scope.profile_id,))
            elif mismatch == 'ended':
                db.commit()
            with pytest.raises(EvidenceError):
                service._association_publication_rows(db, owner, app, generated.id, context=context)
    if mismatch == 'closed':
        with pytest.raises(EvidenceError):
            service._association_publication_rows(db, scope.profile_id, scope.application_id,
                                                 generated.id, context=context)


def test_fresh_generation_calls_discover_independently_and_detect_new_corruption(drafting):  # noqa: F811
    service, scope, *_ = drafting
    generated, *_ = execute(drafting)
    with service.store._read() as db:
        statements = []
        db.set_trace_callback(statements.append)
        for _ in range(2):
            assert service._generation_association(db, scope.profile_id, scope.application_id, generated.id)[0] == generated
        assert len(discovery_queries(statements)) == 2
    with service.store._tx() as db:
        insert_publication(db, scope.profile_id, json.dumps({'draft_id': generated.id}))
    with service.store._read() as db:
        with pytest.raises(EvidenceError):
            service._generation_association(db, scope.profile_id, scope.application_id, generated.id)
