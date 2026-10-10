import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createClient, EditBuffers, externalUrl} from '../backend/ui/workspace/client.js';
import {ActivityController, visibleJobs, jobNotice, usageNotice, canonicalPreview} from '../backend/ui/workspace/activity.js';

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
test('activity routes capture owner/application and use token plus exact cancel body', async () => {
  const calls = [];
  const api = createClient({token: () => 'synthetic', fetchImpl: async (path, init) => { calls.push([path, init]); return {ok: true, json: async () => ({job: {id: 'j'}})}; }});
  const controller = new ActivityController({api, owner: 'owner/a', application: 'app/b', update() {}});
  await controller.jobs(); await controller.usage(); await controller.history(); await controller.run('r/a'); await controller.source('s/a'); await controller.status('j/a'); await controller.cancel('j/a');
  assert.deepEqual(calls.map(([path]) => path), [
    '/api/workspace/profiles/owner%2Fa/jobs', '/api/workspace/profiles/owner%2Fa/usage',
    '/api/workspace/profiles/owner%2Fa/applications/app%2Fb/research',
    '/api/workspace/profiles/owner%2Fa/applications/app%2Fb/research/r%2Fa',
    '/api/workspace/profiles/owner%2Fa/applications/app%2Fb/research/r%2Fa/sources/s%2Fa',
    '/api/workspace/profiles/owner%2Fa/jobs/j%2Fa', '/api/workspace/profiles/owner%2Fa/jobs/j%2Fa/cancel']);
  assert.ok(calls.every(([, init]) => init.headers['X-Evidence-Token'] === 'synthetic'));
  assert.equal(calls.at(-1)[1].body, '{"schema_version":1}');
  assert.equal(calls.at(-1)[1].method, 'POST');
});
for (const slot of ['jobs', 'usage', 'run', 'source']) for (const fail of [false, true]) {
  test(`${slot}: reversed ${fail ? 'errors' : 'successes'} and retired contexts are denied`, async () => {
    const pending = [], updates = [];
    const controller = new ActivityController({api: () => { const d = deferred(); pending.push(d); return d.promise; }, owner: 'a', application: 'one', update: (...args) => updates.push(args)});
    const invoke = () => controller[slot]('id');
    const first = invoke(), second = invoke();
    pending[1].resolve({new: true}); await second;
    if (fail) pending[0].reject(new Error('old')); else pending[0].resolve({old: true});
    await first;
    assert.deepEqual(updates, [[slot, {new: true}, null]]);
    for (const context of ['owner-b:one', 'owner-b:two', 'same-pane-rerender']) {
      const old = invoke(); controller.retire(context);
      if (fail) pending.at(-1).reject(new Error('retired')); else pending.at(-1).resolve({retired: true});
      await old;
    }
    assert.equal(updates.length, 1);
  });
}
test('run selection/history refresh invalidates pending source success and error', async () => {
  for (const fail of [false, true]) {
    const pending = [], updates = [];
    const controller = new ActivityController({api: () => { const d = deferred(); pending.push(d); return d.promise; }, owner: 'a', application: 'one', update: (...args) => updates.push(args)});
    const source = controller.source('old'); const run = controller.run('new');
    pending[1].resolve({}); await run;
    if (fail) pending[0].reject(new Error('old source')); else pending[0].resolve({});
    await source; assert.deepEqual(updates.map(x => x[0]), ['run']);
    const olderRun = controller.run('older'); const history = controller.history();
    pending.at(-1).resolve({current_run_id: null, runs: []}); await history;
    pending.at(-2).resolve({}); await olderRun;
    assert.deepEqual(updates.map(x => x[0]), ['run', 'history']);
  }
});
test('scope filtering, unknown prices and cancellation never invent terminal success', () => {
  assert.deepEqual(visibleJobs([{id: 'a', application_id: 'one'}, {id: 'b', application_id: 'two'}, {id: 'c', application_id: null, kind: 'index'}, {id: 'd', kind: 'cleanup'}], 'one').map(j => j.id), ['a', 'c']);
  assert.match(jobNotice({state: 'running', cancellation_requested: true}), /requested.*not terminal/);
  assert.match(jobNotice({state: 'cancelled'}), /Terminal cancellation/);
  assert.match(usageNotice({reserved_tokens: 4, reported_tokens: 2, unknown_attempts: 1, monetary_cost: null}), /unknown.*not zero/);
  assert.match(usageNotice({}), /Reserved tokens: unknown/);
});
test('canonical text is bounded by code point, literal, and URLs reject hostile protocols', () => {
  assert.equal(canonicalPreview({unit: {text: '🚀<script>'}}, 2), '🚀<\n\nTruncated: showing 2 of 9 canonical code points.');
  assert.match(canonicalPreview({unit: {text: '<script>alert(1)</script>'}}), /<script>/);
  for (const value of ['javascript:alert(1)', 'data:text/html,evil', 'https://user:secret@evil.invalid']) assert.equal(externalUrl(value), null);
});
test('explicit activity refresh does not replace or acknowledge main edit forms; no paid controls', async () => {
  const edits = new EditBuffers(); const form = {}; edits.changed(form);
  const controller = new ActivityController({api: async () => ({}), owner: 'a', application: 'one', update() {}});
  await controller.jobs(); await controller.usage(); await controller.history();
  assert.equal(edits.dirty, true);
  const module = await readFile(new URL('../backend/ui/workspace/activity.js', import.meta.url), 'utf8');
  assert.doesNotMatch(module, /loadSelected|refreshAfter|acknowledge|innerHTML|setInterval|\/retry|\/original/);
  assert.doesNotMatch(module, /button\('(Launch|Draft|Review|Fill|Submit|Retry)/);
});
test('status/cancel responses and errors cannot overwrite a newer jobs refresh', async () => {
  for (const method of ['status', 'cancel']) for (const fail of [false, true]) {
    const pending = [], updates = [];
    const controller = new ActivityController({api: () => { const d = deferred(); pending.push(d); return d.promise; }, owner: 'a', application: 'one', update: (...args) => updates.push(args)});
    const old = controller[method]('job'); const refresh = controller.jobs();
    pending[1].resolve({jobs: []}); await refresh;
    if (fail) pending[0].reject(new Error('stale cancel/status error')); else pending[0].resolve({job: {id: 'job', state: 'running'}});
    await old; assert.deepEqual(updates, [['jobs', {jobs: []}, null]]);
  }
});
test('main application wiring retires activity on context and rerender while preserving forms', async () => {
  const app = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  assert.match(app, /function context\(\) \{\s+retireActivity\(\)/);
  assert.match(app, /async function render\(\) \{\s+if \(state.dirty\) return false;\s+retireActivity\(\)/);
  assert.match(app, /activityPane\(\{api, owner: state.owner, application: application.application_id\}\)/);
  assert.match(app, /applicationForm\(application\)/);
  assert.match(app, /Record local feedback/);
  assert.match(jobNotice({state: 'running', cleanup_pending: true, revisions: {facts: 2}}), /cleanup pending: true.*captured revisions: \{"facts":2\}/);
});
test('activity metadata wraps long hashes and JSON without global overflow hiding', async () => {
  const module = await readFile(new URL('../backend/ui/workspace/activity.js', import.meta.url), 'utf8');
  const css = await readFile(new URL('../backend/ui/workspace/styles.css', import.meta.url), 'utf8');
  assert.match(module, /pane\.classList\.add\('application-activity'\)/);
  assert.match(css, /\.application-activity\s*\{\s*overflow-wrap:\s*anywhere\s*;?\s*\}/);
});
