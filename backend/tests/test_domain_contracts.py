"""S0 characterization: validates contracts, not unimplemented orchestration."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from copilot.domain import contracts as c

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
OWNER, APP, RUN, UNIT, RECORD, GENERATION = (str(uuid4()) for _ in range(6))


def reference(kind='confirmed_fact', **changes):
    text = 'A😀 built tools.'
    values = dict(profile_id=OWNER, kind=kind, generation_id=GENERATION,
                  span=c.SourceSpan(record_id=RECORD, unit_id=UNIT, start=1, end=2,
                                    excerpt='😀', text_sha256=c.text_hash(text)))
    if kind == 'employer':
        values.update(application_id=APP, research_run_id=RUN)
    values.update(changes)
    return c.EvidenceReference(**values)


def packet(**changes):
    values = dict(id=str(uuid4()), profile_id=OWNER, application_id=APP, research_run_id=RUN,
                  question=c.Question(id='q1', text='Why this role?', type='writing',
                                      constraint_origin='user'),
                  query_variants=('engineering not snake handling',), facts=(reference(),),
                  employer=(reference('employer'),), revisions=c.RevisionVector(),
                  model_version='pinned-model', prompt_version='draft-v1')
    values.update(changes)
    return c.EvidencePacket(**values)


def payload(**changes):
    values = dict(profile_id=OWNER, application_id=APP,
                  destination='https://boards.example.test/apply', account_indicator='account-1',
                  vacancy_id='job-1', form_snapshot_id=UNIT, form_revision=1, step_id='one',
                  fields=(c.SemanticField(id='name', step_id='one', value='Ada'),
                          c.SemanticField(id='eligibility', step_id='one', value=True)))
    values.update(changes)
    return c.SemanticPayload(**values)


def draft(**changes):
    values = dict(id=str(uuid4()), profile_id=OWNER, application_id=APP,
                  revisions=c.RevisionVector(), research_run_id=RUN,
                  answers=(c.DraftAnswer(question_id='q1', text='😀', packet_id=UNIT),),
                  ledger=(c.SupportSpan(target='q1', start=0, end=1, excerpt='😀', kind='personal',
                                        assessment='supported', integrity='valid',
                                        citations=(reference(),)),),
                  assessment_state='assessed', inventory_complete=True, created_at=NOW)
    values.update(changes)
    return c.DraftRevision(**values)


@pytest.mark.parametrize('value', ['true', 1, False])
def test_confirmation_never_coerces(value):
    with pytest.raises(ValidationError):
        c.ConfirmFact(profile_id=OWNER, proposal_id=UNIT, confirmed=value, expected_facts_revision=0)


def test_confirmation_and_frozen_extra_fields():
    fact = c.ConfirmFact(profile_id=OWNER, proposal_id=UNIT, confirmed=True, expected_facts_revision=0)
    with pytest.raises(ValidationError):
        fact.confirmed = False
    with pytest.raises(ValidationError):
        c.ConfirmFact(**fact.model_dump(), arbitrary='tool')


def test_revision_dependencies_do_not_reindex_for_ui_edit():
    captured = c.RevisionVector(facts=2, documents=3)
    renamed = c.RevisionVector(metadata=1, facts=2, documents=3)
    assert captured.matches(renamed, ('facts', 'documents'))
    assert not captured.matches(renamed, ('metadata',))
    assert not captured.matches(c.RevisionVector(facts=3), ('facts',))
    with pytest.raises(ValueError):
        captured.matches(renamed, ('invented',))
    with pytest.raises(ValidationError):
        c.RevisionVector(facts=True)


def test_codepoint_spans_reuse_existing_span():
    span = reference().span
    span.validate_text('A😀 built tools.')
    assert span.evidence_span().start == 1
    assert span.evidence_span().end == 2
    with pytest.raises(ValueError):
        span.validate_text('A😀 led tools.')
    with pytest.raises(ValidationError):
        c.SourceSpan(record_id=RECORD, unit_id=UNIT, start=1, end=3, excerpt='😀',
                     text_sha256=span.text_sha256)


def test_canonical_hash_and_unicode_no_normalization():
    assert c.canonical_hash({'b': '😀', 'a': 1}) == c.canonical_hash({'a': 1, 'b': '😀'})
    assert c.text_hash('é') != c.text_hash('e\u0301')
    with pytest.raises(ValueError):
        c.canonical_hash({'x': float('nan')})
    assert packet().packet_sha256 != packet(gaps=('missing result',)).packet_sha256


@pytest.mark.parametrize('change', [dict(profile_id=str(uuid4())), dict(application_id=str(uuid4()))])
def test_packet_owner_and_application_scope(change):
    with pytest.raises(ValidationError):
        packet(employer=(reference('employer', **change),))


def test_packet_separates_corpora_and_requires_research_scope():
    with pytest.raises(ValidationError):
        packet(facts=(reference('employer'),))
    with pytest.raises(ValidationError):
        reference('employer', application_id=None)


def test_ledger_exact_text_and_support_gate():
    assert draft().support_ready
    assert not draft(assessment_state='stale').support_ready
    assert not draft(inventory_complete=False).support_ready
    with pytest.raises(ValidationError):
        draft(answers=(c.DraftAnswer(question_id='q1', text='led', packet_id=UNIT),))
    with pytest.raises(ValidationError):
        c.SupportSpan(target='q1', start=0, end=1, excerpt='x', kind='personal',
                      assessment='supported', integrity='valid')
    unsupported = c.SupportSpan(target='q1', start=0, end=1, excerpt='😀', kind='personal',
                                assessment='unsupported', integrity='valid', citations=(reference(),))
    assert not draft(ledger=(unsupported,)).support_ready


def test_ledger_cross_research_scope_rejected():
    with pytest.raises(ValidationError):
        draft(ledger=(c.SupportSpan(target='q1', start=0, end=1, excerpt='😀', kind='employer',
                                    assessment='supported', integrity='valid',
                                    citations=(reference('employer', research_run_id=str(uuid4())),)),))


def test_question_limits_unicode_and_duplicates():
    question = c.Question(id='q', text='Answer?', type='writing', constraint_origin='form', max_chars=1)
    question.check_answer('😀')
    with pytest.raises(ValueError):
        question.check_answer('😀x')
    with pytest.raises(ValueError):
        question.check_answer(' ')
    with pytest.raises(ValidationError):
        c.ApplicationRecord(profile_id=OWNER, application_id=APP, company='Example', role='Engineer',
                            sector='tech', vacancy_url='https://example.test/role', official_domains=(),
                            questions=(question, question), input_revision=0, created_at=NOW)


def test_hash_normalizes_field_order_not_semantics_or_steps():
    approved = payload()
    assert approved.sha256 == payload(fields=tuple(reversed(approved.fields))).sha256
    changed = payload(fields=(c.SemanticField(id='name', step_id='one', value='Other'),))
    assert approved.sha256 != changed.sha256
    assert not approved.permits_subset(changed)
    assert approved.permits_subset(payload(fields=(approved.fields[0],)))
    assert not approved.permits_subset(payload(account_indicator='other'))
    assert not approved.permits_subset(payload(fields=(c.SemanticField(id='demographic', step_id='one', value='extra'),)))
    with pytest.raises(ValidationError):
        payload(fields=(c.SemanticField(id='name', step_id='two', value='Ada'),))


def test_semantic_duplicates_and_opaque_fields_fail_closed():
    with pytest.raises(ValidationError):
        payload(fields=(payload().fields[0], payload().fields[0]))
    with pytest.raises(ValidationError):
        payload(cookie='secret')
    with pytest.raises(ValidationError):
        c.SemanticField(id='salary', step_id='one', value=120.5)


@pytest.mark.parametrize('url', ['http://example.test/', 'https://user:pass@example.test/',
                                'https://example.test:444/', 'https://example.test/#secret'])
def test_https_syntax_not_network_policy(url):
    with pytest.raises(ValidationError):
        payload(destination=url)


def test_separate_grant_lifetimes_and_consumption():
    values = dict(id=UNIT, profile_id=OWNER, application_id=APP, payload_sha256=payload().sha256,
                  review_id=RECORD, issued_at=NOW, expires_at=NOW + timedelta(minutes=15))
    assert c.FillGrant(**values).kind == 'fill'
    with pytest.raises(ValidationError):
        c.SubmitGrant(**values)
    values['expires_at'] = NOW + timedelta(minutes=5)
    assert c.SubmitGrant(**values, consumed_at=NOW).kind == 'submit'
    with pytest.raises(ValidationError):
        c.SubmitGrant(**values, consumed_at=NOW + timedelta(minutes=6))
    with pytest.raises(ValidationError):
        c.FillGrant(**{**values, 'issued_at': NOW.replace(tzinfo=None)})


def test_durable_job_fence_and_generation_scope():
    values = dict(id=UNIT, profile_id=OWNER, application_id=APP, kind='draft', stage='draft',
                  idempotency_key='one', state='running', revisions=c.RevisionVector(), fence=0)
    with pytest.raises(ValidationError):
        c.DurableJob(**values)
    job = c.DurableJob(**{**values, 'fence': 1}, lease_owner=RECORD,
                       lease_expires_at=NOW + timedelta(minutes=1))
    assert job.fence == 1
    with pytest.raises(ValidationError):
        c.GenerationIntent(profile_id=OWNER, generation_id=GENERATION, corpus='employer', job_id=UNIT,
                           fence=1, lease_expires_at=NOW, revisions=c.RevisionVector(),
                           snapshot_sha256=c.text_hash('snapshot'), model_fingerprint=c.text_hash('model'),
                           chunker_version='v1', dense_collection='evidence_one', sparse_relpath='one',
                           state='registered')


def test_receipt_never_upgrades_user_report():
    values = dict(id=UNIT, profile_id=OWNER, application_id=APP, attempt_id=None, recorded_at=NOW)
    assert c.SubmissionReceipt(**values, state='user_reported_submitted').confirmation_sha256 is None
    with pytest.raises(ValidationError):
        c.SubmissionReceipt(**values, state='receipt_confirmed')
    with pytest.raises(ValidationError):
        c.SubmissionReceipt(**values, state='user_reported_submitted', confirmation_sha256=c.text_hash('page'))
    with pytest.raises(ValidationError):
        c.SubmissionAttempt(id=UNIT, profile_id=OWNER, application_id=APP, grant_id=RECORD,
                            payload_sha256=payload().sha256, state='definitely_unsent',
                            prepared_at=NOW, final_action_at=NOW)


def test_original_provenance_and_closed_role_completeness():
    values = dict(id=UNIT, profile_id=OWNER, application_id=APP, revisions=c.RevisionVector(),
                  acquired_at=NOW, expires_at=NOW + timedelta(days=7))
    with pytest.raises(ValidationError):
        c.ResearchRun(**values, state='complete')
    assert c.ResearchRun(**values, state='closed_role').state == 'closed_role'
    with pytest.raises(ValidationError):
        c.EmployerSource(id=RECORD, profile_id=OWNER, application_id=APP, research_run_id=RUN,
                         purpose='company', provenance='public_fetch', original_url=None, final_url=None,
                         acquired_at=NOW, original_sha256=c.text_hash('bytes'),
                         canonical_sha256=c.text_hash('text'), extraction_version='v1')


def test_fact_reference_can_have_no_document_unit():
    original = reference()
    span = c.SourceSpan(**{**original.span.model_dump(), 'unit_id': None})
    fact = c.EvidenceReference(**{**original.model_dump(), 'span': span})
    assert fact.span.unit_id is None
    with pytest.raises(ValueError):
        fact.span.evidence_span()


def test_non_application_index_job_and_employer_run_scope():
    job = c.DurableJob(id=UNIT, profile_id=OWNER, kind='index', stage='capture',
                       idempotency_key='index', state='queued', revisions=c.RevisionVector(), fence=0)
    assert job.application_id is None
    with pytest.raises(ValidationError):
        c.DurableJob(**{**job.model_dump(), 'kind': 'draft'})
    values = dict(profile_id=OWNER, generation_id=GENERATION, corpus='employer', job_id=UNIT,
                  application_id=APP, fence=1, lease_expires_at=NOW, revisions=c.RevisionVector(),
                  snapshot_sha256=c.text_hash('snapshot'), model_fingerprint=c.text_hash('model'),
                  chunker_version='v1', dense_collection='evidence_one', sparse_relpath='one',
                  state='registered')
    with pytest.raises(ValidationError):
        c.GenerationIntent(**values)
    assert c.GenerationIntent(**values, research_run_id=RUN).research_run_id == RUN


def test_completed_multistep_payload_preserves_duplicate_names_and_order():
    first = payload(fields=(c.SemanticField(id='answer', step_id='one', value='First'),))
    second = payload(step_id='two', form_snapshot_id=RECORD,
                     fields=(c.SemanticField(id='answer', step_id='two', value='Second'),))
    completed = c.CompletedPayload(profile_id=OWNER, application_id=APP, steps=(first, second))
    reversed_steps = c.CompletedPayload(profile_id=OWNER, application_id=APP, steps=(second, first))
    assert completed.sha256 != reversed_steps.sha256
    with pytest.raises(ValidationError):
        c.CompletedPayload(profile_id=OWNER, application_id=APP, steps=(first, first))
    with pytest.raises(ValidationError):
        c.CompletedPayload(profile_id=OWNER, application_id=APP,
                           steps=(first, payload(account_indicator='Other')))


def test_text_hash_independent_of_packet_identity():
    original = draft()
    changed = draft(answers=(c.DraftAnswer(question_id='q1', text='😀', packet_id=RECORD),))
    assert original.text_sha256 == changed.text_sha256


def test_typed_sensitive_confirmation_is_strict():
    values = dict(id=UNIT, profile_id=OWNER, kind='eligibility', field='work_authorization',
                  value=True, purpose='application', explicitly_confirmed=True)
    assert c.TypedValue(**values).value is True
    for value in (1, 'true', False):
        with pytest.raises(ValidationError):
            c.TypedValue(**{**values, 'explicitly_confirmed': value})


def test_valid_schema_json_roundtrip_and_fact_hash():
    values = dict(id=UNIT, profile_id=OWNER, proposal_id=RECORD, text='Built tools',
                  text_sha256=c.text_hash('Built tools'), origin='interview',
                  confirmation_event_id=GENERATION, confirmed_at=NOW)
    fact = c.FactVersion(**values)
    assert c.FactVersion.model_validate_json(fact.model_dump_json()) == fact
    with pytest.raises(ValidationError):
        c.FactVersion(**{**values, 'text': 'Led tools'})
    assert c.ProfileRecord(profile_id=OWNER, name='Ada', sectors=('tech', 'finance'),
                           revisions=c.RevisionVector()).sectors == ('tech', 'finance')
    assert c.Proposal(id=UNIT, profile_id=OWNER, text='A proposed claim', origin='interview',
                      created_at=NOW).status == 'pending'
    assert c.InterviewProgress(profile_id=OWNER).completed is False
    assert c.PreparedAttachment(id=UNIT, profile_id=OWNER, filename='cv.pdf',
                                media_type='application/pdf', size_bytes=100,
                                sha256=c.text_hash('file'), approved_at=NOW).filename == 'cv.pdf'
    with pytest.raises(ValidationError):
        c.PreparedAttachment(id=UNIT, profile_id=OWNER, filename='../cv.pdf',
                             media_type='application/pdf', size_bytes=100,
                             sha256=c.text_hash('file'), approved_at=NOW)


def test_workspace_status_default_closed_and_no_provider_secret():
    status = c.WorkspaceStatus(boot_token='synthetic-bootstrap-token')
    assert not status.cloud_key_configured
    assert not any(value for key, value in status.capabilities.model_dump().items()
                   if key != 'schema_version')
    assert c.WorkspaceStatus.model_validate_json(status.model_dump_json()) == status
    with pytest.raises(ValidationError):
        c.WorkspaceStatus(boot_token='token', cloud_key='secret')
    with pytest.raises(ValidationError):
        c.WorkspaceStatus(boot_token='token', cloud_key_configured='true')


def test_profile_detail_json_roundtrip_strict_tuple_datetime():
    profile = c.ProfileRecord(profile_id=OWNER, name='Ada', sectors=('tech',),
                              revisions=c.RevisionVector())
    answer = c.InterviewAnswer(id=UNIT, profile_id=OWNER, question_id='intro', question='Tell us?',
                               section='background', answer='Built a tool', created_at=NOW)
    consent = c.ConsentRecord(id=RECORD, profile_id=OWNER, provider='test-provider',
                              purposes=('drafting',), granted=True, revision=1, disclosed_at=NOW)
    detail = c.ProfileDetail(profile=profile, consent=consent,
                             interview=c.InterviewProgress(profile_id=OWNER, answer_ids=(UNIT,)),
                             interview_answers=(answer,))
    restored = c.ProfileDetail.model_validate_json(detail.model_dump_json())
    assert restored == detail
    assert isinstance(restored.interview_answers, tuple)
    assert isinstance(restored.interview_answers[0].created_at, datetime)
    assert restored.interview_answers[0].created_at.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        c.ProfileDetail(profile=profile, interview=c.InterviewProgress(profile_id=str(uuid4())))


def test_profile_patch_cannot_smuggle_consent():
    patch = c.ProfilePatch(expected_metadata_revision=0, writing_preferences='')
    assert patch.writing_preferences == ''
    with pytest.raises(ValidationError):
        c.ProfilePatch(expected_metadata_revision=0, name='Ada', cloud_consent=True)
    with pytest.raises(ValidationError):
        c.ProfilePatch(expected_metadata_revision=0)
    with pytest.raises(ValidationError):
        c.ProfileCreate(name='Ada', sectors=('tech', 'tech'))


@pytest.mark.parametrize('kind,reference_kind', [('personal', 'employer'), ('employer', 'confirmed_fact')])
def test_supported_claim_rejects_wrong_corpus(kind, reference_kind):
    with pytest.raises(ValidationError):
        c.SupportSpan(target='q', start=0, end=1, excerpt='x', kind=kind,
                      assessment='supported', integrity='valid', citations=(reference(reference_kind),))


def test_inference_explicitly_accepts_either_corpus():
    for citations in ((reference(),), (reference('employer'),), (reference(), reference('employer'))):
        span = c.SupportSpan(target='q', start=0, end=1, excerpt='x', kind='inference',
                             assessment='supported', integrity='valid', citations=citations)
        assert span.kind == 'inference'


@pytest.mark.parametrize('value', [True, 1.0, '1'])
def test_integer_literal_coercion_rejected_python_and_json(value):
    import json

    with pytest.raises(ValidationError):
        c.RevisionVector(schema_version=value)
    with pytest.raises(ValidationError):
        c.RevisionVector.model_validate_json(json.dumps({'schema_version': value}))
    values = dict(id=UNIT, profile_id=OWNER, application_id=APP, job_id=RECORD, fence=1,
                  reserved_tokens=100, reserved_calls=value, provider='test', model_version='v1',
                  consent_revision=0, state='prepared', prepared_at=NOW)
    with pytest.raises(ValidationError):
        c.ProviderAttempt(**values)
    values['prepared_at'] = NOW.isoformat()
    with pytest.raises(ValidationError):
        c.ProviderAttempt.model_validate_json(json.dumps(values))


def test_application_output_edit_review_invalidates_publication_not_index():
    captured = c.RevisionVector(facts=2, documents=3, application_input=1, application_output=5)
    for event in ('edit', 'review', 'history'):
        current = c.RevisionVector(facts=2, documents=3, application_input=1, application_output=6)
        assert not captured.matches(current, ('facts', 'application_input', 'application_output')), event
        assert captured.matches(current, ('facts', 'documents')), event
    typed_edit = c.RevisionVector(metadata=1, facts=2, documents=3,
                                  application_input=1, application_output=5)
    assert not captured.matches(typed_edit, ('metadata', 'application_output'))
    assert captured.matches(typed_edit, ('facts', 'documents'))


def test_packet_rejects_mixed_research_runs():
    with pytest.raises(ValidationError):
        packet(employer=(reference('employer', research_run_id=str(uuid4())),))


def test_application_retains_long_questions_and_legacy_input_without_truncation():
    question = c.Question(id='long', text='x' * 3000, type='writing', constraint_origin='user')
    app = c.ApplicationRecord(profile_id=OWNER, application_id=APP, company='Example', role='Engineer',
                              sector='tech', vacancy_url='https://example.test/role', official_domains=(),
                              questions=(question,), input_revision=0, created_at=NOW,
                              job_description='Original pasted description', location='London',
                              company_url='https://example.test/company')
    assert len(app.questions[0].text) == 3000
    assert app.job_description == 'Original pasted description'
    assert app.output_revision == 0
    with pytest.raises(ValidationError):
        packet(question=question)
    with pytest.raises(ValidationError):
        c.ApplicationRecord(**{**app.model_dump(), 'company_url': 'http://example.test'})


def test_application_feedback_fact_requires_confirmation_retaining_origin():
    values = dict(id=UNIT, profile_id=OWNER, proposal_id=RECORD, text='Personally built a tool',
                  text_sha256=c.text_hash('Personally built a tool'), origin='application_feedback',
                  confirmation_event_id=GENERATION, confirmed_at=NOW)
    fact = c.FactVersion(**values)
    assert fact.origin == 'application_feedback'
    assert fact.confirmation_method == 'explicit_user'
    with pytest.raises(ValidationError):
        c.FactVersion(**{**values, 'confirmation_method': 'application_review'})
    del values['confirmation_event_id']
    with pytest.raises(ValidationError):
        c.FactVersion(**values)


def test_completed_payload_same_host_different_endpoint_requires_manual():
    first = payload()
    second = payload(step_id='two', destination='https://boards.example.test/other',
                     fields=(c.SemanticField(id='name', step_id='two', value='Ada'),))
    with pytest.raises(ValidationError):
        c.CompletedPayload(profile_id=OWNER, application_id=APP, steps=(first, second))


@pytest.mark.parametrize('value', [1, 'true', False])
def test_confirmation_json_actual_boolean_true(value):
    import json

    with pytest.raises(ValidationError):
        c.ConfirmFact.model_validate_json(json.dumps(dict(profile_id=OWNER, proposal_id=UNIT,
                                                         confirmed=value, expected_facts_revision=0)))
    with pytest.raises(ValidationError):
        c.TypedValue.model_validate_json(json.dumps(dict(id=UNIT, profile_id=OWNER, kind='eligibility',
                                                        field='eligible', value=True, purpose='apply',
                                                        explicitly_confirmed=value)))
