"""Explicit selected upload migration: synthetic bytes only; no private discovery."""
import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from test_workspace import base, create
from test_workspace import client as client

from copilot.domain.migrations import LegacyImporter
from copilot.domain.repository import DomainRepository
from copilot.store import Store

NOW = datetime.now(timezone.utc).isoformat()


def legacy(name='Legacy synthetic'):
    owner, memory, answer, app = [str(uuid4()) for _ in range(4)]
    return {'id': owner, 'name': name, 'sectors': ['tech', 'finance'], 'isDemo': False,
            'cloudConsent': True, 'writingPreferences': 'Direct', 'revision': 4,
            'brainRevision': 2, 'createdAt': NOW, 'updatedAt': NOW,
            'memories': [{'id': memory, 'category': 'project', 'label': 'Synthetic project',
                          'content': 'Built a synthetic tool', 'status': 'verified',
                          'source': {'kind': 'manual'}, 'supersedes': None,
                          'createdAt': NOW, 'confirmedAt': NOW}],
            'interview': {'answers': [{'id': answer, 'questionId': 'common-direction',
                                      'question': 'Career direction', 'section': 'Career',
                                      'answer': 'Engineering', 'createdAt': NOW}],
                          'skippedQuestionIds': ['common-values'], 'completed': False},
            'applications': [{'id': app, 'company': 'Synthetic firm', 'role': 'Engineer',
                              'sector': 'tech', 'location': 'London', 'url': 'https://example.test/job',
                              'companyUrl': 'https://example.test/', 'jobDescription': 'Build tools',
                              'questions': [{'id': 'q1', 'text': 'Why?', 'maxWords': 50, 'maxChars': None}],
                              'status': 'submitted', 'inputRevision': 3, 'createdAt': NOW,
                              'updatedAt': NOW, 'research': None, 'draft': None,
                              'history': [{'eventId': 'legacy-event', 'status': 'submitted',
                                           'timestamp': NOW, 'draftId': str(uuid4()), 'version': str(uuid4()),
                                           'coverLetter': 'Legacy unassessed prose', 'answers': [],
                                           'evidenceIds': []}]}]}


def selected(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode()


def upload(client, content, path='/api/workspace/import/legacy/dry-run', **headers):
    return client.post(path, files={'file': ('chosen-export.json', content, 'application/json')}, headers=headers)


def commit(client, content):
    dry = upload(client, content)
    assert dry.status_code == 200, dry.text
    return upload(client, content, '/api/workspace/import/legacy/commit',
                  **{'x-confirm-legacy-import': 'true', 'x-legacy-source-sha256': dry.json()['source_sha256']})


def test_dry_run_changes_nothing_import_preserves_exact_bytes_and_resets_consent(client):
    profile = legacy()
    content = selected(profile)
    dry = upload(client, content)
    assert dry.status_code == 200
    assert dry.json()['source_sha256'] == hashlib.sha256(content).hexdigest()
    assert client.get('/api/workspace/profiles').json()['profiles'] == []
    imported = commit(client, content)
    assert imported.status_code == 200 and imported.json()['state'] == 'imported'
    detail = client.get(base(profile['id'])).json()
    assert detail['consent'] is None
    assert detail['profile']['writing_preferences'] == 'Direct'
    assert detail['proposals'][0]['origin'] == 'legacy'
    fact = client.get(f"/api/evidence/profiles/{profile['id']}/facts").json()[0]
    assert fact['id'] == profile['memories'][0]['id'] and fact['provenance'] == 'legacy'
    app = detail['applications'][0]
    assert app['input_revision'] == app['output_revision'] == 0
    history = client.get(base(profile['id']) + '/applications/' + app['application_id'] + '/history').json()['history']
    assert history == []
    archives = client.get(base(profile['id']) + '/export').json()['archives']
    assert base64.b64decode(archives[0]['original_bytes_base64']) == content
    assert archives[0]['legacy_profile']['applications'][0]['status'] == 'submitted'
    assert archives[0]['current_approval'] is False
    assert upload(client, content).status_code == 409


def test_import_requires_exact_hash_and_explicit_confirmation(client):
    content = selected(legacy())
    assert upload(client, content, '/api/workspace/import/legacy/commit').status_code == 400
    assert upload(client, content, '/api/workspace/import/legacy/commit', **{'x-confirm-legacy-import': 'true', 'x-legacy-source-sha256': '0' * 64}).status_code == 409
    assert client.get('/api/workspace/profiles').json()['profiles'] == []


def test_whole_multi_profile_validation_before_any_write(client):
    first, second = legacy('First'), legacy('Second')
    second['memories'][0]['supersedes'] = str(uuid4())
    response = upload(client, selected({'version': 1, 'profiles': [first, second]}))
    assert response.status_code == 422
    assert client.get('/api/workspace/profiles').json()['profiles'] == []


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(cloudConsent='true'),
    lambda p: p['memories'][0].update(confirmedAt=None),
    lambda p: p['memories'][0].update(extra='unsupported'),
    lambda p: p['applications'][0]['questions'].append(p['applications'][0]['questions'][0]),
    lambda p: p['applications'][0].update(url='https://user:secret@example.test/'),
    lambda p: p['interview']['answers'][0].update(id=p['id']),
])
def test_malformed_selected_export_fails_closed(client, mutation):
    value = legacy()
    mutation(value)
    assert upload(client, selected(value)).status_code == 422
    assert client.get('/api/workspace/profiles').json()['profiles'] == []


def test_duplicate_json_key_and_upload_bound(client):
    assert upload(client, b'{"id":"one","id":"two"}').status_code == 422
    assert upload(client, b'x' * (1024 * 1024 + 1)).status_code == 413


def test_collision_and_deleted_id_are_non_destructive(client):
    owner = create(client)
    value = legacy()
    value['id'] = owner
    assert upload(client, selected(value)).status_code == 409
    assert client.get(base(owner)).json()['profile']['name'] == 'Synthetic'
    client.delete(base(owner))
    # Profile tombstone must also reserve deleted profile IDs.
    assert upload(client, selected(value)).status_code == 409


def test_transaction_failure_preserves_existing_data_and_rolls_back(tmp_path, monkeypatch):
    store = Store(tmp_path)
    existing = store.create_profile('Existing', ['tech'])
    repository = DomainRepository(store)
    importer = LegacyImporter(repository)
    content = selected(legacy())
    original = repository._save
    calls = 0

    def fail_midway(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError('Synthetic transaction fault')
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, '_save', fail_midway)
    with pytest.raises(RuntimeError):
        importer.run(content, commit=True, expected_sha256=hashlib.sha256(content).hexdigest())
    assert [p.id for p in store.list_profiles()] == [existing.id]
    with store._tx() as db:
        assert db.execute('SELECT count(*) FROM records').fetchone()[0] == 0


def test_missing_legacy_url_is_archive_only_not_invented(client):
    value = legacy()
    value['applications'][0]['url'] = ''
    response = commit(client, selected(value))
    assert response.status_code == 200
    assert response.json()['archived_only_applications'] == 1
    assert client.get(base(value['id'])).json()['applications'] == []


def test_profile_delete_removes_import_backup(client):
    value = legacy()
    assert commit(client, selected(value)).status_code == 200
    assert client.delete(base(value['id'])).json()['deleted']
    with client.app.state.store._tx() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='workspace:legacy_archive'").fetchone()[0] == 0


def test_real_node_profile_export_parity_with_reanswers_and_history(tmp_path, client):
    import subprocess
    from pathlib import Path
    script = """
        import {BrainStore} from './src/store.mjs';
        import {createAI} from './src/ai.mjs';
        const store = new BrainStore({directory: process.argv[1]});
        let p = store.createProfile({name: 'Node synthetic', sectors: ['tech','finance']});
        p = store.addMemory(p.id, {category: 'project', label: 'Project', content: 'Built a synthetic tool'});
        p = store.reviewMemory(p.id, p.memories[0].id, {action: 'confirm'});
        for (const answer of ['First answer', 'Latest answer'])
          p = store.saveInterviewAnswer(p.id, {questionId:'common-direction',question:'Direction?',section:'Career',answer});
        p = store.createApplication(p.id, {company:'Example',role:'Engineer',sector:'tech',url:'https://example.test/job',companyUrl:'https://example.test/',jobDescription:'Build tools',questions:[{id:'q1',text:'Why?'}]});
        const app=p.applications[0];
        p = store.saveResearch(p.id, app.id, await createAI().research(app), {expectedInputRevision:app.inputRevision});
        const current=p.applications[0];
        const draft=await createAI().draft(p,current,{offline:true});
        p = store.saveDraft(p.id,current.id,draft,{expectedBrainRevision:p.brainRevision,expectedInputRevision:current.inputRevision,expectedResearchId:current.research.id,expectedDraftId:null,expectedHistoryLength:0});
        p=store.recordApplication(p.id,current.id,{status:'reviewed',eventId:'synthetic-review',expectedDraftId:p.applications[0].draft.id});
        console.log(JSON.stringify(p));store.close();
    """
    content = subprocess.check_output(['node', '--input-type=module', '-e', script, str(tmp_path / 'synthetic-node')], cwd=Path(__file__).resolve().parents[2])
    response = commit(client, content)
    assert response.status_code == 200, response.text
    original = json.loads(content)
    detail = client.get(base(original['id'])).json()
    assert len(detail['interview_answers']) == 1
    assert detail['interview_answers'][0]['answer'] == 'Latest answer'
    assert detail['consent'] is None
    assert detail['applications'][0]['output_revision'] == 0
    archive = next(a for a in client.get(base(original['id']) + '/export').json()['archives'] if 'original_bytes_base64' in a)
    assert base64.b64decode(archive['original_bytes_base64']) == content
    assert archive['legacy_profile']['applications'][0]['history'][0]['status'] == 'reviewed'


def test_legacy_supersession_remains_immutable_and_inactive(client):
    value = legacy()
    old = dict(value['memories'][0], id=str(uuid4()), status='superseded', content='Old exact fact')
    value['memories'][0]['supersedes'] = old['id']
    value['memories'].append(old)
    assert commit(client, selected(value)).status_code == 200
    active = client.get(base(value['id']) + '/facts').json()['facts']
    assert len(active) == 1
    assert active[0]['confirmation']['supersedes_fact_id'] == old['id']
    with client.app.state.store._tx() as db:
        archived_fact = client.app.state.store._record(db, value['id'], old['id'], 'fact')
        assert archived_fact['text'] == 'Old exact fact' and archived_fact['state'] == 'superseded'


def test_multiple_valid_profiles_rejected_without_cross_owner_backups(client):
    profiles = [legacy('First'), legacy('Second')]
    assert upload(client, selected({'version': 1, 'profiles': profiles})).status_code == 422
    assert client.get('/api/workspace/profiles').json()['profiles'] == []


def test_imported_interview_forget_erases_raw_lineage_not_other_questions(client):
    value = legacy()
    value['memories'][0]['source'] = {'kind': 'interview', 'id': value['interview']['answers'][0]['id']}
    duplicate = dict(value['interview']['answers'][0], id=str(uuid4()), answer='Latest same question')
    other = dict(value['interview']['answers'][0], id=str(uuid4()), questionId='common-values', answer='Other question')
    value['interview']['answers'].extend([duplicate, other])
    assert commit(client, selected(value)).status_code == 200
    client.delete(f"/api/evidence/profiles/{value['id']}/facts/{value['memories'][0]['id']}")
    detail = client.get(base(value['id'])).json()
    assert [a['question_id'] for a in detail['interview_answers']] == ['common-values']
    assert client.get(base(value['id']) + '/export').json()['archives'] == []


def test_imported_pending_correction_supersedes_only_when_confirmed(client):
    value = legacy()
    pending = dict(value['memories'][0], id=str(uuid4()), status='pending', confirmedAt=None, supersedes=value['memories'][0]['id'], content='Proposed correction')
    value['memories'].append(pending)
    assert commit(client, selected(value)).status_code == 200
    detail = client.get(base(value['id'])).json()
    proposal = next(p for p in detail['proposals'] if p['status'] == 'pending')
    assert proposal['supersedes_fact_id'] == value['memories'][0]['id']
    from test_workspace import review
    assert review(client, value['id'], proposal['id']).status_code == 200
    assert [f['text'] for f in client.get(f"/api/evidence/profiles/{value['id']}/facts").json()] == ['Proposed correction']


def test_import_forget_preserves_unrelated_repeated_raw_archives(client):
    value = legacy()
    value['memories'][0]['source'] = {'kind': 'interview', 'id': value['interview']['answers'][0]['id']}
    for answer in ('First other question', 'Latest other question'):
        value['interview']['answers'].append(dict(value['interview']['answers'][0], id=str(uuid4()), questionId='common-values', answer=answer))
    assert commit(client, selected(value)).status_code == 200
    client.delete(f"/api/evidence/profiles/{value['id']}/facts/{value['memories'][0]['id']}")
    archives = client.get(base(value['id']) + '/export').json()['archives']
    assert [a['answer']['answer'] for a in archives] == ['First other question']


def test_imported_confirmed_interview_reanswer_pending_supersession(client):
    from test_workspace import review, revisions
    value = legacy()
    value['memories'][0]['source'] = {'kind': 'interview', 'id': value['interview']['answers'][0]['id']}
    value['interview']['answers'].append(dict(value['interview']['answers'][0], id=str(uuid4()), answer='Latest imported raw version'))
    assert commit(client, selected(value)).status_code == 200
    owner = value['id']
    old_fact_id = value['memories'][0]['id']
    for answer in ('First workspace correction', 'Final workspace correction'):
        response = client.post(base(owner) + '/interview/answers', json={'expected_metadata_revision': revisions(client, owner)['metadata'], 'question_id': 'common-direction', 'answer': answer})
        assert response.status_code == 200
        pending = response.json()['proposals'][-1]
        assert pending['supersedes_fact_id'] == old_fact_id
        active = client.get(base(owner) + '/facts').json()['facts']
        assert len(active) == 1 and active[0]['fact']['id'] == old_fact_id
        assert active[0]['fact']['provenance'] == 'legacy'
    assert review(client, owner, pending['id']).status_code == 200
    active = client.get(base(owner) + '/facts').json()['facts']
    assert len(active) == 1 and active[0]['fact']['text'] == 'Final workspace correction'
