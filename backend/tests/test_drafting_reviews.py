"""Synthetic local writing reviews; no real provider or semantic qualification."""
import pytest
from test_drafting_service import drafting, execute
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.domain import contracts as c
from copilot.drafting.review_contracts import ReviewPreviewRequest, ReviewRequest


@pytest.fixture
def reviewing(packets, request):  # noqa: F811
    store, _, repo, app, scope, *_ = packets
    # Set legitimate canonical input before ANY packet/draft publication exists.
    with store._tx() as db:
        repo._save(db, scope.profile_id, 'application', app.model_copy(update={
            'questions': tuple(q if q.type == 'writing' else q.model_copy(update={'required': False})
                for q in app.questions if q.type == 'writing' or getattr(request, 'param', None) == 'optional_manual')}))
    return drafting.__wrapped__(packets)


def preview_body(service, scope, parent):
    with service.store._read() as db:
        _, publication, _, _ = service._association(db, scope.profile_id, scope.application_id, parent.id)
    return ReviewPreviewRequest(expected_revisions=parent.revisions,
        parent_text_sha256=parent.text_sha256, parent_ledger_sha256=parent.ledger_sha256,
        parent_publication_sha256=publication.publication_sha256)


def review_body(service, scope, parent, key='review'):
    body = preview_body(service, scope, parent)
    preview = service.review_preview(scope.profile_id, scope.application_id, parent.id, body)
    return ReviewRequest(**body.model_dump(), idempotency_key=key,
                         disclosure_sha256=preview['disclosure_sha256'], acknowledged=True)


def test_exact_clone_snapshot_and_no_paid_mutation(reviewing):
    service, scope, _, sent, _ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    with service.store._read() as db:
        before = {t: tuple(db.execute(f'SELECT * FROM {t}').fetchall()) for t in
                  ('jobs', 'provider_attempts', 'drafting_admission_heads', 'drafting_admission_bindings')}
    child = service.review(scope.profile_id, scope.application_id, parent.id, body)
    assert child.id != parent.id and child.text_sha256 == parent.text_sha256
    assert child.ledger == parent.ledger and child.answers == parent.answers
    assert child.revisions.application_output == parent.revisions.application_output + 1
    assert len(sent) == 3
    with service.store._read() as db:
        for table, rows in before.items():
            assert tuple(db.execute(f'SELECT * FROM {table}').fetchall()) == rows
        snapshot = service.domain._all(db, scope.profile_id, 'draft_review', c.ReviewSnapshot)[0]
        assert snapshot.draft_id == child.id and snapshot.revisions == child.revisions
    assert service.review(scope.profile_id, scope.application_id, parent.id, body) == child


@pytest.mark.parametrize('field', ['metadata', 'facts', 'documents', 'consent',
                                  'application_input', 'application_output', 'research'])
def test_each_revision_dimension_is_required_current(reviewing, field):
    from copilot.contracts import EvidenceError
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    revision = body.expected_revisions.model_copy(update={field: getattr(body.expected_revisions, field) + 1})
    with pytest.raises(EvidenceError) as error:
        service.review(scope.profile_id, scope.application_id, parent.id,
                       body.model_copy(update={'expected_revisions': revision}))
    assert error.value.http_status == 409


@pytest.mark.parametrize('change', [dict(acknowledged=1), dict(acknowledged=False),
    dict(idempotency_key=''), dict(disclosure_sha256='bad'),
    dict(expected_revisions={'facts': 0}), dict(parent_text_sha256=True)])
def test_unchecked_request_rejected_before_record_access(reviewing, change, monkeypatch):
    from copilot.contracts import EvidenceError
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = review_body(service, scope, parent)
    monkeypatch.setattr(service.store, '_read', lambda: pytest.fail('invalid request accessed records'))
    with pytest.raises(EvidenceError) as error:
        service.review(scope.profile_id, scope.application_id, parent.id, body.model_copy(update=change))
    assert error.value.http_status == 422


@pytest.mark.parametrize('identifier', ['not-uuid', '', 1, True])
def test_public_scope_ids_validated_before_read(reviewing, identifier, monkeypatch):
    from copilot.contracts import EvidenceError
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = preview_body(service, scope, parent)
    monkeypatch.setattr(service.store, '_read', lambda: pytest.fail('invalid scope accessed records'))
    with pytest.raises(EvidenceError) as error:
        service.review_preview(identifier, scope.application_id, parent.id, body)
    assert error.value.http_status == 422


def test_required_manual_input_blocks_preview_without_reason_rewriting(drafting):
    from copilot.contracts import EvidenceError
    from copilot.domain.repository import TypedInput
    service, scope, *_ = drafting
    with service.store._read() as db:
        metadata = service.domain.revisions(db, scope.profile_id).metadata
    service.domain.typed(scope.profile_id, TypedInput(expected_metadata_revision=metadata,
        kind='identity', field='manual', value='CONFIRMED_MANUAL_CANARY', purpose='application',
        application_id=scope.application_id, explicitly_confirmed=True))
    parent, *_ = execute(drafting)
    body = preview_body(service, scope, parent)
    with pytest.raises(EvidenceError) as error:
        service.review_preview(scope.profile_id, scope.application_id, parent.id, body)
    assert error.value.http_status == 409
    assert 'MANUAL_REQUIREMENTS_UNRESOLVED' in service.detail(
        scope.profile_id, scope.application_id, parent.id)['reason_codes']


def test_unsupported_retained_claim_blocks_review(reviewing):
    from copilot.contracts import EvidenceError
    service, scope, _, _, settings = reviewing
    settings['unsupported'] = True
    parent, *_ = execute(reviewing)
    with pytest.raises(EvidenceError):
        service.review_preview(scope.profile_id, scope.application_id, parent.id,
                               preview_body(service, scope, parent))


def test_preview_is_deterministic_exact_warnings_without_key_or_provider(reviewing, monkeypatch):
    service, scope, _, sent, settings = reviewing
    settings['text'] = '  Café e\u0301 😀\r\n\t '
    parent, *_ = execute(reviewing)
    body = preview_body(service, scope, parent)
    monkeypatch.setattr(service.provider, '_key_supplier', lambda: pytest.fail('preview read a key'))
    first = service.review_preview(scope.profile_id, scope.application_id, parent.id, body)
    assert service.review_preview(scope.profile_id, scope.application_id, parent.id, body) == first
    disclosure = first['disclosure']
    assert disclosure['answers'][0]['text'] == settings['text']
    assert disclosure['cover_letter_target'] is None
    assert disclosure['manual_requirements'] == []
    assert disclosure['warnings'] == ['WRITING_ONLY', 'AUTOMATED_SUPPORT_FALLIBLE',
        'NOT_FACT_CONFIRMATION', 'MANUAL_AND_ATTACHMENTS_NOT_REVIEWED', 'NO_BROWSER_AUTHORIZATION']
    assert first['disclosure_sha256'] == c.canonical_hash(disclosure)
    assert len(sent) == 3


def test_review_child_cannot_be_reviewed_again(reviewing):
    from copilot.contracts import EvidenceError
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    child = service.review(scope.profile_id, scope.application_id, parent.id,
                           review_body(service, scope, parent))
    with pytest.raises(EvidenceError):
        service.review_preview(scope.profile_id, scope.application_id, child.id,
                               preview_body(service, scope, child))


@pytest.mark.parametrize('digest_raw', [False, True])
def test_retained_snapshot_cannot_omit_zero_revision_dimensions(reviewing, digest_raw):
    import json

    from copilot.contracts import EvidenceError
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    child = service.review(scope.profile_id, scope.application_id, parent.id,
                           review_body(service, scope, parent))
    with service.store._tx() as db:
        identity, serialized = db.execute("SELECT id,data FROM records WHERE kind='workspace:draft_review'").fetchone()
        raw = json.loads(serialized)
        assert raw['revisions']['facts'] == 0
        del raw['revisions']['facts']
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), identity))
        pubid, pubjson = db.execute("SELECT id,data FROM records WHERE kind='workspace:draft_dependency' "
            "AND json_extract(data,'$.origin')='review'").fetchone()
        publication = json.loads(pubjson)
        publication['review_snapshot_sha256'] = c.canonical_hash(raw if digest_raw else
            c.ReviewSnapshot.model_validate_json(json.dumps(raw)))
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(publication), pubid))
    with pytest.raises(EvidenceError):
        service.detail(scope.profile_id, scope.application_id, child.id)


@pytest.mark.parametrize('reviewing', ['optional_manual'], indirect=True)
def test_optional_manual_is_disclosed_not_inferred_or_silently_dropped(reviewing):
    service, scope, *_ = reviewing
    parent, *_ = execute(reviewing)
    body = preview_body(service, scope, parent)
    preview = service.review_preview(scope.profile_id, scope.application_id, parent.id, body)
    manual = preview['disclosure']['manual_requirements']
    assert len(manual) == 1 and manual[0]['question_id'] == 'manual'
    assert not next(q for q in preview['disclosure']['canonical_questions'] if q['id'] == 'manual')['required']
    child = service.review(scope.profile_id, scope.application_id, parent.id,
                           review_body(service, scope, parent))
    assert child.answers == parent.answers
