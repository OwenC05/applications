"""Immutable local edits. No provider, model, key or network capability."""
from ..domain import contracts as c
from ..domain.repository import uid
from ..retrieval.packet_service import conflict
from .edit_contracts import EditPublication, EditRequest, edit_reason_codes, edit_request_hash
from .quality import validate_targets, validated


class DraftEdits:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.domain = service.domain

    def _replay(self, db, owner, application_id, parent_id, request, digest):
        # Local edits never mint jobs/job keys. Lookup precedes currentness and CAS.
        rows = db.execute("""SELECT id,data FROM records WHERE owner=?
            AND kind='workspace:draft_dependency' AND json_extract(data,'$.origin')='edit'
            AND json_extract(data,'$.application_id')=?
            AND json_extract(data,'$.request.idempotency_key')=?""",
            (owner, application_id, request.idempotency_key)).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            conflict('Local edit replay association is ambiguous')
        publication = EditPublication.model_validate_json(rows[0][1])
        if (publication.id != rows[0][0] or publication.request_sha256 != digest
                or publication.request != request or publication.parent_draft_id != parent_id):
            conflict('Idempotency key belongs to a different local edit')
        draft, retained, _, _ = self.service._association(db, owner, application_id, publication.draft_id)
        if retained != publication:
            conflict('Local edit replay publication changed')
        return draft

    def _capture(self, db, owner, application_id, parent_id, request):
        association = self.service._association(db, owner, application_id, parent_id)
        parent, publication, parameters, _ = association
        current = self.service.jobs.capture(db, owner, application_id)
        if request.expected_revisions != parent.revisions or current != parent.revisions:
            conflict('Local edit parent is no longer current')
        if (request.parent_text_sha256, request.parent_ledger_sha256, request.parent_publication_sha256) != (
                parent.text_sha256, parent.ledger_sha256, publication.publication_sha256):
            conflict('Local edit parent hashes changed')
        depth = publication.edit_depth if isinstance(publication, EditPublication) else 0
        if depth >= 64:
            conflict('Local edit lineage limit reached')
        validate_targets(parameters.context, request.draft.targets)
        capture = self.service._capture(db, owner, application_id, parameters.request, expected=False)
        return association, capture, depth

    def _draft(self, owner, application_id, draft_id, created_at, parent, context, request):
        texts = {item.target: item.text for item in request.draft.targets}
        return c.DraftRevision(id=draft_id, profile_id=owner, application_id=application_id,
            revisions=request.expected_revisions.model_copy(update={
                'application_output': request.expected_revisions.application_output + 1}),
            research_run_id=parent.research_run_id, cover_letter=texts.get('cover_letter', ''),
            answers=tuple(c.DraftAnswer(question_id=target.question.id, text=texts[target.question.id],
                packet_id=target.packet_id) for target in context.targets if target.question.id != 'cover_letter'),
            ledger=(), assessment_state='unassessed', inventory_complete=False, created_at=created_at)

    def edit(self, owner, application_id, parent_id, request):
        try:
            return self._edit(owner, application_id, parent_id, request)
        except ValueError:
            conflict('Local edit stored schema or integrity mismatch')

    def _edit(self, owner, application_id, parent_id, request):
        request = validated(request, EditRequest)
        digest = edit_request_hash(owner, application_id, parent_id, request)
        with self.store._read() as db:
            replay = self._replay(db, owner, application_id, parent_id, request, digest)
            if replay is not None:
                return replay
            captured = self._capture(db, owner, application_id, parent_id, request)
        association, source_capture, depth = captured
        parent, parent_publication, parameters, batch = association
        self.service._verify(source_capture)
        created_at = self.service.jobs._now()
        draft = self._draft(owner, application_id, uid(), created_at, parent, parameters.context, request)
        publication = EditPublication(id=uid(), profile_id=owner, application_id=application_id,
            draft_id=draft.id, parent_draft_id=parent_id, parent_text_sha256=parent.text_sha256,
            parent_ledger_sha256=parent.ledger_sha256, parent_publication_id=parent_publication.id,
            parent_publication_sha256=parent_publication.publication_sha256,
            batch_id=batch.id, batch_sha256=batch.batch_sha256, pre_revisions=parent.revisions,
            edit_depth=depth + 1, created_at=created_at, request=request, request_sha256=digest,
            text_sha256=draft.text_sha256, ledger_sha256=draft.ledger_sha256,
            reason_codes=edit_reason_codes(parameters.context, request.draft))
        with self.store._tx() as db:
            replay = self._replay(db, owner, application_id, parent_id, request, digest)
            if replay is not None:
                return replay
            if self._capture(db, owner, application_id, parent_id, request) != captured:
                conflict('Local edit canonical capture changed')
            self.service._save_new(db, owner, 'draft', draft)
            self.service._save_new(db, owner, 'draft_dependency', publication)
            self.domain._save(db, owner, 'application', source_capture[0].model_copy(update={
                'output_revision': draft.revisions.application_output}))
        return draft

    def association(self, db, owner, application_id, draft_id, publication, visited):
        publication = validated(publication, EditPublication)
        if (publication.profile_id, publication.application_id, publication.draft_id) != (owner, application_id, draft_id):
            conflict('Local edit publication scope mismatch')
        if not visited or visited[-1] != draft_id or len(visited) != len(set(visited)) or len(visited) > 65:
            conflict('Local edit lineage is invalid')
        parent, parent_publication, parameters, batch = self.service._association(
            db, owner, application_id, publication.parent_draft_id, visited=visited)
        depth = parent_publication.edit_depth if isinstance(parent_publication, EditPublication) else 0
        request = publication.request
        if (publication.edit_depth != depth + 1
                or publication.pre_revisions != parent.revisions
                or request.expected_revisions != parent.revisions
                or publication.parent_publication_id != parent_publication.id
                or (publication.parent_text_sha256, publication.parent_ledger_sha256, publication.parent_publication_sha256)
                != (parent.text_sha256, parent.ledger_sha256, parent_publication.publication_sha256)
                or (request.parent_text_sha256, request.parent_ledger_sha256, request.parent_publication_sha256)
                != (parent.text_sha256, parent.ledger_sha256, parent_publication.publication_sha256)
                or (publication.batch_id, publication.batch_sha256) != (batch.id, batch.batch_sha256)
                or publication.request_sha256 != edit_request_hash(owner, application_id, parent.id, request)
                or publication.reason_codes != edit_reason_codes(parameters.context, request.draft)
                or publication.style_notes):
            conflict('Local edit parent/request integrity mismatch')
        draft = self.domain._get(db, owner, draft_id, 'draft', c.DraftRevision)
        expected = self._draft(owner, application_id, draft_id, publication.created_at,
                               parent, parameters.context, request)
        if (draft != expected or publication.text_sha256 != draft.text_sha256
                or publication.ledger_sha256 != draft.ledger_sha256):
            conflict('Local edit exact text/assessment integrity mismatch')
        return draft, publication, parameters, batch
