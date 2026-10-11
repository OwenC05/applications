import test from 'node:test';
import assert from 'node:assert/strict';
import {DraftBuffer, DraftingController} from '../backend/ui/workspace/drafting.js';
const copy = value => structuredClone(value);
const hash = 'a'.repeat(64);
const vector = {schema_version: 1, metadata: 1, facts: 2, documents: 3, consent: 4, application_input: 5, application_output: 6, research: 7};
function inspection() {
  return {schema_version: 1, current: true, text_sha256: hash, ledger_sha256: hash, publication_sha256: hash,
    publication: {id: 'pub', profile_id: 'owner', application_id: 'app', draft_id: 'parent', text_sha256: hash, ledger_sha256: hash},
    draft: {schema_version: 1, id: 'parent', profile_id: 'owner', application_id: 'app', revisions: copy(vector), research_run_id: 'run', answers: [{schema_version: 1, question_id: 'q', packet_id: 'p', text: 'a\r\né😀 '}], cover_letter: '', ledger: [], assessment_state: 'assessed', inventory_complete: true},
    writing_targets: [{question: {schema_version: 1, id: 'q', text: 'Why?', type: 'writing', required: true, max_words: 200, max_chars: null, options: [], constraint_origin: 'user'}, packet_id: 'p'}, {question: {schema_version: 1, id: 'cover_letter', text: 'Cover', type: 'writing', required: false, max_words: 400, max_chars: null, options: [], constraint_origin: 'user'}, packet_id: 'pc'}]};
}
function accepted(parent, body) {
  const child = copy(parent); child.draft.id = 'child'; child.draft.revisions.application_output++;
  child.draft.answers[0].text = body.draft.targets[0].text; child.draft.cover_letter = body.draft.targets[1].text;
  child.draft.assessment_state = 'unassessed'; child.draft.inventory_complete = false; child.draft.ledger = [];
  child.publication = {id: 'pub-child', origin: 'edit', profile_id: 'owner', application_id: 'app', draft_id: 'child', parent_draft_id: 'parent', parent_text_sha256: hash, parent_ledger_sha256: hash, parent_publication_id: 'pub', parent_publication_sha256: hash, pre_revisions: copy(vector), request: copy(body), text_sha256: hash, ledger_sha256: hash};
  return child;
}
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject}; };
test('buffer preserves untouched exact bytes, canonical order and copied full request', () => {
  const dto = inspection(), b = new DraftBuffer(dto); dto.draft.answers[0].text='mutated';
  assert.deepEqual(b.targets(), [{target:'q',text:'a\r\né😀 '},{target:'cover_letter',text:''}]);
  assert.equal(b.matchesSaved(), true); b.input('cover_letter','new'); assert.equal(b.matchesSaved(),false);
  const req=b.editRequest('key'); assert.deepEqual(req.expected_revisions,vector); assert.equal(req.draft.schema_version,1); assert.equal(req.draft.targets[0].schema_version,1);
  req.draft.targets[0].text='bad'; assert.equal(b.targets()[0].text,'a\r\né😀 ');
  b.input('cover_letter',''); assert.equal(b.matchesSaved(),true); assert.throws(()=>b.input('unknown','x'));
});
for (const mutate of [x=>delete x.writing_targets, x=>x.writing_targets.reverse(), x=>x.draft.answers[0].packet_id='wrong',x=>x.draft.answers.push(copy(x.draft.answers[0])),x=>delete x.draft.revisions.consent,x=>x.draft.revisions.facts=true,x=>x.publication.draft_id='wrong',x=>x.publication_sha256='bad',x=>x.draft.cover_letter=3]) {
  test('buffer rejects inconsistent authoritative DTO',()=> {const dto=inspection();mutate(dto);assert.throws(()=>new DraftBuffer(dto));});
}
test('controller encoded routes and terminal retirement',async()=> {
  const calls=[], updates=[]; const c=new DraftingController({api:async(path,options)=>{calls.push([path,options]);return {};},exportText:async path=>{calls.push([path]);return 'saved';},owner:'a/b',application:'c/d',update:(...a)=>updates.push(a)});
  await c.packets();await c.packet('b/b');await c.packetQuestion('b/b','q/q');await c.drafts();await c.draft('d/d');await c.exportDraft('d/d');
  const base='/api/workspace/profiles/a%2Fb/applications/c%2Fd';
  assert.deepEqual(calls.map(x=>x[0]),[`${base}/packets`,`${base}/packets/b%2Fb`,`${base}/packets/b%2Fb/questions/q%2Fq`,`${base}/drafts`,`${base}/drafts/d%2Fd`,`${base}/drafts/d%2Fd/export`]);
  c.retire();await c.packets();await c.draft('x');await c.exportDraft('x');assert.equal(calls.length,6);
});
for (const method of ['packets','packet','packetQuestion','drafts','draft','exportDraft']) for (const failure of [false,true]) {
 test(`${method} suppresses reordered and retired ${failure?'errors':'successes'}`,async()=>{
  const pending=[],updates=[];const io=()=>{const d=deferred();pending.push(d);return d.promise;};
  const c=new DraftingController({api:io,exportText:io,owner:'owner',application:'app',update:(...a)=>updates.push(a)});
  const invoke=()=>c[method]('same','q');const first=invoke(),second=invoke();const latest=method==='draft'?{...inspection(),draft:{...inspection().draft,id:'same'},publication:{...inspection().publication,draft_id:'same'},new:true}:{new:true};pending[1].resolve(latest);await second;
  failure?pending[0].reject(new Error('old')):pending[0].resolve({old:true});await first;assert.equal(updates.length,1);
  const retired=invoke();c.retire();failure?pending[2].reject(new Error('retired')):pending[2].resolve({});await retired;assert.equal(updates.length,1);
 });
}
test('edit captures immutable operation and correlates exact child without auto-adoption',async()=>{
 const parent=inspection(),body=new DraftBuffer(parent).editRequest('key'),pending=deferred(),updates=[];
 const c=new DraftingController({api:()=>pending.promise,owner:'owner',application:'app',update:(...a)=>updates.push(a)});c.adoptDraft('parent');
 const saved=accepted(parent,body),promise=c.saveEdit(parent,body);parent.draft.id='changed';body.idempotency_key='changed';pending.resolve(saved);await promise;
 const data=updates[0][1];assert.equal(data.operation.parentDraftId,'parent');assert.equal(data.operation.body.idempotency_key,'key');assert.deepEqual(data.published,{owner:'owner',application:'app',draftId:'child',preRevisions:vector,postRevisions:{...vector,application_output:7},current:true});
});
for (const mutate of [x=>x.draft.profile_id='other',x=>x.publication.parent_draft_id='other',x=>x.publication.request.idempotency_key='other',x=>x.draft.revisions.facts++,x=>x.draft.answers[0].packet_id='other',x=>x.draft.assessment_state='assessed',x=>x.draft.inventory_complete=true,x=>x.draft.ledger.push({}),x=>x.writing_targets.reverse(),x=>x.draft.cover_letter='different']) {
 test('mismatched edit success remains uncertain with original operation',async()=>{const p=inspection(),b=new DraftBuffer(p).editRequest('key'),r=accepted(p,b);mutate(r);const updates=[];const c=new DraftingController({api:async()=>r,owner:'owner',application:'app',update:(...a)=>updates.push(a)});c.adoptDraft('parent');await c.saveEdit(p,b);assert.equal(updates[0][2].uncertain,true);assert.deepEqual(updates[0][1].operation.body,b);});
}
for (const status of [409,500,undefined]) test(`failed edit preserves status ${status} and exact replay body`,async()=>{const p=inspection(),b=new DraftBuffer(p).editRequest('key'),updates=[],calls=[];const error=Object.assign(new Error('failed'),{status,code:'CONFLICT'});const c=new DraftingController({api:async(path,options)=>{calls.push(options.body);if(calls.length===1)throw error;return accepted(p,b);},owner:'owner',application:'app',update:(...a)=>updates.push(a)});c.adoptDraft('parent');await c.saveEdit(p,b);assert.equal(updates[0][2],error);const op=updates[0][1].operation;await c.saveEdit(op.parentCapture,op.body);assert.deepEqual(calls[0],calls[1]);assert.equal(updates[1][2],null);});
for(const lateFailure of [false,true]) test('cross-slot old and during-save reads cannot restore baseline',async()=>{
 const pending=[],updates=[];const c=new DraftingController({api:()=>{const d=deferred();pending.push(d);return d.promise;},exportText:()=>{const d=deferred();pending.push(d);return d.promise;},owner:'owner',application:'app',update:(...a)=>updates.push(a)});
 const p=inspection(),b=new DraftBuffer(p).editRequest('k');const detail=c.draft('parent'),ex=c.exportDraft('parent'),save=c.saveEdit(p,b),refresh=c.draft('parent');
 pending[2].resolve(accepted(p,b));await save;
 for(const [i,promise] of [[0,detail],[1,ex],[3,refresh]]) {lateFailure?pending[i].reject(new Error('old')):pending[i].resolve({old:true});await promise;}
 assert.deepEqual(updates.map(x=>x[0]),['edit']);
});
test('same-ID detail does not cancel edit; changed selection does',async()=>{
 const pending=[],updates=[];const c=new DraftingController({api:()=>{const d=deferred();pending.push(d);return d.promise;},owner:'owner',application:'app',update:(...a)=>updates.push(a)});const p=inspection(),b=new DraftBuffer(p).editRequest('k');c.adoptDraft('parent');const save=c.saveEdit(p,b),read=c.draft('parent');pending[0].resolve(accepted(p,b));await save;pending[1].resolve(p);await read;assert.deepEqual(updates.map(x=>x[0]),['edit']);
 const old=c.saveEdit(p,b),other=c.draft('other');pending[2].resolve(accepted(p,b));await old;pending[3].resolve({...p,draft:{...p.draft,id:'other'},publication:{...p.publication,draft_id:'other'}});await other;assert.deepEqual(updates.map(x=>x[0]),['edit','draft-detail']);assert.equal(updates[1][2],null);
});
for(const failure of [false,true]) test('packet changes retire child detail/question tickets only',async()=>{
 const pending=[],updates=[];const c=new DraftingController({api:()=>{const d=deferred();pending.push(d);return d.promise;},owner:'owner',application:'app',update:(...a)=>updates.push(a)});
 const question=c.packetQuestion('old','q'),detail=c.packet('old'),draft=c.draft('parent'),next=c.packet('new');
 for(const [index,promise] of [[0,question],[1,detail]]){failure?pending[index].reject(new Error('old')):pending[index].resolve({old:true});await promise;}
 pending[2].resolve(inspection());await draft;pending[3].resolve({packet:true});await next;assert.deepEqual(updates.map(x=>x[0]),['draft-detail','packet-detail']);
});
test('adoption suppresses late same-ID reads and stale successful edit carries no authority',async()=>{
 const pending=[],updates=[];const c=new DraftingController({api:()=>{const d=deferred();pending.push(d);return d.promise;},owner:'owner',application:'app',update:(...a)=>updates.push(a)});
 const old=c.draft('parent');c.adoptDraft('parent');pending[0].resolve(inspection());await old;assert.equal(updates.length,0);
 const p=inspection(),b=new DraftBuffer(p).editRequest('k'),response=accepted(p,b);response.current=false;const save=c.saveEdit(p,b);pending[1].resolve(response);await save;assert.equal(updates[0][1].published.current,false);assert.throws(()=>new DraftBuffer(response).editRequest('next'));
});
test('controller rejects tampered request locally and never sends paid or unsupported routes',async()=>{
 const p=inspection(),b=new DraftBuffer(p).editRequest('key'),calls=[],updates=[];b.expected_revisions.facts++;
 const c=new DraftingController({api:async(...args)=>{calls.push(args);},owner:'owner',application:'app',update:(...a)=>updates.push(a)});await c.saveEdit(p,b);assert.equal(calls.length,0);assert.ok(updates[0][2]);assert.equal(updates[0][1].operation.body.expected_revisions.facts,3);
});
test('read/update data cannot mutate edit correlation capture',async()=>{
 const p=inspection(),b=new DraftBuffer(p).editRequest('k'),pending=deferred(),updates=[];const c=new DraftingController({api:async(path,options)=>{options.body.idempotency_key='transport mutation';return pending.promise;},owner:'owner',application:'app',update:(...a)=>updates.push(a)});
 const save=c.saveEdit(p,b);pending.resolve(accepted(p,b));await save;assert.equal(updates[0][2],null);assert.equal(updates[0][1].operation.body.idempotency_key,'k');
});
for(const thrown of ['lost response',Object.freeze(Object.assign(new Error('frozen'),{status:500,code:'UNAVAILABLE'}))]) test('non-extensible transport failures retain uncertainty instead of rejecting handler',async()=>{
 const updates=[],p=inspection(),b=new DraftBuffer(p).editRequest('k');const c=new DraftingController({api:async()=>{throw thrown;},owner:'owner',application:'app',update:(...a)=>updates.push(a)});await c.saveEdit(p,b);assert.equal(updates[0][2].uncertain,true);assert.equal(updates[0][2].status,thrown?.status);assert.deepEqual(updates[0][1].operation.body,b);
});
for (const failure of [false, true]) for (const view of ['packet', 'packetQuestion', 'draft']) test(`${view} empty selection retires pending ${failure ? 'error' : 'success'} without IO`, async () => {
  const calls = [], pending = [], updates = [];
  const io = path => { calls.push(path); const d = deferred(); pending.push(d); return d.promise; };
  const c = new DraftingController({api: io, exportText: io, owner: 'owner', application: 'app', update: (...args) => updates.push(args)});
  const request = view === 'packetQuestion' ? c.packetQuestion('batch', 'q') : c[view]('selected');
  const extras = view === 'packet' ? [c.packetQuestion('selected', 'q')]
    : view === 'draft' ? [c.exportDraft('selected'), c.saveEdit({...inspection(), draft: {...inspection().draft, id: 'selected'}, publication: {...inspection().publication, draft_id: 'selected'}}, new DraftBuffer(inspection()).editRequest('key'))] : [];
  const count = calls.length;
  const cleared = view === 'packetQuestion' ? c.packetQuestion('batch', '') : c[view]('');
  assert.equal(calls.length, count, 'clearing selection must not issue a trailing-route request');
  await cleared;
  for (const d of pending) failure ? d.reject(new Error('stale selected content')) : d.resolve({stale: true});
  await Promise.all([request, ...extras]);
  assert.deepEqual(updates, []);
});
for (const mutate of [x=>{x.draft.id='other';x.publication.draft_id='other';},x=>{x.draft.profile_id='other';x.publication.profile_id='other';},x=>{x.draft.application_id='other';x.publication.application_id='other';},x=>delete x.writing_targets,x=>delete x.draft]) test('selected draft detail rejects wrong identity/scope or malformed authoritative DTO',async()=>{
  const dto=inspection(),updates=[];mutate(dto);
  const c=new DraftingController({api:async()=>dto,owner:'owner',application:'app',update:(...args)=>updates.push(args)});
  await c.draft('parent');assert.equal(updates.length,1);assert.equal(updates[0][0],'draft-detail');assert.equal(updates[0][1],null);assert.ok(updates[0][2] instanceof Error);
});
test('correct selected draft detail forwards full immutable authority without adopting another baseline',async()=>{
 const dto=inspection(),updates=[];const c=new DraftingController({api:async()=>dto,owner:'owner',application:'app',update:(...args)=>updates.push(args)});
 await c.draft('parent');assert.deepEqual(updates,[['draft-detail',dto,null]]);
});
test('rejected wrong-ID inspection preserves selected parent and permits exact intended local save',async()=>{
 const p=inspection(),b=new DraftBuffer(p).editRequest('k'),wrong=copy(p),updates=[],calls=[];wrong.draft.id='wrong';wrong.publication.draft_id='wrong';
 const c=new DraftingController({api:async(path,options)=>{calls.push([path,options]);return options?accepted(p,b):wrong;},owner:'owner',application:'app',update:(...args)=>updates.push(args)});
 await c.draft('parent');await c.saveEdit(p,b);assert.equal(updates[0][1],null);assert.ok(updates[0][2]);assert.equal(updates[1][2],null);assert.equal(updates[1][1].published.draftId,'child');assert.ok(calls[1][0].endsWith('/parent/edits'));
});
