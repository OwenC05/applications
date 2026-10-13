import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {advanceApplicationOutput, WritingRecovery, EditBuffers, RequestGate} from '../backend/ui/workspace/client.js';

const vector = {schema_version: 1, metadata: 4, facts: 2, documents: 1, consent: 3,
  application_input: 5, application_output: 8, research: 2};
const published = () => ({owner: 'owner', application: 'app', draftId: 'child', current: true,
  preRevisions: {...vector}, postRevisions: {...vector, application_output: 9}});

test('own correlated publication advances only the existing application output field monotonically', () => {
  const app = {profile_id: 'owner', application_id: 'app', input_revision: 5, output_revision: 8, company: 'Synthetic'};
  const before = structuredClone(app);
  assert.equal(advanceApplicationOutput(app, published()), true);
  assert.deepEqual(app, {...before, output_revision: 9});
  assert.equal(advanceApplicationOutput(app, published()), false);
  app.output_revision = 10;
  assert.equal(advanceApplicationOutput(app, published()), false);
  assert.equal(app.output_revision, 10);
});

test('publication callback rejects wrong scope, malformed or non-output-only vectors', () => {
  for (const change of [{owner: 'other'}, {application: 'other'}, {draftId: ''}, {current: 'true'},
    {postRevisions: {...vector, application_output: 10}},
    {postRevisions: {...vector, facts: 3, application_output: 9}},
    {preRevisions: {...vector, metadata: true}}, {postRevisions: {application_output: 9}}]) {
    const app = {profile_id: 'owner', application_id: 'app', input_revision: 5, output_revision: 8};
    assert.equal(advanceApplicationOutput(app, {...published(), ...change}), false);
    assert.equal(app.output_revision, 8);
  }
});

test('root mounts and retires writing separately without reloading or clearing unrelated edits on publication', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  assert.match(source, /import \{draftingPane\} from '\.\/drafting-pane\.js'/);
  assert.match(source, /writing\?\.retire\(\)/);
  assert.match(source, /exportText: createTextClient\(\{token: \(\) => state\.token\}\)/);
  const callback = source.match(/onPublished: published => \{([\s\S]*?)\n\s*\}\}\);/);
  assert.ok(callback);
  assert.match(callback[1], /advanceApplicationOutput\(application, published\)/);
  assert.doesNotMatch(callback[1], /edits\.clear|loadSelected|refreshAfter|replaceChildren|input_revision\s*=/);
  assert.ok(source.indexOf("append(activity.element)") < source.indexOf("append(writing.element)"));
  assert.ok(source.indexOf("append(writing.element)") < source.indexOf("gate.capture('history')"));
});

test('internal navigation blocks pending/unresolved writing without trapping explicit scope deletion', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  assert.match(source, /function writingBlocksNavigation\(\) \{[\s\S]*?if \(writing\?\.unresolved\)/);
  assert.match(source, /button\('Reload selected data', \(\) => \{\s+if \(writingBlocksNavigation\(\)\) return;/);
  assert.match(source, /application\.role\}`, \(\) => \{\s+if \(writingBlocksNavigation\(\)\) return;/);
  assert.match(source, /if \(writingBlocksNavigation\(\)\) \{ location\.hash = state\.route; return; \}/);
  assert.match(source, /if \(writingBlocksNavigation\(\)\) \{ event\.target\.value = state\.owner; return; \}/);
  const context = source.match(/function context\(\) \{([\s\S]*?)\n\}/)[1];
  assert.doesNotMatch(context, /writingBlocksNavigation/);
  assert.match(source, /Delete application and linked feedback content/);
});

const capsule = (owner = 'owner', application = 'app') => ({operation: {parentDraftId: 'parent',
  parentCapture: {draft: {id: 'parent', profile_id: owner, application_id: application}},
  body: {idempotency_key: 'retained', draft: {targets: [{target: 'writing', text: 'submitted'}]}}},
targets: [{target: 'writing', text: 'newer\r\ne\u0301 😀'}]});

test('read-only recovery retains exact operation/newer bytes with defensive copies and collision-free scopes', () => {
  const recovery = new WritingRecovery(), source = capsule();
  assert.equal(recovery.retain('owner', 'app', source), true);
  source.operation.body.idempotency_key = 'changed'; source.targets[0].text = 'changed';
  const read = recovery.get('owner', 'app');
  assert.equal(read.operation.body.idempotency_key, 'retained');
  assert.equal(read.targets[0].text, 'newer\r\ne\u0301 😀');
  read.targets[0].text = 'mutated-copy';
  assert.equal(recovery.get('owner', 'app').targets[0].text, 'newer\r\ne\u0301 😀');
  assert.equal(recovery.retain('owner', 'app', capsule()), false);
  assert.equal(recovery.get('other', 'app'), null);
  assert.equal(recovery.retain('other', 'app', capsule()), false);
  assert.equal(recovery.retain('a:b', 'c', capsule('a:b', 'c')), true);
  assert.equal(recovery.retain('a', 'b:c', capsule('a', 'b:c')), true);
  assert.equal(recovery.size, 3);
});

test('only successful privacy cleanup removes matching recovery scopes', async () => {
  const recovery = new WritingRecovery();
  recovery.retain('owner', 'app', capsule());
  recovery.retain('owner', 'other-app', capsule('owner', 'other-app'));
  recovery.retain('other-owner', 'app', capsule('other-owner', 'app'));
  const forgetAfter = action => recovery.forgetOwnerAfter('owner', action);
  await assert.rejects(forgetAfter(async () => { throw Object.assign(new Error('Conflict'), {status: 409}); }));
  assert.equal(recovery.has('owner', 'app'), true);
  await forgetAfter(async () => ({deleted: true}));
  assert.equal(recovery.has('owner', 'app'), false);
  assert.equal(recovery.has('owner', 'other-app'), false);
  recovery.forgetOwner('owner');
  assert.equal(recovery.has('owner', 'other-app'), false);
  assert.equal(recovery.has('other-owner', 'app'), true);
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  assert.equal((source.match(/operation\(\(\) => forgetWritingAfter\('(?:workspace|evidence)', \(\) => api\(/g) || []).length, 5);
  assert.doesNotMatch(source, /async result => \{\s*writingRecovery\.forgetOwner/);
});

test('successful deletion purges captured owner even after navigation retires its UI ticket', async () => {
  const recovery = new WritingRecovery();
  recovery.retain('owner', 'app', capsule());
  recovery.retain('other-owner', 'app', capsule('other-owner', 'app'));
  let complete, currentOwner = 'owner';
  const action = new Promise(resolve => { complete = resolve; });
  const pending = recovery.forgetOwnerAfter(currentOwner, () => action);
  currentOwner = 'other-owner';
  complete({deleted: true, cleanup_pending: true});
  assert.deepEqual(await pending, {deleted: true, cleanup_pending: true});
  assert.equal(recovery.has('owner', 'app'), false);
  assert.equal(recovery.has(currentOwner, 'app'), true);
});

test('actual root operation keeps privacy completion outside stale success/error UI suppression', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  const definition = source.slice(source.indexOf('async function operation('), source.indexOf('async function loadSelected('));
  for (const fail of [false, true]) {
    const recovery = new WritingRecovery(), gate = new RequestGate(), edits = new EditBuffers();
    recovery.retain('owner', 'app', capsule()); recovery.retain('other', 'app', capsule('other', 'app'));
    const state = {busy: false, dirty: false}, notices = [];
    const operation = new Function('state', 'gate', 'edits', 'busy', 'offerReload', 'notify', `return (${definition.trim()});`)(
      state, gate, edits, value => { state.busy = value; }, (...args) => notices.push(args), (...args) => notices.push(args));
    let resolve, reject, owner = 'owner', after = 0;
    const response = new Promise((yes, no) => { resolve = yes; reject = no; });
    gate.select(owner);
    const pending = operation(() => recovery.forgetOwnerAfter(owner, () => response), () => { after++; });
    owner = 'other'; gate.select(owner);
    if (fail) reject(Object.assign(new Error('Conflict'), {status: 409})); else resolve({schema_version: 1, deleted: true, cleanup_pending: false});
    await pending;
    assert.equal(recovery.has('owner', 'app'), fail);
    assert.equal(recovery.has('other', 'app'), true);
    assert.equal(after, 0); assert.deepEqual(notices, []);
  }
  const privacy = source.slice(source.indexOf('async function forgetWritingAfter('), source.indexOf('function context('));
  assert.match(privacy, /const owner = state.owner/);
  assert.match(privacy, /await writingRecovery.forgetOwnerAfter\(owner, async \(\) =>/);
  assert.match(privacy, /if \(state.owner === owner\) \{ writing\?\.retire\(\); writing = null; retireDisclosure\(\); retireBudget\(\); \}/);
});

test('retiring an owned writing form forgets only its browser tracking without acknowledging another form', () => {
  const edits = new EditBuffers(), own = {}, other = {};
  edits.changed(own); edits.changed(other);
  edits.forget(own);
  assert.equal(edits.forms.has(own), false);
  assert.equal(edits.dirty, true);
  assert.equal(edits.forms.get(other).dirty, true);
});

test('actual privacy completion retires same-owner writing but never another owner or a rejected deletion', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  const definition = source.slice(source.indexOf('async function forgetWritingAfter('), source.indexOf('function context('));
  for (const switched of [false, true]) for (const fail of [false, true]) {
    const recovery = new WritingRecovery(), state = {owner: 'owner'};
    recovery.retain('owner', 'app', capsule()); recovery.retain('other', 'app', capsule('other', 'app'));
    let retired = 0, resolve, reject;
    const response = new Promise((yes, no) => { resolve = yes; reject = no; });
    let disclosureRetired = 0, budgetRetired = 0; const reviewForgotten = [];
    const helper = new Function('state', 'writingRecovery', 'paidRegistry', 'reviewRegistry', 'recognizedPrivacyDeletion', 'startWriting', 'retireDisclosure', 'retireBudget', `let writing = startWriting; ${definition}; return {run: forgetWritingAfter, current: () => writing};`)(
      state, recovery, {forgetOwner() {}}, {forgetOwner(owner) { reviewForgotten.push(owner); }}, (kind, value) => kind === 'workspace' && value?.schema_version === 1 && value.deleted === true && typeof value.cleanup_pending === 'boolean', {retire() { retired++; }}, () => { disclosureRetired++; }, () => { budgetRetired++; });
    const pending = helper.run('workspace', () => response);
    if (switched) state.owner = 'other';
    if (fail) reject(Error('Deletion failed')); else resolve({schema_version: 1, deleted: true, cleanup_pending: false});
    if (fail) await assert.rejects(pending); else await pending;
    assert.equal(retired, !switched && !fail ? 1 : 0);
    assert.equal(disclosureRetired, !switched && !fail ? 1 : 0);
    assert.equal(budgetRetired, !switched && !fail ? 1 : 0);
    assert.equal(helper.current() === null, !switched && !fail);
    assert.deepEqual(reviewForgotten, fail ? [] : ['owner']);
    assert.equal(recovery.has('owner', 'app'), fail);
    assert.equal(recovery.has('other', 'app'), true);
  }
});

test('quarantine locks input/history writes, remounts scoped recovery and warns on hard close', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  assert.match(source, /if \(application && applicationMutationLocked\(application.profile_id, application.application_id\)\) return notify/);
  assert.match(source, /if \(applicationMutationLocked\(application.profile_id, application.application_id\)\) return notify\('History writes/);
  assert.match(source, /recovery: writingRecovery.get\(state.owner, application.application_id\)/);
  assert.match(source, /const retained = writingRecovery.retain\(application.profile_id, application.application_id, capsule\)/);
  assert.match(source, /if \(state.dirty \|\| writingRecovery.size \|\| paidRegistry.size \|\| reviewRegistry.needsUnloadWarning\(\)\)/);
});

test('ending global busy state does not visually unlock quarantined form actions', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  const definition = source.slice(source.indexOf('function busy('), source.indexOf('function button('));
  const locked = {dataset: {writingLocked: 'true'}, disabled: true};
  const ordinary = {dataset: {}, disabled: false};
  const state = {busy: false};
  const busy = new Function('state', 'document', 'applicationLocks', 'writing', 'disclosure', 'review', `return (${definition.trim()});`)(
    state, {querySelectorAll: () => [locked, ordinary]}, null, null, null, null);
  busy(true);
  assert.equal(locked.disabled, true); assert.equal(ordinary.disabled, true);
  busy(false);
  assert.equal(state.busy, false);
  assert.equal(locked.disabled, true); assert.equal(ordinary.disabled, false);
  assert.match(source, /control.dataset.writingLocked = locked \? 'true' : 'false'/);
});

test('root mounts a read-only disclosure bridge without changing saved-output or edit ownership', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  assert.match(source, /import \{disclosurePane\} from '\.\/disclosure-pane\.js'/);
  assert.match(source, /onCaptureChange: \(\) => \{ disclosure\?\.invalidate\(\); review\?\.invalidate\(\); \}/);
  assert.match(source, /getCapture: \(\) => writing\?\.capture\(\)/);
  assert.match(source, /isDirty: \(\) => state.dirty/);
  assert.ok(source.indexOf('append(writing.element)') < source.indexOf('append(disclosure.element)'));
  assert.ok(source.indexOf('append(disclosure.element)') < source.indexOf("gate.capture('history')"));
  const retirement = source.slice(source.indexOf('function retireDisclosure('), source.indexOf('function retireActivity('));
  assert.match(retirement, /disclosure\?\.retire\(\); disclosure = null/);
});

test('domain input invalidates a disclosure without clearing buffers or replacing its controls', async () => {
  const source = await readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
  const definition = source.slice(source.indexOf('function invalidateDisclosureInput('), source.indexOf('function exactEnvelope('));
  let invalidated = 0, reviewInvalidated = 0;
  const own = {}, other = {}, reviewOwn = {};
  const disclosure = {element: {contains: target => target === own}, invalidate() { invalidated++; }};
  const review = {element: {contains: target => target === reviewOwn}, invalidate() { reviewInvalidated++; }};
  const invalidate = new Function('disclosure', 'review', `return (${definition.trim()});`)(disclosure, review);
  invalidate({target: own}); assert.equal(invalidated, 0);
  invalidate({target: other}); assert.equal(invalidated, 1); assert.equal(reviewInvalidated, 2);
  invalidate({target: reviewOwn}); assert.equal(invalidated, 1); assert.equal(reviewInvalidated, 2);
  assert.doesNotMatch(definition, /edits\.|replaceChildren|\.value\s*=/);
  assert.match(source, /\$\('main'\).addEventListener\('input', invalidateDisclosureInput\)/);
});
