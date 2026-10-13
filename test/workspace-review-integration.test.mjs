import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {PaidLaunchController, PaidLaunchRegistry} from '../backend/ui/workspace/paid-launch.js';
import {ReviewRegistry} from '../backend/ui/workspace/review.js';
import {WritingRecovery} from '../backend/ui/workspace/client.js';
const copy = x => structuredClone(x), h = 'a'.repeat(64);
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const owner=id(1), application=id(2);
const vector={schema_version:1,metadata:1,facts:2,documents:3,consent:4,application_input:5,application_output:6,research:7};
const admission=()=>({schema_version:1,profile_id:owner,application_id:application,admission_revision:0,eligible:true,reason_codes:[],head_job:null});
const q={schema_version:1,id:'why',text:'Why <img>?',type:'writing',required:true,max_words:200,max_chars:null,options:[],constraint_origin:'user'};
function capture(){
 const request={schema_version:1,batch_id:id(3),batch_sha256:h,expected_revisions:copy(vector),model:'test-model',max_output_tokens:2000};
 return {kind:'generation',owner,application,admission:admission(),request,disclosure:{schema_version:1,batch_id:id(3),batch_sha256:h,expected_revisions:copy(vector),model:'test-model',max_output_tokens:2000,consent_revision:4,maximum_calls:5,conservative_reservation_units:5*(131072+2000),configured_limits:{daily_tokens:1000000,daily_calls:20,job_tokens:1000000,job_calls:5},prompt_version:'prompt-v1',selected_context:{company:'Employer',role:'Engineer',sector:'tech',targets:[{question:copy(q),packet_id:id(4),facts:[],employer:[],criteria:[],gaps:[]}]},selected_context_sha256:h,output_schema_version:1,monetary_cost:null,pricing_status:'unknown',semantic_assessment:'fallible_not_release_qualified'},disclosure_sha256:h,state:{schema_version:1,profile_id:owner,application_id:application,current_revisions:copy(vector),consent:{provider:'openai',purposes:['drafting','assessment'],granted:true,revision:4}},packetInspection:{schema_version:1,current:true,batch_sha256:h,publication_sha256:h,batch:{schema_version:1,id:id(3),profile_id:owner,application_id:application,research_run_id:id(5),canonical_questions:[copy(q)],cover_letter_target:null,packets:[{id:id(4),question:copy(q)}]},publication:{schema_version:1,id:id(9),profile_id:owner,application_id:application,batch_id:id(3),batch_sha256:h}},draftInspection:null};
}
function job(state='queued'){return {schema_version:1,job:{id:id(6),profile_id:owner,application_id:application,kind:'draft',state,stage:state==='completed'?'draft_published':'queued',revisions:copy(vector),fence:state==='queued'?0:1,attempt_count:0,cancellation_requested:false,heartbeat_at:null,lease_expires_at:null,cleanup_pending:false}};}
function inspection(){return {schema_version:1,current:true,text_sha256:h,ledger_sha256:h,publication_sha256:h,canonical_questions:[copy(q)],writing_targets:[{question:copy(q),packet_id:id(4)}],draft:{schema_version:1,id:id(7),profile_id:owner,application_id:application,research_run_id:id(5),revisions:{...vector,application_output:7},answers:[{schema_version:1,question_id:'why',packet_id:id(4),text:'é😀\r\n<script>literal</script>'}],cover_letter:'',ledger:[],assessment_state:'assessed',inventory_complete:true},publication:{schema_version:1,id:id(8),profile_id:owner,application_id:application,draft_id:id(7),intent_id:id(10),job_id:id(6),fence:1,batch_id:id(3),batch_sha256:h,text_sha256:h,ledger_sha256:h,request_sha256:h,disclosure_sha256:h,prompt_version:'prompt-v1',stage_names:['plan','draft','critique'],reason_codes:[],style_notes:[]}};}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function harness(api=async()=>job(),registry=new PaidLaunchRegistry({})){const calls=[],updates=[],published=[];const controller=new PaidLaunchController({api:async(...a)=>{calls.push(copy(a));return api(...a);},owner,application,registry,update:(...a)=>updates.push(a),onPublished:a=>published.push(a),keyFactory:()=> 'original-key'});return {controller,registry,calls,updates,published};}

const source=()=>readFile(new URL('../backend/ui/workspace/app.js',import.meta.url),'utf8');
test('paid competing mutation guard denies registration without a transport or key',async()=>{
 let calls=0,keys=0;const registry=new PaidLaunchRegistry({});const controller=new PaidLaunchController({owner,application,registry,competingMutationLocked:()=>true,keyFactory:()=>{keys++;return 'key';},api:async()=>{calls++;return job();}});
 assert.equal(await controller.launch(capture(),true),false);assert.equal(registry.size,0);assert.equal(calls,0);assert.equal(keys,0);
});
test('paid initial transport rechecks reentrant review lock but exact recovery bypasses competing guard',async()=>{
 let locked=false,calls=0;const registry=new PaidLaunchRegistry({onChange(){locked=true;}});const controller=new PaidLaunchController({owner,application,registry,competingMutationLocked:()=>locked,keyFactory:()=> 'key',api:async()=>{calls++;return job();}});
 assert.equal(await controller.launch(capture(),true),false);assert.equal(calls,0);assert.equal(controller.snapshot().state,'unknown');
 assert.equal(await controller.reconcileOriginal(),true);assert.equal(calls,1);assert.equal(controller.snapshot().state,'known');
});
test('root mutation locks compose review ownership independently of paid/edit',async()=>{
 const app=await source(),definition=app.slice(app.indexOf('function applicationMutationLocked('),app.indexOf('function writingBlocksNavigation('));
 for(const edit of [false,true])for(const paid of [false,true])for(const review of [false,true]){
  const guard=new Function('writingRecovery','paidRegistry','reviewRegistry',`${definition};return applicationMutationLocked;`)({has:()=>edit},{has:()=>paid},{isMutationLocked:(o,a)=>{assert.equal(o,owner);assert.equal(a,application);return review;}});
  assert.equal(guard(owner,application),edit||paid||review);
 }
});
test('root review navigation gate uses scope and does not conflate successful retained results with locks',async()=>{
 const app=await source(),definition=app.slice(app.indexOf('function writingBlocksNavigation('),app.indexOf('async function revokeCloudPurposes('));
 for(const reviewBlocked of [false,true]){let notices=0;const guard=new Function('writing','paidController','reviewRegistry','state','notify',`${definition};return writingBlocksNavigation;`)({unresolved:false},{snapshot:()=>null},{blocksNavigation:(o,a)=>{assert.equal(o,owner);assert.equal(a,application);return reviewBlocked;}},{owner,application},()=>notices++);
 assert.equal(guard(),reviewBlocked);assert.equal(notices,reviewBlocked?1:0);}
});
test('root mounts distinct review recovery and explicit result selection without auto-adoption',async()=>{
 const app=await source();assert.match(app,/new ReviewRegistry/);assert.match(app,/reviewPane\(/);assert.match(app,/readOnlyWriting: reviewRegistry\.recovery/);
 assert.match(app,/isSelectionLocked:.*reviewRegistry\.isMutationLocked/);assert.match(app,/captureWriting:.*captureReadOnlyWriting/);assert.match(app,/freezeWriting:.*freezeReadOnlyWriting/);
 assert.match(app,/readOnly \? writing\.inspectSavedResult\(id\) : writing\.selectSavedResult\(id\)/);
 assert.match(app,/reviewRegistry\.needsUnloadWarning\(\)/);
});
test('root recognized privacy clears review for captured owner despite a stale visible scope, failures preserve it',async()=>{
 const app=await source(),definition=app.slice(app.indexOf('async function forgetWritingAfter('),app.indexOf('function context('));
 for(const success of [false,true])for(const switched of [false,true]){
  const state={owner},calls=[],recovery=new WritingRecovery();let retired=0;
  const run=new Function('state','writingRecovery','paidRegistry','reviewRegistry','recognizedPrivacyDeletion','startWriting','retireDisclosure','retireBudget',`let writing=startWriting;${definition};return forgetWritingAfter;`)(state,recovery,{forgetOwner:o=>calls.push(['paid',o])},{forgetOwner:o=>calls.push(['review',o])},(_k,v)=>v?.deleted===true,{retire:()=>retired++},()=>retired++,()=>{});
  const d=deferred(),pending=run('workspace',()=>d.promise);if(switched)state.owner=id(99);d.resolve(success?{deleted:true}:{});
  if(success)await pending;else await assert.rejects(pending);
  assert.deepEqual(calls,success?[['paid',owner],['review',owner]]:[]);assert.equal(retired,success&&!switched?2:0);
 }
});
test('root review registry notification synchronizes only matching views without recursive preview invalidation',async()=>{
 const app=await source(),definition=app.slice(app.indexOf('const reviewRegistry = new ReviewRegistry('),app.indexOf('const workspace ='));
 let callback;const calls=[];const pane=name=>({syncLocks:()=>calls.push(name),syncOperation:()=>calls.push(name),invalidate:()=>{throw Error('Recursive preview invalidation');}});
 new Function('ReviewRegistry','state','applicationLocks','writing','disclosure','review',definition)(class {constructor({onChange}){callback=onChange;}},{owner,application},()=>calls.push('forms'),pane('writing'),pane('disclosure'),pane('review'));
 callback({owner:id(99),application});assert.deepEqual(calls,[]);callback({owner,application});assert.deepEqual(calls,['forms','writing','disclosure','review']);
});
test('root exact review callbacks fence scope, preserve forms and use explicit quarantine inspection',async()=>{
 const app=await source(),start=app.indexOf('  review = reviewPane('),end=app.indexOf("  const ticket = gate.capture('history');",start),definition=app.slice(start,end);
 const applicationRecord={application_id:application,profile_id:owner,revisions:copy(vector)},foreign={application_id:id(77),profile_id:owner,revisions:copy(vector)};
 const state={owner,application,dirty:true,busy:false,detail:{applications:[applicationRecord,foreign]}},calls=[];let options;
 const writing={capture:()=>({epoch:1}),captureReadOnlyWriting:()=>({epoch:2}),freezeReadOnlyWriting:e=>{calls.push(['freeze',e]);return true;},fenceExternalDispatch:()=>{calls.push(['fence']);return true;},syncLocks:()=>{},inspectSavedResult:async id=>{calls.push(['readOnly',id]);return true;},selectSavedResult:async id=>{calls.push(['adopt',id]);return true;}};
 const mount=new Function('api','owner','appId','reviewRegistry','state','writing','writingRecovery','paidRegistry','disclosure','applicationLocks','reviewPane','$','advanceApplicationOutput',`let review;${definition};return ()=>options;`.replace('return ()=>options;','return review;'));
 mount(()=>{},owner,application,new ReviewRegistry(),state,writing,{has:()=>false},{has:()=>false},{invalidate:()=>calls.push(['invalidatePaid']),syncOperation:()=>{}},()=>{},o=>{options=o;return {element:{},syncLocks(){}};},()=>({append(){}}),(a,p)=>calls.push(['advance',a.application_id,p.postRevisions.application_output]));
 assert.deepEqual(options.getContext().writing,{epoch:1});assert.equal(options.getContext().dirty,true);
 options.beforeDispatch();assert.deepEqual(calls,[['fence'],['invalidatePaid']]);
 await options.selectResult(id(9),{readOnly:true});await options.selectResult(id(9),{readOnly:false});assert.deepEqual(calls.slice(-2),[['readOnly',id(9)],['adopt',id(9)]]);
 options.onPublished({owner,application:foreign.application_id,postRevisions:{application_output:8}});assert.deepEqual(calls.at(-1),['advance',foreign.application_id,8]);assert.equal(state.dirty,true);
 const before=calls.length;state.owner=id(99);assert.equal(options.getContext(),null);assert.equal(await options.selectResult(id(9),{readOnly:false}),false);assert.throws(()=>options.beforeDispatch());options.onPublished({owner,application,postRevisions:{application_output:9}});assert.equal(calls.length,before);
});
test('review controls do not invalidate their own preview through bubbled checkbox input',async()=>{
 const app=await source(),definition=app.slice(app.indexOf('function invalidateDisclosureInput('),app.indexOf('function exactEnvelope('));
 const calls=[],inside={},outside={};const run=new Function('review','disclosure',`${definition};return invalidateDisclosureInput;`)({element:{contains:t=>t===inside},invalidate:()=>calls.push('review')},{element:{contains:()=>false},invalidate:()=>calls.push('paid')});
 run({target:inside});assert.deepEqual(calls,[]);run({target:outside});assert.deepEqual(calls,['paid','review']);
});
