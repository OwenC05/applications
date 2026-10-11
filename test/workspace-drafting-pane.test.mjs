import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {EditBuffers} from '../backend/ui/workspace/client.js';
import {draftingPane, savedSpan, writingWarning} from '../backend/ui/workspace/drafting-pane.js';

// An intentionally small synthetic DOM verifies events and node preservation.
// It is NOT a browser, layout, focus/caret or accessibility qualification.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; this.disabled = false; this.events = {}; this.classList = {add() {}}; }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return (this.content ?? '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.content = ''; this.children = []; this.append(...children); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  removeEventListener(name, callback) { if (this.events[name] === callback) delete this.events[name]; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  click() { return this.onclick?.(); }
}
const walk = root => [root, ...root.children.flatMap(walk)];
const find = (root, label) => walk(root).find(item => item.tagName === 'button' && item.textContent === label);
const selectNamed = (root, label) => walk(root).find(item => item.tagName === 'label' && item.children[0]?.textContent === label).children[1];
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const revisions = {schema_version: 1, documents: 1, facts: 1, metadata: 1, consent: 1, application_input: 1, application_output: 1, research: 1};
function inspection(id = 'parent', value = 'Saved 🚀\r\ntext') {
  return {schema_version: 1, draft: {schema_version: 1, id, profile_id: 'owner', application_id: 'app', research_run_id: 'research', revisions: {...revisions}, answers: [{schema_version: 1, question_id: 'q', text: value, packet_id: 'packet'}], cover_letter: '', ledger: [], assessment_state: 'unassessed', inventory_complete: false},
    publication: {schema_version: 1, id: 'pub', origin: 'generation', profile_id: 'owner', application_id: 'app', draft_id: id, text_sha256: 'a'.repeat(64), ledger_sha256: 'b'.repeat(64), style_notes: []}, text_sha256: 'a'.repeat(64), ledger_sha256: 'b'.repeat(64), publication_sha256: 'c'.repeat(64), current: true,
    writing_targets: [{question: {schema_version: 1, id: 'q', type: 'writing', text: '<script>Question</script>', required: true, max_chars: 200, max_words: null, options: [], constraint_origin: 'user'}, packet_id: 'packet'}], reason_codes: [], manual_requirements: []};
}
function accepted(parent, body, id = 'child') {
  const child = inspection(id, body.draft.targets[0].text);
  child.draft.revisions.application_output++;
  child.publication = {schema_version: 1, id: 'child-pub', origin: 'edit', profile_id: 'owner', application_id: 'app', draft_id: id, parent_draft_id: parent.draft.id,
    parent_text_sha256: parent.text_sha256, parent_ledger_sha256: parent.ledger_sha256, parent_publication_id: parent.publication.id, parent_publication_sha256: parent.publication_sha256,
    pre_revisions: body.expected_revisions, request: body, text_sha256: child.text_sha256, ledger_sha256: child.ledger_sha256, style_notes: []};
  return child;
}
async function harness({respond, confirmDiscard = () => true, exportText = async () => 'SAVED export', recovery = null, onHandoff = () => false, initialize = true} = {}) {
  const previous = globalThis.document; globalThis.document = {createElement: tag => new Element(tag)};
  const calls = [], edits = new EditBuffers(), notices = [], published = [];
  const parent = inspection();
  const api = async (path, options) => { calls.push([path, options]); if (respond) { const result = respond(path, options, parent); if (result !== undefined) return result; }
    if (path.endsWith('/drafts')) return {drafts: [parent]};
    if (path.endsWith('/packets')) return {batches: []};
    return parent;
  };
  const pane = draftingPane({api, exportText, recovery, onHandoff, owner: 'owner', application: 'app', edits, notify: value => notices.push(value), keyFactory: () => 'logical-key', confirmDiscard, onPublished: value => published.push(value)});
  const select = selectNamed(pane.element, 'Saved draft revision');
  if (initialize) { find(pane.element, 'Refresh saved draft list').click(); await tick(); select.value = 'parent'; select.onchange(); await tick(); }
  const input = walk(pane.element).find(item => item.tagName === 'textarea');
  return {pane, calls, edits, parent, input, select, published, restore() { pane.retire(); globalThis.document = previous; }};
}
function type(input, value) { input.value = value; input.events.input(); }

test('saved ranges and counters use code points, exact excerpts and actual max_chars', () => {
  const draft = {answers: [{question_id: 'q', text: '🚀e\u0301\r\n<script>'}], cover_letter: ''};
  assert.equal(savedSpan(draft, {target: 'q', start: 0, end: 3, excerpt: '🚀e\u0301'}), '🚀e\u0301');
  for (const span of [{target: 'q', start: 0, end: 2, excerpt: 'bad'}, {target: 'q', start: -1, end: 2, excerpt: ''}, {target: 'missing', start: 0, end: 1, excerpt: 'x'}]) assert.equal(savedSpan(draft, span), null);
  assert.match(writingWarning({max_chars: 1}, '🚀x'), /2 Unicode.*Character limit exceeded/);
  assert.match(writingWarning({required: true}, ''), /Required writing is blank/);
  assert.match(writingWarning({max_words: 1}, 'one two'), /Word limit exceeded/);
});
test('explicit lists and unrelated refresh preserve editor nodes and dirty other forms', async () => {
  const h = await harness();
  try {
    const other = {}; h.edits.changed(other); type(h.input, '<img src=x onerror=alert(1)>🚀');
    find(h.pane.element, 'Refresh saved packet list').click(); find(h.pane.element, 'Refresh saved draft list').click(); await tick();
    assert.equal(walk(h.pane.element).find(item => item.tagName === 'textarea'), h.input);
    assert.equal(h.input.value, '<img src=x onerror=alert(1)>🚀'); assert.equal(h.edits.dirty, true);
    assert.match(h.pane.element.textContent, /saved assessment does not apply/);
    assert.equal(h.select.value, 'parent');
    find(h.pane.element, 'Refresh selected saved inspection').click(); await tick();
    assert.equal(walk(h.pane.element).find(item => item.tagName === 'textarea'), h.input);
    assert.match(h.pane.element.textContent, /baseline are unchanged/);
  } finally { h.restore(); }
});
test('successful immutable save acknowledges only own captured generation, preserving editor node', async () => {
  const h = await harness({respond(path, options, parent) { if (options?.method === 'POST') return accepted(parent, options.body); }});
  try {
    const other = {}; h.edits.changed(other); type(h.input, 'New writing');
    find(h.pane.element, 'Save new local revision').click(); await tick();
    assert.equal(h.published.length, 1); assert.equal(h.select.value, 'child');
    assert.equal(h.input.value, 'New writing'); assert.equal(walk(h.pane.element).find(item => item.tagName === 'textarea'), h.input);
    assert.equal(h.edits.dirty, true); assert.equal(find(h.pane.element, 'Save new local revision').disabled, true);
  } finally { h.restore(); }
});
for (const kind of ['lost', '409', 'malformed']) test(`${kind}: explicit reconciliation reuses exact operation and preserves newer typing`, async () => {
  const wait = deferred(); let committed, count = 0;
  const h = await harness({respond(path, options, parent) {
    if (options?.method !== 'POST') return;
    count++; committed ??= accepted(parent, options.body);
    if (count === 1) return wait.promise;
    return committed;
  }});
  try {
    type(h.input, 'Captured writing'); find(h.pane.element, 'Save new local revision').click();
    type(h.input, 'Newer typing 🚀');
    if (kind === 'malformed') wait.resolve({not: 'inspection'});
    else { const error = new Error('detail after commit failed'); if (kind === '409') error.status = 409; wait.reject(error); }
    await tick();
    assert.equal(h.input.value, 'Newer typing 🚀'); assert.equal(h.published.length, 0);
    assert.equal(find(h.pane.element, 'Save new local revision').disabled, true);
    assert.equal(find(h.pane.element, 'Resolve saved edit outcome').disabled, false);
    find(h.pane.element, 'Resolve saved edit outcome').click(); await tick();
    const writes = h.calls.filter(([, options]) => options?.method === 'POST');
    assert.equal(writes.length, 2); assert.deepEqual(writes[0], writes[1]);
    assert.equal(h.input.value, 'Newer typing 🚀'); assert.equal(h.select.value, 'parent'); assert.equal(h.published.length, 1);
    assert.equal(h.edits.dirty, true); assert.equal(find(h.pane.element, 'Save new local revision').disabled, true);
  } finally { h.restore(); }
});
test('retirement suppresses late save, publication callback and future refresh requests', async () => {
  const wait = deferred();
  const h = await harness({respond(path, options) { if (options?.method === 'POST') return wait.promise; }});
  try {
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click();
    const refresh = find(h.pane.element, 'Refresh saved draft list');
    h.pane.retire(); const count = h.calls.length;
    const body = h.calls.find(([, options]) => options?.method === 'POST')[1].body;
    wait.resolve(accepted(h.parent, body)); await tick();
    refresh.click(); await tick();
    assert.equal(h.calls.length, count); assert.equal(h.published.length, 0); assert.equal(h.edits.dirty, false);
  } finally { h.restore(); }
});
test('pane source is literal DOM only, bounded local controls and fixed saved-text export', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/drafting-pane.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /innerHTML|fetch\(|loadSelected|setInterval|edits\.clear|localStorage|\/reassess|\/submit|\/review/);
  assert.match(source, /application-draft\.txt/); assert.match(source, /URL\.revokeObjectURL/);
  assert.match(source, /new Blob\(\[data\]/); assert.match(source, /edits\.acknowledge\(pending\.generation\)/);
});
test('declined draft switch preserves buffer and only asks about this pane', async () => {
  let prompts = 0;
  const h = await harness({confirmDiscard(message) { prompts++; assert.match(message, /only this writing buffer/); return false; }});
  try {
    type(h.input, 'Keep me'); const count = h.calls.length;
    h.select.value = 'another'; h.select.onchange(); await tick();
    assert.equal(prompts, 1); assert.equal(h.select.value, 'parent'); assert.equal(h.input.value, 'Keep me');
    assert.equal(h.calls.length, count); assert.equal(h.edits.dirty, true);
  } finally { h.restore(); }
});
test('exact undo acknowledges only own form and required/length warnings never block local save', async () => {
  const h = await harness();
  try {
    const other = {}; h.edits.changed(other);
    type(h.input, ''); assert.match(h.pane.element.textContent, /Required writing is blank/);
    assert.equal(find(h.pane.element, 'Save new local revision').disabled, false);
    type(h.input, h.parent.draft.answers[0].text);
    assert.equal(find(h.pane.element, 'Save new local revision').disabled, true);
    assert.equal(h.edits.dirty, true);
    assert.doesNotMatch(h.pane.element.textContent, /saved assessment does not apply/);
  } finally { h.restore(); }
});
test('saved-only download has fixed filename, revokes URL and never acknowledges writing', async () => {
  const created = [], revoked = [], clicks = [];
  const create = URL.createObjectURL, revoke = URL.revokeObjectURL, click = Element.prototype.click;
  URL.createObjectURL = blob => { created.push(blob); return 'blob:synthetic'; };
  URL.revokeObjectURL = value => revoked.push(value);
  Element.prototype.click = function () { if (this.tagName === 'a') clicks.push({download: this.download, href: this.href}); else return click.call(this); };
  const h = await harness();
  try {
    type(h.input, 'UNSAVED writing'); find(h.pane.element, 'Export SAVED revision (.txt)').click(); await tick();
    assert.equal(await created[0].text(), 'SAVED export');
    assert.deepEqual(clicks, [{download: 'application-draft.txt', href: 'blob:synthetic'}]);
    assert.deepEqual(revoked, ['blob:synthetic']); assert.equal(h.edits.dirty, true);
    assert.equal(h.input.value, 'UNSAVED writing');
  } finally { h.restore(); URL.createObjectURL = create; URL.revokeObjectURL = revoke; Element.prototype.click = click; }
});
test('late selected inspection cannot restore parent after accepted save', async () => {
  const wait = deferred(); let detailCount = 0;
  const h = await harness({respond(path, options, parent) {
    if (options?.method === 'POST') return accepted(parent, options.body);
    if (path.endsWith('/drafts/parent') && ++detailCount > 1) return wait.promise;
  }});
  try {
    find(h.pane.element, 'Refresh selected saved inspection').click();
    type(h.input, 'Save child'); find(h.pane.element, 'Save new local revision').click(); await tick();
    wait.resolve(h.parent); await tick();
    assert.equal(h.select.value, 'child'); assert.equal(h.input.value, 'Save child');
    assert.equal(h.published.length, 1); assert.match(h.pane.element.textContent, /Revision child/);
  } finally { h.restore(); }
});
test('retired late export never creates a download and preserves unsaved writing', async () => {
  const wait = deferred(), create = URL.createObjectURL; let downloads = 0;
  URL.createObjectURL = () => { downloads++; return 'blob:unexpected'; };
  const h = await harness({exportText: () => wait.promise});
  try {
    type(h.input, 'Unsaved'); find(h.pane.element, 'Export SAVED revision (.txt)').click(); h.pane.retire();
    wait.resolve('Saved'); await tick(); assert.equal(downloads, 0); assert.equal(h.edits.dirty, false);
  } finally { h.restore(); URL.createObjectURL = create; }
});
test('created but no-longer-current child remains inspectable without edit authority', async () => {
  const h = await harness({respond(path, options, parent) {
    if (options?.method === 'POST') { const result = accepted(parent, options.body); result.current = false; return result; }
  }});
  try {
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click(); await tick();
    assert.equal(h.published.length, 1); assert.equal(h.published[0].current, false); assert.equal(h.input.disabled, true);
    assert.equal(find(h.pane.element, 'Save new local revision').disabled, true);
    assert.match(h.pane.element.textContent, /server-current at last inspection: false/);
  } finally { h.restore(); }
});
for (const action of ['discard', 'adopt', 'switch']) test(`uncertain save cannot escape via ${action}, including dirty same-ID refresh`, async () => {
  let committed, writes = 0;
  const h = await harness({respond(path, options, parent) {
    if (options?.method === 'POST') {
      writes++; committed ??= accepted(parent, options.body);
      if (writes === 1) throw Object.assign(new Error('post-commit inspection unavailable'), {status: 409});
      return committed;
    }
  }});
  try {
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click(); await tick();
    type(h.input, 'Newer typing');
    find(h.pane.element, 'Refresh selected saved inspection').click(); await tick();
    const count = h.calls.length;
    if (action === 'discard') find(h.pane.element, 'Discard this writing buffer and inspect selected saved revision').click();
    if (action === 'adopt') find(h.pane.element, 'Inspect/adopt saved revision').click();
    if (action === 'switch') { h.select.value = 'another'; h.select.onchange(); }
    await tick();
    assert.equal(h.calls.length, count); assert.equal(h.select.value, 'parent');
    assert.equal(h.input.value, 'Newer typing'); assert.equal(h.edits.dirty, true);
    assert.equal(find(h.pane.element, 'Save new local revision').disabled, true);
    assert.equal(find(h.pane.element, 'Resolve saved edit outcome').disabled, false);
    find(h.pane.element, 'Resolve saved edit outcome').click(); await tick();
    const sent = h.calls.filter(([, options]) => options?.method === 'POST');
    assert.deepEqual(sent[0], sent[1]); assert.equal(h.input.value, 'Newer typing');
    assert.equal(h.published.length, 1);
  } finally { h.restore(); }
});
for (const mutation of ['wrong-id', 'wrong-owner', 'missing-targets', 'bad-bindings']) test(`dirty refreshed ${mutation} detail cannot become adoptable`, async () => {
  let reads = 0;
  const h = await harness({respond(path, options, parent) {
    if (!path.endsWith('/drafts/parent') || ++reads === 1) return;
    const value = structuredClone(parent);
    if (mutation === 'wrong-id') value.draft.id = 'another';
    if (mutation === 'wrong-owner') value.draft.profile_id = 'another';
    if (mutation === 'missing-targets') delete value.writing_targets;
    if (mutation === 'bad-bindings') value.draft.answers[0].packet_id = 'wrong';
    return value;
  }});
  try {
    type(h.input, 'Keep buffer'); find(h.pane.element, 'Refresh selected saved inspection').click(); await tick();
    assert.equal(find(h.pane.element, 'Inspect/adopt saved revision').disabled, true);
    assert.equal(h.select.value, 'parent'); assert.equal(h.input.value, 'Keep buffer'); assert.equal(h.edits.dirty, true);
    assert.equal(walk(h.pane.element).find(item => item.tagName === 'textarea'), h.input);
  } finally { h.restore(); }
});
test('pane unresolved getter covers pending save, uncertainty and replay but clears after correlated result', async () => {
  const first = deferred(), replay = deferred(); let writes = 0, retained;
  const h = await harness({respond(path, options, parent) {
    if (options?.method !== 'POST') return;
    retained ??= accepted(parent, options.body); return ++writes === 1 ? first.promise : replay.promise;
  }});
  try {
    assert.equal(h.pane.unresolved, false);
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click();
    assert.equal(h.pane.unresolved, true);
    first.reject(new Error('Unknown save outcome')); await tick(); assert.equal(h.pane.unresolved, true);
    find(h.pane.element, 'Resolve saved edit outcome').click(); assert.equal(h.pane.unresolved, true);
    replay.resolve(retained); await tick(); assert.equal(h.pane.unresolved, false);
  } finally { h.restore(); }
});
test('explicit adoption updates visible selector and removes resolved-unsaved assessment notice', async () => {
  const wait = deferred(); let captured;
  const h = await harness({respond(path, options, parent) { if (options?.method === 'POST') { captured = accepted(parent, options.body); return wait.promise; } }});
  try {
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click();
    type(h.input, 'New typing'); wait.resolve(captured); await tick();
    assert.equal(h.select.value, 'parent');
    find(h.pane.element, 'Inspect/adopt saved revision').click();
    assert.equal(h.select.value, 'child'); assert.ok(h.select.children.some(item => item.value === 'child'));
    assert.equal(h.pane.unresolved, false); assert.equal(h.edits.dirty, false);
    assert.doesNotMatch(h.pane.element.textContent, /Unsaved or unresolved writing/);
    assert.match(h.pane.element.textContent, /Saved assessment: unassessed/);
    assert.equal(walk(h.pane.element).find(item => item.tagName === 'textarea').value, 'Captured');
  } finally { h.restore(); }
});
test('permanent 409 can hand off copied read-only recovery, remount with no IO and replay only exact key', async () => {
  let capsule, attempts = 0;
  const respond = (path, options) => { if (options?.method === 'POST') { attempts++; throw Object.assign(new Error('Permanent conflict'), {status: 409}); } };
  const h = await harness({respond, onHandoff(value) { capsule = value; return true; }});
  try {
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click(); await tick();
    type(h.input, 'Latest\r\n🚀');
    // The displayed value is not authority: mimic native normalization without input.
    h.input.value = 'Latest\n🚀';
    find(h.pane.element, 'Continue read-only with unresolved outcome').click();
    assert.equal(h.pane.unresolved, false); assert.equal(h.input.disabled, true);
    assert.deepEqual(Object.keys(capsule).sort(), ['operation', 'targets']);
    assert.equal(capsule.targets[0].text, 'Latest\r\n🚀');
    assert.equal(capsule.operation.body.draft.targets[0].text, 'Captured');
    const originalBody = structuredClone(capsule.operation.body);
    capsule.targets[0].text = 'External mutation';
    assert.equal(h.input.value, 'Latest\n🚀');
    capsule.targets[0].text = 'Latest\r\n🚀';
    h.restore();
    const restored = await harness({respond, recovery: capsule, initialize: false});
    try {
      assert.equal(restored.calls.length, 0); assert.equal(restored.pane.unresolved, false);
      assert.equal(restored.input.value, 'Latest\r\n🚀'); assert.equal(restored.input.disabled, true);
      assert.equal(find(restored.pane.element, 'Save new local revision').disabled, true);
      find(restored.pane.element, 'Resolve saved edit outcome').click(); await tick();
      assert.equal(restored.calls.length, 1); assert.deepEqual(restored.calls[0][1].body, originalBody);
      assert.equal(attempts, 2); assert.equal(restored.pane.unresolved, false);
    } finally { restored.restore(); }
  } finally { h.restore(); }
});
test('handoff is unavailable while saving and requires successful explicit retention', async () => {
  const wait = deferred(); let retained = 0;
  const h = await harness({respond(path, options) { if (options?.method === 'POST') return wait.promise; }, onHandoff() { retained++; return false; }});
  try {
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click();
    const action = find(h.pane.element, 'Continue read-only with unresolved outcome');
    assert.equal(action.disabled, true); action.click(); assert.equal(retained, 0);
    wait.reject(new Error('Unknown')); await tick(); action.click();
    assert.equal(retained, 1); assert.equal(h.pane.unresolved, true); assert.equal(h.input.disabled, false);
  } finally { h.restore(); }
});
test('lost committed response recovery preserves newer text and correlated replay never silently unlocks', async () => {
  let capsule, child, firstBody;
  const h = await harness({respond(path, options, parent) { if (options?.method === 'POST') { firstBody = structuredClone(options.body); child = accepted(parent, options.body); throw new Error('Lost committed response'); } }, onHandoff(value) { capsule = value; return true; }});
  try {
    type(h.input, 'Captured'); find(h.pane.element, 'Save new local revision').click(); await tick();
    type(h.input, 'Newer\r\ntext'); find(h.pane.element, 'Continue read-only with unresolved outcome').click();
    h.restore();
    const restored = await harness({recovery: capsule, initialize: false, respond(path, options) { if (options?.method === 'POST') return child; }});
    try {
      find(restored.pane.element, 'Resolve saved edit outcome').click(); await tick();
      assert.deepEqual(restored.calls[0][1].body, firstBody); assert.equal(restored.published.length, 1);
      assert.equal(restored.input.value, 'Newer\r\ntext'); assert.equal(restored.input.disabled, true);
      assert.equal(restored.select.value, 'parent');
      assert.equal(find(restored.pane.element, 'Save new local revision').disabled, true);
      assert.equal(find(restored.pane.element, 'Inspect/adopt saved revision').disabled, true);
      assert.match(restored.pane.element.textContent, /Revision child/);
    } finally { restored.restore(); }
  } finally { h.restore(); }
});
test('mismatched owner recovery is rejected without IO or new save authority', async () => {
  const parent = inspection(); parent.draft.profile_id = 'other'; parent.publication.profile_id = 'other';
  const recovery = {operation: {parentDraftId: 'parent', parentCapture: parent, body: {}}, targets: [{target: 'q', text: 'Private'}]};
  const h = await harness({recovery, initialize: false});
  try {
    assert.equal(h.calls.length, 0); assert.equal(find(h.pane.element, 'Save new local revision').disabled, true);
    assert.doesNotMatch(h.pane.element.textContent, /Private/); assert.match(h.pane.element.textContent, /recovery.*invalid|invalid.*recovery/i);
  } finally { h.restore(); }
});
for (const mutation of ['key', 'targets', 'application']) test(`invalid retained ${mutation} capsule cannot auto-send or enable mutation`, async () => {
  let capsule;
  const original = await harness({respond(path, options) { if (options?.method === 'POST') throw new Error('Unknown'); }, onHandoff(value) { capsule = value; return true; }});
  try {
    type(original.input, 'Captured'); find(original.pane.element, 'Save new local revision').click(); await tick();
    find(original.pane.element, 'Continue read-only with unresolved outcome').click();
  } finally { original.restore(); }
  if (mutation === 'key') capsule.operation.body.idempotency_key = '';
  if (mutation === 'targets') capsule.targets.push({target: 'unexpected', text: 'Private'});
  if (mutation === 'application') capsule.operation.parentCapture.draft.application_id = 'other';
  const restored = await harness({recovery: capsule, initialize: false});
  try {
    assert.equal(restored.calls.length, 0); assert.equal(find(restored.pane.element, 'Save new local revision').disabled, true);
    assert.equal(find(restored.pane.element, 'Resolve saved edit outcome').disabled, true);
    assert.match(restored.pane.element.textContent, /Invalid recovery/);
  } finally { restored.restore(); }
});
test('retained read-only recovery can inspect/export other revisions then replay original operation without replacing CRLF writing', async () => {
  let capsule;
  const original = await harness({respond(path, options) { if (options?.method === 'POST') throw new Error('Unknown'); }, onHandoff(value) { capsule = value; return true; }});
  try {
    type(original.input, 'Captured'); find(original.pane.element, 'Save new local revision').click(); await tick();
    type(original.input, 'Newer\r\n🚀'); find(original.pane.element, 'Continue read-only with unresolved outcome').click();
  } finally { original.restore(); }
  const wait = deferred(), exports = [], create = URL.createObjectURL, revoke = URL.revokeObjectURL;
  URL.createObjectURL = () => 'blob:synthetic'; URL.revokeObjectURL = () => {};
  const recovered = await harness({recovery: capsule, initialize: false,
    respond(path, options) { if (options?.method === 'POST') return wait.promise; if (path.endsWith('/drafts/other')) return inspection('other', 'Other saved text'); },
    exportText: async path => { exports.push(path); return 'Other saved text'; }});
  try {
    const input = recovered.input;
    recovered.select.value = 'other'; recovered.select.onchange(); await tick();
    assert.equal(recovered.select.value, 'other'); assert.match(recovered.pane.element.textContent, /Revision other/);
    assert.equal(input.value, 'Newer\r\n🚀'); assert.equal(input.disabled, true);
    find(recovered.pane.element, 'Export SAVED revision (.txt)').click(); await tick();
    assert.match(exports[0], /\/drafts\/other\/export$/);
    find(recovered.pane.element, 'Resolve saved edit outcome').click();
    assert.equal(recovered.select.value, 'parent'); assert.equal(recovered.pane.unresolved, true);
    recovered.select.value = 'other'; recovered.select.onchange();
    find(recovered.pane.element, 'Export SAVED revision (.txt)').click(); await tick();
    assert.equal(recovered.select.value, 'parent'); assert.equal(exports.length, 1);
    const writes = recovered.calls.filter(([, options]) => options?.method === 'POST');
    assert.equal(writes.length, 1); assert.match(writes[0][0], /\/drafts\/parent\/edits$/);
    assert.deepEqual(writes[0][1].body, capsule.operation.body);
    wait.resolve(accepted(capsule.operation.parentCapture, capsule.operation.body)); await tick();
    assert.equal(input.value, 'Newer\r\n🚀'); assert.equal(input.disabled, true); assert.equal(recovered.pane.unresolved, false);
    assert.match(recovered.pane.element.textContent, /still read-only/);
    assert.doesNotMatch(recovered.pane.element.textContent, /before another save/);
    recovered.select.value = 'other'; recovered.select.onchange(); await tick(); assert.equal(recovered.select.value, 'other');
    assert.equal(walk(recovered.pane.element).find(item => item.tagName === 'textarea'), input);
  } finally { recovered.restore(); URL.createObjectURL = create; URL.revokeObjectURL = revoke; }
});
test('privacy retirement removes own captured prose and form state, preserves unrelated dirty forms, and denies late effects', async () => {
  const wait = deferred(), exported = deferred(); let downloads = 0;
  const create = URL.createObjectURL; URL.createObjectURL = () => { downloads++; return 'blob:unexpected'; };
  const h = await harness({respond(path, options) { if (options?.method === 'POST') return wait.promise; }, exportText: () => exported.promise});
  try {
    const other = {}; h.edits.changed(other); type(h.input, 'PRIVATE unsaved writing');
    find(h.pane.element, 'Export SAVED revision (.txt)').click();
    find(h.pane.element, 'Save new local revision').click();
    const body = h.calls.find(([, options]) => options?.method === 'POST')[1].body;
    h.pane.retire();
    assert.equal(h.input.value, ''); assert.doesNotMatch(h.pane.element.textContent, /PRIVATE|Saved 🚀|Captured/);
    assert.equal(walk(h.pane.element).some(item => item.tagName === 'textarea'), false);
    assert.equal(h.edits.forms.size, 1); assert.equal(h.edits.forms.get(other).dirty, true);
    assert.equal(h.pane.unresolved, false);
    wait.resolve(accepted(h.parent, body)); exported.resolve('PRIVATE export'); await tick();
    assert.equal(h.published.length, 0); assert.equal(downloads, 0);
    assert.doesNotMatch(h.pane.element.textContent, /PRIVATE|Saved 🚀/);
  } finally { h.restore(); URL.createObjectURL = create; }
});
