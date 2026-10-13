import test from 'node:test';
import assert from 'node:assert/strict';
import {disclosurePane} from '../backend/ui/workspace/disclosure-pane.js';

// Synthetic DOM event/lifetime checks only: not browser, layout or accessibility proof.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; this.disabled = false; this.events = {}; }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return (this.content ?? '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.content = ''; this.children = [...children]; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(key, value) { this.events[key] = value; }
  removeEventListener(key, value) { if (this.events[key] === value) delete this.events[key]; }
  click() { return this.onclick?.(); }
}
const walk = root => [root, ...root.children.flatMap(walk)];
const named = (root, label) => walk(root).find(item => item.tagName === 'button' && item.textContent === label);
const control = (root, label) => walk(root).find(item => item.tagName === 'label' && item.children[0]?.textContent === label).children[1];
const tick = () => new Promise(resolve => setImmediate(resolve));
const capture = () => ({epoch: 1, packetInspection: null, draftInspection: null, bufferMatchesSaved: false, saving: false, uncertain: false, quarantined: false, retired: false});
function harness({snapshot = capture(), dirty = false, api = async () => { throw new Error('No IO expected'); }, paidController, onSelectResult, isBusy, isMutationLocked} = {}) {
  const previous = globalThis.document; globalThis.document = {createElement: tag => new Element(tag)};
  const calls = [], notices = [];
  const pane = disclosurePane({owner: '11111111-1111-4111-8111-111111111111', application: '22222222-2222-4222-8222-222222222222', api: (...args) => { calls.push(args); return api(...args); }, getCapture: () => snapshot, isDirty: () => dirty, isBusy, isMutationLocked, paidController, onSelectResult, notify: message => notices.push(message)});
  return {pane, calls, notices, snapshot, model: control(pane.element, 'Provider model identifier'), ceiling: control(pane.element, 'Maximum output tokens per call'), restore() { pane.retire(); globalThis.document = previous; }};
}
const generation = 'Inspect generation data and limits';
const assessment = 'Inspect saved-edit assessment data and limits';
test('construction performs no IO and exposes labelled empty model and default ceiling only', () => {
  const h = harness(); try {
    assert.equal(h.calls.length, 0); assert.equal(h.model.value, ''); assert.equal(h.ceiling.value, '2000');
    assert.ok(named(h.pane.element,generation)); assert.ok(named(h.pane.element,assessment)); assert.equal(named(h.pane.element,'Launch acknowledged paid operation').disabled,true);
    assert.ok(walk(h.pane.element).filter(x => x.tagName === 'button' && x['aria-describedby']).every(x => x.type === 'button' && walk(h.pane.element).some(reason => reason.id === x['aria-describedby'])));
    assert.match(h.pane.element.textContent, /separate unchecked acknowledgement/i);
    assert.ok(walk(h.pane.element).some(x => x.role === 'status' && x['aria-live'] === 'polite'));
  } finally { h.restore(); }
});
for (const [flag, value, expected] of [['saving', true, /save/i], ['uncertain', true, /unresolved/i], ['quarantined', true, /read-only/i], ['retired', true, /retired/i]]) test(`blocks ${flag} without IO`, async () => {
  const h = harness({snapshot: {...capture(), [flag]: value}}); try {
    h.model.value = 'configured-model'; await named(h.pane.element, generation).click(); await named(h.pane.element, assessment).click();
    assert.equal(h.calls.length, 0); assert.match(h.pane.element.textContent, expected);
  } finally { h.restore(); }
});
test('global dirty state and missing captures fail closed with explanations', async () => {
  for (const dirty of [true, false]) {
    const h = harness({dirty}); try {
      h.model.value = 'configured-model'; await named(h.pane.element, generation).click();
      assert.match(h.pane.element.textContent, dirty ? /unsaved/i : /selected.*packet/i);
      await named(h.pane.element, assessment).click(); assert.equal(h.calls.length, 0);
    } finally { h.restore(); }
  }
});
test('invalid configuration never performs IO and input invalidation preserves controls', async () => {
  const h = harness(); try {
    const model = h.model, ceiling = h.ceiling;
    for (const [value, limit] of [['', '2000'], ['bad model', '2000'], ['model', '0'], ['model', '8001'], ['model', '1.5']]) {
      model.value = value; ceiling.value = limit; await named(h.pane.element, generation).click(); assert.equal(h.calls.length, 0);
    }
    model.events.input(); ceiling.events.input(); h.pane.invalidate();
    assert.equal(control(h.pane.element, 'Provider model identifier'), model); assert.equal(control(h.pane.element, 'Maximum output tokens per call'), ceiling);
  } finally { h.restore(); }
});
test('terminal retirement clears private controls and ignores retained callbacks', async () => {
  const h = harness(); try {
    h.model.value = 'private-model'; const action = named(h.pane.element, generation).onclick;
    h.pane.retire(); h.pane.retire(); h.pane.invalidate(); await action();
    assert.equal(h.pane.element.textContent, ''); assert.equal(h.model.value, ''); assert.equal(h.calls.length, 0);
    assert.equal(h.model.events.input, undefined);
  } finally { h.restore(); }
});

// Independent minimal real-controller API fixtures; no response mocks injected
// into the controller itself and no import of another executing test module.
const digest = 'a'.repeat(64);
const vector = {schema_version: 1, metadata: 1, facts: 2, documents: 3, consent: 4, application_input: 5, application_output: 6, research: 7};
const question = id => ({schema_version: 1, id, text: '<script>literal question</script>', type: 'writing', required: id !== 'cover_letter', max_words: 200, max_chars: null, options: [], constraint_origin: 'user'});
function packet() {
  const questions = [question('q'), question('cover_letter')];
  return {schema_version: 1, current: true, batch_sha256: digest, publication_sha256: digest, publication: {schema_version: 1, id: '77777777-7777-4777-8777-777777777777', profile_id: '11111111-1111-4111-8111-111111111111', application_id: '22222222-2222-4222-8222-222222222222', batch_id: '33333333-3333-4333-8333-333333333333', batch_sha256: digest}, review_eligible: false, browser_eligible: false,
    batch: {schema_version: 1, id: '33333333-3333-4333-8333-333333333333', profile_id: '11111111-1111-4111-8111-111111111111', application_id: '22222222-2222-4222-8222-222222222222', research_run_id: '44444444-4444-4444-8444-444444444444', revisions: {...vector}, canonical_questions: [questions[0]], cover_letter_target: questions[1], hiring_criteria: [],
      packets: questions.map((question, i) => ({schema_version: 1, id: `88888888-8888-4888-8888-88888888888${i}`, profile_id: '11111111-1111-4111-8111-111111111111', application_id: '22222222-2222-4222-8222-222222222222', research_run_id: '44444444-4444-4444-8444-444444444444', question, facts: [], employer: [], criteria: [], gaps: ['<img onerror=alert(1)>']}))}};
}
function parent() {
  return {schema_version: 1, current: true, text_sha256: digest, ledger_sha256: digest, publication_sha256: digest,
    publication: {id: '66666666-6666-4666-8666-666666666666', origin: 'edit', profile_id: '11111111-1111-4111-8111-111111111111', application_id: '22222222-2222-4222-8222-222222222222', draft_id: '55555555-5555-4555-8555-555555555555', batch_id: '33333333-3333-4333-8333-333333333333', batch_sha256: digest, text_sha256: digest, ledger_sha256: digest},
    draft: {schema_version: 1, id: '55555555-5555-4555-8555-555555555555', profile_id: '11111111-1111-4111-8111-111111111111', application_id: '22222222-2222-4222-8222-222222222222', research_run_id: '44444444-4444-4444-8444-444444444444', revisions: {...vector}, answers: [{schema_version: 1, question_id: 'q', packet_id: '88888888-8888-4888-8888-888888888880', text: 'a\r\né😀 '}], cover_letter: '', ledger: [], assessment_state: 'unassessed', inventory_complete: false},
    writing_targets: packet().batch.packets.map(p => ({question: p.question, packet_id: p.id}))};
}
const currentState = consent => ({schema_version: 1, profile_id: '11111111-1111-4111-8111-111111111111', application_id: '22222222-2222-4222-8222-222222222222', current_revisions: {...vector, consent: consent == null ? 0 : 4}, consent});
const consent = (provider = 'openai', purposes = ['drafting', 'assessment'], granted = true) => ({provider, purposes, granted, revision: 4});
function preview(body, kind) {
  return {schema_version: 1, review_eligible: false, browser_eligible: false, disclosure_sha256: digest,
    disclosure: {schema_version: 1, ...body, batch_id: '33333333-3333-4333-8333-333333333333', batch_sha256: digest, selected_context: {company: '<img src=x onerror=alert(1)>', role: 'Role', sector: 'tech', targets: packet().batch.packets.map(p => ({question: p.question, packet_id: p.id, facts: p.facts, employer: p.employer, criteria: p.criteria, gaps: p.gaps}))},
      selected_context_sha256: digest, prompt_version: 'v1', output_schema_version: 1, consent_revision: body.expected_revisions.consent, maximum_calls: kind === 'generation' ? 5 : 1,
      conservative_reservation_units: (kind === 'generation' ? 5 : 1) * (131072 + body.max_output_tokens), reservation_unit_description: 'Conservative UTF-8 byte input plus output ceiling; not exact tokens or money', configured_limits: null, monetary_cost: null, pricing_status: 'unknown', semantic_assessment: 'fallible_not_release_qualified',
      ...(kind === 'assessment' ? {input_draft: {schema_version: 1, targets: [{schema_version: 1, target: 'q', text: 'a\r\né😀 '}, {schema_version: 1, target: 'cover_letter', text: ''}]}} : {})}};
}
function ready(kind = 'generation') { return {...capture(), packetInspection: packet(), draftInspection: parent(), bufferMatchesSaved: kind === 'assessment'}; }
function realApi({permission = consent(), post = (body, kind) => preview(body, kind), get = null} = {}) {
  return async (path, options) => path.endsWith('/drafting-admission') ? {schema_version:1,profile_id:'11111111-1111-4111-8111-111111111111',application_id:'22222222-2222-4222-8222-222222222222',admission_revision:0,eligible:true,reason_codes:[],head_job:null} : options ? post(options.body, path.includes('/assessment/') ? 'assessment' : 'generation') : get ? get() : currentState(permission);
}
function configured(h) { h.model.value = 'chosen-model'; h.model.events.input(); }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
test('explicit generation displays literal selected data, costs, consent and limits without launch controls', async () => {
  const h = harness({snapshot: ready(), api: realApi()}); try {
    configured(h); assert.equal(h.calls.length, 0); assert.equal(named(h.pane.element, generation).disabled, false);
    named(h.pane.element, generation).click(); await tick();
    assert.equal(h.calls.length, 3); assert.match(h.pane.element.textContent, /three to five calls/); assert.match(h.pane.element.textContent, /at most one rewrite/);
    assert.match(h.pane.element.textContent, /Monetary cost: unknown, not zero/); assert.match(h.pane.element.textContent, /not an exact future outgoing payload/);
    assert.match(h.pane.element.textContent, /OpenAI drafting permission: granted in the captured consent record/);
    assert.match(h.pane.element.textContent, /before generation/); assert.match(h.pane.element.textContent, /stales retained drafts/);
    assert.match(h.pane.element.textContent, /<img src=x onerror=alert\(1\)>/);
    assert.equal(walk(h.pane.element).some(x => ['img', 'script'].includes(x.tagName)), false);
    assert.ok(walk(h.pane.element).filter(x => x.tagName === 'details').length >= 3);
    assert.match(h.pane.element.textContent, /Not configured/);
    h.model.events.input(); assert.doesNotMatch(h.pane.element.textContent, /<img src=x/);
  } finally { h.restore(); }
});
for (const [permission, expected] of [[null, /permission: absent/], [consent('Other'), /not granted; this other-provider grant is not applicable/], [consent('OpenAI'), /not granted; this other-provider grant is not applicable/], [consent('openai', ['research']), /drafting permission: not granted/], [consent('openai', ['drafting'], false), /drafting permission: not granted/]]) test('consent summary is purpose/provider specific and never blocks local inspection', async () => {
  const h = harness({snapshot: ready(), api: realApi({permission})}); try { configured(h); named(h.pane.element, generation).click(); await tick(); assert.equal(h.calls.length, 3); assert.match(h.pane.element.textContent, expected); } finally { h.restore(); }
});
test('assessment displays exact saved Unicode/CRLF and empty cover letter with one critique and no rewrite', async () => {
  const h = harness({snapshot: ready('assessment'), api: realApi()}); try {
    configured(h); named(h.pane.element, assessment).click(); await tick();
    assert.equal(h.calls.length, 3); assert.match(h.pane.element.textContent, /one critique call, no rewrite/);
    assert.match(h.pane.element.textContent, /a\\r\\né😀 /); assert.match(h.pane.element.textContent, /"target": "cover_letter"/);
    assert.match(h.pane.element.textContent, /OpenAI assessment permission: granted/);
  } finally { h.restore(); }
});
for (const mutate of [s => s.bufferMatchesSaved = false, s => s.draftInspection.publication.origin = 'generation', s => s.draftInspection.draft.ledger.push({}), s => s.draftInspection.current = false, s => s.draftInspection.draft.inventory_complete = true]) test('assessment rejects unsaved, non-edit or assessed captures before IO', () => {
  const snapshot = ready('assessment'); mutate(snapshot); const h = harness({snapshot, api: realApi()}); try { configured(h); named(h.pane.element, assessment).click(); assert.equal(h.calls.length, 0); assert.match(h.pane.element.textContent, /unchanged, current, unassessed/); } finally { h.restore(); }
});
for (const change of ['options', 'capture', 'explicit', 'retire']) test(`${change} invalidation suppresses delayed selected data`, async () => {
  const wait = deferred(); let body;
  const h = harness({snapshot: ready(), api: realApi({post: request => { body = request; return wait.promise; }})}); try {
    configured(h); named(h.pane.element, generation).click(); await tick();
    if (change === 'options') { h.model.value = 'another-model'; h.model.events.input(); }
    if (change === 'capture') h.snapshot.epoch++;
    if (change === 'explicit') h.pane.invalidate();
    if (change === 'retire') h.pane.retire();
    wait.resolve(preview(body, 'generation')); await tick();
    assert.doesNotMatch(h.pane.element.textContent, /<img src=x/);
    if (change === 'retire') assert.equal(h.pane.element.textContent, '');
  } finally { h.restore(); }
});
test('generation pending GET invalidation makes no later POST; no automatic retry', async () => {
  const wait = deferred(); const h = harness({snapshot: ready(), api: realApi({get: () => wait.promise})}); try {
    configured(h); named(h.pane.element, generation).click(); h.pane.invalidate(); wait.resolve(currentState(consent())); await tick();
    assert.equal(h.calls.length, 1);
  } finally { h.restore(); }
});
test('current inspection errors are polite, safe and never expose hostile raw error content', async () => {
  const h = harness({snapshot: ready(), api: realApi({post: () => { throw new Error('<script>secret</script>'); }})}); try {
    configured(h); named(h.pane.element, generation).click(); await tick(); assert.match(h.pane.element.textContent, /Inspection failed/);
    assert.doesNotMatch(h.pane.element.textContent, /secret/); assert.equal(h.notices.length, 1); assert.equal(h.calls.length, 3);
  } finally { h.restore(); }
});
for (const fail of [false, true]) test(`cross-kind replacement suppresses stale ${fail ? 'error' : 'success'} and preserves model nodes`, async () => {
  const wait = deferred(); let oldBody;
  const h = harness({snapshot: ready('assessment'), api: realApi({post: (body, kind) => { if (kind === 'generation') { oldBody = body; return wait.promise; } return preview(body, kind); }})}); try {
    configured(h); const model = h.model;
    named(h.pane.element, generation).click(); await tick(); named(h.pane.element, assessment).click(); await tick();
    assert.match(h.pane.element.textContent, /one critique call, no rewrite/);
    if (fail) wait.reject(new Error('old private error')); else wait.resolve(preview(oldBody, 'generation'));
    await tick(); assert.match(h.pane.element.textContent, /one critique call, no rewrite/);
    assert.doesNotMatch(h.pane.element.textContent, /three to five calls|old private error/); assert.equal(h.notices.length, 0);
    assert.equal(control(h.pane.element, 'Provider model identifier'), model);
  } finally { h.restore(); }
});
test('invalidating rendered selected data clears all private excerpts without fetching', async () => {
  const h = harness({snapshot: ready(), api: realApi()}); try {
    configured(h); named(h.pane.element, generation).click(); await tick(); assert.match(h.pane.element.textContent, /<script>literal question/);
    const retained = walk(h.pane.element).filter(item => item.tagName === 'pre');
    const calls = h.calls.length; h.snapshot.epoch++; h.pane.invalidate();
    assert.ok(retained.every(item => item.textContent === ''));
    assert.doesNotMatch(h.pane.element.textContent, /<script>literal question/); assert.equal(h.calls.length, calls);
    h.ceiling.value = '3000'; h.ceiling.events.input(); assert.equal(h.calls.length, calls);
  } finally { h.restore(); }
});
test('strict domain dirtiness and failed capture cannot authorize IO', () => {
  for (const dirty of [undefined, null, 'false', 0]) {
    // Explicit undefined is defaulted by harness; exercise other non-booleans.
    if (dirty === undefined) continue;
    const h = harness({snapshot: ready(), dirty, api: realApi()}); try { configured(h); named(h.pane.element, generation).click(); assert.equal(h.calls.length, 0); assert.match(h.pane.element.textContent, /state is unavailable/); } finally { h.restore(); }
  }
});
test('retirement recursively blanks retained private details and descendant nodes', async () => {
  const h = harness({snapshot: ready('assessment'), api: realApi()}); try {
    configured(h); named(h.pane.element, assessment).click(); await tick();
    const privateDetails = walk(h.pane.element).filter(item => item.tagName === 'details');
    const privatePre = walk(h.pane.element).filter(item => item.tagName === 'pre');
    assert.ok(privatePre.some(item => item.textContent.includes('<img src=x')));
    assert.ok(privatePre.some(item => item.textContent.includes('a\\r\\né😀 ')));
    const action = named(h.pane.element, assessment).onclick;
    const calls = h.calls.length; h.pane.retire(); await action();
    for (const item of [...privateDetails, ...privatePre]) assert.equal(item.textContent, '');
    assert.equal(h.calls.length, calls); assert.equal(h.notices.length, 0);
  } finally { h.restore(); }
});

const ackLabel = 'I acknowledge this exact selected operation may transmit data and incur unknown charges';
const launchLabel = 'Launch acknowledged paid operation';
function paidHarness({snapshot=ready(), mutate, permission=consent(), state=null, select=async()=>true}={}) {
 const calls=[],p={snapshot:()=>state,launch:async(...args)=>{calls.push(['launch',...args]);state={kind:args[0].kind,state:'unknown',job:null,actionPending:false,handedOff:false,resultDraftId:null,admission:null};},reconcileOriginal:async()=>calls.push(['reconcile']),handoffUnknown:async()=>calls.push(['handoff']),refreshKnownJob:async()=>calls.push(['status']),cancelKnownJob:async()=>calls.push(['cancel']),inspectKnownResult:async()=>calls.push(['result']),refreshAdmission:async()=>calls.push(['admission']),resolveKnown:async()=>{calls.push(['resolve']);state=null;}};
 const h=harness({snapshot,paidController:p,onSelectResult:select,api:realApi({permission,post:(b,k)=>{const r=preview(b,k);r.disclosure.configured_limits={daily_tokens:10000000,daily_calls:20,job_tokens:1000000,job_calls:5};mutate?.(r);return r;}})});
 return {...h,paid:p,paidCalls:calls,setState:x=>{state=x;}};
}
async function inspected(h,kind=generation){configured(h);named(h.pane.element,kind).click();await tick();}
test('new paid operation is unchecked, launches exact captured preview only on separate acknowledgement',async()=>{const h=paidHarness();try{await inspected(h);const ack=control(h.pane.element,ackLabel),button=named(h.pane.element,launchLabel);assert.equal(ack.checked,false);assert.equal(button.disabled,true);await button.click();assert.equal(h.paidCalls.length,0);ack.checked=true;ack.onchange();assert.equal(button.disabled,false);await button.click();assert.equal(h.paidCalls.length,1);const [,c,a]=h.paidCalls[0];assert.equal(a,true);assert.equal(c.kind,'generation');assert.equal(c.admission.admission_revision,0);assert.equal(ack.checked,false);assert.equal(h.calls.length,3);assert.equal(button.disabled,true);}finally{h.restore();}});
for(const flag of ['saving','uncertain','quarantined','retired'])test(`paid launch handler rechecks ${flag} even if programmatically clicked`,async()=>{const h=paidHarness();try{await inspected(h);const ack=control(h.pane.element,ackLabel);ack.checked=true;ack.onchange();h.snapshot[flag]=true;await named(h.pane.element,launchLabel).click();assert.equal(h.paidCalls.length,0);}finally{h.restore();}});
for(const change of ['model','ceiling','selection','invalidate'])test(`paid acknowledgement cannot survive ${change} change`,async()=>{const h=paidHarness();try{await inspected(h);const ack=control(h.pane.element,ackLabel);ack.checked=true;ack.onchange();if(change==='model')h.model.value='different';if(change==='ceiling')h.ceiling.value='3000';if(change==='selection')h.snapshot.epoch++;if(change==='invalidate')h.pane.invalidate();await named(h.pane.element,launchLabel).click();assert.equal(h.paidCalls.length,0);assert.equal(ack.checked,false);}finally{h.restore();}});
for(const mutate of [r=>r.disclosure.configured_limits=null,r=>r.disclosure.configured_limits.job_calls=4,r=>r.disclosure.configured_limits.job_tokens=1])test('configured ceilings cannot be bypassed through click handler',async()=>{const h=paidHarness({mutate});try{await inspected(h);const ack=control(h.pane.element,ackLabel);ack.checked=true;ack.onchange();await named(h.pane.element,launchLabel).click();assert.equal(h.paidCalls.length,0);}finally{h.restore();}});
test('unknown remount exposes only explicit identical resend and handoff, never automatic IO',async()=>{const h=paidHarness({state:{kind:'generation',state:'unknown',job:null,actionPending:false,handedOff:false,resultDraftId:null,admission:null}});try{assert.equal(h.calls.length,0);assert.equal(h.paidCalls.length,0);h.pane.syncOperation();assert.equal(h.calls.length,0);await named(h.pane.element,'Resend original acknowledged request (may first queue paid work)').click();await named(h.pane.element,'Hand off unresolved operation for read-only navigation').click();assert.deepEqual(h.paidCalls,[['reconcile'],['handoff']]);await named(h.pane.element,'Read known job status').click();assert.equal(h.paidCalls.length,2);}finally{h.restore();}});
test('resolved registry state clears old preview and acknowledgement without hidden reads',async()=>{const h=paidHarness();try{await inspected(h);const ack=control(h.pane.element,ackLabel);ack.checked=true;ack.onchange();h.setState({kind:'generation',state:'known',job:{},actionPending:false,handedOff:false,resultDraftId:null,admission:null});h.pane.syncOperation();h.setState(null);h.pane.syncOperation();assert.equal(ack.checked,false);assert.doesNotMatch(h.pane.element.textContent,/<img src=x/);assert.equal(h.calls.length,3);}finally{h.restore();}});
test('result inspection never selects automatically and explicit selection passes only exact scope/id',async()=>{const selected=[],h=paidHarness({state:{kind:'generation',state:'known',job:{},actionPending:false,handedOff:false,resultDraftId:'saved',admission:null},select:async c=>{selected.push(c);return true;}});try{h.pane.updatePaid('result',{draft:{id:'saved'}},null);assert.equal(selected.length,0);await named(h.pane.element,'Select inspected saved result').click();assert.deepEqual(selected,[{owner:'11111111-1111-4111-8111-111111111111',application:'22222222-2222-4222-8222-222222222222',draftId:'saved'}]);assert.equal(h.calls.length,0);}finally{h.restore();}});
test('retained paid callbacks after retire cannot launch or act, and pane does not retire root controller',async()=>{const h=paidHarness();try{await inspected(h);const action=named(h.pane.element,launchLabel).onclick;h.pane.retire();await action();h.pane.updatePaid('operation',{},null);h.pane.syncOperation();assert.equal(h.paidCalls.length,0);assert.equal(h.pane.element.textContent,'');}finally{h.restore();}});

for(const permission of [null,consent('Other'),consent('OpenAI'),consent('openai',['research']),consent('openai',['drafting'],false)])test('paid handler rejects absent/revoked/wrong-provider/purpose consent despite checkbox',async()=>{const h=paidHarness({permission});try{await inspected(h);const ack=control(h.pane.element,ackLabel);ack.checked=true;ack.onchange();await named(h.pane.element,launchLabel).click();assert.equal(h.paidCalls.length,0);}finally{h.restore();}});
test('assessment paid launch retains exact unchanged saved text and one-call disclosure',async()=>{const h=paidHarness({snapshot:ready('assessment')});try{await inspected(h,assessment);const ack=control(h.pane.element,ackLabel);ack.checked=true;ack.onchange();await named(h.pane.element,launchLabel).click();assert.equal(h.paidCalls.length,1);const c=h.paidCalls[0][1];assert.equal(c.kind,'assessment');assert.equal(c.packetInspection,null);assert.equal(c.disclosure.maximum_calls,1);assert.equal(c.draftInspection.draft.answers[0].text,'a\r\né😀 ');assert.equal(c.disclosure.input_draft.targets[0].text,'a\r\né😀 ');}finally{h.restore();}});

test('known job state stage cleanup and admission blockers are explicit observations, not resolution authority', () => {
 const h=paidHarness({state:{kind:'generation',state:'known',job:{state:'failed',stage:'drafting_failed',cleanup_pending:true},actionPending:false,handedOff:false,resultDraftId:null,admission:{admission_revision:7,eligible:false,reason_codes:['provider_outcome_unresolved']}}});
 try { assert.match(h.pane.element.textContent,/Job observation: failed; stage: drafting_failed; cleanup pending: true/);assert.match(h.pane.element.textContent,/Admission observation: revision 7; eligible: false; reasons: provider_outcome_unresolved/);assert.match(h.pane.element.textContent,/Observations do not resolve or authorize a new operation/);assert.equal(named(h.pane.element,launchLabel).disabled,true); } finally {h.restore();}
});
for (const [method,label,view] of [['refreshKnownJob','Read known job status','operation'],['refreshAdmission','Read current admission (does not resolve operation)','admission']]) test(`${method} error survives final synchronization and clears only on next permitted explicit action`,async()=>{
 const state={kind:'generation',state:'known',job:{state:'failed',stage:'drafting_failed',cleanup_pending:false},actionPending:false,handedOff:false,resultDraftId:null,admission:null},h=paidHarness({state});
 try { h.paid[method]=async()=>{h.pane.updatePaid(view,null,new Error('private raw error'));return false;};await named(h.pane.element,label).click();h.pane.syncOperation();assert.match(h.pane.element.textContent,/Paid action could not be verified/);assert.doesNotMatch(h.pane.element.textContent,/private raw error/);assert.match(h.pane.element.textContent,/Job observation: failed/);h.pane.updatePaid('operation',state,null);assert.match(h.pane.element.textContent,/Paid action could not be verified/);h.paid[method]=async()=>true;await named(h.pane.element,label).click();assert.doesNotMatch(h.pane.element.textContent,/Paid action could not be verified/); } finally {h.restore();}
});
test('thrown paid action failure survives finally synchronization without exposing raw error',async()=>{
 const h=paidHarness({state:{kind:'generation',state:'known',job:{state:'failed',stage:'drafting_failed',cleanup_pending:false},actionPending:false,handedOff:false,resultDraftId:null,admission:null}});
 try{h.paid.refreshKnownJob=async()=>{throw new Error('private transport');};await named(h.pane.element,'Read known job status').click();h.pane.syncOperation();assert.match(h.pane.element.textContent,/Paid action failed/);assert.doesNotMatch(h.pane.element.textContent,/private transport/);}finally{h.restore();}
});

for (const guard of ['isBusy', 'isMutationLocked']) test(`new disclosure ${guard} guard blocks preview and launch without blocking retained paid status`, async () => {
  let blocked = true, refreshed = 0;
  const operation = {state: 'known', kind: 'generation', actionPending: false, resultDraftId: null};
  const h = harness({[guard]: () => blocked, paidController: {snapshot: () => operation, refreshKnownJob: async () => { refreshed++; }}});
  try {
    h.model.value = 'configured-model'; h.pane.syncOperation();
    await named(h.pane.element, generation).click(); await named(h.pane.element, assessment).click();
    assert.equal(h.calls.length, 0); assert.equal(named(h.pane.element, generation).disabled, true);
    assert.equal(named(h.pane.element, 'Launch acknowledged paid operation').disabled, true);
    const status = named(h.pane.element, 'Read known job status'); assert.equal(status.disabled, false); await status.click(); assert.equal(refreshed, 1);
    blocked = false; h.pane.syncOperation(); assert.equal(status.disabled, false);
  } finally { h.restore(); }
});
