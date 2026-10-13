"""Immutable exact human writing acknowledgements; no provider capabilities."""
import json

from pydantic import TypeAdapter

from ..contracts import EvidenceError
from ..domain import contracts as c
from ..domain.repository import uid
from ..retrieval.packet_service import conflict
from .review_contracts import (
    ReviewDisclosure,
    ReviewPreviewRequest,
    ReviewPublication,
    ReviewRequest,
    review_request_hash,
    review_validated,
)

_IDENTIFIER = TypeAdapter(c.Identifier, config={'strict': True})
_WARNINGS = ('WRITING_ONLY', 'AUTOMATED_SUPPORT_FALLIBLE', 'NOT_FACT_CONFIRMATION',
             'MANUAL_AND_ATTACHMENTS_NOT_REVIEWED', 'NO_BROWSER_AUTHORIZATION')


class DraftReviews:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.domain = service.domain

    def _input(self, owner, application_id, parent_id, request, model):
        try:
            for value in (owner, application_id, parent_id):
                _IDENTIFIER.validate_python(value)
            return review_validated(request, model)
        except (ValueError, TypeError):
            raise EvidenceError('INVALID_REQUEST', 'Invalid writing review request', 422) from None

    def _scope(self, db, owner, application_id, parent_id):
        self.store._profile(db, owner)
        application = self.domain._get(db, owner, application_id, 'application', c.ApplicationRecord)
        parent = self.domain._get(db, owner, parent_id, 'draft', c.DraftRevision)
        if ((application.profile_id, application.application_id) != (owner, application_id)
                or (parent.profile_id, parent.application_id, parent.id) != (owner, application_id, parent_id)):
            raise EvidenceError('NOT_FOUND', 'Draft not found', 404)

    @staticmethod
    def writing_ready(association):
        draft, publication, parameters, batch = association
        manual_ids = {m.question_id for m in batch.manual_requirements}
        if (not draft.support_ready or publication.reason_codes
                or any(q.required and q.id in manual_ids for q in batch.canonical_questions)):
            return False
        targets = parameters.context.targets
        expected = tuple(t.question.id for t in targets if t.question.id != 'cover_letter')
        if tuple(a.question_id for a in draft.answers) != expected:
            return False
        texts = {'cover_letter': draft.cover_letter, **{a.question_id: a.text for a in draft.answers}}
        for target in targets:
            question = target.question
            text = texts[question.id]
            if (question.required and not text.strip()
                    or question.max_chars is not None and len(text) > question.max_chars
                    or question.max_words is not None and len(text.split()) > question.max_words):
                return False
        return True

    @staticmethod
    def _disclosure(association):
        parent, publication, _, batch = association
        return ReviewDisclosure(profile_id=parent.profile_id, application_id=parent.application_id,
            parent_draft_id=parent.id, parent_publication_id=publication.id,
            parent_publication_sha256=publication.publication_sha256,
            batch_id=batch.id, batch_sha256=batch.batch_sha256, research_run_id=parent.research_run_id,
            pre_revisions=parent.revisions, post_revisions=parent.revisions.model_copy(update={
                'application_output': parent.revisions.application_output + 1}),
            text_sha256=parent.text_sha256, ledger_sha256=parent.ledger_sha256,
            cover_letter=parent.cover_letter, answers=parent.answers, ledger=parent.ledger,
            canonical_questions=batch.canonical_questions, cover_letter_target=batch.cover_letter_target,
            manual_requirements=batch.manual_requirements, style_notes=publication.style_notes, warnings=_WARNINGS)

    def _capture(self, db, owner, application_id, parent_id, request):
        from .admission_runtime import DraftAdmission
        context = self.service._association_context(db, owner, application_id)
        association = self.service._association(db, owner, application_id, parent_id, context=context)
        parent, publication, parameters, _ = association
        counts = self.service._lineage_counts(db, owner, application_id, parent_id,
                                             append_origin='review', context=context)
        if not self.writing_ready(association):
            conflict('Writing is not ready for human review')
        if (request.expected_revisions != parent.revisions
                or self.service.jobs.capture(db, owner, application_id) != parent.revisions
                or (request.parent_text_sha256, request.parent_ledger_sha256, request.parent_publication_sha256)
                != (parent.text_sha256, parent.ledger_sha256, publication.publication_sha256)):
            conflict('Writing review parent changed')
        source_capture = self.service._capture(db, owner, application_id, parameters.request, expected=False)
        history = DraftAdmission(self.service).capture_history(db, owner, application_id)
        if any(reason != 'admission_exhausted' for reason in history.reason_codes):
            conflict('Writing review is blocked by retained operation history')
        disclosure = self._disclosure(association)
        if isinstance(request, ReviewRequest) and request.disclosure_sha256 != c.canonical_hash(disclosure):
            conflict('Writing review disclosure changed')
        return association, source_capture, counts, history, disclosure

    def preview(self, owner, application_id, parent_id, request):
        request = self._input(owner, application_id, parent_id, request, ReviewPreviewRequest)
        try:
            with self.store._read() as db:
                self._scope(db, owner, application_id, parent_id)
                captured = self._capture(db, owner, application_id, parent_id, request)
            self.service._verify(captured[1])
            with self.store._read() as db:
                if self._capture(db, owner, application_id, parent_id, request) != captured:
                    conflict('Writing review changed during preview')
            disclosure = captured[4]
            return dict(schema_version=1, disclosure=disclosure.model_dump(mode='json'),
                        disclosure_sha256=c.canonical_hash(disclosure))
        except (ValueError, TypeError):
            conflict('Writing review stored integrity mismatch')

    def _replay(self, db, owner, application_id, parent_id, request, digest):
        rows = db.execute("""SELECT id,data FROM records WHERE owner=?
            AND kind='workspace:draft_dependency' AND json_extract(data,'$.origin')='review'
            AND json_extract(data,'$.application_id')=?
            AND json_extract(data,'$.request.idempotency_key')=?""",
            (owner, application_id, request.idempotency_key)).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            conflict('Writing review replay is ambiguous')
        publication = ReviewPublication.model_validate_json(rows[0][1])
        if (publication.id != rows[0][0] or publication.request != request
                or publication.request_sha256 != digest or publication.parent_draft_id != parent_id):
            conflict('Idempotency key belongs to another writing review')
        draft, retained, _, _ = self.service._association(db, owner, application_id, publication.draft_id)
        if retained != publication:
            conflict('Writing review replay association changed')
        return draft

    @staticmethod
    def _clone(parent, draft_id, created_at):
        return review_validated(parent.model_copy(update={'id': draft_id, 'created_at': created_at,
            'revisions': parent.revisions.model_copy(update={
                'application_output': parent.revisions.application_output + 1})}), c.DraftRevision)

    def review(self, owner, application_id, parent_id, request):
        request = self._input(owner, application_id, parent_id, request, ReviewRequest)
        digest = review_request_hash(owner, application_id, parent_id, request)
        try:
            return self._review(owner, application_id, parent_id, request, digest)
        except (ValueError, TypeError):
            conflict('Writing review stored integrity mismatch')

    def _review(self, owner, application_id, parent_id, request, digest):
        with self.store._read() as db:
            self._scope(db, owner, application_id, parent_id)
            replay = self._replay(db, owner, application_id, parent_id, request, digest)
            if replay is not None:
                return replay
            captured = self._capture(db, owner, application_id, parent_id, request)
        association, source_capture, counts, _, disclosure = captured
        parent, prior, _, batch = association
        self.service._verify(source_capture)
        now = self.service.jobs._now()
        child = self._clone(parent, uid(), now)
        snapshot = c.ReviewSnapshot(id=uid(), profile_id=owner, application_id=application_id,
            draft_id=child.id, text_sha256=child.text_sha256, ledger_sha256=child.ledger_sha256,
            revisions=child.revisions, research_run_id=child.research_run_id,
            disclosure_sha256=request.disclosure_sha256, reviewed_at=now)
        publication = ReviewPublication(id=uid(), profile_id=owner, application_id=application_id,
            draft_id=child.id, parent_draft_id=parent.id, parent_text_sha256=parent.text_sha256,
            parent_ledger_sha256=parent.ledger_sha256, parent_publication_id=prior.id,
            parent_publication_sha256=prior.publication_sha256, batch_id=batch.id,
            batch_sha256=batch.batch_sha256, pre_revisions=parent.revisions, edit_depth=counts.edits,
            created_at=now, request=request, request_sha256=digest, disclosure=disclosure,
            review_snapshot_id=snapshot.id, review_snapshot_sha256=c.canonical_hash(snapshot),
            text_sha256=child.text_sha256, ledger_sha256=child.ledger_sha256,
            reason_codes=(), style_notes=prior.style_notes)
        with self.store._tx() as db:
            self._scope(db, owner, application_id, parent_id)
            replay = self._replay(db, owner, application_id, parent_id, request, digest)
            if replay is not None:
                return replay
            if self._capture(db, owner, application_id, parent_id, request) != captured:
                conflict('Writing review canonical capture changed')
            self.service._save_new(db, owner, 'draft', child)
            self.service._save_new(db, owner, 'draft_dependency', publication)
            self.service._save_new(db, owner, 'draft_review', snapshot)
            self.domain._save(db, owner, 'application', source_capture[0].model_copy(update={
                'output_revision': child.revisions.application_output}))
        return child

    def snapshot(self, db, owner, application_id, draft_id, publication):
        publication = review_validated(publication, ReviewPublication)
        rows = db.execute("""SELECT id,data FROM records WHERE owner=? AND kind='workspace:draft_review'
            AND json_extract(data,'$.draft_id')=?""", (owner, draft_id)).fetchall()
        if len(rows) != 1:
            conflict('Writing review snapshot association is ambiguous')
        raw = json.loads(rows[0][1])
        vector = raw.get('revisions') if isinstance(raw, dict) else None
        if (not isinstance(vector, dict) or set(vector) - {'schema_version'}
                != set(c.RevisionVector.model_fields) - {'schema_version'}):
            conflict('Writing review snapshot requires every revision dimension')
        snapshot = c.ReviewSnapshot.model_validate_json(rows[0][1])
        expected = c.ReviewSnapshot(id=publication.review_snapshot_id, profile_id=owner,
            application_id=application_id, draft_id=draft_id, text_sha256=publication.text_sha256,
            ledger_sha256=publication.ledger_sha256, revisions=publication.disclosure.post_revisions,
            research_run_id=publication.disclosure.research_run_id,
            disclosure_sha256=publication.request.disclosure_sha256, reviewed_at=publication.created_at)
        if (rows[0][0] != snapshot.id or snapshot != expected
                or c.canonical_hash(snapshot) != publication.review_snapshot_sha256
                or (publication.profile_id, publication.application_id, publication.draft_id)
                != (owner, application_id, draft_id)):
            conflict('Writing review snapshot integrity mismatch')
        return snapshot

    def association(self, db, owner, application_id, draft_id, publication, visited, *, context=None):
        publication = review_validated(publication, ReviewPublication)
        if ((publication.profile_id, publication.application_id, publication.draft_id)
                != (owner, application_id, draft_id) or not visited or visited[-1] != draft_id
                or len(visited) != len(set(visited)) or len(visited) > 193):
            conflict('Writing review publication scope or lineage mismatch')
        association = self.service._association(db, owner, application_id, publication.parent_draft_id,
                                                visited=visited, context=context)
        parent, prior, parameters, batch = association
        request = publication.request
        if (getattr(prior, 'origin', 'generation') not in ('generation', 'assessment')
                or not self.writing_ready(association)
                or publication.pre_revisions != parent.revisions or request.expected_revisions != parent.revisions
                or publication.parent_publication_id != prior.id
                or (publication.parent_text_sha256, publication.parent_ledger_sha256, publication.parent_publication_sha256)
                != (parent.text_sha256, parent.ledger_sha256, prior.publication_sha256)
                or (request.parent_text_sha256, request.parent_ledger_sha256, request.parent_publication_sha256)
                != (parent.text_sha256, parent.ledger_sha256, prior.publication_sha256)
                or (publication.batch_id, publication.batch_sha256) != (batch.id, batch.batch_sha256)
                or publication.request_sha256 != review_request_hash(owner, application_id, parent.id, request)
                or publication.disclosure != self._disclosure(association)
                or request.disclosure_sha256 != c.canonical_hash(publication.disclosure)
                or publication.style_notes != prior.style_notes):
            conflict('Writing review parent/request/disclosure integrity mismatch')
        child = self.domain._get(db, owner, draft_id, 'draft', c.DraftRevision)
        if (child != self._clone(parent, draft_id, publication.created_at)
                or publication.text_sha256 != child.text_sha256 or publication.ledger_sha256 != child.ledger_sha256):
            conflict('Writing review exact clone mismatch')
        self.snapshot(db, owner, application_id, draft_id, publication)
        return child, publication, parameters, batch
