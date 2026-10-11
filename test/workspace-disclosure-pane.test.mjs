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
function harness({snapshot = capture(), dirty = false, api = async () => { throw new Error('No IO expected'); }} = {}) {
  const previous = globalThis.document; globalThis.document = {createElement: tag => new Element(tag)};
  const calls = [], notices = [];
  const pane = disclosurePane({owner: 'owner', application: 'app', api: (...args) => { calls.push(args); return api(...args); }, getCapture: () => snapshot, isDirty: () => dirty, notify: message => notices.push(message)});
  return {pane, calls, notices, snapshot, model: control(pane.element, 'Provider model identifier'), ceiling: control(pane.element, 'Maximum output tokens per call'), restore() { pane.retire(); globalThis.document = previous; }};
}
const generation = 'Inspect generation data and limits';
const assessment = 'Inspect saved-edit assessment data and limits';
test('construction performs no IO and exposes labelled empty model and default ceiling only', () => {
  const h = harness(); try {
    assert.equal(h.calls.length, 0); assert.equal(h.model.value, ''); assert.equal(h.ceiling.value, '2000');
    assert.deepEqual(walk(h.pane.element).filter(x => x.tagName === 'button').map(x => x.textContent), [generation, assessment]);
    assert.ok(walk(h.pane.element).filter(x => x.tagName === 'button').every(x => x.type === 'button' && walk(h.pane.element).some(reason => reason.id === x['aria-describedby'])));
    assert.match(h.pane.element.textContent, /launch remains unavailable/i);
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
  return {schema_version: 1, current: true, batch_sha256: digest, publication_sha256: digest, publication: {schema_version: 1, id: 'packetpub', profile_id: 'owner', application_id: 'app', batch_id: 'batch', batch_sha256: digest}, review_eligible: false, browser_eligible: false,
    batch: {schema_version: 1, id: 'batch', profile_id: 'owner', application_id: 'app', research_run_id: 'run', revisions: {...vector}, canonical_questions: [questions[0]], cover_letter_target: questions[1], hiring_criteria: [],
      packets: questions.map((question, i) => ({schema_version: 1, id: `p${i}`, profile_id: 'owner', application_id: 'app', research_run_id: 'run', question, facts: [], employer: [], criteria: [], gaps: ['<img onerror=alert(1)>']}))}};
}
function parent() {
  return {schema_version: 1, current: true, text_sha256: digest, ledger_sha256: digest, publication_sha256: digest,
    publication: {id: 'pub', origin: 'edit', profile_id: 'owner', application_id: 'app', draft_id: 'edit', batch_id: 'batch', batch_sha256: digest, text_sha256: digest, ledger_sha256: digest},
    draft: {schema_version: 1, id: 'edit', profile_id: 'owner', application_id: 'app', research_run_id: 'run', revisions: {...vector}, answers: [{schema_version: 1, question_id: 'q', packet_id: 'p0', text: 'a\r\né😀 '}], cover_letter: '', ledger: [], assessment_state: 'unassessed', inventory_complete: false},
    writing_targets: packet().batch.packets.map(p => ({question: p.question, packet_id: p.id}))};
}
const currentState = consent => ({schema_version: 1, profile_id: 'owner', application_id: 'app', current_revisions: {...vector, consent: consent == null ? 0 : 4}, consent});
const consent = (provider = 'openai', purposes = ['drafting', 'assessment'], granted = true) => ({provider, purposes, granted, revision: 4});
function preview(body, kind) {
  return {schema_version: 1, review_eligible: false, browser_eligible: false, disclosure_sha256: digest,
    disclosure: {schema_version: 1, ...body, batch_id: 'batch', batch_sha256: digest, selected_context: {company: '<img src=x onerror=alert(1)>', role: 'Role', sector: 'tech', targets: packet().batch.packets.map(p => ({question: p.question, packet_id: p.id, facts: p.facts, employer: p.employer, criteria: p.criteria, gaps: p.gaps}))},
      selected_context_sha256: digest, prompt_version: 'v1', output_schema_version: 1, consent_revision: body.expected_revisions.consent, maximum_calls: kind === 'generation' ? 5 : 1,
      conservative_reservation_units: (kind === 'generation' ? 5 : 1) * (131072 + body.max_output_tokens), reservation_unit_description: 'Conservative UTF-8 byte input plus output ceiling; not exact tokens or money', configured_limits: null, monetary_cost: null, pricing_status: 'unknown', semantic_assessment: 'fallible_not_release_qualified',
      ...(kind === 'assessment' ? {input_draft: {schema_version: 1, targets: [{schema_version: 1, target: 'q', text: 'a\r\né😀 '}, {schema_version: 1, target: 'cover_letter', text: ''}]}} : {})}};
}
function ready(kind = 'generation') { return {...capture(), packetInspection: packet(), draftInspection: parent(), bufferMatchesSaved: kind === 'assessment'}; }
function realApi({permission = consent(), post = (body, kind) => preview(body, kind), get = null} = {}) {
  return async (path, options) => options ? post(options.body, path.includes('/assessment/') ? 'assessment' : 'generation') : get ? get() : currentState(permission);
}
function configured(h) { h.model.value = 'chosen-model'; h.model.events.input(); }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
test('explicit generation displays literal selected data, costs, consent and limits without launch controls', async () => {
  const h = harness({snapshot: ready(), api: realApi()}); try {
    configured(h); assert.equal(h.calls.length, 0); assert.equal(named(h.pane.element, generation).disabled, false);
    named(h.pane.element, generation).click(); await tick();
    assert.equal(h.calls.length, 2); assert.match(h.pane.element.textContent, /three to five calls/); assert.match(h.pane.element.textContent, /at most one rewrite/);
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
  const h = harness({snapshot: ready(), api: realApi({permission})}); try { configured(h); named(h.pane.element, generation).click(); await tick(); assert.equal(h.calls.length, 2); assert.match(h.pane.element.textContent, expected); } finally { h.restore(); }
});
test('assessment displays exact saved Unicode/CRLF and empty cover letter with one critique and no rewrite', async () => {
  const h = harness({snapshot: ready('assessment'), api: realApi()}); try {
    configured(h); named(h.pane.element, assessment).click(); await tick();
    assert.equal(h.calls.length, 2); assert.match(h.pane.element.textContent, /one critique call, no rewrite/);
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
    assert.doesNotMatch(h.pane.element.textContent, /secret/); assert.equal(h.notices.length, 1); assert.equal(h.calls.length, 2);
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
