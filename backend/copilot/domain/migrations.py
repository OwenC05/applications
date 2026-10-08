"""Explicit selected-byte migration. Never discovers or reads legacy data paths.

Legacy research/model prose is retained as an untrusted archive, not promoted to
current sources, supported drafts, approvals or new consent. Whole-upload schema
validation and dry-run precede one atomic SQLite import transaction.
"""
import base64
import hashlib
import json
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from ..contracts import Profile
from ..store import fail
from . import contracts as c
from .repository import DomainRepository, uid


class Legacy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @field_validator('*')
    @classmethod
    def utc_times(cls, value):
        if isinstance(value, datetime) and value.utcoffset() != timedelta(0):
            raise ValueError('Legacy times must be UTC aware')
        return value


class LegacySource(Legacy):
    kind: Annotated[str, Field(min_length=1, max_length=100)]
    id: str | None = None
    label: str | None = None


class LegacyMemory(Legacy):
    id: c.Identifier
    category: Annotated[str, Field(min_length=1, max_length=200)]
    label: Annotated[str, Field(min_length=1, max_length=1000)]
    content: Annotated[str, Field(min_length=1, max_length=20_000)]
    status: Literal['pending', 'verified', 'superseded', 'rejected']
    source: LegacySource
    supersedes: c.Identifier | None
    createdAt: datetime
    confirmedAt: datetime | None


class LegacyAnswer(Legacy):
    questionId: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(max_length=100_000)]
    evidenceIds: list[str]
    researchIds: list[str]


class LegacyQuality(Legacy):
    version: Literal[1]
    status: Literal['assessed', 'needs_work', 'stale']
    checkedAt: datetime
    rewriteCount: Literal[0, 1]
    summary: Annotated[str, Field(min_length=1, max_length=10000)]
    questionPlans: list[dict]
    issues: list[dict]


class LegacyDraft(Legacy):
    id: c.Identifier
    mode: Literal['offline', 'cloud']
    coverLetter: Annotated[str, Field(max_length=100_000)]
    answers: list[LegacyAnswer]
    evidenceIds: list[str]
    researchIds: list[str]
    missingFacts: list[str]
    warnings: list[str]
    brainRevision: c.Revision
    inputRevision: c.Revision
    researchId: c.Identifier | None
    createdAt: datetime
    stale: StrictBool
    humanEdited: StrictBool
    quality: LegacyQuality | None = None


class LegacyHistory(Legacy):
    eventId: Annotated[str, Field(min_length=1, max_length=200)]
    status: Literal['reviewed', 'submitted']
    timestamp: datetime
    draftId: c.Identifier
    version: c.Identifier
    coverLetter: Annotated[str, Field(max_length=100_000)]
    answers: list[LegacyAnswer]
    evidenceIds: list[str]
    quality: LegacyQuality | None = None


class LegacyQuestion(Legacy):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=10000)]
    maxWords: Annotated[int, Field(gt=0, le=100000)] | None
    maxChars: Annotated[int, Field(gt=0, le=100000)] | None


class LegacyClaim(Legacy):
    id: Annotated[str, Field(min_length=1, max_length=200)]
    text: c.Text
    sourceUrls: list[str]
    supportExcerpt: c.Text


class LegacyPriority(LegacyClaim):
    inference: StrictBool
    supportingClaimIds: list[str]


class LegacyResearchSource(Legacy):
    url: str
    title: str


class LegacyResearch(Legacy):
    id: c.Identifier
    inputRevision: c.Revision
    mode: Literal['offline', 'cloud']
    status: Literal['not_researched', 'researched', 'incomplete']
    summary: str
    companyFacts: list[LegacyClaim]
    requirements: list[LegacyClaim]
    hiringPriorities: list[LegacyPriority]
    sources: list[LegacyResearchSource]
    unknowns: list[str]
    warnings: list[str]
    retrievedAt: datetime | None


class LegacyApplication(Legacy):
    id: c.Identifier
    company: Annotated[str, Field(min_length=1, max_length=300)]
    role: Annotated[str, Field(min_length=1, max_length=300)]
    sector: c.Sector
    location: Annotated[str, Field(max_length=300)]
    url: str
    companyUrl: str
    jobDescription: Annotated[str, Field(max_length=100_000)]
    questions: Annotated[list[LegacyQuestion], Field(max_length=100)]
    status: Literal['draft', 'reviewed', 'submitted']
    inputRevision: c.Revision
    createdAt: datetime
    updatedAt: datetime
    research: LegacyResearch | None
    draft: LegacyDraft | None
    history: list[LegacyHistory]


class LegacyInterviewAnswer(Legacy):
    id: c.Identifier
    questionId: Annotated[str, Field(min_length=1, max_length=200)]
    question: c.Text
    section: Annotated[str, Field(min_length=1, max_length=200)]
    answer: Annotated[str, Field(min_length=1, max_length=20_000)]
    createdAt: datetime
    updatedAt: datetime | None = None


class LegacyInterview(Legacy):
    answers: list[LegacyInterviewAnswer]
    skippedQuestionIds: list[str]
    completed: StrictBool


class LegacyProfile(Legacy):
    id: c.Identifier
    name: Annotated[str, Field(min_length=1, max_length=200)]
    sectors: Annotated[list[c.Sector], Field(min_length=1, max_length=2)]
    isDemo: StrictBool
    cloudConsent: StrictBool
    writingPreferences: Annotated[str, Field(max_length=10000)]
    revision: c.Revision
    brainRevision: c.Revision
    createdAt: datetime
    updatedAt: datetime
    memories: list[LegacyMemory]
    interview: LegacyInterview
    applications: list[LegacyApplication]


class LegacyState(Legacy):
    version: Literal[1]
    profiles: list[LegacyProfile]


def _unique(items):
    if len(items) != len(set(items)):
        raise ValueError('Duplicate legacy IDs/references')


def _json_object(pairs):
    keys = [key for key, _ in pairs]
    _unique(keys)
    return dict(pairs)


def _validate_quality(quality, questions):
    if quality is None:
        return
    seen = []
    for plan in quality.questionPlans:
        if set(plan) != {'questionId', 'kind', 'approach', 'evidenceIds', 'researchIds', 'missingFacts'}:
            raise ValueError('Invalid quality plan')
        if (plan['questionId'] not in questions or plan['kind'] not in ('factual', 'motivation', 'behavioral', 'technical', 'commercial', 'other')
                or not isinstance(plan['approach'], str) or not plan['approach'].strip()):
            raise ValueError('Invalid quality plan fields')
        seen.append(plan['questionId'])
        for key in ('evidenceIds', 'researchIds', 'missingFacts'):
            if not isinstance(plan[key], list) or any(not isinstance(item, str) for item in plan[key]):
                raise ValueError('Invalid quality references')
            _unique(plan[key])
    _unique(seen)
    if set(seen) != set(questions):
        raise ValueError('Quality question coverage mismatch')
    for issue in quality.issues:
        if set(issue) != {'target', 'category', 'severity', 'detail', 'evidenceIds', 'researchIds'}:
            raise ValueError('Invalid quality issue')
        if (issue['target'] not in (*questions, 'coverLetter') or issue['category'] not in ('truthfulness', 'specificity', 'role_fit', 'question_coverage', 'clarity', 'format', 'voice')
                or issue['severity'] not in ('must_fix', 'improve') or not isinstance(issue['detail'], str) or not issue['detail'].strip()):
            raise ValueError('Invalid quality issue fields')
        for key in ('evidenceIds', 'researchIds'):
            if not isinstance(issue[key], list) or any(not isinstance(item, str) for item in issue[key]):
                raise ValueError('Invalid quality issue references')
            _unique(issue[key])
    if quality.status == 'assessed' and (quality.issues or any(p['missingFacts'] for p in quality.questionPlans)):
        raise ValueError('Assessed legacy quality has unresolved issues')


def decode(original):
    if not isinstance(original, bytes) or not original or len(original) > 1024 * 1024:
        fail('LIMIT_EXCEEDED', 'Legacy upload must be at most 1 MiB', 413)
    try:
        raw = json.loads(original, object_pairs_hook=_json_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(raw, dict):
            raise ValueError()
        encoded = json.dumps(raw, allow_nan=False)
        profiles = (LegacyState.model_validate_json(encoded).profiles if 'profiles' in raw
                    else [LegacyProfile.model_validate_json(encoded)])
        if len(profiles) != 1:
            raise ValueError('Upload a single profile export; cross-owner original backups are forbidden')
        ids = []
        for p in profiles:
            ids.append(p.id)
            _unique(p.sectors)
            memories = {m.id: m for m in p.memories}
            for m in p.memories:
                ids.append(m.id)
                if (not m.content.strip() or m.supersedes and m.supersedes not in memories
                        or m.status in ('verified', 'superseded') and m.confirmedAt is None):
                    raise ValueError('Invalid legacy memory confirmation/ownership')
                if m.source.kind == 'interview' and m.source.id and m.source.id not in {a.id for a in p.interview.answers}:
                    raise ValueError('Foreign legacy interview source')
                if m.source.kind == 'application' and m.source.id and m.source.id not in {a.id for a in p.applications}:
                    raise ValueError('Foreign legacy application source')
            for a in p.interview.answers:
                ids.append(a.id)
            _unique(p.interview.skippedQuestionIds)
            for a in p.applications:
                ids.append(a.id)
                _unique([q.id for q in a.questions])
                _unique([h.eventId for h in a.history])
                # Old optional URLs may be empty: archive instead of inventing a URL.
                if a.url:
                    c.https_url(a.url)
                if a.companyUrl:
                    c.https_url(a.companyUrl)
                if a.research is not None:
                    ids.append(a.research.id)
                    claim_ids = [claim.id for claim in (*a.research.companyFacts, *a.research.requirements, *a.research.hiringPriorities)]
                    _unique(claim_ids)
                    source_urls = {source.url for source in a.research.sources}
                    for source in a.research.sources:
                        c.https_url(source.url)
                    for claim in (*a.research.companyFacts, *a.research.requirements, *a.research.hiringPriorities):
                        _unique(claim.sourceUrls)
                        if not claim.sourceUrls or not set(claim.sourceUrls) <= source_urls:
                            raise ValueError('Invalid archived source references')
                    factual_ids = {claim.id for claim in (*a.research.companyFacts, *a.research.requirements)}
                    for priority in a.research.hiringPriorities:
                        if priority.inference is not True or not priority.supportingClaimIds or not set(priority.supportingClaimIds) <= factual_ids:
                            raise ValueError('Invalid archived inference references')
                if a.draft:
                    ids.append(a.draft.id)
                    _unique([answer.questionId for answer in a.draft.answers])
                    _validate_quality(a.draft.quality, [answer.questionId for answer in a.draft.answers])
                    for values in (a.draft.evidenceIds, a.draft.researchIds, a.draft.missingFacts, a.draft.warnings):
                        _unique(values)
                for h in a.history:
                    _unique([answer.questionId for answer in h.answers])
                    _validate_quality(h.quality, [answer.questionId for answer in h.answers])
                for answer in ((a.draft.answers if a.draft else []) + [answer for h in a.history for answer in h.answers]):
                    _unique(answer.evidenceIds)
                    _unique(answer.researchIds)
        _unique(ids)
        return profiles
    except (ValueError, TypeError, KeyError, RecursionError, ValidationError):
        fail('INVALID_INPUT', 'The entire selected legacy export failed validation; nothing imported', 422)


class LegacyImporter:
    def __init__(self, repository: DomainRepository):
        self.repository = repository
        self.store = repository.store

    def _collision(self, db, profiles, sha):
        if db.execute("SELECT 1 FROM records WHERE kind='workspace:legacy_archive' AND json_extract(data,'$.source_sha256')=?", (sha,)).fetchone():
            fail('CONFLICT', 'This exact upload was already imported', 409)
        ids = []
        for p in profiles:
            ids.extend([p.id, *(m.id for m in p.memories), *(a.id for a in p.interview.answers), *(a.id for a in p.applications)])
        for record_id in ids:
            if (db.execute('SELECT 1 FROM profiles WHERE id=?', (record_id,)).fetchone()
                    or db.execute('SELECT 1 FROM records WHERE id=?', (record_id,)).fetchone()
                    or db.execute('SELECT 1 FROM tombstones WHERE id=?', (record_id,)).fetchone()):
                fail('CONFLICT', 'Legacy ID collides with existing or deleted canonical data', 409)

    def run(self, original, commit=False, expected_sha256=None):
        profiles = decode(original)
        sha = hashlib.sha256(original).hexdigest()
        if commit and expected_sha256 != sha:
            fail('CONFLICT', 'Upload differs from the explicitly approved dry-run hash', 409)
        with self.store._tx() as db:
            self._collision(db, profiles, sha)
            summary = {'schema_version': 1, 'source_sha256': sha, 'state': 'dry_run',
                       'profile_ids': [p.id for p in profiles],
                       'profiles': len(profiles), 'memories': sum(len(p.memories) for p in profiles),
                       'applications': sum(len(p.applications) for p in profiles),
                       'archived_only_applications': sum(sum(not a.url for a in p.applications) for p in profiles),
                       'active_cloud_consent': False, 'legacy_drafts_current': False}
            if not commit:
                return summary
            repo = self.repository
            for p in profiles:
                canonical = Profile(p.id, p.name, p.sectors, 0)
                db.execute('INSERT INTO profiles VALUES(?,?)', (p.id, json.dumps(canonical.__dict__)))
                repo._ensure(db, p.id)
                prefs_id = uid()
                repo._save(db, p.id, 'preferences', {'id': prefs_id, 'writing_preferences': p.writingPreferences}, prefs_id)
                archive_id = uid()
                repo._save(db, p.id, 'legacy_archive', {
                    'id': archive_id, 'profile_id': p.id, 'source_sha256': sha,
                    'original_bytes_base64': base64.b64encode(original).decode('ascii'),
                    'legacy_profile': p.model_dump(mode='json'), 'current_approval': False,
                    'legacy_cloud_consent_not_carried_forward': p.cloudConsent}, archive_id)
                for m in p.memories:
                    # Rejected prose remains in the immutable original archive only.
                    if m.status == 'rejected':
                        continue
                    proposal = c.Proposal(profile_id=p.id, id=uid(), text=m.content, origin='legacy',
                                          origin_id=m.id, created_at=m.createdAt,
                                          supersedes_fact_id=m.supersedes if m.status == 'pending' else None)
                    repo._save(db, p.id, 'proposal', proposal)
                    legacy_source = m.source.model_dump(mode='json')
                    if legacy_source['kind'] == 'interview' and legacy_source.get('id'):
                        linked = next((a for a in p.interview.answers if a.id == legacy_source['id']), None)
                        if linked is None:
                            fail('INVALID_INPUT', 'Legacy interview source is not owned by selected profile', 422)
                        legacy_source['question_id'] = linked.questionId
                    relation_id = uid()
                    repo._save(db, p.id, 'legacy_relation', {'id': relation_id, 'proposal_id': proposal.id,
                               'legacy_source': legacy_source}, relation_id)
                    if m.status in ('verified', 'superseded'):
                        repo._confirm(db, p.id, proposal, fact_id=m.id, timestamp=m.confirmedAt)
                        fact_metadata = json.loads(db.execute('SELECT data FROM fact_metadata WHERE id=?', (m.id,)).fetchone()[0])
                        fact_metadata['legacy_source'] = legacy_source
                        db.execute('UPDATE fact_metadata SET data=? WHERE id=?', (json.dumps(fact_metadata), m.id))
                        if m.status == 'superseded':
                            canonical_fact = self.store._record(db, p.id, m.id, 'fact')
                            canonical_fact['state'] = 'superseded'
                            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(canonical_fact), m.id))
                for m in p.memories:
                    if m.status in ('verified', 'superseded') and m.supersedes:
                        metadata = json.loads(db.execute('SELECT data FROM fact_metadata WHERE id=?', (m.id,)).fetchone()[0])
                        metadata['supersedes_fact_id'] = m.supersedes
                        db.execute('UPDATE fact_metadata SET data=? WHERE id=?', (json.dumps(metadata), m.id))
                # Legacy reanswers were append-only. Expose latest per question;
                # every earlier raw answer remains unchanged in the original archive.
                latest_answers = {a.questionId: a for a in p.interview.answers}
                for a in p.interview.answers:
                    if a.id != latest_answers[a.questionId].id:
                        archived = c.InterviewAnswer(profile_id=p.id, id=a.id, question_id=a.questionId,
                                                     question=a.question, section=a.section,
                                                     answer=a.answer, created_at=a.createdAt)
                        archive_id = uid()
                        repo._save(db, p.id, 'interview_archive', {'id': archive_id,
                                   'answer': archived.model_dump(mode='json')}, archive_id)
                for a in latest_answers.values():
                    repo._save(db, p.id, 'interview_answer', c.InterviewAnswer(
                        profile_id=p.id, id=a.id, question_id=a.questionId, question=a.question,
                        section=a.section, answer=a.answer, created_at=a.createdAt))
                repo._save(db, p.id, 'interview_progress', c.InterviewProgress(
                    profile_id=p.id, answer_ids=tuple(a.id for a in latest_answers.values()),
                    skipped_question_ids=tuple(p.interview.skippedQuestionIds), completed=p.interview.completed), uid())
                for a in p.applications:
                    if not a.url:
                        continue
                    repo._save(db, p.id, 'application', c.ApplicationRecord(
                        profile_id=p.id, application_id=a.id, company=a.company, role=a.role,
                        sector=a.sector, vacancy_url=a.url, company_url=a.companyUrl or None,
                        job_description=a.jobDescription, location=a.location, official_domains=(),
                        questions=tuple(c.Question(id=q.id, text=q.text, max_words=q.maxWords,
                                                   max_chars=q.maxChars, type='writing', constraint_origin='user') for q in a.questions),
                        input_revision=0, output_revision=0, created_at=a.createdAt))
                repo._bump(db, p.id)
            summary['state'] = 'imported'
            return summary
