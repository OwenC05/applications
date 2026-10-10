"""Hostile immutable edit histories using synthetic originals and provider output."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_drafting_service import drafting, execute  # noqa: F401
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.contracts import EvidenceError
from copilot.domain import contracts as c
from copilot.domain.repository import ApplicationInput, ApplicationPatch
from copilot.drafting.edit_contracts import EditRequest
from copilot.drafting.schemas import DraftOutput
from copilot.drafting.service import DraftingService


def edit_request(service, scope, parent, key='edit', text='  Edited Café\n\n'):
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    targets = [dict(target=answer.question_id, text=text) for answer in parent.answers]
    if any(q['id'] == 'cover_letter' for q in detail['canonical_questions']):
        targets.append(dict(target='cover_letter', text=text))
    return EditRequest(expected_revisions=parent.revisions, parent_text_sha256=parent.text_sha256,
        parent_ledger_sha256=parent.ledger_sha256,
        parent_publication_sha256=c.canonical_hash(detail['publication']),
        draft=DraftOutput.model_validate_json(json.dumps({'targets': targets})), idempotency_key=key)


def edit(service, scope, parent, key='edit'):
    return service.edit(scope.profile_id, scope.application_id, parent.id,
                        edit_request(service, scope, parent, key))


def publication_row(service, owner, draft_id):
    with service.store._read() as db:
        row = db.execute("SELECT id,data FROM records WHERE owner=? AND kind='workspace:draft_dependency' "
                         "AND json_extract(data,'$.draft_id')=?", (owner, draft_id)).fetchone()
    return row[0], json.loads(row[1])


@pytest.mark.parametrize('mutation', ['input', 'output', 'consent', 'expiry', 'generation', 'parent', 'publication'])
def test_canonical_change_after_original_verification_denies_edit(drafting, monkeypatch, mutation):  # noqa: F811
    service, scope, batch, sent, _ = drafting
    parent, *_ = execute(drafting)
    request = edit_request(service, scope, parent)
    publication_id, _ = publication_row(service, scope.profile_id, parent.id)
    original = service._verify
    changed = []

    def verify(capture):
        original(capture)
        if changed:
            return
        changed.append(True)
        with service.store._tx() as db:
            if mutation in ('input', 'output'):
                app = service.domain._get(db, scope.profile_id, scope.application_id, 'application', c.ApplicationRecord)
                field = 'input_revision' if mutation == 'input' else 'output_revision'
                service.domain._save(db, scope.profile_id, 'application', app.model_copy(update={field: getattr(app, field) + 1}))
            elif mutation == 'consent':
                record = service.domain._singleton(db, scope.profile_id, 'consent', c.ConsentRecord)
                service.domain._save(db, scope.profile_id, 'consent', record.model_copy(update={'revision': record.revision + 1}))
                service.domain._bump(db, scope.profile_id, 'consent')
            elif mutation == 'expiry':
                run = service.domain._get(db, scope.profile_id, scope.research_run_id, 'research_run', c.ResearchRun)
                service.domain._save(db, scope.profile_id, 'research_run', run.model_copy(update={
                    'acquired_at': service.jobs._now() - timedelta(days=1),
                    'expires_at': service.jobs._now() - timedelta(seconds=1)}))
            elif mutation == 'generation':
                db.execute("UPDATE manifests SET state='retired' WHERE id=?", (batch.facts_generation.generation_id,))
            else:
                db.execute('DELETE FROM records WHERE id=?', (parent.id if mutation == 'parent' else publication_id,))

    monkeypatch.setattr(service, '_verify', verify)
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, request)
    assert changed and len(sent) == 3
    with service.store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='workspace:draft_dependency' AND json_extract(data,'$.origin')='edit'").fetchone()
    expected_output = parent.revisions.application_output + (mutation == 'output')
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == expected_output


def test_exact_replay_race_rechecks_key_before_writer_cas(drafting, monkeypatch):  # noqa: F811
    service, scope, _, sent, _ = drafting
    parent, *_ = execute(drafting)
    request = edit_request(service, scope, parent)
    original = service._verify
    winner = []
    inside = False

    def verify(capture):
        nonlocal inside
        original(capture)
        if not inside:
            inside = True
            winner.append(service.edit(scope.profile_id, scope.application_id, parent.id, request))

    monkeypatch.setattr(service, '_verify', verify)
    actual = service.edit(scope.profile_id, scope.application_id, parent.id, request)
    assert actual == winner[0]
    assert actual.revisions.application_output == parent.revisions.application_output + 1
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == actual.revisions.application_output
    assert len(sent) == 3


def test_original_corruption_observed_during_edit_verification_denies_publication(drafting):  # noqa: F811
    service, scope, batch, sent, _ = drafting
    parent, *_ = execute(drafting)
    request = edit_request(service, scope, parent)
    with service.store._read() as db:
        _, values = service.store.capture_scoped_sources(db, batch.employer_generation.scope, service.jobs._now())
    service.store._blob(values[0]['source']['id']).write_bytes(b'corrupted unmanaged original')
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, request)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output
    with service.store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='workspace:draft'").fetchone()[0] == 1
    assert len(sent) == 3


@pytest.mark.parametrize('tamper', ['parent_hash', 'self_cycle', 'parent_missing', 'packet', 'revision', 'ledger', 'depth', 'duplicate_other_app'])
def test_corrupt_edited_history_rejected_before_stale_original_bypass(drafting, monkeypatch, tamper):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    child = edit(service, scope, parent)
    publication_id, publication = publication_row(service, scope.profile_id, child.id)
    with service.store._tx() as db:
        app = service.domain._get(db, scope.profile_id, scope.application_id, 'application', c.ApplicationRecord)
        service.domain._save(db, scope.profile_id, 'application', app.model_copy(update={'input_revision': app.input_revision + 1}))
    monkeypatch.setattr(service.store, 'verify_scoped_sources', lambda *a: pytest.fail('Known stale history touched originals'))
    assert not service.detail(scope.profile_id, scope.application_id, child.id)['current']
    with service.store._tx() as db:
        if tamper == 'parent_hash':
            publication['parent_publication_sha256'] = '0' * 64
        elif tamper == 'self_cycle':
            publication['parent_draft_id'] = child.id
        elif tamper == 'parent_missing':
            db.execute('DELETE FROM records WHERE id=?', (parent.id,))
        elif tamper == 'depth':
            publication['edit_depth'] = 63
        elif tamper == 'duplicate_other_app':
            duplicate = dict(publication, id=str(uuid4()), application_id=str(uuid4()))
            db.execute('INSERT INTO records(id,owner,kind,data) VALUES(?,?,?,?)',
                (duplicate['id'], scope.profile_id, 'workspace:draft_dependency', json.dumps(duplicate)))
        else:
            row = db.execute('SELECT data FROM records WHERE id=?', (child.id,)).fetchone()
            raw = json.loads(row[0])
            if tamper == 'packet':
                raw['answers'][0]['packet_id'] = str(uuid4())
            elif tamper == 'revision':
                raw['revisions']['application_output'] += 1
            else:
                raw['assessment_state'] = 'assessed'
                raw['inventory_complete'] = True
            db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(raw), child.id))
        db.execute('UPDATE records SET data=? WHERE id=?', (json.dumps(publication), publication_id))
    with pytest.raises(EvidenceError):
        DraftingService(service.store).detail(scope.profile_id, scope.application_id, child.id)


def test_all_64_edit_links_are_readable_and_65th_does_not_publish(drafting):  # noqa: F811
    service, scope, _, sent, _ = drafting
    parent, *_ = execute(drafting)
    first = None
    for depth in range(1, 65):
        parent = edit(service, scope, parent, key=f'edit-{depth}')
        first = first or parent
    detail = service.detail(scope.profile_id, scope.application_id, parent.id)
    assert detail['current'] and detail['publication']['edit_depth'] == 64
    assert not service.detail(scope.profile_id, scope.application_id, first.id)['current']
    request = edit_request(service, scope, parent, key='edit-65')
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, request)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output
    with service.store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='workspace:draft'").fetchone()[0] == 65
    assert len(sent) == 3


def test_reordered_valid_two_target_inventory_is_rejected(packets):  # noqa: F811
    store, _, repo, app, scope, *_ = packets
    with store._tx() as db:
        app = app.model_copy(update={'questions': app.questions + (
            c.Question(id='second-writing', text='Why this role?', type='writing', constraint_origin='user'),)})
        repo._save(db, scope.profile_id, 'application', app)
    local = drafting.__wrapped__(packets)
    service = local[0]
    parent, *_ = execute(local)
    request = edit_request(service, scope, parent)
    assert len(request.draft.targets) == 2
    reversed_output = request.draft.model_copy(update={'targets': tuple(reversed(request.draft.targets))})
    request = request.model_copy(update={'draft': reversed_output})
    with pytest.raises(EvidenceError):
        service.edit(scope.profile_id, scope.application_id, parent.id, request)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output


@pytest.mark.parametrize('scope_change', ['owner', 'application'])
def test_other_owner_or_application_cannot_read_or_edit_child(drafting, scope_change):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    child = edit(service, scope, parent)
    request = edit_request(service, scope, child, key='foreign')
    owner, app = scope.profile_id, scope.application_id
    if scope_change == 'owner':
        owner = service.store.create_profile('Other synthetic', ['finance']).id
    else:
        with service.store._read() as db:
            metadata = service.domain.revisions(db, owner).metadata
        app = service.domain.create_application(owner, ApplicationInput(expected_metadata_revision=metadata,
            company='Other', role='Engineer', sector='tech', vacancy_url='https://other.example/job')).application_id
    with pytest.raises(EvidenceError):
        service.detail(owner, app, child.id)
    with pytest.raises(EvidenceError):
        service.edit(owner, app, child.id, request)
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == child.revisions.application_output


@pytest.mark.parametrize('forget', ['application', 'profile'])
def test_forget_erases_orphan_edit_text_without_parent_or_jobs(drafting, forget):  # noqa: F811
    service, scope, *_ = drafting
    parent, *_ = execute(drafting)
    child = edit(service, scope, parent)
    with service.store._tx() as db:
        db.execute('DELETE FROM records WHERE id=?', (parent.id,))
        db.execute('DELETE FROM jobs WHERE owner=?', (scope.profile_id,))
    if forget == 'application':
        app = service.domain.application(scope.profile_id, scope.application_id)
        service.domain.delete_application(scope.profile_id, scope.application_id, ApplicationPatch(
            expected_input_revision=app.input_revision, expected_output_revision=app.output_revision))
    else:
        service.store.delete_profile(scope.profile_id)
    with service.store._read() as db:
        assert not db.execute("SELECT 1 FROM records WHERE owner=? AND kind IN ('workspace:draft','workspace:draft_dependency')",
                              (scope.profile_id,)).fetchone()
        assert db.execute('SELECT 1 FROM tombstones WHERE id=?', (child.id,)).fetchone()
