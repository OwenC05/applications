import test from 'node:test';
import assert from 'node:assert/strict';
import {EditBuffers} from '../backend/ui/workspace/client.js';
import {budgetPane} from '../backend/ui/workspace/budget-pane.js';

// Synthetic event and lifetime checks, not browser/layout/accessibility proof.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; this.checked = false; this.events = {}; this.dataset = {}; }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return (this.content ?? '') + this.children.map(x => x.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(key, value) { this.events[key] = value; }
  removeEventListener(key, value) { if (this.events[key] === value) delete this.events[key]; }
  click() { return this.onclick?.(); }
}
const walk = root => [root, ...root.children.flatMap(walk)];
const keys = ['daily_tokens', 'daily_calls', 'job_tokens', 'job_calls'];
const limits = {daily_tokens: 1000, daily_calls: 10, job_tokens: 100, job_calls: 2};
const envelope = (configured_limits = limits) => ({schema_version: 1, configured_limits, reservation_units: 'conservative_input_bytes_plus_output_ceiling', monetary_cost: null, pricing_status: 'unknown', grants_cloud_consent: false});
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
function harness(api = async () => envelope(), callbacks = {}) {
  const previous = globalThis.document; globalThis.document = {createElement: tag => new Element(tag)};
  const calls = [], notices = [], changes = [], edits = new EditBuffers();
  const pane = budgetPane({api: (...args) => { calls.push(args); return api(...args); }, owner: 'owner /?#', edits,
    notify: value => { notices.push(value); callbacks.notify?.(); }, onConfigurationChanged: () => { changes.push(true); callbacks.changed?.(); }});
  const nodes = walk(pane.element), inputs = Object.fromEntries(keys.map(key => [key, nodes.find(n => n.name === key)]));
  const checkbox = nodes.find(n => n.type === 'checkbox'), form = nodes.find(n => n.tagName === 'form');
  const refresh = nodes.find(n => n.tagName === 'button' && /Refresh/.test(n.textContent));
  const save = nodes.find(n => n.tagName === 'button' && /Save/.test(n.textContent));
  const fill = (values = limits) => { for (const key of keys) { inputs[key].value = String(values[key]); inputs[key].events.input(); } };
  const ack = () => { checkbox.checked = true; checkbox.events.change(); };
  return {pane, calls, notices, changes, edits, inputs, checkbox, form, refresh, save, fill, ack,
    close() { pane.retire(); globalThis.document = previous; }};
}
test('blank stable controls, explicit actions, no construction IO or global action marker', () => {
  const h = harness(); try {
    assert.equal(h.calls.length, 0); assert.equal(h.checkbox.checked, false);
    for (const input of Object.values(h.inputs)) { assert.equal(input.value, ''); assert.equal(input.min, '1'); assert.equal(input.max, '100000000'); assert.equal(input.step, '1'); }
    assert.ok(walk(h.pane.element).every(n => !n.dataset.actionControl));
    assert.match(h.pane.element.textContent, /queued\/running/); assert.match(h.pane.element.textContent, /unknown/);
  } finally { h.close(); }
});
test('explicit GET uses captured encoded scope and never fills or acknowledges controls', async () => {
  const h = harness(async () => envelope(null)); try {
    h.fill(); h.ack(); await h.refresh.click();
    assert.deepEqual(h.calls, [['/api/workspace/profiles/owner%20%2F%3F%23/budget']]);
    assert.equal(h.inputs.daily_tokens.value, '1000'); assert.equal(h.checkbox.checked, false); assert.equal(h.edits.dirty, true);
    assert.match(h.pane.element.textContent, /Not configured/); assert.equal(h.changes.length, 1);
  } finally { h.close(); }
});
for (const value of ['', '0', '-1', '1.5', '1e3', ' 2', '100000001', 'Infinity']) test(`rejects numeric input ${JSON.stringify(value)} before IO`, async () => {
  const h = harness(); try { h.fill(); h.inputs.daily_tokens.value = value; h.ack(); await h.save.click(); assert.equal(h.calls.length, 0); } finally { h.close(); }
});
test('requires own fresh acknowledgement and valid job-to-daily ratios', async () => {
  const h = harness(); try {
    h.fill(); await h.save.click(); assert.equal(h.calls.length, 0);
    h.ack(); h.inputs.daily_calls.events.input(); assert.equal(h.checkbox.checked, false); await h.save.click();
    h.fill({...limits, job_calls: 11}); h.ack(); await h.save.click();
    h.fill({...limits, job_tokens: 1001}); h.ack(); await h.save.click(); assert.equal(h.calls.length, 0);
  } finally { h.close(); }
});
test('immutable POST acknowledges only captured form, preserves newer typing and other edits', async () => {
  const d = deferred(), h = harness(() => d.promise); try {
    const other = {}; h.edits.changed(other); h.fill(); h.ack(); const pending = h.save.click();
    assert.deepEqual(h.calls[0], ['/api/workspace/profiles/owner%20%2F%3F%23/budget', {method: 'POST', body: {...limits, acknowledged: true}}]);
    assert.equal(h.checkbox.checked, false); assert.equal(h.inputs.daily_tokens.disabled, undefined);
    const original = h.inputs.daily_tokens; original.value = '2000'; original.events.input();
    d.resolve(envelope()); await pending;
    assert.equal(h.inputs.daily_tokens, original); assert.equal(original.value, '2000'); assert.equal(h.edits.forms.get(h.form).dirty, true); assert.equal(h.edits.forms.get(other).dirty, true);
    assert.match(h.pane.element.textContent, /observed configuration/i);
  } finally { h.close(); }
});
test('matching POST acknowledges unchanged own form only and accepts exact upper bound', async () => {
  const maximum = Object.fromEntries(keys.map(k => [k, 100000000])); const h = harness(async () => envelope(maximum)); try {
    const other = {}; h.edits.changed(other); h.fill(maximum); h.ack(); await h.save.click();
    assert.equal(h.edits.forms.get(h.form).dirty, false); assert.equal(h.edits.forms.get(other).dirty, true);
  } finally { h.close(); }
});
test('pending guards defeat forced-enabled double/cross-kind clicks and checkbox changes', async () => {
  const d = deferred(), h = harness(() => d.promise); try {
    h.fill(); h.ack(); const pending = h.save.click();
    h.save.disabled = false; h.refresh.disabled = false; h.checkbox.disabled = false;
    await h.save.click(); await h.refresh.click(); h.ack(); assert.equal(h.checkbox.checked, false); assert.equal(h.calls.length, 1);
    d.resolve(envelope()); await pending;
  } finally { h.close(); }
});
const invalid = [null, {...envelope(), schema_version: 2}, {...envelope(), reservation_units: 'tokens'}, {...envelope(), monetary_cost: 0}, {...envelope(), pricing_status: 'known'}, {...envelope(), grants_cloud_consent: true}, envelope({...limits, job_calls: 11}), envelope({...limits, daily_tokens: '1000'}), envelope({...limits, extra: 1}), envelope({...limits, daily_tokens: 100000001})];
for (const [i, response] of invalid.entries()) test(`malformed GET envelope ${i} fails closed without acknowledgement`, async () => {
  const h = harness(async () => response); try { h.fill(); await h.refresh.click(); assert.equal(h.edits.dirty, true); assert.match(h.pane.element.textContent, /failed/i); } finally { h.close(); }
});
for (const [name, result] of [['error', () => { throw new Error('<script>private</script>'); }], ['null limits', () => envelope(null)], ['mismatch', () => envelope({...limits, job_calls: 1})], ['malformed', () => null]]) test(`POST ${name} retains uncertainty until explicit valid observation, never retries automatically`, async () => {
  let phase = 0; const h = harness(async () => phase === 0 ? result() : phase === 1 ? null : envelope()); try {
    h.fill(); h.ack(); await h.save.click(); assert.equal(h.calls.length, 1); assert.equal(h.edits.dirty, true);
    assert.match(h.pane.element.textContent, /outcome unknown/i); assert.doesNotMatch(h.pane.element.textContent, /<script>/);
    h.ack(); await h.save.click(); assert.equal(h.calls.length, 1);
    phase = 1; await h.refresh.click(); h.ack(); await h.save.click(); assert.equal(h.calls.length, 2);
    phase = 2; await h.refresh.click(); assert.equal(h.calls.length, 3); assert.equal(h.edits.dirty, true); assert.equal(h.checkbox.checked, false);
    assert.match(h.pane.element.textContent, /earlier requests or other tabs/i); assert.match(h.pane.element.textContent, /not.*reconciliation/i);
    h.ack(); await h.save.click(); assert.equal(h.calls.length, 4); assert.match(h.pane.element.textContent, /earlier requests or other tabs/i);
  } finally { h.close(); }
});
test('retirement clears retained private DOM and own dirty form, suppressing late completions/callbacks', async () => {
  const d = deferred(), h = harness(() => d.promise); try {
    const other = {}; h.edits.changed(other); h.fill(); h.ack(); const pending = h.save.click();
    const retained = walk(h.pane.element), action = h.refresh.onclick, input = h.inputs.daily_tokens.events.input;
    h.pane.retire(); h.pane.retire(); d.resolve(envelope()); await pending; await action(); input();
    assert.equal(h.calls.length, 1); assert.equal(h.changes.length, 1); assert.equal(h.notices.length, 0);
    assert.equal(h.edits.forms.has(h.form), false); assert.equal(h.edits.forms.get(other).dirty, true);
    for (const n of retained) { assert.equal(n.textContent, ''); assert.equal(n.value, ''); assert.equal(n.checked, false); }
  } finally { h.close(); }
});
test('callback failures do not disrupt request cleanup or acknowledgement', async () => {
  const h = harness(undefined, {changed() { throw new Error('private observer'); }, notify() { throw new Error('private notification'); }}); try {
    h.fill(); h.ack(); await h.save.click(); assert.equal(h.edits.forms.get(h.form).dirty, false);
    await h.refresh.click(); assert.equal(h.calls.length, 2);
  } finally { h.close(); }
});

test('GET pending preserves editable identity and guards save even with forced enabled controls', async () => {
  const d = deferred(), h = harness(() => d.promise); try {
    h.fill(); const input = h.inputs.daily_tokens; const pending = h.refresh.click();
    input.value = '3000'; input.events.input(); h.save.disabled = false; h.refresh.disabled = false;
    await h.refresh.click(); await h.save.click(); assert.equal(h.calls.length, 1);
    d.resolve(envelope()); await pending; assert.equal(input.value, '3000'); assert.equal(h.inputs.daily_tokens, input); assert.equal(h.edits.dirty, true);
  } finally { h.close(); }
});
test('failed GET shows only generic error and notification failures do not leave pending locked', async () => {
  let fail = true; const h = harness(async () => { if (fail) throw new Error('<img private>'); return envelope(); }, {notify() { throw new Error('observer'); }}); try {
    h.fill(); await h.refresh.click(); assert.doesNotMatch(h.pane.element.textContent, /<img private>/); assert.equal(h.refresh.disabled, false);
    fail = false; await h.refresh.click(); assert.equal(h.calls.length, 2); assert.equal(h.edits.dirty, true);
  } finally { h.close(); }
});
test('synchronous configuration observer retirement prevents dispatch and late mutation', async () => {
  let pane; const h = harness(undefined, {changed() { pane.retire(); }}); pane = h.pane; try {
    h.fill(); h.ack(); await h.save.click(); assert.equal(h.calls.length, 0); assert.equal(h.edits.forms.has(h.form), false); assert.equal(h.pane.element.textContent, '');
  } finally { h.close(); }
});
test('retirement scrubs separately retained observed children after a valid refresh', async () => {
  const h = harness(); try {
    await h.refresh.click(); const retained = walk(h.pane.element); assert.ok(retained.some(n => n.tagName === 'pre' && /1000/.test(n.textContent)));
    h.pane.retire(); assert.ok(retained.every(n => n.textContent === ''));
  } finally { h.close(); }
});

test('valid observation and matching later save replace stale global error instructions without settling historical uncertainty', async () => {
  let fail = true;
  const h = harness(async () => { if (fail) throw new Error('lost response'); return envelope(); }); try {
    h.fill(); h.ack(); await h.save.click();
    assert.match(h.notices.at(-1), /outcome unknown/i);
    fail = false; await h.refresh.click();
    assert.match(h.notices.at(-1), /Refresh returned an observed configuration/);
    assert.match(h.notices.at(-1), /does not settle or cancel earlier writes/);
    assert.match(h.pane.element.textContent, /Earlier requests or other tabs may still change/);
    h.ack(); await h.save.click();
    assert.match(h.notices.at(-1), /Save request returned an observed configuration matching/);
    assert.match(h.notices.at(-1), /not a lasting configuration or money\/spend guarantee/);
    assert.match(h.pane.element.textContent, /not reconciliation or retry/);
  } finally { h.close(); }
});
