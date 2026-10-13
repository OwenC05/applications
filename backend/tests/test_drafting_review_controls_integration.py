"""Production ReviewController against real local ASGI responses, not canned DTOs.

Synthetic evidence/provider setup only; local review must never load keys/models.
Node uses a bounded stdio bridge to TestClient. This is not browser qualification.
"""
import json
import selectors
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_drafting_review_api import api_for, authorize
from test_drafting_review_cover_letter import covering
from test_drafting_service import execute
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
EXACT = '  Café e\u0301 😀\r\n<script>window.__reviewExecuted=true</script>\n '
TABLES = ('jobs', 'job_keys', 'job_stages', 'job_retries', 'provider_attempts',
          'provider_retry_links', 'budget_limits', 'drafting_admission_heads',
          'drafting_admission_bindings')


@pytest.fixture
def controls(packets, request, monkeypatch):  # noqa: F811
    store, _, repo, app, scope, *_ = packets
    sector, historical = request.param
    with store._tx() as db:
        app = app.model_copy(update={'sector': sector, 'questions': tuple(
            q if q.type == 'writing' else q.model_copy(update={'required': False})
            for q in app.questions)})
        repo._save(db, scope.profile_id, 'application', app)
    configured_packets = (*packets[:3], app, *packets[4:])
    configured = covering.__wrapped__(configured_packets, SimpleNamespace(param={'manual': True}), monkeypatch)
    configured[-1]['text'] = EXACT
    parent, *_ = execute(configured)
    return configured, parent, historical, sector


def rows(service):
    with service.store._read() as db:
        return {table: tuple(db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall())
                for table in TABLES}


SCRIPT = r"""
import assert from 'node:assert/strict';
import {createInterface} from 'node:readline';
import {ReviewController, ReviewRegistry} from './backend/ui/workspace/review.js';
import {DraftBuffer} from './backend/ui/workspace/drafting.js';
const lines=createInterface({input:process.stdin})[Symbol.asyncIterator]();
const input=JSON.parse((await lines.next()).value);
const transport=async(path,options)=>{
 console.log(JSON.stringify({request:{path,options}}));
 const response=JSON.parse((await lines.next()).value);
 if(response.error)throw new Error(response.error);
 return response.value;
};
const p=input.parent, owner=p.draft.profile_id, application=p.draft.application_id;
let context={owner,application,dirty:false,busy:false,competingMutationLocked:false,
 writing:{draftInspection:p,bufferMatchesSaved:true,saving:false,uncertain:false,quarantined:false,retired:false,epoch:1}};
const registry=new ReviewRegistry(),published=[];
const controller=new ReviewController({owner,application,registry,api:transport,
 getContext:()=>context,keyFactory:()=> 'actual-controller-lost-review',
 onPublished:v=>published.push(v)});
assert.equal(await controller.preview(),true);
const preview=controller.snapshot().preview;
assert.equal(preview.envelope.disclosure.cover_letter,input.exact);
assert.equal(preview.envelope.disclosure.answers[0].text,input.exact);
assert.equal(preview.envelope.disclosure.manual_requirements.length,1);
assert.equal(preview.envelope.disclosure.canonical_questions.find(q=>q.id==='manual').required,false);
assert.equal(await controller.record(preview.token,false),false);
assert.equal(registry.size,0);
assert.equal(await controller.record(preview.token,true),false);
assert.equal(controller.snapshot().operation.state,'uncertain');
assert.equal(registry.size,1);assert.equal(published.length,0);
// Newer writing belongs to the live buffer, not the immutable sent request.
const newer=input.exact+'newer unsaved typing 😀';
context={...context,dirty:true,writing:{...context.writing,bufferMatchesSaved:false,epoch:2}};
const capsule={schema_version:1,owner,application,baselineInspection:p,
 targets:new DraftBuffer(p).targets().map(t=>({...t,text:t.text+'newer unsaved typing 😀'}))};
let frozen=false;
assert.equal(controller.handoffUncertain({epoch:2,capsule},epoch=>{assert.equal(epoch,2);frozen=true;return true;}),true);
assert.equal(frozen,true);assert.equal(registry.blocksNavigation(owner,application),false);
controller.retire();
const remount=new ReviewController({owner,application,registry,api:transport,getContext:()=>context,
 onPublished:v=>published.push(v),keyFactory:()=>{throw new Error('Replay must not create a key');}});
assert.deepEqual(registry.recovery(owner,application),capsule);
assert.equal(await remount.replayOriginal(),true);
const result=remount.snapshot().operation;
assert.equal(result.state,'recorded');assert.equal(result.quarantined,true);
assert.equal(registry.isMutationLocked(owner,application),true);
assert.equal(result.result.current,!input.historical);
assert.equal(result.result.reviewed_current,!input.historical);
assert.equal(result.result.draft.cover_letter,input.exact);
assert.equal(result.result.draft.answers[0].text,input.exact);
assert.equal(result.result.browser_eligible,false);
assert.equal(published.length,1);assert.equal(published[0].current,!input.historical);
assert.deepEqual(registry.recovery(owner,application),capsule);
assert.equal(context.writing.epoch,2);assert.equal(context.dirty,true);
assert.equal(registry.recovery(owner,application).targets[0].text,newer);
registry.forgetOwner(owner);remount.retire();
assert.equal(registry.size,0);assert.equal(registry.recovery(owner,application),null);
console.log(JSON.stringify({done:{child:result.result.draft.id,current:result.result.current,
 sameText:result.result.text_sha256===p.text_sha256,sameLedger:result.result.ledger_sha256===p.ledger_sha256}}));
"""


def run_controller(payload, dispatch):
    node = shutil.which('node')
    assert node, 'Production controller qualification requires Node'
    process = subprocess.Popen([node, '--input-type=module', '-e', SCRIPT], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
        process.stdin.flush()
        for _ in range(8):
            assert selector.select(30), 'Node controller bridge exceeded its response deadline'
            line = process.stdout.readline()
            assert line, process.stderr.read()
            value = json.loads(line)
            if 'done' in value:
                process.stdin.close()
                assert process.wait(timeout=10) == 0, process.stderr.read()
                return value['done']
            response = dispatch(value['request'])
            process.stdin.write(json.dumps(response, ensure_ascii=False) + '\n')
            process.stdin.flush()
        pytest.fail('Controller issued unexpected additional requests')
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if not pipe.closed:
                pipe.close()


@pytest.mark.parametrize('controls', [('tech', False), ('finance', False),
                                      ('tech', True), ('finance', True)], indirect=True)
def test_actual_controller_postcommit_loss_exact_replay_quarantined_newer_writing(
        controls, tmp_path, monkeypatch):
    configured, parent, historical, sector = controls
    service, scope, _, sent, _ = configured
    api = api_for(service, tmp_path)
    original = api.state.drafting.detail
    detail_calls = []

    def lost(*args):
        detail_calls.append(args[-1])
        if len(detail_calls) == 1:
            raise RuntimeError('Synthetic HTTP response lost AFTER committed review')
        return original(*args)

    monkeypatch.setattr(api.state.drafting, 'detail', lost)
    parent_dto = service.detail(scope.profile_id, scope.application_id, parent.id)
    assert service.domain.application(scope.profile_id, scope.application_id).sector == sector
    assert parent_dto['review_eligible'], parent_dto['reason_codes']
    assert parent.cover_letter == EXACT
    before = rows(service)
    requests = []
    with TestClient(api, base_url='http://127.0.0.1:3001', raise_server_exceptions=False) as client:
        authorize(client)

        def dispatch(request):
            requests.append(request)
            # Advance through a real successor generation before exact replay.
            if historical and len(requests) == 3:
                execute(configured, key='actual-controller-successor')
            response = client.post(request['path'], json=request['options']['body'])
            if len(requests) == 2:
                assert response.status_code == 500, response.text
                assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output + 1
                assert rows(service) == before
            else:
                assert response.status_code in (200, 201), response.text
            return {'error': 'Actual HTTP 500 after commit'} if response.status_code == 500 else {'value': response.json()}

        result = run_controller(dict(parent=parent_dto, exact=EXACT, historical=historical), dispatch)
        assert len(requests) == 3
        assert requests[1] == requests[2]  # Exact path, key, body, acknowledgment.
        assert requests[1]['options']['body']['acknowledged'] is True
        assert result['sameText'] and result['sameLedger']
        assert result['current'] is not historical
        child = result['child']
        exported = client.get(f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}/drafts/{child}/export')
        assert exported.status_code == 200 and EXACT in exported.text
    with service.store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='workspace:draft_review'").fetchone() == (1,)
    assert detail_calls == [child, child, child]  # Lost response, replay, export inspection.
    assert len(sent) == (6 if historical else 3)
    if not historical:
        assert rows(service) == before
    assert service.domain.application(scope.profile_id, scope.application_id).output_revision == parent.revisions.application_output + (2 if historical else 1)
