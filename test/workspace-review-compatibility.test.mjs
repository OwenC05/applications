import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {DraftBuffer, DraftingController} from '../backend/ui/workspace/drafting.js';
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
function rehash(v) {
 const p=v.publication;
 p.request.disclosure_sha256=hash(p.disclosure);
 p.request_sha256=hash({operation:'human_writing_review',profile_id:p.profile_id,application_id:p.application_id,parent_draft_id:p.parent_draft_id,request:p.request});
 v.review_snapshot.disclosure_sha256=p.request.disclosure_sha256;
 p.review_snapshot_sha256=hash(v.review_snapshot);v.publication_sha256=hash(p);
}
test('review child retains exact bytes, full edit capture, inspection and export without adoption',async()=>{
 const v=inspection(),b=new DraftBuffer(v);assert.equal(b.targets()[0].text,v.draft.answers[0].text);const body=b.editRequest('edit-key');assert.deepEqual(body.expected_revisions,v.draft.revisions);assert.equal(body.parent_publication_sha256,v.publication_sha256);
 const calls=[],updates=[];const c=new DraftingController({owner:id(1),application:id(2),api:async path=>{calls.push(path);return v;},exportText:async path=>{calls.push(path);return 'exact saved export';},update:(...args)=>updates.push(args)});
 await c.draft(v.draft.id);await c.exportDraft(v.draft.id);assert.equal(updates[0][2],null);assert.equal(updates[1][1],'exact saved export');assert.equal(calls.length,2);
});
const mutations={
 'unknown origin':v=>v.publication.origin='mystery','literal generation origin':v=>v.publication.origin='generation',
 'review job extras':v=>v.publication.job_id=id(11),'review fence extras':v=>v.publication.fence=1,'review intent extras':v=>v.publication.intent_id=id(11),
 'unknown envelope':v=>v.extra=true,'missing review flag':v=>delete v.reviewed_current,'bad review flag':v=>v.reviewed_current=1,
 'unknown draft':v=>v.draft.extra=true,'invalid UUID':v=>v.review_snapshot.id='alias','wrong snapshot child':v=>v.review_snapshot.draft_id=id(3),
 'wrong snapshot owner':v=>v.review_snapshot.profile_id=id(12),'snapshot revision':v=>v.review_snapshot.revisions={...v.review_snapshot.revisions,facts:99},
 'snapshot timestamp':v=>v.review_snapshot.reviewed_at='2026-10-12T12:00:00Z','snapshot text':v=>v.review_snapshot.text_sha256='b'.repeat(64),
 'request parent':v=>v.publication.request.parent_publication_sha256='b'.repeat(64),'request vector':v=>v.publication.request.expected_revisions={...vector,facts:99},
 'disclosure parent':v=>v.publication.disclosure.parent_draft_id=id(12),'disclosure text':v=>v.publication.disclosure.answers[0].text='forged',
 'disclosure ledger':v=>v.publication.disclosure.ledger.push({}),'disclosure post vector':v=>v.publication.disclosure.post_revisions={...vector,application_output:8},
 'disclosure questions':v=>v.publication.disclosure.canonical_questions[0].required=false,'disclosure warnings':v=>v.publication.disclosure.warnings.reverse(),
 'false ack':v=>v.publication.request.acknowledged=false,'review action':v=>v.review_eligible=true,'browser authority':v=>v.browser_eligible=true,
 'fake ledger hash':v=>{v.draft.ledger=[];v.ledger_sha256=v.publication.ledger_sha256='c'.repeat(64);},
 'bad text exact hash':v=>v.draft.answers[0].text+='x','wrong creation time':v=>v.draft.created_at='2026-10-12T12:00:00Z',
};
for(const [name,mutate] of Object.entries(mutations))test(`review rejects rehashed ${name}`,()=>{const v=inspection();mutate(v);rehash(v);assert.throws(()=>new DraftBuffer(v));});
test('review rejects hash tampering without rehash',()=>{for(const path of ['publication_sha256','text_sha256','ledger_sha256']){const v=inspection();v[path]='invalid-digest';assert.throws(()=>new DraftBuffer(v));}});
test('stale review remains inspectable but never editable or current-reviewed',()=>{const v=inspection();v.current=false;v.writing_review_ready=false;v.reviewed_current=false;v.reason_codes=['revision_changed'];const b=new DraftBuffer(v);assert.equal(b.targets()[0].text,v.draft.answers[0].text);assert.throws(()=>b.editRequest('no'));});

test('edit from reviewed child creates ordinary unassessed writing without inherited snapshot',async()=>{
 const parent=inspection(),buffer=new DraftBuffer(parent);buffer.input('q','New unsaved 😀\r\ntext');const body=buffer.editRequest('edit-key'),child=copy(parent);
 child.draft.id=id(20);child.draft.revisions.application_output++;child.draft.answers[0].text=body.draft.targets[0].text;child.draft.assessment_state='unassessed';child.draft.inventory_complete=false;
 child.review_snapshot=null;child.reviewed_current=false;child.writing_review_ready=false;
 child.publication={schema_version:1,id:id(21),origin:'edit',profile_id:id(1),application_id:id(2),draft_id:id(20),parent_draft_id:parent.draft.id,parent_text_sha256:parent.text_sha256,parent_ledger_sha256:parent.ledger_sha256,parent_publication_id:parent.publication.id,parent_publication_sha256:parent.publication_sha256,pre_revisions:copy(parent.draft.revisions),request:copy(body),text_sha256:child.text_sha256,ledger_sha256:child.ledger_sha256};
 const updates=[],c=new DraftingController({owner:id(1),application:id(2),api:async()=>child,update:(...args)=>updates.push(args)});c.adoptDraft(parent.draft.id);await c.saveEdit(parent,body);assert.equal(updates[0][2],null);assert.equal(updates[0][1].published.draftId,id(20));assert.equal(updates.length,1);
});
test('nonreview child cannot inherit an ancestor review snapshot',()=>{const v=inspection();v.publication.origin='edit';assert.throws(()=>new DraftBuffer(v));});
function withLedger() {
 const v=inspection(),text=v.draft.answers[0].text;
 const reference={schema_version:1,profile_id:id(1),kind:'confirmed_fact',generation_id:id(30),application_id:null,research_run_id:null,span:{schema_version:1,record_id:id(31),unit_id:null,start:0,end:4,text_sha256:'a'.repeat(64),excerpt:'Fact'}};
 v.draft.ledger=[{schema_version:1,target:'q',start:0,end:Array.from(text).length,excerpt:text,kind:'personal',assessment:'supported',citations:[reference],integrity:'valid'}];v.ledger_sha256=hash(v.draft.ledger);v.publication.ledger_sha256=v.publication.parent_ledger_sha256=v.publication.request.parent_ledger_sha256=v.publication.disclosure.ledger_sha256=v.review_snapshot.ledger_sha256=v.ledger_sha256;v.publication.disclosure.ledger=copy(v.draft.ledger);rehash(v);return v;
}
test('review ledger preserves code-point offsets and closed scoped evidence references',()=>{const v=withLedger();assert.equal(new DraftBuffer(v).targets()[0].text,v.draft.answers[0].text);});
for(const mutate of [v=>v.draft.ledger[0].end--,v=>v.draft.ledger[0].citations[0].profile_id=id(32),v=>v.draft.ledger[0].citations[0].span.extra=true,v=>v.draft.ledger[0].citations[0].span.end++,v=>v.draft.ledger[0].assessment='unassessed'])test('review rejects rehashed malformed ledger',()=>{const v=withLedger();mutate(v);v.publication.disclosure.ledger=copy(v.draft.ledger);rehash(v);assert.throws(()=>new DraftBuffer(v));});

import {PaidLaunchController, PaidLaunchRegistry} from '../backend/ui/workspace/paid-launch.js';
function paidCapture() {
 const v=inspection(),h='a'.repeat(64),request={schema_version:1,batch_id:id(5),batch_sha256:h,expected_revisions:copy(vector),model:'test-model',max_output_tokens:2000};
 return {kind:'generation',owner:id(1),application:id(2),admission:{schema_version:1,profile_id:id(1),application_id:id(2),admission_revision:0,eligible:true,reason_codes:[],head_job:null},request,
 disclosure:{schema_version:1,batch_id:id(5),batch_sha256:h,expected_revisions:copy(vector),model:'test-model',max_output_tokens:2000,consent_revision:4,maximum_calls:5,conservative_reservation_units:5*(131072+2000),configured_limits:{daily_tokens:1000000,daily_calls:20,job_tokens:1000000,job_calls:5},prompt_version:'prompt-v1',selected_context:{company:'Employer',role:'Engineer',sector:'tech',targets:[{question:copy(question),packet_id:id(7)}]},selected_context_sha256:h,output_schema_version:1,monetary_cost:null,pricing_status:'unknown',semantic_assessment:'fallible_not_release_qualified'},disclosure_sha256:h,
 state:{schema_version:1,profile_id:id(1),application_id:id(2),current_revisions:copy(vector),consent:{provider:'openai',purposes:['drafting'],granted:true,revision:4}},
 packetInspection:{schema_version:1,current:true,batch_sha256:h,publication_sha256:h,batch:{schema_version:1,id:id(5),profile_id:id(1),application_id:id(2),research_run_id:id(6),canonical_questions:copy(v.canonical_questions),cover_letter_target:null,packets:[{id:id(7),question:copy(question)}]},publication:{schema_version:1,id:id(33),profile_id:id(1),application_id:id(2),batch_id:id(5),batch_sha256:h}},draftInspection:null};
}
for(const malicious of [false,true])test(`review is never resolved as paid result, malicious extras=${malicious}`,async()=>{
 const reviewed=inspection(),published=[],registry=new PaidLaunchRegistry(),calls=[];
 if(malicious)Object.assign(reviewed.publication,{job_id:id(34),intent_id:id(35),fence:1});
 const job={id:id(34),profile_id:id(1),application_id:id(2),kind:'draft',state:'completed',stage:'draft_published',revisions:copy(vector),fence:1,attempt_count:3,cancellation_requested:false,heartbeat_at:null,lease_expires_at:null,cleanup_pending:false};
 const c=new PaidLaunchController({owner:id(1),application:id(2),registry,keyFactory:()=> 'paid-key',onPublished:v=>published.push(v),api:async(path,opt)=>{calls.push([path,opt]);return opt?.method==='POST'?{schema_version:1,job}:path.endsWith('/drafts')?{schema_version:1,drafts:[reviewed]}:path.includes('/drafts/')?reviewed:{schema_version:1,job};}});
 assert.equal(await c.launch(paidCapture(),true),true);assert.equal(await c.inspectKnownResult(),false);assert.equal(published.length,0);assert.equal(registry.size,1);assert.equal(c.snapshot().resultDraftId,null);assert.equal(calls.filter(([,opt])=>opt?.method==='POST').length,1);
});

import {EditBuffers} from '../backend/ui/workspace/client.js';
import {draftingPane} from '../backend/ui/workspace/drafting-pane.js';
// Synthetic DOM only: no browser/layout/accessibility qualification is claimed.
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.value='';this.disabled=false;this.events={};this.classList={add(){}};}
 set textContent(v){this.content=String(v);this.children=[];}get textContent(){return (this.content??'')+this.children.map(c=>c.textContent).join('');}
 append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
 replaceChildren(...children){this.content='';this.children=[];this.append(...children);}
 setAttribute(k,v){this[k]=v;}addEventListener(k,v){this.events[k]=v;}removeEventListener(k,v){if(this.events[k]===v)delete this.events[k];}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}click(){return this.onclick?.();}
}
const walk = root => [root,...root.children.flatMap(walk)];
const tick = () => new Promise(resolve=>setImmediate(resolve));
test('manual selection of review child preserves dirty editor and uses neutral writing-only prose',async()=>{
 const previous=globalThis.document;globalThis.document={createElement:tag=>new Element(tag)};const reviewed=inspection(),calls=[],edits=new EditBuffers();let pane;
 try {
  pane=draftingPane({owner:id(1),application:id(2),api:async(path,opt)=>{calls.push([path,opt]);return path.endsWith('/drafts')?{schema_version:1,drafts:[reviewed]}:path.endsWith('/packets')?{batches:[]}:reviewed;},exportText:async()=> 'Saved bytes',edits,notify:()=>{},confirmDiscard:()=>false});
  const button=label=>walk(pane.element).find(e=>e.tagName==='button'&&e.textContent===label);
  const select=walk(pane.element).find(e=>e.tagName==='label'&&e.children[0]?.textContent==='Saved draft revision').children[1];
  button('Refresh saved draft list').click();await tick();assert.equal(pane.capture().draftInspection,null,'listing never auto-selects');
  select.value=reviewed.draft.id;select.onchange();await tick();const editor=walk(pane.element).find(e=>e.tagName==='textarea');assert.ok(editor);assert.equal(editor.value,reviewed.draft.answers[0].text);
  assert.doesNotMatch(pane.element.textContent,/not human reviewed/i);assert.match(pane.element.textContent,/No fact confirmation/);assert.match(pane.element.textContent,/browser authorization/);
  assert.equal(walk(pane.element).filter(e=>e.tagName==='button'&&/review|approve/i.test(e.textContent)).length,0);
  editor.value='Dirty Unicode 😀';editor.events.input();button('Refresh saved draft list').click();await tick();assert.equal(walk(pane.element).find(e=>e.tagName==='textarea'),editor);assert.equal(editor.value,'Dirty Unicode 😀');assert.equal(pane.capture().bufferMatchesSaved,false);assert.equal(calls.filter(([,opt])=>opt?.method==='POST').length,0);
 }finally{pane?.retire();globalThis.document=previous;}
});

test('optional manual fields, including unpacketized writing, remain visibly excluded',()=>{
 const v=inspection();const q={...question,id:'optional',required:false};v.canonical_questions.push(q);v.manual_requirements=[{schema_version:1,question_id:'optional',reason:'No selected evidence'}];v.publication.disclosure.canonical_questions=copy(v.canonical_questions);v.publication.disclosure.manual_requirements=copy(v.manual_requirements);rehash(v);assert.equal(new DraftBuffer(v).targets().length,1);
 const required=copy(v);required.canonical_questions[1].required=true;required.publication.disclosure.canonical_questions=copy(required.canonical_questions);rehash(required);assert.throws(()=>new DraftBuffer(required));
});
