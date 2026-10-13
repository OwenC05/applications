import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {ReviewRegistry} from '../backend/ui/workspace/review.js';
import {reviewPane} from '../backend/ui/workspace/review-pane.js';

// Synthetic DOM checks only: not browser, caret, layout or accessibility proof.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; this.checked = false; this.disabled = false; }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return (this.content ?? '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this[key] = value; }
  click() { return this.onclick?.(); }
}
const walk = root => [root, ...root.children.flatMap(walk)];
const button = (root, label) => walk(root).find(x => x.tagName === 'button' && x.textContent === label);
const ack = root => walk(root).find(x => x.type === 'checkbox');
const inspect = 'Inspect writing for human review', record = 'Record my writing review';
function harness(options = {}) {
  const previous = globalThis.document;
  globalThis.document = {createElement: tag => new Element(tag)};
  const calls = [];
  const pane = reviewPane({owner: '11111111-1111-4111-8111-111111111111', application: '22222222-2222-4222-8222-222222222222', api: async (...args) => { calls.push(args); throw new Error('No transport expected'); }, getContext: () => ({owner: '11111111-1111-4111-8111-111111111111', application: '22222222-2222-4222-8222-222222222222', writing: null, dirty: false, busy: false, competingMutationLocked: false}), ...options});
  return {pane, calls, restore() { pane.retire(); globalThis.document = previous; }};
}
test('construction is local, stable, separately acknowledged and accessible', () => {
  const h = harness(); try {
    assert.equal(h.calls.length, 0);
    assert.ok(button(h.pane.element, inspect)); assert.ok(button(h.pane.element, record));
    assert.equal(ack(h.pane.element).checked, false); assert.equal(button(h.pane.element, record).disabled, true);
    assert.match(h.pane.element.textContent, /not.*facts|not fact/i);
    assert.ok(walk(h.pane.element).some(x => x.role === 'status' && x['aria-live'] === 'polite'));
    for (const x of walk(h.pane.element).filter(x => x.tagName === 'button')) {
      assert.equal(x.type, 'button'); assert.ok(x['aria-describedby']);
      assert.ok(x['aria-describedby'].split(' ').every(id => walk(h.pane.element).some(reason => reason.id === id)));
    }
  } finally { h.restore(); }
});
test('checkbox alone never invokes transport and invalidation resets same checkbox', () => {
  const h = harness(); try {
    const checkbox = ack(h.pane.element); checkbox.checked = true; checkbox.onchange();
    assert.equal(h.calls.length, 0); h.pane.invalidate();
    assert.equal(ack(h.pane.element), checkbox); assert.equal(checkbox.checked, false);
    h.pane.syncLocks(); assert.equal(h.calls.length, 0);
  } finally { h.restore(); }
});
test('retirement clears detached private DOM and retained callbacks cannot transport', async () => {
  const h = harness(); try {
    const checkbox = ack(h.pane.element), action = button(h.pane.element, inspect).onclick;
    checkbox.checked = true; h.pane.retire(); h.pane.retire(); h.pane.invalidate(); h.pane.syncLocks(); await action();
    assert.equal(h.pane.element.textContent, ''); assert.equal(checkbox.checked, false); assert.equal(checkbox.onchange, null); assert.equal(h.calls.length, 0);
  } finally { h.restore(); }
});

// Independent local copy of agreed synthetic strict DTO.
const copy = structuredClone;
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const canonical = v => JSON.stringify(v, (_k, x) => x && typeof x === 'object' && !Array.isArray(x) ? Object.fromEntries(Object.keys(x).sort().map(k => [k, x[k]])) : x);
const hash = x => createHash('sha256').update(canonical(x)).digest('hex');
const vector = {schema_version:1,metadata:1,facts:2,documents:3,consent:4,application_input:5,application_output:6,research:7};
const question = {schema_version:1,id:'q',text:'Why?',type:'writing',required:true,max_words:200,max_chars:null,options:[],constraint_origin:'user'};
function inspection() {
 const text='Exact é 😀\r\ntext ', textHash=hash({cover_letter:'',answers:[{question_id:'q',text}]}), ledgerHash=hash([]), prior='a'.repeat(64), post={...vector,application_output:7};
 const request={schema_version:1,expected_revisions:copy(vector),parent_text_sha256:textHash,parent_ledger_sha256:ledgerHash,parent_publication_sha256:prior,idempotency_key:'review-key',disclosure_sha256:'',acknowledged:true};
 const disclosure={schema_version:1,profile_id:id(1),application_id:id(2),operation:'human_writing_review',parent_draft_id:id(3),parent_publication_id:id(4),parent_publication_sha256:prior,batch_id:id(5),batch_sha256:prior,research_run_id:id(6),pre_revisions:copy(vector),post_revisions:post,text_sha256:textHash,ledger_sha256:ledgerHash,cover_letter:'',answers:[{schema_version:1,question_id:'q',text,packet_id:id(7)}],ledger:[],canonical_questions:[copy(question)],cover_letter_target:null,manual_requirements:[],style_notes:['Style is advisory'],warnings:['WRITING_ONLY','AUTOMATED_SUPPORT_FALLIBLE','NOT_FACT_CONFIRMATION','MANUAL_AND_ATTACHMENTS_NOT_REVIEWED','NO_BROWSER_AUTHORIZATION']};
 request.disclosure_sha256=hash(disclosure);
 const snapshot={schema_version:1,profile_id:id(1),application_id:id(2),id:id(8),draft_id:id(9),text_sha256:textHash,ledger_sha256:ledgerHash,revisions:post,research_run_id:id(6),disclosure_sha256:request.disclosure_sha256,reviewed_at:'2026-10-12T11:00:00Z'};
 const publication={schema_version:1,profile_id:id(1),application_id:id(2),origin:'review',id:id(10),draft_id:id(9),parent_draft_id:id(3),parent_text_sha256:textHash,parent_ledger_sha256:ledgerHash,parent_publication_id:id(4),parent_publication_sha256:prior,batch_id:id(5),batch_sha256:prior,pre_revisions:copy(vector),edit_depth:0,created_at:snapshot.reviewed_at,request,request_sha256:hash({operation:'human_writing_review',profile_id:id(1),application_id:id(2),parent_draft_id:id(3),request}),disclosure,review_snapshot_id:snapshot.id,review_snapshot_sha256:hash(snapshot),text_sha256:textHash,ledger_sha256:ledgerHash,reason_codes:[],style_notes:copy(disclosure.style_notes)};
 return {schema_version:1,draft:{schema_version:1,profile_id:id(1),application_id:id(2),id:id(9),revisions:post,research_run_id:id(6),cover_letter:'',answers:copy(disclosure.answers),ledger:[],assessment_state:'assessed',inventory_complete:true,created_at:snapshot.reviewed_at},publication,publication_sha256:hash(publication),text_sha256:textHash,ledger_sha256:ledgerHash,current:true,reason_codes:[],canonical_questions:[copy(question)],writing_targets:[{question:copy(question),packet_id:id(7)}],manual_requirements:[],semantic_assessment:'fallible_not_release_qualified',writing_review_ready:true,reviewed_current:true,review_snapshot:snapshot,review_eligible:false,browser_eligible:false,capabilities:{generation:true,edit:true,reassess:true,review:true}};
}
function readyHarness({response, childFactory = inspection, ...options} = {}) {
  const child = childFactory(), disclosure = copy(child.publication.disclosure), parent = copy(child);
  parent.draft.id = disclosure.parent_draft_id; parent.draft.revisions = copy(vector);
  parent.publication = {schema_version:1,id:disclosure.parent_publication_id,draft_id:parent.draft.id,profile_id:id(1),application_id:id(2),batch_id:disclosure.batch_id,batch_sha256:disclosure.batch_sha256,text_sha256:parent.text_sha256,ledger_sha256:parent.ledger_sha256,style_notes:copy(disclosure.style_notes)};
  parent.publication_sha256 = disclosure.parent_publication_sha256; parent.review_snapshot = null; parent.reviewed_current = false; parent.review_eligible = true;
  const context = {owner:id(1),application:id(2),writing:{epoch:1,draftInspection:parent,bufferMatchesSaved:true,saving:false,uncertain:false,quarantined:false,retired:false},dirty:false,busy:false,competingMutationLocked:false};
  const envelope = {schema_version:1,disclosure,disclosure_sha256:child.publication.request.disclosure_sha256};
  const calls = [], selections = [], registry = new ReviewRegistry();
  const h = harness({owner:id(1),application:id(2),registry,keyFactory:()=> 'review-key',getContext:()=>copy(context),selectResult:async (...args)=>{selections.push(args);return true;},api:async(path,opt)=>{calls.push([path,copy(opt)]); return response ? response(path,opt,{child,envelope,context}) : copy(path.endsWith('/preview') ? envelope : child);},...options});
  return {...h,context,child,envelope,calls,selections,registry};
}
const select = 'Inspect recorded revision';
const resend = 'Resend exact review request to resolve outcome (may record it now)';
const handoff = 'Keep unresolved review and writing for read-only navigation';
async function previewAndAck(h) {
  await button(h.pane.element,inspect).click();
  const checkbox=ack(h.pane.element); assert.equal(checkbox.checked,false); assert.equal(checkbox.disabled,false);
  checkbox.checked=true;checkbox.onchange();assert.equal(button(h.pane.element,record).disabled,false);
}
test('complete exact preview renders preserved Unicode strings, limits and ordered warnings',async()=>{
 const h=readyHarness();try {
  await button(h.pane.element,inspect).click();
  assert.ok(walk(h.pane.element).some(x=>x.tagName==='pre'&&x.textContent==='Exact é 😀\r\ntext '));
  assert.match(h.pane.element.textContent,/Cover letter: not configured/);
  assert.match(h.pane.element.textContent,/Canonical question and limits: q/);
  assert.match(h.pane.element.textContent,/Style is advisory/);
  const warnings=walk(h.pane.element).find(x=>x.tagName==='ol');assert.equal(warnings.children.length,5);
  assert.match(warnings.children[0].textContent,/Writing only/);assert.match(warnings.children[4].textContent,/No browser authorization/);
  assert.equal(h.calls.length,1);assert.equal(h.selections.length,0);
 }finally{h.restore();}
});
test('record needs separate acknowledgement; successful review never selects or replaces writing',async()=>{
 const h=readyHarness();try {
  await button(h.pane.element,inspect).click();await button(h.pane.element,record).click();assert.equal(h.calls.length,1);
  ack(h.pane.element).checked=true;ack(h.pane.element).onchange();await button(h.pane.element,record).click();
  assert.equal(h.calls.length,2);assert.equal(ack(h.pane.element).checked,false);assert.equal(h.selections.length,0);
  assert.match(h.pane.element.textContent,/Writing review recorded for saved revision/);assert.match(h.pane.element.textContent,/editor was not replaced/);
  assert.equal(h.registry.isMutationLocked(id(1),id(2)),false);
  await button(h.pane.element,select).click();assert.deepEqual(h.selections,[[id(9),{readOnly:false}]]);
 }finally{h.restore();}
});
test('uncertain outcome retains explicit exact replay, never a fresh acknowledgement or adoption',async()=>{
 let attempts=0;const h=readyHarness({response:(path,opt,{child,envelope})=>{if(path.endsWith('/preview'))return copy(envelope);if(++attempts===1)throw Error('lost');child.current=child.reviewed_current=child.writing_review_ready=false;return copy(child);}});try {
  await previewAndAck(h);await button(h.pane.element,record).click();
  assert.equal(ack(h.pane.element).checked,false);assert.match(h.pane.element.textContent,/not proof of rollback/);
  assert.equal(button(h.pane.element,resend).hidden,false);assert.equal(button(h.pane.element,inspect).disabled,true);
  await button(h.pane.element,resend).click();assert.deepEqual(h.calls[1],h.calls[2]);
  assert.match(h.pane.element.textContent,/Historical saved revision/);assert.equal(h.selections.length,0);
 }finally{h.restore();}
});
test('context invalidation retires acknowledgement and late preview cannot restore private content',async()=>{
 let finish;const h=readyHarness({response:(_p,_o,{envelope})=>new Promise(resolve=>{finish=()=>resolve(copy(envelope));})});try{
  const pending=button(h.pane.element,inspect).click();h.context.owner=id(11);h.pane.invalidate();finish();await pending;
  assert.equal(ack(h.pane.element).checked,false);assert.equal(ack(h.pane.element).disabled,true);
  assert.ok(!h.pane.element.textContent.includes('Exact é'));assert.equal(h.calls.length,1);
 }finally{h.restore();}
});
for(const field of ['dirty','busy','competingMutationLocked'])test(`root ${field} guard denies requests and explains the lock`,async()=>{
 const h=readyHarness();try{h.context[field]=true;h.pane.syncLocks();await button(h.pane.element,inspect).click();assert.equal(h.calls.length,0);assert.equal(button(h.pane.element,inspect).disabled,true);}finally{h.restore();}
});
test('lock synchronization with newly dirty context retires accepted acknowledgement',async()=>{
 const h=readyHarness();try{await previewAndAck(h);h.context.dirty=true;h.pane.syncLocks();assert.equal(ack(h.pane.element).checked,false);assert.ok(!h.pane.element.textContent.includes('Exact é'));}finally{h.restore();}
});
test('handoff captures post-send typing, freezes exact epoch, and replay remains read-only',async()=>{
 let attempts=0,captures=0;const frozen=[];let h;
 h=readyHarness({response:(path,opt,{child,envelope})=>{if(path.endsWith('/preview'))return copy(envelope);if(++attempts===1)throw Error('lost');return copy(child);},confirmHandoff:()=>true,captureWriting:()=>{captures++;return {epoch:9,capsule:{schema_version:1,owner:id(1),application:id(2),baselineInspection:copy(h.context.writing.draftInspection),targets:[{target:'q',text:'newer private typing'}]}};},freezeWriting:epoch=>{frozen.push(epoch);return true;}});
 try{await previewAndAck(h);await button(h.pane.element,record).click();h.context.writing.epoch=9;await button(h.pane.element,handoff).click();
  assert.equal(captures,1);assert.deepEqual(frozen,[9]);assert.equal(h.registry.recovery(id(1),id(2)).targets[0].text,'newer private typing');
  assert.equal(h.registry.blocksNavigation(id(1),id(2)),false);assert.equal(h.registry.isMutationLocked(id(1),id(2)),true);
  await button(h.pane.element,resend).click();await button(h.pane.element,select).click();assert.deepEqual(h.selections,[[id(9),{readOnly:true}]]);assert.equal(h.registry.isMutationLocked(id(1),id(2)),true);
 }finally{h.restore();}
});
test('failed or refused handoff cannot release navigation or lose the original operation',async()=>{
 for(const confirm of [false,true]){let captures=0;const h=readyHarness({response:(path,opt,{envelope})=>{if(path.endsWith('/preview'))return copy(envelope);throw Error('lost');},confirmHandoff:()=>confirm,captureWriting:()=>{captures++;return null;}});try{
  await previewAndAck(h);await button(h.pane.element,record).click();await button(h.pane.element,handoff).click();assert.equal(captures,confirm?1:0);assert.equal(h.registry.blocksNavigation(id(1),id(2)),true);assert.equal(h.registry.snapshot(id(1),id(2)).state,'uncertain');
 }finally{h.restore();}}
});
test('privacy-forgotten registry cannot retain preview or recorded private content on synchronization',async()=>{
 for(const recorded of [false,true]){const h=readyHarness();try{await previewAndAck(h);if(recorded)await button(h.pane.element,record).click();h.registry.forgetOwner(id(1));h.pane.syncLocks();assert.equal(ack(h.pane.element).checked,false);assert.ok(!h.pane.element.textContent.includes('Exact é'));assert.ok(!h.pane.element.textContent.includes('Writing review recorded for saved revision'));}finally{h.restore();}}
});
function rehash(v) {
 const p=v.publication;p.request.disclosure_sha256=hash(p.disclosure);
 p.request_sha256=hash({operation:'human_writing_review',profile_id:p.profile_id,application_id:p.application_id,parent_draft_id:p.parent_draft_id,request:p.request});
 v.review_snapshot.disclosure_sha256=p.request.disclosure_sha256;p.review_snapshot_sha256=hash(v.review_snapshot);v.publication_sha256=hash(p);
}
function richInspection(cover) {
 const v=inspection(),d=v.publication.disclosure;
 const q={...copy(question),id:'manual',type:'file',required:false,text:'Upload <img onerror=alert(1)> evidence'};
 const target={...copy(question),id:'cover_letter',required:false,text:'Letter <script>alert(1)</script>',max_words:400};
 d.cover_letter_target=target;d.cover_letter=cover;v.draft.cover_letter=cover;
 v.canonical_questions.push(q);d.canonical_questions=copy(v.canonical_questions);
 v.manual_requirements=[{schema_version:1,question_id:'manual',reason:'Optional <script>not executable</script> attachment'}];d.manual_requirements=copy(v.manual_requirements);
 v.writing_targets.push({question:copy(target),packet_id:id(21)});
 v.text_sha256=hash({cover_letter:cover,answers:v.draft.answers.map(a=>({question_id:a.question_id,text:a.text}))});
 for(const o of [v.publication,d,v.review_snapshot])o.text_sha256=v.text_sha256;
 v.publication.parent_text_sha256=v.publication.request.parent_text_sha256=v.text_sha256;
 const source={schema_version:1,profile_id:id(1),kind:'confirmed_fact',generation_id:id(30),application_id:null,research_run_id:null,span:{schema_version:1,record_id:id(31),unit_id:null,start:0,end:4,text_sha256:'a'.repeat(64),excerpt:'Fact'}};
 const content=v.draft.answers[0].text;
 v.draft.ledger=[{schema_version:1,target:'q',start:0,end:Array.from(content).length,excerpt:content,kind:'personal',assessment:'supported',citations:[source],integrity:'valid'}];
 d.ledger=copy(v.draft.ledger);v.ledger_sha256=hash(d.ledger);
 for(const o of [v.publication,d,v.review_snapshot])o.ledger_sha256=v.ledger_sha256;
 v.publication.parent_ledger_sha256=v.publication.request.parent_ledger_sha256=v.ledger_sha256;
 rehash(v);return v;
}
for(const cover of ['', '<script>literal é 😀\r\nletter</script> '])test(`configured ${cover ? 'nonblank' : 'blank'} cover, manual exclusions and complete citations stay literal`,async()=>{
 const h=readyHarness({childFactory:()=>richInspection(cover)});try{
  await button(h.pane.element,inspect).click();assert.equal(ack(h.pane.element).disabled,false);
  assert.ok(walk(h.pane.element).some(x=>x.tagName==='pre'&&x.textContent===cover));
  assert.match(h.pane.element.textContent,/Optional manual item: excluded/);assert.match(h.pane.element.textContent,/Optional <script>not executable<\/script> attachment/);
  assert.match(h.pane.element.textContent,/Canonical cover-letter question and limits/);assert.match(h.pane.element.textContent,/"max_words": 400/);
  assert.match(h.pane.element.textContent,/"excerpt": "Fact"/);assert.match(h.pane.element.textContent,/"generation_id"/);
  assert.ok(walk(h.pane.element).every(x=>!['script','img','iframe'].includes(x.tagName)));
  if(cover==='')assert.match(h.pane.element.textContent,/configured saved cover letter is blank/);
 }finally{h.restore();}
});
test('double record clicks dispatch only one immutable request and do not select a result',async()=>{
 let finish;const h=readyHarness({response:(path,opt,{child,envelope})=>path.endsWith('/preview')?copy(envelope):new Promise(resolve=>{finish=()=>resolve(copy(child));})});try{
  await previewAndAck(h);const first=button(h.pane.element,record).click();await button(h.pane.element,record).click();assert.equal(h.calls.length,2);assert.equal(button(h.pane.element,record).disabled,true);finish();await first;assert.equal(h.selections.length,0);
 }finally{h.restore();}
});
for(const reject of [false,true])test(`retired pending ${reject?'error':'success'} cannot repopulate detached private DOM`,async()=>{
 let finish;const h=readyHarness({response:(path,opt,{child,envelope})=>path.endsWith('/preview')?copy(envelope):new Promise((resolve,rejectPromise)=>{finish=()=>reject?rejectPromise(Error('lost private')):resolve(copy(child));})});try{
  await previewAndAck(h);const pending=button(h.pane.element,record).click();h.pane.retire();finish();await pending;assert.equal(h.pane.element.textContent,'');assert.equal(h.selections.length,0);
 }finally{h.restore();}
});
test('result selection failure is associated to actions and never replaces the saved editor implicitly',async()=>{
 const h=readyHarness({selectResult:async()=>{throw Error('no selection');}});try{await previewAndAck(h);await button(h.pane.element,record).click();await button(h.pane.element,select).click();assert.match(h.pane.element.textContent,/Recorded revision inspection failed/);assert.equal(h.calls.length,2);}finally{h.restore();}
});
test('required manual blocker names the canonical question, never a guessed required field',async()=>{
 const h=readyHarness();try{
  const p=h.context.writing.draftInspection;p.review_eligible=false;p.canonical_questions.push({...copy(question),id:'file',text:'Required identity evidence <img>',type:'file',required:true});p.manual_requirements.push({schema_version:1,question_id:'file',reason:'Must be handled manually'});
  h.pane.syncLocks();await button(h.pane.element,inspect).click();assert.equal(h.calls.length,0);assert.match(h.pane.element.textContent,/Required identity evidence <img>/);assert.match(h.pane.element.textContent,/Required manual/);
 }finally{h.restore();}
});
