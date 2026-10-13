"""Actual synthetic HTTP DTOs consumed by production workspace JavaScript.

Node uses stdin, not generated fixture files. This is DTO/controller integration,
not a real browser, independent canonical hash proof or model-quality evaluation.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_drafting_assessment import assess
from test_drafting_review_api import api_for, authorize, url_for
from test_drafting_reviews import preview_body, reviewing  # noqa: F401
from test_drafting_service import body, capture_draft_launch, execute
from test_packet_service import packets  # noqa: F401
from test_scoped_retrieval import runtime  # noqa: F401

from copilot.domain import contracts as c
from copilot.domain.repository import ConsentInput
from copilot.drafting.runtime_contracts import DraftPreviewRequest

ROOT = Path(__file__).resolve().parents[2]


def node(script, payload):
    executable = shutil.which('node')
    assert executable, 'Node is required to verify actual workspace DTO compatibility'
    result = subprocess.run([executable, '--input-type=module', '-e', script],
        input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True,
        cwd=ROOT, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


IMPORTS = """
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {DraftBuffer, DraftingController} from './backend/ui/workspace/drafting.js';
import {PaidLaunchController, PaidLaunchRegistry} from './backend/ui/workspace/paid-launch.js';
const input = JSON.parse(readFileSync(0, 'utf8'));
"""


@pytest.mark.parametrize('reviewing', [None, 'optional_manual'], indirect=True)
def test_actual_review_http_dtos_workspace_edit_assessment_and_paid_separation(reviewing, tmp_path):  # noqa: F811
    service, scope, _, sent, settings = reviewing
    exact = '  Café e\u0301 😀\n\tA\r\n '
    settings['text'] = exact
    with service.store._read() as db:
        consent = service.domain._singleton(db, scope.profile_id, 'consent', c.ConsentRecord)
    service.domain.consent(scope.profile_id, ConsentInput(expected_consent_revision=consent.revision,
        provider='openai', purposes=('research', 'drafting', 'assessment'), granted=True))
    request = body(reviewing)
    request_fields = request.model_dump(mode='json', exclude={
        'idempotency_key', 'disclosure_sha256', 'acknowledged'})
    preview = service.preview(scope.profile_id, scope.application_id,
        DraftPreviewRequest.model_validate_json(json.dumps(request_fields)))
    launch = capture_draft_launch(reviewing, request)
    admission = service.admission_state(scope.profile_id, scope.application_id)
    packet = service.packets.detail(scope.profile_id, scope.application_id, request.batch_id)
    api = api_for(service, tmp_path)
    with TestClient(api, base_url='http://127.0.0.1:3001') as client:
        authorize(client)
        base = f'/api/workspace/profiles/{scope.profile_id}/applications/{scope.application_id}'
        state = client.get(base + '/drafting-state').json()
        generated, job, _ = execute(reviewing, launch=launch)
        generation_response = client.get(base + f'/drafts/{generated.id}')
        assert generation_response.status_code == 200, generation_response.text
        generation = generation_response.json()
        review_url = url_for(scope, generated)
        review_preview = client.post(review_url + '/preview',
            json=preview_body(service, scope, generated).model_dump(mode='json'))
        assert review_preview.status_code == 200, review_preview.text
        review_request = dict(preview_body(service, scope, generated).model_dump(mode='json'),
            idempotency_key='ui-review', acknowledged=True,
            disclosure_sha256=review_preview.json()['disclosure_sha256'])
        response = client.post(review_url, json=review_request)
        assert response.status_code == 201, response.text
        review = response.json()
        detail = client.get(base + '/drafts/' + review['draft']['id'])
        assert detail.status_code == 200 and detail.json() == review
        exported = client.get(base + '/drafts/' + review['draft']['id'] + '/export')
        assert exported.status_code == 200 and exact in exported.text
        job_response = client.get(f'/api/workspace/profiles/{scope.profile_id}/jobs/{job.id}')
        assert job_response.status_code == 200, job_response.text
        capture = dict(kind='generation', owner=scope.profile_id, application=scope.application_id,
            admission=admission, request=request_fields, disclosure=preview['disclosure'],
            disclosure_sha256=preview['disclosure_sha256'], state=state,
            packetInspection=packet, draftInspection=None)
        edit_request = node(IMPORTS + """
const v=input.review, b=new DraftBuffer(v), updates=[];
assert.equal(new DraftBuffer(input.generation).targets()[0].text,input.exact);
assert.equal(b.targets()[0].text,input.exact);
const c=new DraftingController({owner:v.draft.profile_id,application:v.draft.application_id,
 api:async()=>v,exportText:async()=>input.exported,update:(...args)=>updates.push(args)});
await c.draft(v.draft.id);await c.exportDraft(v.draft.id);
assert.equal(updates[0][2],null);assert.equal(updates[1][1],input.exported);
assert.equal(updates.length,2); // Inspection/export never issue an automatic edit.
const req=b.editRequest('ui-edit');
assert.deepEqual(req.expected_revisions,v.draft.revisions);
assert.equal(req.draft.targets[0].text,input.exact);
for(const malicious of [false,true]) {
 const r=structuredClone(v),registry=new PaidLaunchRegistry(),published=[];
 if(malicious)Object.assign(r.publication,{job_id:input.job.job.id,intent_id:input.generation.publication.intent_id,fence:input.job.job.fence});
 const paid=new PaidLaunchController({owner:v.draft.profile_id,application:v.draft.application_id,
 registry,keyFactory:()=> 'synthetic-original',onPublished:x=>published.push(x),
 api:async(path,opt)=>opt?.method==='POST'?input.job:path.endsWith('/drafts')?{schema_version:1,drafts:[r]}:path.includes('/drafts/')?r:input.job});
 assert.equal(await paid.launch(input.capture,true),true);
 assert.equal(await paid.inspectKnownResult(),false);
 assert.equal(published.length,0);assert.equal(paid.snapshot().resultDraftId,null);
 assert.equal(registry.size,1); // Rejected review never resolves paid uncertainty.
}
console.log(JSON.stringify(req));
""", dict(review=review, generation=generation, exact=exact, exported=exported.text,
            capture=capture, job=job_response.json()))
        edited_response = client.post(base + '/drafts/' + review['draft']['id'] + '/edits',
            json=edit_request)
        assert edited_response.status_code == 201, edited_response.text
        edited = edited_response.json()
        assert edited['review_snapshot'] is None and not edited['reviewed_current']
        assert edited['draft']['answers'][0]['text'] == exact
        with service.store._read() as db:
            edit_model = service.domain._get(db, scope.profile_id, edited['draft']['id'], 'draft', c.DraftRevision)
        assessed, *_ = assess((service, scope, edit_model, sent, settings))
        assessed_response = client.get(base + f'/drafts/{assessed.id}')
        assert assessed_response.status_code == 200, assessed_response.text
        stale_response = client.get(base + '/drafts/' + review['draft']['id'])
        assert stale_response.status_code == 200, stale_response.text
        final = node(IMPORTS + """
assert.equal(new DraftBuffer(input.edit).targets()[0].text,input.exact);
const updates=[],c=new DraftingController({owner:input.review.draft.profile_id,application:input.review.draft.application_id,
 api:async()=>input.edit,update:(...args)=>updates.push(args)});
c.adoptDraft(input.review.draft.id);await c.saveEdit(input.review,input.editRequest);
assert.equal(updates.length,1);assert.equal(updates[0][2],null);
assert.equal(updates[0][1].published.draftId,input.edit.draft.id);
assert.equal(new DraftBuffer(input.assessment).targets()[0].text,input.exact);
assert.equal(input.edit.review_snapshot,null);assert.equal(input.edit.reviewed_current,false);
assert.equal(input.assessment.review_snapshot,null);assert.equal(input.assessment.reviewed_current,false);
assert.equal(input.stale.current,false);assert.equal(input.stale.reviewed_current,false);
const stale=new DraftBuffer(input.stale);assert.equal(stale.targets()[0].text,input.exact);
assert.throws(()=>stale.editRequest('stale-edit'));
console.log(JSON.stringify({verified:true}));
""", dict(review=review, editRequest=edit_request, edit=edited,
            assessment=assessed_response.json(), stale=stale_response.json(), exact=exact))
        assert final == {'verified': True}
    assert len(sent) == 4  # Three synthetic generation calls + one synthetic assessment.
