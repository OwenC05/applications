"""Invocation-local graph validation and no-cost history certificates."""
import json
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from test_drafting_assessment import assess, assessment  # noqa: F401
from test_drafting_edits import request as edit_request
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.drafting.admission_runtime import DraftAdmission


def test_validated_parent_is_shared_only_within_one_invocation(drafting, monkeypatch):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    child = service.edit(scope.profile_id, scope.application_id, parent.id,
                         edit_request(service, scope, parent, 'exact'))
    original = service._generation_association
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[-1])
        return original(*args, **kwargs)

    monkeypatch.setattr(service, '_generation_association', counted)
    with service.store._read() as db:
        context = service._association_context(db, scope.profile_id, scope.application_id)
        first = service._association(db, scope.profile_id, scope.application_id, child.id, context=context)
        assert service._association(db, scope.profile_id, scope.application_id, child.id, context=context) == first
        counts = service._lineage_counts(db, scope.profile_id, scope.application_id, child.id, context=context)
        assert (counts.edits, counts.assessments, counts.reviews, counts.total) == (1, 0, 0, 1)
        projected = service._lineage_counts(db, scope.profile_id, scope.application_id, child.id,
                                            append_origin='assessment', context=context)
        assert (projected.edits, projected.assessments, projected.reviews, projected.total) == (1, 1, 0, 2)
        with pytest.raises(FrozenInstanceError):
            counts.edits = 2
        assert calls == [parent.id]
        with pytest.raises(EvidenceError):
            service._association(db, scope.profile_id, scope.application_id, child.id,
                                 visited=(child.id,), context=context)
    with service.store._read() as db:
        service._association(db, scope.profile_id, scope.application_id, child.id)
    assert calls == [parent.id, parent.id]


@pytest.mark.parametrize('mismatch', ['connection', 'owner', 'application', 'write', 'closed'])
def test_context_never_crosses_scope_connection_or_write(drafting, mismatch):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    with service.store._tx() as db:
        context = service._association_context(db, scope.profile_id, scope.application_id)
        service._association(db, scope.profile_id, scope.application_id, parent.id, context=context)
        if mismatch == 'closed':
            pass
        elif mismatch == 'connection':
            with service.store._read() as other:
                with pytest.raises(EvidenceError):
                    service._association(other, scope.profile_id, scope.application_id, parent.id, context=context)
        else:
            owner = str(uuid4()) if mismatch == 'owner' else scope.profile_id
            app = str(uuid4()) if mismatch == 'application' else scope.application_id
            if mismatch == 'write':
                db.execute('UPDATE profiles SET data=data WHERE id=?', (scope.profile_id,))
            with pytest.raises(EvidenceError):
                service._association(db, owner, app, parent.id, context=context)
    if mismatch == 'closed':
        with pytest.raises(EvidenceError):
            service._association(db, scope.profile_id, scope.application_id, parent.id, context=context)


def test_projected_origin_legality_and_actual_edit_count(drafting):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    with service.store._read() as db:
        counts = service._lineage_counts(db, scope.profile_id, scope.application_id, parent.id,
                                         append_origin='review')
        assert (counts.edits, counts.assessments, counts.reviews, counts.total) == (0, 0, 1, 1)
        for origin in ('assessment', 'unknown'):
            with pytest.raises(EvidenceError):
                service._lineage_counts(db, scope.profile_id, scope.application_id, parent.id,
                                        append_origin=origin)


def test_history_capture_matches_state_without_writes_or_source_io(drafting, monkeypatch):  # noqa: F811
    service, scope, *_ = drafting
    execute(drafting)
    monkeypatch.setattr(service, '_verify', lambda *a: pytest.fail('History read original bytes'))
    expected = service.admission_state(scope.profile_id, scope.application_id)
    with service.store._read() as db:
        before = db.total_changes
        capture = DraftAdmission(service).capture_history(db, scope.profile_id, scope.application_id)
        assert db.total_changes == before
        assert capture.head.revision == expected['admission_revision']
        assert capture.reason_codes == tuple(expected['reason_codes'])
        assert len(capture.completed_publication_certificates) == 1
        assert capture.completed_publication_certificates[0].kind == 'draft'
        with pytest.raises(FrozenInstanceError):
            capture.reason_codes = ()


def test_one_history_snapshot_shares_association_not_authority_between_jobs(assessment, monkeypatch):  # noqa: F811
    service, scope, *_ = assessment
    assess(assessment)
    original = service._generation_association
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[-1])
        return original(*args, **kwargs)

    monkeypatch.setattr(service, '_generation_association', counted)
    with service.store._read() as db:
        capture = DraftAdmission(service).capture_history(db, scope.profile_id, scope.application_id)
        assert len(capture.completed_publication_certificates) == 2
        assert {p.kind for p in capture.completed_publication_certificates} == {'draft', 'assess'}
        assert len(calls) == 1


def test_inspection_exposes_writing_review_flags_without_clearing_manual_gate(drafting):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    assert detail['capabilities']['review'] is True
    assert detail['writing_review_ready'] is False
    assert detail['reviewed_current'] is False and detail['review_snapshot'] is None
    assert detail['review_eligible'] is False and detail['browser_eligible'] is False
    assert 'MANUAL_REQUIREMENTS_UNRESOLVED' in detail['reason_codes']


def test_history_parses_all_publications_once_per_snapshot_and_matches_uncached_certificates(assessment, monkeypatch):  # noqa: F811
    service, scope, *_ = assessment
    assess(assessment)
    admission = DraftAdmission(service)
    original = admission._publication_rows
    calls = []

    def counted(*args):
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(admission, '_publication_rows', counted)
    with service.store._read() as db:
        changes = db.total_changes
        capture = admission.capture_history(db, scope.profile_id, scope.application_id)
        assert len(capture.completed_publication_certificates) == 2
        assert len(calls) == 1
        independent = tuple(admission._completed_publication_certificate(
            db, scope.profile_id, scope.application_id, certificate.job_id)
            for certificate in capture.completed_publication_certificates)
        assert independent == capture.completed_publication_certificates
        assert len(calls) == 3  # Context-free calls each parse independently.
        again = admission.capture_history(db, scope.profile_id, scope.application_id)
        assert again == capture and len(calls) == 4
        assert db.total_changes == changes
    with service.store._read() as db:
        assert admission.capture_history(db, scope.profile_id, scope.application_id) == capture
        assert len(calls) == 5


@pytest.mark.parametrize('mismatch', ['owner', 'application', 'write'])
def test_publication_cache_guard_precedes_seeded_cache_consumption(drafting, monkeypatch, mismatch):  # noqa: F811
    service, scope, *_ = drafting
    _, job, _ = execute(drafting)
    admission = DraftAdmission(service)
    with service.store._tx() as db:
        context = service._association_context(db, scope.profile_id, scope.application_id)
        context.publication_rows = ((), ())
        owner = str(uuid4()) if mismatch == 'owner' else scope.profile_id
        app = str(uuid4()) if mismatch == 'application' else scope.application_id
        if mismatch == 'write':
            db.execute('UPDATE profiles SET data=data WHERE id=?', (scope.profile_id,))
        monkeypatch.setattr(admission, '_attempts', lambda *a: pytest.fail('Invalid context consumed authority'))
        with pytest.raises(EvidenceError):
            admission._completed_publication_certificate(db, owner, app, job.id, context=context)


@pytest.mark.parametrize('corruption', ['malformed_other_app', 'duplicate_owned', 'foreign_draft', 'foreign_job'])
def test_cached_history_preserves_owner_wide_parser_and_collision_guards(drafting, corruption):  # noqa: F811
    service, scope, *_ = drafting
    draft, job, _ = execute(drafting)
    with service.store._tx() as db:
        identifier = str(uuid4())
        owner = scope.profile_id
        if corruption == 'malformed_other_app':
            payload = dict(profile_id=owner, application_id=str(uuid4()), draft_id=str(uuid4()), origin='edit')
        elif corruption == 'duplicate_owned':
            payload = json.loads(db.execute("SELECT data FROM records WHERE owner=? AND kind='workspace:draft_dependency'",
                                            (owner,)).fetchone()[0])
            payload['id'] = identifier
        else:
            owner = str(uuid4())
            payload = {'draft_id': draft.id} if corruption == 'foreign_draft' else {'job_id': job.id}
        db.execute('INSERT INTO records VALUES(?,?,?,?)',
                   (identifier, owner, 'workspace:draft_dependency', json.dumps(payload)))
    with service.store._read() as db:
        changes = db.total_changes
        capture = DraftAdmission(service).capture_history(db, scope.profile_id, scope.application_id)
        assert capture.reason_codes == ('integrity_blocked',)
        assert capture.completed_publication_certificates == ()
        assert db.total_changes == changes


@pytest.mark.parametrize('failure', ['parser', 'write_during_parser'])
def test_publication_cache_installed_only_after_complete_unchanged_parse(drafting, monkeypatch, failure):  # noqa: F811
    service, scope, *_ = drafting
    execute(drafting)
    admission = DraftAdmission(service)
    original = admission._publication_rows
    with service.store._tx() as db:
        context = service._association_context(db, scope.profile_id, scope.application_id)

        def failed_parser(*args):
            if failure == 'parser':
                raise ValueError('Synthetic incomplete parse')
            parsed = original(*args)
            db.execute('UPDATE profiles SET data=data WHERE id=?', (scope.profile_id,))
            return parsed

        monkeypatch.setattr(admission, '_publication_rows', failed_parser)
        with pytest.raises(ValueError if failure == 'parser' else EvidenceError):
            admission._snapshot_publications(db, scope.profile_id, scope.application_id, context)
        assert context.publication_rows is None
