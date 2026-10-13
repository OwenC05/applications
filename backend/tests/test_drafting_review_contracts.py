"""Pure strict review schemas: validation never grants writing/browser authority."""
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from copilot.domain import contracts as c
from copilot.drafting.review_contracts import (
    ReviewDisclosure,
    ReviewPreviewRequest,
    ReviewPublication,
    ReviewRequest,
    review_request_hash,
    review_validated,
)
from copilot.retrieval.packet_contracts import CoverLetterTarget, ManualRequirement

WARNINGS = ('WRITING_ONLY', 'AUTOMATED_SUPPORT_FALLIBLE', 'NOT_FACT_CONFIRMATION',
            'MANUAL_AND_ATTACHMENTS_NOT_REVIEWED', 'NO_BROWSER_AUTHORIZATION')
TEXT = '  Café e\u0301 😀\n\tA\r\n '
DIGEST = 'a' * 64


def identifier():
    return str(uuid4())


def request_body():
    return dict(expected_revisions=c.RevisionVector(), parent_text_sha256=DIGEST,
                parent_ledger_sha256=DIGEST, parent_publication_sha256=DIGEST,
                idempotency_key='review-key', disclosure_sha256=DIGEST, acknowledged=True)


def disclosure_body():
    owner, app = identifier(), identifier()
    span = c.SourceSpan(record_id=identifier(), unit_id=None, start=0, end=1,
                        text_sha256=DIGEST, excerpt='A')
    reference = c.EvidenceReference(profile_id=owner, kind='confirmed_fact', span=span,
                                    generation_id=identifier())
    support = c.SupportSpan(target='q', start=0, end=1, excerpt='A', kind='personal',
                            assessment='supported', citations=(reference,), integrity='valid')
    return dict(operation='human_writing_review', profile_id=owner, application_id=app,
        parent_draft_id=identifier(), parent_publication_id=identifier(),
        parent_publication_sha256=DIGEST, batch_id=identifier(), batch_sha256=DIGEST,
        research_run_id=identifier(), pre_revisions=c.RevisionVector(),
        post_revisions=c.RevisionVector(application_output=1), text_sha256=DIGEST,
        ledger_sha256=DIGEST, cover_letter=TEXT,
        answers=(c.DraftAnswer(question_id='q', text='A', packet_id=identifier()),),
        ledger=(support,), canonical_questions=(c.Question(id='q', text='Why?', type='writing',
        constraint_origin='user'),), cover_letter_target=CoverLetterTarget(text='Letter',
        constraint_origin='user'), manual_requirements=(ManualRequirement(question_id='optional',
        reason='Manually reviewed elsewhere'),), style_notes=('Advisory only',), warnings=WARNINGS)


def publication_body():
    disclosure = ReviewDisclosure(**disclosure_body())
    body = request_body()
    body['disclosure_sha256'] = c.canonical_hash(disclosure)
    request = ReviewRequest(**body)
    return dict(profile_id=disclosure.profile_id, application_id=disclosure.application_id,
        origin='review', id=identifier(), draft_id=identifier(),
        parent_draft_id=disclosure.parent_draft_id, parent_text_sha256=DIGEST,
        parent_ledger_sha256=DIGEST, parent_publication_id=disclosure.parent_publication_id,
        parent_publication_sha256=DIGEST, batch_id=disclosure.batch_id, batch_sha256=DIGEST,
        pre_revisions=c.RevisionVector(), edit_depth=0, created_at=datetime.now(UTC),
        request=request, request_sha256=review_request_hash(disclosure.profile_id,
        disclosure.application_id, disclosure.parent_draft_id, request), disclosure=disclosure,
        review_snapshot_id=identifier(), review_snapshot_sha256=DIGEST,
        text_sha256=DIGEST, ledger_sha256=DIGEST, reason_codes=(), style_notes=('Advisory only',))


def test_exact_schema_and_no_paid_fields():
    assert set(ReviewPreviewRequest.model_fields) == {'schema_version', 'expected_revisions',
        'parent_text_sha256', 'parent_ledger_sha256', 'parent_publication_sha256'}
    assert set(ReviewRequest.model_fields) == set(ReviewPreviewRequest.model_fields) | {
        'idempotency_key', 'disclosure_sha256', 'acknowledged'}
    assert set(ReviewDisclosure.model_fields) == set(disclosure_body()) | {'schema_version'}
    assert set(ReviewPublication.model_fields) == set(publication_body()) | {'schema_version'}
    assert not {'job_id', 'fence', 'intent_id', 'model', 'budget'} & set(ReviewPublication.model_fields)


@pytest.mark.parametrize('model,body', [(ReviewRequest, request_body),
    (ReviewDisclosure, disclosure_body), (ReviewPublication, publication_body)])
def test_python_and_json_roundtrip(model, body):
    value = model(**body())
    assert review_validated(value, model) == value
    assert model.model_validate_json(value.model_dump_json()) == value
    assert model.model_validate(value) == value
    with pytest.raises(ValidationError):
        value.schema_version = 2


@pytest.mark.parametrize('value', [1, 0, False, 'true', None])
def test_acknowledgement_identity(value):
    with pytest.raises(ValidationError):
        ReviewRequest(**(request_body() | {'acknowledged': value}))
    forged = ReviewRequest(**request_body()).model_copy(update={'acknowledged': value})
    with pytest.raises(ValidationError):
        review_request_hash(identifier(), identifier(), identifier(), forged)


@pytest.mark.parametrize('dimension', tuple(c.RevisionVector.model_fields)[1:])
@pytest.mark.parametrize('value', [True, '1', -1, None])
def test_every_vector_dimension_strict(dimension, value):
    vector = c.RevisionVector().model_copy(update={dimension: value})
    with pytest.raises(ValidationError):
        ReviewRequest(**(request_body() | {'expected_revisions': vector}))


@pytest.mark.parametrize('field', ['expected_revisions', 'pre_revisions', 'post_revisions'])
@pytest.mark.parametrize('dimension', tuple(c.RevisionVector.model_fields)[1:])
def test_every_vector_dimension_explicit(field, dimension):
    model, body = (ReviewRequest, request_body()) if field == 'expected_revisions' else (
        ReviewDisclosure, disclosure_body())
    vector = c.RevisionVector().model_dump()
    del vector[dimension]
    with pytest.raises(ValidationError):
        model(**(body | {field: vector}))
    forged = c.RevisionVector.model_construct()
    object.__delattr__(forged, dimension)
    with pytest.raises(ValidationError):
        model(**(body | {field: forged}))


@pytest.mark.parametrize('scope', [0, 1, 2])
@pytest.mark.parametrize('bad', ['bad', True, '', '00000000-0000-0000-0000-000000000000'])
def test_hash_validates_uuid_scope(scope, bad):
    ids = [identifier(), identifier(), identifier()]
    ids[scope] = bad
    with pytest.raises(ValidationError):
        review_request_hash(*ids, ReviewRequest(**request_body()))


def test_hash_exact_namespace_and_unicode():
    ids = (identifier(), identifier(), identifier())
    request = ReviewRequest(**request_body())
    expected = c.canonical_hash(dict(operation='human_writing_review', profile_id=ids[0],
        application_id=ids[1], parent_draft_id=ids[2], request=request.model_dump(mode='json')))
    assert review_request_hash(*ids, request) == expected
    for index in range(3):
        other = list(ids)
        other[index] = identifier()
        assert review_request_hash(*other, request) != expected
    for key in [TEXT, TEXT.replace('\r\n', '\n'), TEXT.replace('e\u0301', 'é')]:
        modified = request.model_copy(update={'idempotency_key': key})
        assert review_request_hash(*ids, modified) != expected
    assert len({review_request_hash(*ids, request.model_copy(update={'idempotency_key': key}))
                for key in [TEXT, TEXT.replace('\r\n', '\n'), TEXT.replace('e\u0301', 'é')]}) == 3


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'ack'])
def test_forged_request_rejected_before_hash(mutation):
    request = ReviewRequest(**request_body())
    if mutation == 'extra':
        request = request.model_copy(update={'job_id': identifier()})
    elif mutation == 'missing':
        object.__delattr__(request, 'parent_text_sha256')
    else:
        request = request.model_copy(update={'acknowledged': 1})
    for validate in [lambda: ReviewRequest.model_validate(request),
                     lambda: review_validated(request, ReviewRequest),
                     lambda: review_request_hash(identifier(), identifier(), identifier(), request)]:
        with pytest.raises(ValidationError):
            validate()


@pytest.mark.parametrize('field', ['answers', 'ledger', 'canonical_questions',
                                  'manual_requirements', 'style_notes', 'warnings'])
def test_python_lists_rejected_json_arrays_accepted(field):
    body = disclosure_body()
    with pytest.raises(ValidationError):
        ReviewDisclosure(**(body | {field: list(body[field])}))
    valid = ReviewDisclosure(**body)
    assert ReviewDisclosure.model_validate_json(valid.model_dump_json()) == valid


@pytest.mark.parametrize('field', ['answers', 'ledger', 'canonical_questions',
                                  'manual_requirements', 'cover_letter_target'])
@pytest.mark.parametrize('mutation', ['extra', 'missing'])
def test_nested_forged_instances_rejected(field, mutation):
    body = disclosure_body()
    nested = body[field][0] if isinstance(body[field], tuple) else body[field]
    if mutation == 'extra':
        forged = nested.model_copy(update={'injected': True})
    else:
        forged = nested.model_copy()
        required = next(name for name, spec in type(nested).model_fields.items() if spec.is_required())
        object.__delattr__(forged, required)
    body[field] = (forged,) if isinstance(body[field], tuple) else forged
    with pytest.raises(ValidationError):
        ReviewDisclosure(**body)


@pytest.mark.parametrize('level', ['reference', 'span', 'citation_tuple'])
@pytest.mark.parametrize('mutation', ['extra', 'missing', 'scalar'])
def test_nested_citations_and_source_spans_revalidated(level, mutation):
    body = disclosure_body()
    support = body['ledger'][0]
    reference = support.citations[0]
    nested = reference.span if level == 'span' else reference
    if mutation == 'extra':
        forged = nested.model_copy(update={'forged': True})
    elif mutation == 'missing':
        forged = nested.model_copy()
        object.__delattr__(forged, 'excerpt' if level == 'span' else 'generation_id')
    else:
        forged = nested.model_copy(update={'start': True} if level == 'span' else {'profile_id': 1})
    if level == 'span':
        forged = reference.model_copy(update={'span': forged})
    citations = [forged] if level == 'citation_tuple' else (forged,)
    body['ledger'] = (support.model_copy(update={'citations': citations}),)
    with pytest.raises(ValidationError):
        ReviewDisclosure(**body)


@pytest.mark.parametrize('warnings', [(), WARNINGS[::-1], WARNINGS[:-1], WARNINGS + ('EXTRA',),
                                     ('WRITING_ONLY',) * 5])
def test_fixed_ordered_warnings(warnings):
    with pytest.raises(ValidationError):
        ReviewDisclosure(**(disclosure_body() | {'warnings': warnings}))


def test_disclosure_hash_binds_exact_writing_targets_and_notes():
    disclosure = ReviewDisclosure(**disclosure_body())
    original = c.canonical_hash(disclosure)
    for updates in [{'cover_letter': TEXT.replace('\r\n', '\n')},
                    {'cover_letter': TEXT.replace('e\u0301', 'é')},
                    {'cover_letter_target': disclosure.cover_letter_target.model_copy(update={'required': False})},
                    {'cover_letter_target': disclosure.cover_letter_target.model_copy(update={'max_words': 399})},
                    {'cover_letter_target': disclosure.cover_letter_target.model_copy(update={'max_chars': 999})},
                    {'cover_letter_target': None}, {'style_notes': ('Different advice',)}]:
        changed = review_validated(disclosure.model_copy(update=updates), ReviewDisclosure)
        assert c.canonical_hash(changed) != original
    assert disclosure.cover_letter == TEXT


@pytest.mark.parametrize('field,value', [('reason_codes', ('BLOCKED',)), ('edit_depth', True),
    ('edit_depth', -1), ('edit_depth', 65), ('origin', 'generation'),
    ('created_at', datetime(2026, 1, 1)),
    ('created_at', datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))))])
def test_publication_strict_fields(field, value):
    with pytest.raises(ValidationError):
        ReviewPublication(**(publication_body() | {field: value}))


def test_publication_hash_revalidates_copies_and_nested_request():
    publication = ReviewPublication(**publication_body())
    assert publication.publication_sha256 == c.canonical_hash(publication)
    for update in [{'hidden': True}, {'request': publication.request.model_copy(update={'acknowledged': 1})},
                   {'disclosure': publication.disclosure.model_copy(update={'unknown': True})}]:
        forged = publication.model_copy(update=update)
        with pytest.raises(ValidationError):
            _ = forged.publication_sha256


def test_wire_rejects_unknown_and_nonliteral_schema():
    for model, body in [(ReviewRequest, request_body), (ReviewDisclosure, disclosure_body),
                        (ReviewPublication, publication_body)]:
        wire = json.loads(model(**body()).model_dump_json())
        for update in [{'unknown': True}, {'schema_version': True}, {'schema_version': 2}]:
            with pytest.raises(ValidationError):
                model.model_validate_json(json.dumps(wire | update))


@pytest.mark.parametrize('dimension', tuple(c.RevisionVector.model_fields)[1:])
def test_disclosure_output_only_next_vector(dimension):
    body = disclosure_body()
    post = body['post_revisions'].model_copy(update={dimension: 2})
    with pytest.raises(ValidationError):
        ReviewDisclosure(**(body | {'post_revisions': post}))


@pytest.mark.parametrize('model,body', [(ReviewRequest, request_body),
    (ReviewDisclosure, disclosure_body), (ReviewPublication, publication_body)])
def test_json_schema_is_closed(model, body):
    schema = model.model_json_schema()
    assert schema['additionalProperties'] is False
    assert set(schema['properties']) == set(model.model_fields)
    assert model.model_validate_json(model(**body()).model_dump_json())


@pytest.mark.parametrize('field', ['idempotency_key', 'disclosure_sha256', 'parent_text_sha256',
                                  'parent_ledger_sha256', 'parent_publication_sha256'])
def test_missing_request_fields_from_model_construct(field):
    body = request_body()
    del body[field]
    forged = ReviewRequest.model_construct(**body)
    with pytest.raises(ValidationError):
        review_request_hash(identifier(), identifier(), identifier(), forged)


@pytest.mark.parametrize('key', ['', 'x' * 201, 1, None])
def test_idempotency_key_bounds(key):
    with pytest.raises(ValidationError):
        ReviewRequest(**(request_body() | {'idempotency_key': key}))


def test_python_datetime_string_not_coerced_but_json_utc_accepted():
    body = publication_body()
    with pytest.raises(ValidationError):
        ReviewPublication(**(body | {'created_at': body['created_at'].isoformat()}))
    wire = json.loads(ReviewPublication(**body).model_dump_json())
    assert ReviewPublication.model_validate_json(json.dumps(wire)).created_at == body['created_at']


def test_preserves_extra_storage_on_forged_nested_models():
    body = disclosure_body()
    nested = body['answers'][0].model_copy()
    object.__setattr__(nested, '__pydantic_extra__', {'unknown': 'retained'})
    with pytest.raises(ValidationError):
        ReviewDisclosure(**(body | {'answers': (nested,)}))
