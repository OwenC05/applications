"""S1 domain/API parity and authority, with synthetic data and no provider calls."""
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_api import FakeService
from test_store import chunks

from copilot.api import create_app
from copilot.config import Settings
from copilot.domain import contracts as c
from copilot.domain import interview
from copilot.domain.repository import DomainRepository
from copilot.store import Store


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(tmp_path / 'data', tmp_path / 'models'),
                     lambda store, _: FakeService(store))
    with TestClient(app, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/workspace/status').json()['boot_token']
        yield client


def create(client, name='Synthetic', sectors=None):
    response = client.post('/api/workspace/profiles', json={'name': name, 'sectors': sectors or ['tech']})
    assert response.status_code == 200, response.text
    return response.json()['profile']['profile_id']


def base(owner):
    return '/api/workspace/profiles/' + owner


def revisions(client, owner):
    return client.get(base(owner)).json()['profile']['revisions']


def propose(client, owner, text='Built a synthetic project', **extra):
    response = client.post(base(owner) + '/proposals', json={
        'expected_metadata_revision': revisions(client, owner)['metadata'], 'text': text, **extra})
    assert response.status_code == 200, response.text
    return response.json()['id']


def review(client, owner, proposal, **extra):
    rev = revisions(client, owner)
    return client.post(base(owner) + '/proposals/' + proposal + '/review', json={
        'expected_metadata_revision': rev['metadata'], 'expected_facts_revision': rev['facts'],
        'action': 'confirm', 'confirmed': True, **extra})


def application(client, owner):
    response = client.post(base(owner) + '/applications', json={
        'expected_metadata_revision': revisions(client, owner)['metadata'], 'company': 'Example',
        'role': 'Engineer', 'sector': 'tech', 'vacancy_url': 'https://example.test/job',
        'job_description': 'Build tools.', 'location': 'London',
        'questions': [{'id': 'q', 'text': 'Why?', 'type': 'writing', 'constraint_origin': 'user'}]})
    assert response.status_code == 200, response.text
    return response.json()['application_id']


def test_workspace_shared_ids_capabilities_and_name(client):
    owner = create(client, name='X' * 200, sectors=['tech', 'finance'])
    assert client.get('/api/evidence/profiles').json()[0]['id'] == owner
    assert client.get('/api/workspace/profiles').json()['profiles'][0]['profile_id'] == owner
    status = client.get('/api/workspace/status').json()
    assert status['capabilities']['profile_management']
    assert not status['capabilities']['drafting'] and not status['capabilities']['browser_submit']
    assert status['cloud_key_configured'] is False
    other = client.post('/api/evidence/profiles', json={'name': 'Evidence profile', 'sectors': ['finance']}).json()['id']
    assert client.get(base(other)).json()['profile']['profile_id'] == other


def test_metadata_consent_facts_documents_revisions_are_shared_and_distinct(client):
    owner = create(client)
    response = client.patch(base(owner), json={'expected_metadata_revision': 0, 'name': 'Renamed', 'writing_preferences': 'Direct'})
    assert response.status_code == 200
    assert revisions(client, owner)['metadata'] == 1
    assert revisions(client, owner)['facts'] == 0
    assert client.post(base(owner) + '/consent', json={'expected_consent_revision': 0, 'provider': 'openai', 'purposes': ['drafting'], 'granted': True}).status_code == 200
    assert revisions(client, owner)['consent'] == 1
    fact = client.post(f'/api/evidence/profiles/{owner}/facts', json={'text': 'Confirmed', 'confirmed': True})
    assert fact.status_code == 200
    assert revisions(client, owner)['facts'] == 1
    assert client.post(f'/api/evidence/profiles/{owner}/sources', files={'file': ('test.txt', b'Synthetic document', 'text/plain')}).status_code == 200
    assert revisions(client, owner)['documents'] == 1
    assert revisions(client, owner)['facts'] == 1
    assert client.patch(base(owner), json={'expected_metadata_revision': 0, 'name': 'Stale'}).status_code == 409
    assert client.patch(base(owner), json={'expected_metadata_revision': 1, 'cloud_consent': True}).status_code == 422


@pytest.mark.parametrize('field,value', [('confirmed', 'true'), ('confirmed', 1)])
def test_strict_confirmation(client, field, value):
    owner = create(client)
    pending = propose(client, owner)
    assert review(client, owner, pending, **{field: value}).status_code == 422
    assert client.get(f'/api/evidence/profiles/{owner}/facts').json() == []


def test_atomic_confirmation_supersession_owner_and_correction(client):
    owner, other = create(client), create(client, 'Other')
    pending = propose(client, owner)
    assert review(client, other, pending).status_code == 404
    assert review(client, owner, pending).status_code == 200
    old = client.get(f'/api/evidence/profiles/{owner}/facts').json()[0]['id']
    replacement = propose(client, owner, 'Updated exact text', supersedes_fact_id=old)
    assert review(client, owner, replacement, text='Corrected explicitly').status_code == 200
    active = client.get(f'/api/evidence/profiles/{owner}/facts').json()
    assert len(active) == 1 and active[0]['text'] == 'Corrected explicitly'
    confirmation = client.get(base(owner) + '/facts').json()['facts'][0]['confirmation']
    assert confirmation['supersedes_fact_id'] == old
    assert confirmation['proposal_id'] == replacement
    assert review(client, owner, replacement).status_code == 409


def test_question_bank_matches_node_exactly():
    output = subprocess.check_output(['node', '--input-type=module', '-e',
                                      "import {QUESTION_BANK} from './src/interview.mjs'; console.log(JSON.stringify(QUESTION_BANK))"], cwd=Path(__file__).resolve().parents[2])
    assert list(interview.QUESTION_BANK) == json.loads(output)
    assert len(interview.QUESTION_BANK) == 48
    assert len(interview.questions(['tech'])) == 38
    assert len(interview.questions(['finance'])) == 38


def test_interview_resume_reanswer_and_skips_are_pending(client):
    owner = create(client)
    url = base(owner) + '/interview/answers'
    interview_response = client.get(base(owner) + '/interview').json()
    assert len(interview_response['questions']) == 38
    assert interview_response['answers'] == []
    assert interview_response['progress']['completed'] is False
    for text in ('First raw answer', 'Second raw answer'):
        response = client.post(url, json={'expected_metadata_revision': revisions(client, owner)['metadata'], 'question_id': 'common-direction', 'answer': text})
        assert response.status_code == 200, response.text
    detail = client.get(base(owner)).json()
    assert len(detail['interview_answers']) == 1
    assert len(detail['proposals']) == 2
    assert {p['status'] for p in detail['proposals']} == {'pending'}
    assert client.get(f'/api/evidence/profiles/{owner}/facts').json() == []
    assert client.patch(base(owner) + '/interview', json={'expected_metadata_revision': revisions(client, owner)['metadata'], 'skipped_question_ids': ['common-values'], 'completed': True}).status_code == 200
    assert client.post(url, json={'expected_metadata_revision': revisions(client, owner)['metadata'], 'question_id': 'finance-risk', 'answer': 'Not applicable'}).status_code == 400
    archive = client.get(base(owner) + '/export').json()['archives']
    assert archive[0]['answer']['answer'] == 'First raw answer'


def test_typed_values_never_become_story_facts_and_owner_deletion(client):
    owner, other = create(client), create(client, 'Other')
    payload = {'expected_metadata_revision': 0, 'kind': 'contact', 'field': 'email', 'value': 'synthetic@example.test', 'purpose': 'Contact', 'explicitly_confirmed': True}
    response = client.post(base(owner) + '/typed-values', json=payload)
    assert response.status_code == 200, response.text
    record_id = response.json()['id']
    assert client.get(f'/api/evidence/profiles/{owner}/facts').json() == []
    assert client.request('DELETE', base(other) + '/typed-values/' + record_id, json={'expected_metadata_revision': 0}).status_code == 404
    assert client.request('DELETE', base(owner) + '/typed-values/' + record_id, json={'expected_metadata_revision': 1}).status_code == 200
    assert client.get(base(owner)).json()['typed_values'] == []


def test_application_edits_output_history_and_no_auto_facts(client):
    owner, other = create(client), create(client, 'Other')
    app = application(client, owner)
    url = base(owner) + '/applications/' + app
    assert client.get(base(other) + '/applications/' + app).status_code == 404
    changed = client.patch(url, json={'expected_input_revision': 0, 'expected_output_revision': 0, 'role': 'AI Engineer'})
    assert changed.status_code == 200 and changed.json()['input_revision'] == 1
    event = {'expected_input_revision': 1, 'expected_output_revision': 1, 'event_id': str(uuid4()), 'text': 'Recruiter feedback', 'kind': 'feedback'}
    first = client.post(url + '/history', json=event)
    assert first.status_code == 200, first.text
    assert client.post(url + '/history', json=event).json() == first.json()
    assert client.get(url).json()['output_revision'] == 2
    assert client.patch(url, json={'expected_input_revision': 1, 'expected_output_revision': 1, 'role': 'Stale'}).status_code == 409
    assert client.get(f'/api/evidence/profiles/{owner}/facts').json() == []
    assert client.get(base(owner)).json()['proposals'][0]['origin'] == 'application_feedback'
    proposal = client.get(base(owner)).json()['proposals'][0]['id']
    assert review(client, owner, proposal).status_code == 200
    assert client.get(f'/api/evidence/profiles/{owner}/facts').json()[0]['provenance'] == 'manual'
    assert client.get(url + '/history').json()['history'][0]['application_snapshot']['role'] == 'AI Engineer'


def test_profile_delete_revokes_all_owned_domain_data_before_cleanup(client):
    owner, other = create(client), create(client, 'Other')
    pending = propose(client, owner)
    review(client, owner, pending)
    application(client, owner)
    response = client.delete(base(owner))
    assert response.status_code == 200 and response.json()['cleanup_pending'] is True
    assert client.get(base(owner)).status_code == 404
    assert client.get(base(other)).status_code == 200
    with client.app.state.store._tx() as db:
        assert db.execute('SELECT count(*) FROM records WHERE owner=?', (owner,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fact_metadata WHERE owner=?', (owner,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM profile_revisions WHERE owner=?', (owner,)).fetchone()[0] == 0


def test_invalid_token_and_nonjson_strict_http(client):
    assert client.post('/api/workspace/profiles', headers={'x-evidence-token': 'wrong'}, json={'name': 'Name', 'sectors': ['tech']}).status_code == 403
    assert client.post('/api/workspace/profiles', content=b'not json', headers={'content-type': 'application/json'}).status_code == 422
    assert client.post('/api/workspace/profiles', json={'name': 'Name', 'sectors': ['tech', 'tech']}).status_code == 422
    assert client.post('/api/workspace/profiles', json={'name': 'Name', 'sectors': ['tech'], 'profile_id': str(uuid4())}).status_code == 422


def test_corpus_manifests_survive_unrelated_metadata_and_corpus_mutations(tmp_path):
    import chromadb
    from test_retrieval import FixtureModels

    from copilot.retrieval.dense import DenseIndex
    from copilot.retrieval.service import EvidenceService
    store = Store(tmp_path)
    owner = store.create_profile('Name', ['tech']).id
    store.confirm_fact(owner, 'Synthetic fact')
    fact_snapshot = store.snapshot(owner, 'facts', chunks)
    service = EvidenceService(store, tmp_path / 'indexes',
        DenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'chroma'))), FixtureModels())
    manifest = service.build(owner, 'facts')
    repository = DomainRepository(store)
    repository.patch_profile(owner, c.ProfilePatch(expected_metadata_revision=0, name='Renamed'))
    assert store.active_manifest(owner, 'facts') == manifest
    source = store.add_source(owner, 'notes.txt', 'text/plain', b'Text', ['Text'], 'synthetic')
    assert store.active_manifest(owner, 'facts') == manifest
    store.delete_source(owner, source.id)
    assert store.active_manifest(owner, 'facts') == manifest
    store.validate_snapshot(owner, fact_snapshot.revision, 'facts')


def test_non_destructive_old_database_revision_backfill(tmp_path):
    from copilot.contracts import Manifest
    from copilot.store import digest
    store = Store(tmp_path)
    owner = store.create_profile('Old profile', ['tech']).id
    store.confirm_fact(owner, 'One fact')
    store.add_source(owner, 'notes.txt', 'text/plain', b'Text', ['Text'], 'synthetic')
    snap = store.snapshot(owner, 'facts', chunks)
    with store._tx() as db:
        revision = store._profile(db, owner).revision
        db.execute('DELETE FROM profile_revisions WHERE owner=?', (owner,))
        manifest = Manifest(str(uuid4()), owner, 'facts', revision, 'model', 'chunker',
                            digest('\n'.join(sorted(c.id for c in snap.chunks))),
                            len(snap.chunks), 'collection', 'path')
        db.execute('INSERT INTO manifests VALUES(?,?,?,?,?)', (manifest.generation_id, owner, 'facts', 'active', json.dumps(asdict(manifest))))
    reopened = Store(tmp_path)
    assert reopened.active_manifest(owner, 'facts') == manifest
    vector = DomainRepository(reopened).detail(owner).profile.revisions
    assert vector.facts == vector.documents == revision


def test_source_delete_removes_pending_proposals_and_confirmation_metadata(client):
    owner = create(client)
    uploaded = client.post(f'/api/evidence/profiles/{owner}/sources', files={'file': ('notes.txt', b'Synthetic documented fact', 'text/plain')}).json()
    unit = client.get(f"/api/evidence/profiles/{owner}/sources/{uploaded['id']}/units").json()[0]
    span = {'record_id': uploaded['id'], 'unit_id': unit['id'], 'start': 0, 'end': len(unit['text']), 'excerpt': unit['text'], 'text_sha256': unit['text_sha256']}
    pending = propose(client, owner, source_spans=[span])
    review(client, owner, pending)
    propose(client, owner, source_spans=[span])
    client.delete(f"/api/evidence/profiles/{owner}/sources/{uploaded['id']}")
    assert client.get(base(owner)).json()['proposals'] == []
    assert client.get(base(owner) + '/facts').json()['facts'] == []
    with client.app.state.store._tx() as db:
        assert db.execute('SELECT count(*) FROM fact_metadata WHERE owner=?', (owner,)).fetchone()[0] == 0


def test_application_delete_uses_revisions_and_removes_application_scoped_pii(client):
    owner = create(client)
    app = application(client, owner)
    response = client.post(base(owner) + '/typed-values', json={
        'expected_metadata_revision': revisions(client, owner)['metadata'], 'kind': 'declaration',
        'field': 'acknowledgement', 'value': True, 'purpose': 'Application declaration',
        'application_id': app, 'explicitly_confirmed': True})
    assert response.status_code == 200
    url = base(owner) + '/applications/' + app
    assert client.request('DELETE', url, json={'expected_input_revision': 0, 'expected_output_revision': 1}).status_code == 409
    assert client.request('DELETE', url, json={'expected_input_revision': 0, 'expected_output_revision': 0}).status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(base(owner)).json()['typed_values'] == []


def test_second_provenance_source_delete_revokes_multi_source_fact(client):
    owner = create(client)
    sources = []
    spans = []
    for index in range(2):
        source = client.post(f'/api/evidence/profiles/{owner}/sources', files={'file': (f'notes{index}.txt', b'Synthetic evidence', 'text/plain')}).json()
        sources.append(source)
        unit = client.get(f"/api/evidence/profiles/{owner}/sources/{source['id']}/units").json()[0]
        spans.append({'record_id': source['id'], 'unit_id': unit['id'], 'start': 0, 'end': len(unit['text']), 'excerpt': unit['text'], 'text_sha256': unit['text_sha256']})
    proposal = propose(client, owner, source_spans=spans)
    assert review(client, owner, proposal).status_code == 200
    client.delete(f"/api/evidence/profiles/{owner}/sources/{sources[1]['id']}")
    assert client.get(base(owner) + '/facts').json()['facts'] == []


def test_revoke_interview_fact_forgets_linked_raw_answer_and_progress(client):
    owner = create(client)
    response = client.post(base(owner) + '/interview/answers', json={
        'expected_metadata_revision': 0, 'question_id': 'common-direction', 'answer': 'Sensitive synthetic answer'})
    proposal = response.json()['proposals'][0]['id']
    assert review(client, owner, proposal).status_code == 200
    fact_id = client.get(base(owner) + '/facts').json()['facts'][0]['fact']['id']
    assert client.delete(f'/api/evidence/profiles/{owner}/facts/{fact_id}').status_code == 202
    detail = client.get(base(owner)).json()
    assert detail['interview_answers'] == []
    assert detail['interview']['answer_ids'] == []
    assert detail['proposals'] == []


def test_interview_reanswer_proposes_supersession_without_mutating_confirmed_fact(client):
    owner = create(client)
    url = base(owner) + '/interview/answers'
    response = client.post(url, json={'expected_metadata_revision': 0, 'question_id': 'common-direction', 'answer': 'Original confirmed answer'})
    assert review(client, owner, response.json()['proposals'][0]['id']).status_code == 200
    fact = client.get(base(owner) + '/facts').json()['facts'][0]['fact']
    for answer in ('Pending updated answer', 'Another pending updated answer'):
        response = client.post(url, json={'expected_metadata_revision': revisions(client, owner)['metadata'], 'question_id': 'common-direction', 'answer': answer})
        assert response.status_code == 200
        assert response.json()['proposals'][-1]['supersedes_fact_id'] == fact['id']
        assert client.get(base(owner) + '/facts').json()['facts'][0]['fact']['text'] == fact['text']


def test_forget_preserves_unrelated_question_archives(client):
    owner = create(client)
    for question_id in ('common-direction', 'common-values'):
        for answer in ('First ' + question_id, 'Second ' + question_id):
            assert client.post(base(owner) + '/interview/answers', json={'expected_metadata_revision': revisions(client, owner)['metadata'], 'question_id': question_id, 'answer': answer}).status_code == 200
    detail = client.get(base(owner)).json()
    direction = next(p for p in detail['proposals'] if p['text'] == 'Second common-direction')
    assert review(client, owner, direction['id']).status_code == 200
    fact = client.get(base(owner) + '/facts').json()['facts'][0]['fact']
    client.delete(f"/api/evidence/profiles/{owner}/facts/{fact['id']}")
    archives = client.get(base(owner) + '/export').json()['archives']
    assert any(a.get('answer', {}).get('question_id') == 'common-values' for a in archives)
    assert not any(a.get('answer', {}).get('question_id') == 'common-direction' for a in archives)


def test_delete_proposal_frozen_route_and_confirmed_cascade(client):
    owner, other = create(client), create(client, 'Other')
    unrelated = propose(client, owner, 'Unrelated standalone fact')
    assert review(client, owner, unrelated).status_code == 200
    pending = propose(client, owner, 'Forgotten confirmed fact')
    assert review(client, owner, pending).status_code == 200
    url = base(owner) + '/proposals/' + pending
    assert client.request('DELETE', base(other) + '/proposals/' + pending, json={'expected_metadata_revision': revisions(client, other)['metadata'], 'expected_facts_revision': revisions(client, other)['facts']}).status_code == 404
    rev = revisions(client, owner)
    assert client.request('DELETE', url, json={'expected_metadata_revision': rev['metadata'], 'expected_facts_revision': rev['facts']}).status_code == 200
    assert [f['fact']['text'] for f in client.get(base(owner) + '/facts').json()['facts']] == ['Unrelated standalone fact']


def test_application_delete_cascades_confirmed_feedback_fact(client):
    owner = create(client)
    unrelated = propose(client, owner, 'Unrelated independent fact')
    review(client, owner, unrelated)
    app = application(client, owner)
    url = base(owner) + '/applications/' + app
    event = client.post(url + '/history', json={'expected_input_revision': 0, 'expected_output_revision': 0, 'event_id': str(uuid4()), 'text': 'Application-dependent feedback'}).json()
    proposal = next(p for p in client.get(base(owner)).json()['proposals'] if p['origin_id'] == event['id'])
    review(client, owner, proposal['id'])
    assert client.request('DELETE', url, json={'expected_input_revision': 0, 'expected_output_revision': 1}).status_code == 200
    assert [f['fact']['text'] for f in client.get(base(owner) + '/facts').json()['facts']] == ['Unrelated independent fact']


def test_workspace_rejects_non_json_mutation(client):
    response = client.post('/api/workspace/profiles', content='{"name":"Synthetic","sectors":["tech"]}', headers={'content-type': 'text/plain'})
    assert response.status_code == 415


def test_pending_proposal_deletion_preserves_prior_fact(client):
    owner = create(client)
    prior = propose(client, owner, 'Prior confirmed fact')
    review(client, owner, prior)
    fact = client.get(base(owner) + '/facts').json()['facts'][0]['fact']
    pending = propose(client, owner, 'Unconfirmed correction', supersedes_fact_id=fact['id'])
    rev = revisions(client, owner)
    response = client.request('DELETE', base(owner) + '/proposals/' + pending, json={
        'expected_metadata_revision': rev['metadata'], 'expected_facts_revision': rev['facts']})
    assert response.status_code == 200 and not response.json()['cleanup_pending']
    assert client.get(base(owner) + '/facts').json()['facts'][0]['fact']['text'] == 'Prior confirmed fact'


def test_application_forget_fault_rolls_back_facts_history_and_ticket(client, monkeypatch):
    owner = create(client)
    app = application(client, owner)
    url = base(owner) + '/applications/' + app
    client.post(url + '/history', json={'expected_input_revision': 0, 'expected_output_revision': 0, 'event_id': str(uuid4()), 'text': 'Atomic feedback fact'})
    proposal = client.get(base(owner)).json()['proposals'][0]
    review(client, owner, proposal['id'])
    store = client.app.state.store
    original = store._delete

    def fault_after_cascade(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('Synthetic fault after deletion ticket')

    monkeypatch.setattr(store, '_delete', fault_after_cascade)
    with pytest.raises(RuntimeError):
        client.request('DELETE', url, json={'expected_input_revision': 0, 'expected_output_revision': 1})
    assert client.get(url).status_code == 200
    assert len(client.get(url + '/history').json()['history']) == 1
    assert client.get(base(owner) + '/facts').json()['facts'][0]['fact']['text'] == 'Atomic feedback fact'
    assert store.pending_cleanup() == []


@pytest.mark.parametrize('forget_kind', ['fact', 'source'])
def test_direct_forget_invalidates_workspace_metadata_cas(client, forget_kind):
    owner = create(client)
    source_id = None
    if forget_kind == 'fact':
        answer = client.post(base(owner) + '/interview/answers', json={'expected_metadata_revision': 0, 'question_id': 'common-direction', 'answer': 'Confirmed interview answer'}).json()
        review(client, owner, answer['proposals'][0]['id'])
        fact_id = client.get(base(owner) + '/facts').json()['facts'][0]['fact']['id']
    else:
        source = client.post(f'/api/evidence/profiles/{owner}/sources', files={'file': ('notes.txt', b'Synthetic source', 'text/plain')}).json()
        source_id = source['id']
        unit = client.get(f'/api/evidence/profiles/{owner}/sources/{source_id}/units').json()[0]
        span = {'record_id': source_id, 'unit_id': unit['id'], 'start': 0, 'end': len(unit['text']), 'excerpt': unit['text'], 'text_sha256': unit['text_sha256']}
        propose(client, owner, source_spans=[span])
    before = revisions(client, owner)['metadata']
    target = f'/api/evidence/profiles/{owner}/sources/{source_id}' if source_id else f'/api/evidence/profiles/{owner}/facts/{fact_id}'
    client.delete(target)
    assert revisions(client, owner)['metadata'] > before
    assert client.patch(base(owner), json={'expected_metadata_revision': before, 'name': 'Stale UI'}).status_code == 409
    current = revisions(client, owner)['metadata']
    assert client.patch(base(owner), json={'expected_metadata_revision': current, 'name': 'Current UI'}).status_code == 200
