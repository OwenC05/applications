"""Workspace domain records join the evidence store's single canonical authority.

No cloud calls or generated facts. All mutations use short SQLite transactions;
S2 owns replacing the existing process-wide API lock and adding job fencing.
"""
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field, StrictBool

from ..contracts import Fact, Profile
from ..store import digest
from ..store import fail as store_fail
from . import contracts as c
from . import interview


def fail(code='NOT_FOUND', message='Workspace record not found', status=None):
    store_fail(code, message, status if status is not None else 400 if code == 'INVALID_INPUT' else 404)


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid.uuid4())


class ConsentInput(c.Contract):
    expected_consent_revision: c.Revision
    provider: Annotated[str, Field(min_length=1, max_length=100)]
    purposes: tuple[Literal['research', 'drafting', 'assessment'], ...]
    granted: StrictBool

    @c.field_validator('purposes')
    @classmethod
    def unique_purposes(cls, value):
        if not value or len(set(value)) != len(value):
            raise ValueError('Choose unique explicit purposes')
        return value


class ProposalInput(c.Contract):
    expected_metadata_revision: c.Revision
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    source_spans: tuple[c.SourceSpan, ...] = ()
    supersedes_fact_id: c.Identifier | None = None


class ReviewInput(c.Contract):
    expected_metadata_revision: c.Revision
    expected_facts_revision: c.Revision
    action: Literal['confirm', 'reject']
    confirmed: StrictBool = False
    text: Annotated[str, Field(min_length=1, max_length=20_000)] | None = None


class InterviewInput(c.Contract):
    expected_metadata_revision: c.Revision
    question_id: Annotated[str, Field(min_length=1, max_length=200)]
    answer: Annotated[str, Field(min_length=1, max_length=20_000)]


class ProgressInput(c.Contract):
    expected_metadata_revision: c.Revision
    skipped_question_ids: tuple[str, ...]
    completed: StrictBool


class TypedInput(c.Contract):
    expected_metadata_revision: c.Revision
    kind: Literal['contact', 'identity', 'eligibility', 'demographic', 'declaration']
    field: Annotated[str, Field(min_length=1, max_length=200)]
    value: Annotated[str, Field(max_length=20_000)] | StrictBool
    purpose: c.Text
    jurisdiction: str | None = None
    application_id: c.Identifier | None = None
    explicitly_confirmed: StrictBool

    @c.field_validator('explicitly_confirmed')
    @classmethod
    def affirmative(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation required')
        return value


class ApplicationInput(c.Contract):
    expected_metadata_revision: c.Revision
    company: Annotated[str, Field(min_length=1, max_length=300)]
    role: Annotated[str, Field(min_length=1, max_length=300)]
    sector: c.Sector
    vacancy_url: str
    vacancy_id: str | None = None
    job_description: Annotated[str, Field(max_length=100_000)] = ''
    location: Annotated[str, Field(max_length=300)] = ''
    company_url: str | None = None
    official_domains: tuple[str, ...] = ()
    questions: tuple[c.Question, ...] = ()

    _vacancy_url = c.field_validator('vacancy_url')(c.https_url)
    _company_url = c.field_validator('company_url')(
        lambda value: c.https_url(value) if value is not None else None)


class ApplicationPatch(c.Contract):
    expected_input_revision: c.Revision
    expected_output_revision: c.Revision
    company: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    role: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    sector: c.Sector | None = None
    vacancy_url: str | None = None
    vacancy_id: str | None = None
    job_description: Annotated[str, Field(max_length=100_000)] | None = None
    location: Annotated[str, Field(max_length=300)] | None = None
    company_url: str | None = None
    official_domains: tuple[str, ...] | None = None
    questions: tuple[c.Question, ...] | None = None


class ProposalDelete(c.Contract):
    expected_metadata_revision: c.Revision
    expected_facts_revision: c.Revision


class ExpectedMetadata(c.Contract):
    expected_metadata_revision: c.Revision


class FeedbackInput(c.Contract):
    expected_input_revision: c.Revision
    expected_output_revision: c.Revision
    event_id: c.Identifier
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    kind: Literal['feedback', 'user_reported_submitted'] = 'feedback'


class DomainRepository:
    def __init__(self, store):
        self.store = store

    def _ensure(self, db, owner):
        profile = self.store._profile(db, owner)
        db.execute('INSERT OR IGNORE INTO profile_revisions(owner) VALUES(?)', (owner,))
        return profile

    def revisions(self, db, owner):
        self._ensure(db, owner)
        row = db.execute('SELECT metadata,facts,documents,consent FROM profile_revisions WHERE owner=?',
                         (owner,)).fetchone()
        return c.RevisionVector(metadata=row[0], facts=row[1], documents=row[2], consent=row[3])

    def _check(self, db, owner, kind, expected):
        if getattr(self.revisions(db, owner), kind) != expected:
            fail('CONFLICT', 'Workspace revision changed; reload before editing', 409)

    def _bump(self, db, owner, kind='metadata'):
        if kind not in ('metadata', 'consent'):
            raise ValueError('Domain revision must be metadata or consent')
        db.execute(f'UPDATE profile_revisions SET {kind}={kind}+1 WHERE owner=?', (owner,))

    def _save(self, db, owner, kind, record, key=None):
        key = key or getattr(record, 'id', None) or getattr(record, 'application_id', None)
        serialized = record.model_dump_json() if isinstance(record, c.Contract) else json.dumps(record)
        existing = db.execute('SELECT owner,kind FROM records WHERE id=?', (key,)).fetchone()
        if existing and existing != (owner, 'workspace:' + kind):
            fail('CONFLICT', 'Record identifier already exists', 409)
        db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (key, owner, 'workspace:' + kind, serialized))

    def _get(self, db, owner, key, kind, model=None):
        value = self.store._record(db, owner, key, 'workspace:' + kind)
        return model.model_validate_json(json.dumps(value)) if model else value

    def _all(self, db, owner, kind, model=None):
        self._ensure(db, owner)
        rows = db.execute('SELECT data FROM records WHERE owner=? AND kind=? ORDER BY rowid',
                          (owner, 'workspace:' + kind))
        return tuple(model.model_validate_json(row[0]) if model else json.loads(row[0]) for row in rows)

    def _singleton(self, db, owner, kind, model):
        rows = self._all(db, owner, kind, model)
        return rows[0] if rows else None

    def _profile(self, db, owner):
        p = self._ensure(db, owner)
        prefs = self._all(db, owner, 'preferences')
        return c.ProfileRecord(profile_id=owner, name=p.name, sectors=tuple(p.sectors),
                               writing_preferences=prefs[0]['writing_preferences'] if prefs else '',
                               revisions=self.revisions(db, owner))

    def _detail(self, db, owner):
        answers = self._all(db, owner, 'interview_answer', c.InterviewAnswer)
        progress = self._singleton(db, owner, 'interview_progress', c.InterviewProgress)
        progress = progress or c.InterviewProgress(profile_id=owner)
        return c.ProfileDetail(profile=self._profile(db, owner),
                               consent=self._singleton(db, owner, 'consent', c.ConsentRecord),
                               interview=progress, interview_answers=answers,
                               proposals=self._all(db, owner, 'proposal', c.Proposal),
                               applications=self._all(db, owner, 'application', c.ApplicationRecord),
                               typed_values=self._all(db, owner, 'typed_value', c.TypedValue))

    def detail(self, owner):
        with self.store._tx() as db:
            return self._detail(db, owner)

    def profiles(self):
        with self.store._tx() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM profiles ORDER BY id')]
            return c.ProfileList(profiles=tuple(self._profile(db, owner) for owner in ids))

    def create_profile(self, body):
        # Single transaction: evidence/workspace callers observe the same ID immediately.
        with self.store._tx() as db:
            owner = uid()
            p = Profile(owner, body.name.strip(), list(body.sectors), 0)
            if not p.name:
                fail('INVALID_INPUT', 'Name must not be blank')
            db.execute('INSERT INTO profiles VALUES(?,?)', (owner, json.dumps(asdict(p))))
            return self._detail(db, owner)

    def patch_profile(self, owner, body):
        with self.store._tx() as db:
            p = self._ensure(db, owner)
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            name = body.name.strip() if body.name is not None else p.name
            if not name:
                fail('INVALID_INPUT', 'Name must not be blank')
            updated = Profile(owner, name, list(body.sectors) if body.sectors else p.sectors, p.revision)
            db.execute('UPDATE profiles SET data=? WHERE id=?', (json.dumps(asdict(updated)), owner))
            if body.writing_preferences is not None:
                old = self._all(db, owner, 'preferences')
                key = old[0]['id'] if old else uid()
                self._save(db, owner, 'preferences', {'id': key, 'writing_preferences': body.writing_preferences}, key)
            self._bump(db, owner)
            return self._detail(db, owner)

    def consent(self, owner, body):
        with self.store._tx() as db:
            self._check(db, owner, 'consent', body.expected_consent_revision)
            old = self._singleton(db, owner, 'consent', c.ConsentRecord)
            record = c.ConsentRecord(profile_id=owner, id=old.id if old else uid(),
                                     provider=body.provider, purposes=body.purposes,
                                     granted=body.granted, revision=body.expected_consent_revision + 1,
                                     disclosed_at=now())
            self._save(db, owner, 'consent', record)
            self._bump(db, owner, 'consent')
            return record

    def _validate_spans(self, db, owner, spans):
        for span in spans:
            if span.unit_id is None:
                fail('INVALID_INPUT', 'Document-derived proposal requires canonical document units')
            unit = self.store._record(db, owner, span.unit_id, 'unit')
            if unit['source_id'] != span.record_id:
                fail('INVALID_INPUT', 'Source span record mismatch')
            try:
                span.validate_text(unit['text'])
            except ValueError:
                fail('INVALID_INPUT', 'Source span integrity mismatch')

    def _proposal(self, db, owner, text, origin, origin_id=None, spans=(), supersedes=None):
        if not text.strip():
            fail('INVALID_INPUT', 'Proposal text must not be blank')
        self._validate_spans(db, owner, spans)
        if supersedes:
            old = self.store._record(db, owner, supersedes, 'fact')
            if old['state'] != 'active':
                fail('CONFLICT', 'Superseded fact is not active', 409)
        record = c.Proposal(profile_id=owner, id=uid(), text=text, origin=origin,
                            origin_id=origin_id, source_spans=spans,
                            supersedes_fact_id=supersedes, created_at=now())
        self._save(db, owner, 'proposal', record)
        return record

    def propose(self, owner, body):
        with self.store._tx() as db:
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            proposal = self._proposal(db, owner, body.text,
                                      'document_derived' if body.source_spans else 'manual',
                                      spans=body.source_spans, supersedes=body.supersedes_fact_id)
            self._bump(db, owner)
            return proposal

    def _confirm(self, db, owner, proposal, text=None, fact_id=None, timestamp=None):
        self._validate_spans(db, owner, proposal.source_spans)
        text = text if text is not None else proposal.text
        if not text.strip() or len(text) > 20_000:
            fail('INVALID_INPUT', 'Confirmed text must contain 1–20000 characters')
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if proposal.supersedes_fact_id:
            old = self.store._record(db, owner, proposal.supersedes_fact_id, 'fact')
            if old['state'] != 'active':
                fail('CONFLICT', 'Superseded fact is not active', 409)
            old['state'] = 'superseded'
            db.execute('UPDATE records SET data=? WHERE id=?',
                       (json.dumps(old), proposal.supersedes_fact_id))
        origin = proposal.source_spans[0].evidence_span() if proposal.source_spans else None
        provenance = 'manual' if proposal.origin == 'application_feedback' else proposal.origin
        timestamp = timestamp or now()
        fact = Fact(fact_id or uid(), owner, text, digest(text), timestamp.isoformat(), provenance, origin)
        self.store._put(db, owner, 'fact', fact)
        # No second authoritative copy of text: metadata joins canonical fact on read.
        metadata = dict(proposal_id=proposal.id, source_spans=[s.model_dump(mode='json') for s in proposal.source_spans],
                        supersedes_fact_id=proposal.supersedes_fact_id, confirmation_event_id=uid())
        db.execute('INSERT INTO fact_metadata VALUES(?,?,?)', (fact.id, owner, json.dumps(metadata)))
        self._save(db, owner, 'proposal', proposal.model_copy(update={'status': 'confirmed'}))
        self.store._bump(db, owner, 'facts')
        return fact

    def review(self, owner, proposal_id, body):
        with self.store._tx() as db:
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            self._check(db, owner, 'facts', body.expected_facts_revision)
            proposal = self._get(db, owner, proposal_id, 'proposal', c.Proposal)
            if proposal.status != 'pending':
                fail('CONFLICT', 'Only pending proposals can be reviewed', 409)
            if body.action == 'confirm':
                if body.confirmed is not True:
                    fail('INVALID_INPUT', 'Explicit confirmation required')
                self._confirm(db, owner, proposal, body.text)
            else:
                if body.text is not None or body.confirmed:
                    fail('INVALID_INPUT', 'Rejection cannot include confirmed text')
                self._save(db, owner, 'proposal', proposal.model_copy(update={'status': 'rejected'}))
            self._bump(db, owner)
            return self._detail(db, owner)

    def delete_proposal(self, owner, proposal_id, body):
        with self.store._tx() as db:
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            self._check(db, owner, 'facts', body.expected_facts_revision)
            proposal = self._get(db, owner, proposal_id, 'proposal', c.Proposal)
            related = [row[0] for row in db.execute('SELECT id,data FROM fact_metadata WHERE owner=?', (owner,))
                       if json.loads(row[1])['proposal_id'] == proposal_id]
            for fact_id in related:
                self.store._delete(owner, fact_id=fact_id, transaction=db)
            if not related:
                self.store.forget_proposal_origin(db, owner, proposal.model_dump(mode='json'))
            db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (proposal_id, owner, 'workspace:proposal'))
            db.execute('DELETE FROM records WHERE id=? AND owner=?', (proposal_id, owner))
            if self.revisions(db, owner).metadata == body.expected_metadata_revision:
                self._bump(db, owner)
            return c.DeleteResult(deleted=True, cleanup_pending=bool(related))

    def facts(self, owner):
        with self.store._tx() as db:
            self._ensure(db, owner)
            values = []
            for row in db.execute("SELECT id,data FROM records WHERE owner=? AND kind='fact' ORDER BY rowid", (owner,)):
                fact = json.loads(row[1])
                if fact['state'] != 'active':
                    continue
                meta = db.execute('SELECT data FROM fact_metadata WHERE id=? AND owner=?', (row[0], owner)).fetchone()
                values.append({'fact': fact, 'confirmation': json.loads(meta[0]) if meta else None})
            return {'schema_version': 1, 'facts': values}

    def answer(self, owner, body):
        with self.store._tx() as db:
            profile = self._ensure(db, owner)
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            q = interview.question(body.question_id, profile.sectors)
            if q is None or not body.answer.strip():
                fail('INVALID_INPUT', 'Choose an applicable interview question and nonblank answer')
            old = next((a for a in self._all(db, owner, 'interview_answer', c.InterviewAnswer)
                        if a.question_id == q['id']), None)
            # Replacing an answer preserves the prior raw answer in an immutable archive.
            if old:
                archive = {'id': uid(), 'answer': old.model_dump(mode='json')}
                self._save(db, owner, 'interview_archive', archive, archive['id'])
                db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (old.id, owner, 'workspace:interview_answer'))
                db.execute('DELETE FROM records WHERE id=? AND owner=?', (old.id, owner))
            answer = c.InterviewAnswer(profile_id=owner, id=uid(), question_id=q['id'],
                                       question=q['prompt'], section=q['section'], answer=body.answer,
                                       created_at=now())
            self._save(db, owner, 'interview_answer', answer)
            prior_answer_ids = {old.id} if old else set()
            prior_answer_ids.update(value['answer']['id'] for value in self._all(db, owner, 'interview_archive')
                                    if value['answer']['question_id'] == q['id'])
            supersedes = None
            legacy_prior_ids = {relation['proposal_id'] for relation in self._all(db, owner, 'legacy_relation')
                                if relation['legacy_source'].get('kind') == 'interview'
                                and relation['legacy_source'].get('question_id') == q['id']
                                and relation['legacy_source'].get('id') in prior_answer_ids}
            for prior in self._all(db, owner, 'proposal', c.Proposal):
                same_question = (prior.origin == 'interview' and prior.origin_id in prior_answer_ids
                                 or prior.origin == 'legacy' and prior.id in legacy_prior_ids)
                if same_question and prior.status == 'confirmed':
                    for fact_id, fact_text in db.execute("SELECT id,data FROM records WHERE owner=? AND kind='fact'", (owner,)):
                        metadata = db.execute('SELECT data FROM fact_metadata WHERE id=? AND owner=?', (fact_id, owner)).fetchone()
                        if metadata and json.loads(metadata[0])['proposal_id'] == prior.id and json.loads(fact_text)['state'] == 'active':
                            supersedes = fact_id
            self._proposal(db, owner, body.answer, 'interview', origin_id=answer.id, supersedes=supersedes)
            progress = self._singleton(db, owner, 'interview_progress', c.InterviewProgress)
            progress_id = db.execute("SELECT id FROM records WHERE owner=? AND kind='workspace:interview_progress'", (owner,)).fetchall()
            self._save(db, owner, 'interview_progress', c.InterviewProgress(
                profile_id=owner,
                answer_ids=tuple(a.id for a in self._all(db, owner, 'interview_answer', c.InterviewAnswer)),
                skipped_question_ids=tuple(i for i in (progress.skipped_question_ids if progress else ()) if i != q['id']),
                completed=progress.completed if progress else False),
                key=progress_id[0][0] if progress_id else uid())
            self._bump(db, owner)
            return self._detail(db, owner)

    def progress(self, owner, body):
        with self.store._tx() as db:
            profile = self._ensure(db, owner)
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            allowed = {q['id'] for q in interview.questions(profile.sectors)}
            answers = self._all(db, owner, 'interview_answer', c.InterviewAnswer)
            answered = {a.question_id for a in answers}
            if len(set(body.skipped_question_ids)) != len(body.skipped_question_ids) or not set(body.skipped_question_ids) <= allowed or set(body.skipped_question_ids) & answered:
                fail('INVALID_INPUT', 'Skipped questions must be applicable, unique and unanswered')
            record = c.InterviewProgress(profile_id=owner, answer_ids=tuple(a.id for a in answers),
                                         skipped_question_ids=body.skipped_question_ids, completed=body.completed)
            rows = db.execute("SELECT id FROM records WHERE owner=? AND kind='workspace:interview_progress'", (owner,)).fetchall()
            self._save(db, owner, 'interview_progress', record, key=rows[0][0] if rows else uid())
            self._bump(db, owner)
            return record

    def typed(self, owner, body):
        with self.store._tx() as db:
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            if body.application_id:
                self._get(db, owner, body.application_id, 'application', c.ApplicationRecord)
            record = c.TypedValue(profile_id=owner, id=uid(), **body.model_dump(exclude={'schema_version', 'expected_metadata_revision'}))
            old = [v for v in self._all(db, owner, 'typed_value', c.TypedValue)
                   if v.field == record.field and v.application_id == record.application_id]
            for item in old:
                db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (item.id, owner, 'workspace:typed_value'))
                db.execute('DELETE FROM records WHERE owner=? AND id=?', (owner, item.id))
            self._save(db, owner, 'typed_value', record)
            self._bump(db, owner)
            return record

    def delete_typed(self, owner, record_id, body):
        with self.store._tx() as db:
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            self._get(db, owner, record_id, 'typed_value', c.TypedValue)
            db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (record_id, owner, 'workspace:typed_value'))
            db.execute('DELETE FROM records WHERE owner=? AND id=?', (owner, record_id))
            self._bump(db, owner)
            return c.DeleteResult(deleted=True)

    def create_application(self, owner, body):
        with self.store._tx() as db:
            self._check(db, owner, 'metadata', body.expected_metadata_revision)
            record = c.ApplicationRecord(profile_id=owner, application_id=uid(), input_revision=0,
                                         created_at=now(), **body.model_dump(exclude={'schema_version', 'expected_metadata_revision'}))
            self._save(db, owner, 'application', record)
            self._bump(db, owner)
            return record

    def application(self, owner, application_id):
        with self.store._tx() as db:
            return self._get(db, owner, application_id, 'application', c.ApplicationRecord)

    def patch_application(self, owner, application_id, body):
        with self.store._tx() as db:
            record = self._get(db, owner, application_id, 'application', c.ApplicationRecord)
            if (record.input_revision, record.output_revision) != (body.expected_input_revision, body.expected_output_revision):
                fail('CONFLICT', 'Application changed; reload before editing', 409)
            updates = body.model_dump(mode='json', exclude_unset=True,
                                      exclude={'schema_version', 'expected_input_revision', 'expected_output_revision'})
            if not updates:
                fail('INVALID_INPUT', 'Application patch requires a change')
            values = record.model_dump(mode='json') | updates
            values['input_revision'] += 1
            values['output_revision'] += 1
            updated = c.ApplicationRecord.model_validate_json(json.dumps(values))
            self._save(db, owner, 'application', updated)
            return updated

    def delete_application(self, owner, application_id, body):
        with self.store._tx() as db:
            app = self._get(db, owner, application_id, 'application', c.ApplicationRecord)
            metadata_before = self.revisions(db, owner).metadata
            if (app.input_revision, app.output_revision) != (body.expected_input_revision, body.expected_output_revision):
                fail('CONFLICT', 'Application changed', 409)
            self.store.forget_jobs(db, owner, application_id=application_id)
            history_ids = {event['id'] for event in self._all(db, owner, 'history')
                           if event['application_id'] == application_id}
            proposal_ids = {proposal.id for proposal in self._all(db, owner, 'proposal', c.Proposal)
                            if proposal.origin_id in history_ids}
            for relation in self._all(db, owner, 'legacy_relation'):
                if relation['legacy_source'].get('kind') == 'application' and relation['legacy_source'].get('id') == application_id:
                    proposal_ids.add(relation['proposal_id'])
            related_facts = [row[0] for row in db.execute('SELECT id,data FROM fact_metadata WHERE owner=?', (owner,))
                             if json.loads(row[1])['proposal_id'] in proposal_ids]
            for fact_id in related_facts:
                self.store._delete(owner, fact_id=fact_id, transaction=db)
            for record_id, kind, serialized in db.execute('SELECT id,kind,data FROM records WHERE owner=?', (owner,)).fetchall():
                value = json.loads(serialized)
                if (record_id == application_id or record_id in history_ids
                        or kind.startswith('workspace:') and value.get('application_id') == application_id
                        or kind == 'workspace:proposal' and record_id in proposal_ids
                        or kind == 'workspace:legacy_relation' and value.get('proposal_id') in proposal_ids
                        or kind == 'workspace:legacy_archive'):
                    db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?)', (record_id, owner, kind))
                    db.execute('DELETE FROM records WHERE id=? AND owner=?', (record_id, owner))
            if self.revisions(db, owner).metadata == metadata_before:
                self._bump(db, owner)
            return c.DeleteResult(deleted=True, cleanup_pending=bool(related_facts))

    def feedback(self, owner, application_id, body):
        with self.store._tx() as db:
            app = self._get(db, owner, application_id, 'application', c.ApplicationRecord)
            old = db.execute('SELECT owner,kind,data FROM records WHERE id=?', (body.event_id,)).fetchone()
            if old:
                if old[:2] == (owner, 'workspace:history'):
                    existing = json.loads(old[2])
                    if (existing['application_id'], existing['kind'], existing['text']) == (application_id, body.kind, body.text):
                        return existing
                fail('CONFLICT', 'Event ID already used', 409)
            if (app.input_revision, app.output_revision) != (body.expected_input_revision, body.expected_output_revision):
                fail('CONFLICT', 'Application changed', 409)
            event = {'id': body.event_id, 'profile_id': owner, 'application_id': application_id,
                     'kind': body.kind, 'text': body.text, 'recorded_at': now().isoformat(),
                     'input_revision': app.input_revision, 'output_revision': app.output_revision,
                     'application_snapshot': app.model_dump(mode='json')}
            self._save(db, owner, 'history', event, body.event_id)
            self._proposal(db, owner, body.text, 'application_feedback', origin_id=body.event_id)
            self._save(db, owner, 'application', app.model_copy(update={'output_revision': app.output_revision + 1}))
            self._bump(db, owner)
            return event

    def history(self, owner, application_id):
        with self.store._tx() as db:
            self._get(db, owner, application_id, 'application', c.ApplicationRecord)
            return {'schema_version': 1, 'history': [e for e in self._all(db, owner, 'history') if e['application_id'] == application_id]}

    def export(self, owner):
        with self.store._tx() as db:
            detail = self._detail(db, owner).model_dump(mode='json')
            detail['evidence_records'] = [json.loads(r[0]) for r in db.execute(
                "SELECT data FROM records WHERE owner=? AND kind IN ('fact','source','unit')", (owner,))]
            detail['archives'] = [json.loads(r[0]) for r in db.execute(
                "SELECT data FROM records WHERE owner=? AND kind IN ('workspace:history','workspace:legacy_archive','workspace:interview_archive')", (owner,))]
            return detail
