import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {WritingRecovery, EditBuffers, RequestGate} from '../backend/ui/workspace/client.js';

const owner = '11111111-1111-4111-8111-111111111111';
const application = '22222222-2222-4222-8222-222222222222';
const source = () => readFile(new URL('../backend/ui/workspace/app.js', import.meta.url), 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const vector = () => ({schema_version: 1, metadata: 1, facts: 2, documents: 3, consent: 4, application_input: 5, application_output: 6, research: 7});
const draftingState = () => ({schema_version: 1, profile_id: owner, application_id: application, current_revisions: vector(),
  consent: {provider: 'openai', purposes: ['research', 'drafting', 'assessment'], granted: true, revision: 4}});
const revocation = () => ({schema_version: 1, profile_id: owner, id: '33333333-3333-4333-8333-333333333333', provider: 'openai',
  purposes: ['research', 'drafting', 'assessment'], granted: false, revision: 5, disclosed_at: '2026-10-11T17:00:00Z'});
const connectionStatus = () => ({schema_version: 1, boot_token: 'new-local-boot-token', cloud_key_configured: false,
  capabilities: {schema_version: 1, profile_management: true, interview: true, proposals: true, application_management: true,
    research: false, drafting: false, browser_fill: false, browser_submit: false}});
async function connectionHarness(api, overrides = {}, feedback = () => {}) {
  const app = await source(), start = app.indexOf('function validateConnectionStatus('), end = app.indexOf('function applicationMutationLocked(');
  assert.ok(start >= 0 && end > start, 'root owns a narrow explicit connection-refresh helper');
  const state = {owner, application, token: 'previous-token', status: {previous: true}, busy: true, initialized: true, dirty: true,
    detail: {application_output: 6}, ...overrides};
  const control = {disabled: false}, notices = [];
  const helper = new Function('api', 'state', 'connection', 'notify', 'exactEnvelope',
    `let connectionPending = false; ${app.slice(start, end)}; return {run: refreshLocalConnection, validate: validateConnectionStatus, pending: () => connectionPending};`)(
    api, state, control, (...args) => { notices.push(args); feedback(...args); }, (await validation()).exactEnvelope);
  return {...helper, state, control, notices};
}
async function validation() {
  const app = await source(), start = app.indexOf('function exactEnvelope('), end = app.indexOf('function writingBlocksNavigation(');
  assert.ok(start >= 0 && end > start, 'root owns explicit envelope validation before private-state purge');
  return new Function(`${app.slice(start, end)}; return {exactEnvelope, recognizedPrivacyDeletion, cloudRevocationState, recognizedCloudRevocation};`)();
}

test('privacy success accepts only exact endpoint-specific envelopes, never arbitrary readable 2xx', async () => {
  const {recognizedPrivacyDeletion: valid} = await validation();
  for (const cleanup_pending of [false, true]) assert.equal(valid('workspace', {schema_version: 1, deleted: true, cleanup_pending}), true);
  const evidence = {ticket_id: '33333333-3333-4333-8333-333333333333', state: 'pending', scope: 'access_revoked_cleanup_pending'};
  assert.equal(valid('evidence', evidence), true);
  for (const value of [null, {}, {deleted: true}, {schema_version: 1, deleted: 1, cleanup_pending: false},
    {schema_version: 1, deleted: true, cleanup_pending: 'false'}, {schema_version: 1, deleted: true, cleanup_pending: false, extra: true}, evidence]) {
    assert.equal(valid('workspace', value), false);
  }
  for (const value of [{...evidence, state: 'complete'}, {...evidence, schema_version: 1}, {...evidence, ticket_id: 'bad'},
    {schema_version: 1, deleted: true, cleanup_pending: true}]) assert.equal(valid('evidence', value), false);
  assert.equal(valid('invented', evidence), false);
});

test('revocation validates exact current state and exact returned false consent without coercion', async () => {
  const {cloudRevocationState: state, recognizedCloudRevocation: valid} = await validation();
  assert.deepEqual(state(draftingState(), owner, application), draftingState());
  const absent = draftingState(); absent.consent = null; absent.current_revisions.consent = 0;
  assert.deepEqual(state(absent, owner, application), absent);
  for (const change of [{profile_id: application}, {application_id: owner}, {extra: true}, {consent: null},
    {current_revisions: {...vector(), consent: true}}, {current_revisions: {...vector(), facts: Number.MAX_SAFE_INTEGER + 1}},
    {consent: {...draftingState().consent, revision: 3}}, {consent: {...draftingState().consent, granted: 1}},
    {consent: {...draftingState().consent, purposes: ['drafting', 'drafting']}}]) {
    assert.throws(() => state({...draftingState(), ...change}, owner, application));
  }
  assert.equal(valid(revocation(), owner, 4), true);
  for (const change of [{schema_version: true}, {profile_id: application}, {id: 'bad'}, {provider: 'OpenAI'}, {granted: 0},
    {granted: true}, {revision: 4}, {revision: 6}, {purposes: ['drafting']}, {extra: true},
    {disclosed_at: '2026-02-30T00:00:00Z'}, {disclosed_at: '2026-10-11T17:00:00+01:00'}]) {
    assert.equal(valid({...revocation(), ...change}, owner, 4), false);
  }
});

test('actual root privacy helper validates before purging both owners even across retired UI tickets', async () => {
  const app = await source(), {recognizedPrivacyDeletion} = await validation();
  const definition = app.slice(app.indexOf('async function forgetWritingAfter('), app.indexOf('function context('));
  for (const kind of ['workspace', 'evidence']) for (const outcome of ['recognized', 'malformed', 'failed']) for (const switched of [false, true]) {
    const writingRecovery = new WritingRecovery(), paid = new Set([owner, application]), state = {owner};
    writingRecovery.retain(owner, 'app', {operation: {parentDraftId: 'parent', parentCapture: {draft: {id: 'parent', profile_id: owner, application_id: 'app'}}, body: {idempotency_key: 'retained'}}, targets: []});
    let writingRetired = 0, disclosureRetired = 0, budgetRetired = 0;
    const helper = new Function('state', 'writingRecovery', 'paidRegistry', 'recognizedPrivacyDeletion', 'startWriting', 'retireDisclosure', 'retireBudget',
      `const reviewRegistry = {forgetOwner() {}}; let writing = startWriting; ${definition}; return forgetWritingAfter;`)(state, writingRecovery, {forgetOwner(id) { paid.delete(id); }}, recognizedPrivacyDeletion,
      {retire() { writingRetired++; }}, () => { disclosureRetired++; }, () => { budgetRetired++; });
    const wait = deferred(), result = helper(kind, () => wait.promise);
    if (switched) state.owner = application;
    if (outcome === 'failed') wait.reject(Error('synthetic failure'));
    else wait.resolve(outcome === 'malformed' ? {deleted: true} : kind === 'workspace'
      ? {schema_version: 1, deleted: true, cleanup_pending: true}
      : {ticket_id: '33333333-3333-4333-8333-333333333333', state: 'pending', scope: 'access_revoked_cleanup_pending'});
    if (outcome === 'recognized') await result; else await assert.rejects(result);
    assert.equal(paid.has(owner), outcome !== 'recognized'); assert.equal(paid.has(application), true);
    assert.equal(writingRecovery.has(owner, 'app'), outcome !== 'recognized');
    assert.equal(writingRetired, outcome === 'recognized' && !switched ? 1 : 0);
    assert.equal(disclosureRetired, writingRetired); assert.equal(budgetRetired, writingRetired);
  }
});

test('direct revocation captures URLs and revision before awaits, never purges or acknowledges writing', async () => {
  const app = await source(), validators = await validation();
  const definition = app.slice(app.indexOf('async function revokeCloudPurposes('), app.indexOf('async function forgetWritingAfter('));
  assert.ok(definition.startsWith('async function revokeCloudPurposes('));
  for (const malformed of [false, true]) {
    const wait = deferred(), calls = [], state = {owner, application}, edits = new EditBuffers(), other = {};
    edits.changed(other); let invalidated = 0;
    const api = async (path, options) => { calls.push({path, options}); return options ? malformed ? {deleted: true} : revocation() : wait.promise; };
    const revoke = new Function('api', 'disclosure', 'state', 'cloudRevocationState', 'recognizedCloudRevocation', `const review = null; ${definition};return revokeCloudPurposes;`)(
      api, {invalidate() { invalidated++; }}, state, validators.cloudRevocationState, validators.recognizedCloudRevocation);
    const pending = revoke(state.owner, state.application);
    assert.equal(invalidated, 1); state.owner = application; state.application = owner;
    wait.resolve(draftingState());
    if (malformed) await assert.rejects(pending); else assert.deepEqual(await pending, revocation());
    assert.equal(calls.length, 2);
    assert.equal(calls[0].path, `/api/workspace/profiles/${owner}/applications/${application}/drafting-state`);
    assert.equal(calls[1].path, `/api/workspace/profiles/${owner}/consent`);
    assert.deepEqual(calls[1].options, {method: 'POST', body: {expected_consent_revision: 4, provider: 'openai', purposes: ['research', 'drafting', 'assessment'], granted: false}});
    assert.equal(edits.dirty, true);
  }
});

test('root composes paid and writing mutation guards; paid actions never occupy global busy', async () => {
  const app = await source();
  assert.match(app, /new PaidLaunchRegistry/); assert.match(app, /new PaidLaunchController/);
  assert.match(app, /function applicationMutationLocked\(owner, application\)/);
  assert.match(app, /isMutationLocked: \(\) => applicationMutationLocked/);
  assert.match(app, /state\.dirty \|\| writingRecovery\.size \|\| paidRegistry\.size/);
  assert.match(app, /Revoke all cloud purposes/);
  assert.match(app, /revokeCloudPurposes\(owner, appId\)/);
  assert.equal((app.match(/forgetWritingAfter\('workspace',/g) || []).length, 4);
  assert.equal((app.match(/forgetWritingAfter\('evidence',/g) || []).length, 1);
  const mount = app.slice(app.indexOf('paidController = new PaidLaunchController('), app.indexOf("gate.capture('history')", app.indexOf('paidController = new PaidLaunchController(')));
  assert.doesNotMatch(mount, /operation\(|busy\(true\)|edits\.acknowledge|edits\.clear/);
  assert.match(mount, /onSelectResult/); assert.match(mount, /writing\.selectSavedResult\(draftId\)/);
});

test('actual navigation guard permits only known or explicitly handed-off settled unknown operations', async () => {
  const app = await source();
  const definition = app.slice(app.indexOf('function writingBlocksNavigation('), app.indexOf('async function revokeCloudPurposes('));
  for (const writingPending of [false, true]) for (const summary of [null, {state: 'pending'}, {state: 'unknown'}, {state: 'unknown', handedOff: true}, {state: 'known'}]) {
    let notices = 0;
    const guard = new Function('writing', 'paidController', 'notify', `const reviewRegistry = {blocksNavigation:()=>false},state={owner:"",application:""}; return (${definition.trim()});`)(
      {unresolved: writingPending}, {snapshot: () => summary}, () => { notices++; });
    const blocked = writingPending || summary?.state === 'pending' || summary?.state === 'unknown' && summary.handedOff !== true;
    assert.equal(guard(), Boolean(blocked)); assert.equal(notices, blocked ? 1 : 0);
  }
});

test('actual application and history handlers reject composed mutation locks before reading form values', async () => {
  const app = await source();
  const record = {profile_id: owner, application_id: application, company: 'Synthetic', role: 'Intern', sector: 'tech', official_domains: [], questions: []};
  const definition = app.slice(app.indexOf('function applicationForm('), app.indexOf('async function applications('));
  let notices = 0;
  const make = new Function('form', 'field', 'text', 'applicationMutationLocked', 'notify', 'values', `return (${definition.trim()});`)(
    (_label, submit) => submit, () => ({}), () => ({}), () => true, () => { notices++; }, () => { throw Error('Locked handler read private form'); });
  make(record)({}); assert.equal(notices, 1);
  const start = app.indexOf("const feedbackForm = form('Record local feedback', element => {") + "const feedbackForm = form('Record local feedback', ".length;
  const end = app.indexOf("\n  }, field('kind'", start);
  assert.ok(start > 0 && end > start);
  const history = new Function('application', 'applicationMutationLocked', 'notify', 'values', `return (${app.slice(start, end)}\n});`)(
    record, () => true, () => { notices++; }, () => { throw Error('Locked history handler read form'); });
  history({}); assert.equal(notices, 2);
});

test('composed form locks recompute on busy(false) and unlock only after matching recovery resolves', async () => {
  const app = await source(), controls = [{dataset: {}, disabled: false}, {dataset: {}, disabled: false}];
  const start = app.indexOf('  const lockForms = () => {'), end = app.indexOf('\n  applicationLocks = lockForms;', start);
  const definition = app.slice(start, end);
  assert.ok(start > 0 && end > start);
  let locked = true, syncs = 0; const state = {busy: false}, notice = {};
  const lockForms = new Function('application', 'appForms', 'lockNotice', 'applicationMutationLocked', 'state', `${definition}; return lockForms;`)(
    {profile_id: owner, application_id: application}, [{querySelectorAll: () => controls}], notice, () => locked, state);
  const busyDefinition = app.slice(app.indexOf('function busy('), app.indexOf('function button('));
  const busy = new Function('state', 'document', 'applicationLocks', 'writing', `const disclosure = null, review = null; return (${busyDefinition.trim()});`)(
    state, {querySelectorAll: () => controls}, lockForms, {syncLocks() { syncs++; }});
  busy(true); busy(false); assert.ok(controls.every(control => control.disabled)); assert.equal(syncs, 2);
  locked = false; busy(false); assert.ok(controls.every(control => !control.disabled)); assert.equal(notice.textContent, '');
});

test('malformed current revocation state never sends a consent POST or retries', async () => {
  const app = await source(), validators = await validation();
  const definition = app.slice(app.indexOf('async function revokeCloudPurposes('), app.indexOf('async function forgetWritingAfter('));
  for (const result of [null, {}, {...draftingState(), profile_id: application}]) {
    const calls = [];
    const run = new Function('api', 'disclosure', 'state', 'cloudRevocationState', 'recognizedCloudRevocation', `const review = null; ${definition};return revokeCloudPurposes;`)(
      async (path, options) => { calls.push({path, options}); return result; }, null, {owner}, validators.cloudRevocationState, validators.recognizedCloudRevocation);
    await assert.rejects(run(owner, application)); assert.equal(calls.length, 1); assert.equal(calls[0].options, undefined);
  }
});

test('explicit result selection callback checks live owner/application before entering writing flow', async () => {
  const app = await source(), start = app.indexOf('onSelectResult: async ('), end = app.indexOf('\n    }});', start);
  assert.ok(start > 0 && end > start);
  const expression = app.slice(start, end + '\n    }'.length).replace('onSelectResult: ', '');
  const state = {owner, application}; let selections = 0;
  const select = new Function('state', 'writing', `return (${expression});`)(state, {selectSavedResult: async () => { selections++; return true; }});
  assert.equal(await select({owner: application, application, draftId: 'result'}), false);
  assert.equal(await select({owner, application: owner, draftId: 'result'}), false); assert.equal(selections, 0);
  assert.equal(await select({owner, application, draftId: 'result'}), true); assert.equal(selections, 1);
});

test('recognized revocation invalidates new same-owner previews acquired during either await, not other owners or malformed outcomes', async () => {
  const app = await source(), validators = await validation();
  const definition = app.slice(app.indexOf('async function revokeCloudPurposes('), app.indexOf('async function forgetWritingAfter('));
  for (const phase of ['state', 'consent']) for (const scope of ['same application', 'same owner other application', 'away and back', 'other owner']) {
    for (const recognized of [false, true]) {
      const state = {owner, application}, wait = deferred(), entered = deferred(), edits = new EditBuffers(), form = {};
      const capsules = new Set([owner, application]);
      edits.changed(form); const before = edits.capture(form);
      let initialInvalidations = 0, currentInvalidations = 0, acknowledgement = true;
      const api = async (_path, options) => {
        if (Boolean(options) === (phase === 'consent')) { entered.resolve(); await wait.promise; }
        return options ? recognized ? revocation() : {...revocation(), granted: true} : draftingState();
      };
      const helper = new Function('api', 'state', 'initialDisclosure', 'cloudRevocationState', 'recognizedCloudRevocation',
        `const review = null; let disclosure = initialDisclosure; ${definition}; return {run: revokeCloudPurposes, replace(value) {disclosure = value;}};`)(
        api, state, {invalidate() { initialInvalidations++; }}, validators.cloudRevocationState, validators.recognizedCloudRevocation);
      const pending = helper.run(owner, application);
      await entered.promise;
      assert.equal(initialInvalidations, 1);
      if (scope === 'same owner other application') state.application = owner;
      if (scope === 'away and back') { state.owner = application; state.owner = owner; }
      if (scope === 'other owner') state.owner = application;
      helper.replace({invalidate() { currentInvalidations++; acknowledgement = false; }});
      wait.resolve();
      if (recognized) await pending; else await assert.rejects(pending, /outcome is not recognized/);
      const shouldInvalidate = recognized && scope !== 'other owner';
      assert.equal(currentInvalidations, shouldInvalidate ? 1 : 0, `${phase}: ${scope}: recognized=${recognized}`);
      assert.equal(acknowledgement, !shouldInvalidate);
      assert.deepEqual(edits.capture(form), before); assert.equal(edits.dirty, true);
      assert.deepEqual([...capsules], [owner, application]);
    }
  }
});

test('recognized profile revocation invalidates current same-owner disclosure before a stale root success ticket is suppressed', async () => {
  const app = await source(), validators = await validation();
  const revokeDefinition = app.slice(app.indexOf('async function revokeCloudPurposes('), app.indexOf('async function forgetWritingAfter('));
  const operationDefinition = app.slice(app.indexOf('async function operation('), app.indexOf('async function loadSelected('));
  const state = {owner, application, busy: false, dirty: true}, gate = new RequestGate(), edits = new EditBuffers(), form = {};
  edits.changed(form); const capturedEdit = edits.capture(form), response = deferred(), consentEntered = deferred();
  let initialInvalidations = 0, currentInvalidations = 0, after = 0; const notices = [];
  const helper = new Function('api', 'state', 'initialDisclosure', 'cloudRevocationState', 'recognizedCloudRevocation',
    `const review = null; let disclosure = initialDisclosure; ${revokeDefinition}; return {run: revokeCloudPurposes, replace(value) {disclosure = value;}};`)(
    async (_path, options) => { if (!options) return draftingState(); consentEntered.resolve(); return response.promise; }, state,
    {invalidate() { initialInvalidations++; }}, validators.cloudRevocationState, validators.recognizedCloudRevocation);
  const operation = new Function('state', 'gate', 'edits', 'busy', 'offerReload', 'notify', `return (${operationDefinition.trim()});`)(
    state, gate, edits, value => { state.busy = value; }, (...args) => notices.push(args), (...args) => notices.push(args));
  gate.select(`${owner}:${application}`);
  const pending = operation(() => helper.run(owner, application), () => { after++; }, null, true);
  await consentEntered.promise;
  gate.select(`${application}:${owner}`); gate.select(`${owner}:${owner}`); state.application = owner;
  helper.replace({invalidate() { currentInvalidations++; }});
  response.resolve(revocation()); await pending;
  assert.equal(initialInvalidations, 1); assert.equal(currentInvalidations, 1);
  assert.equal(after, 0); assert.deepEqual(notices, []); assert.equal(state.busy, false);
  assert.deepEqual(edits.capture(form), capturedEdit); assert.equal(edits.dirty, true);
});

test('Overview describes bounded paid and local human-review controls without claiming research or browser support', async () => {
  const app = await source(), overview = app.slice(app.indexOf('function overview('), app.indexOf('async function interview('));
  assert.match(overview, /explicitly acknowledged generation/);
  assert.match(overview, /saved-edit assessment/);
  assert.match(overview, /Human writing review is a separate local acknowledgement, not factual verification or browser permission/);
  assert.match(overview, /Research launch and supervised browser actions are not wired/);
  assert.doesNotMatch(overview, /Research launch, paid drafting\/reassessment/);
});

test('unverified revocation transport outcome directs explicit inspection without retry or success claims', async () => {
  const app = await source(), validators = await validation();
  const definition = app.slice(app.indexOf('async function revokeCloudPurposes('), app.indexOf('async function forgetWritingAfter('));
  for (const status of [undefined, 409, 503]) {
    let calls = 0, invalidations = 0;
    const run = new Function('api', 'disclosure', 'state', 'cloudRevocationState', 'recognizedCloudRevocation', `const review = null; ${definition};return revokeCloudPurposes;`)(
      async (_path, options) => { calls++; if (!options) return draftingState(); throw Object.assign(new Error('Synthetic transport failure'), {status}); },
      {invalidate() { invalidations++; }}, {owner}, validators.cloudRevocationState, validators.recognizedCloudRevocation);
    await assert.rejects(run(owner, application), error => {
      assert.match(error.message, /Inspect consent explicitly/); assert.match(error.message, /not assumed/);
      assert.equal(error.status, status); return true;
    });
    assert.equal(calls, 2); assert.equal(invalidations, 1);
  }
});

test('connection refresh validates the exact existing workspace status DTO without coercion', async () => {
  const helper = await connectionHarness(async () => connectionStatus());
  const accepted = helper.validate(connectionStatus()); assert.deepEqual(accepted, connectionStatus());
  accepted.capabilities.interview = false; assert.equal(helper.validate(connectionStatus()).capabilities.interview, true);
  for (const bad of [null, {}, {...connectionStatus(), schema_version: true}, {...connectionStatus(), extra: true},
    {...connectionStatus(), boot_token: ''}, {...connectionStatus(), boot_token: 42}, {...connectionStatus(), boot_token: 'a'.repeat(257)},
    {...connectionStatus(), cloud_key_configured: 0}, {...connectionStatus(), capabilities: null},
    {...connectionStatus(), capabilities: {...connectionStatus().capabilities, schema_version: true}},
    {...connectionStatus(), capabilities: {...connectionStatus().capabilities, profile_management: false}},
    {...connectionStatus(), capabilities: {...connectionStatus().capabilities, research: 0}},
    {...connectionStatus(), capabilities: {...connectionStatus().capabilities, extra: false}}]) assert.throws(() => helper.validate(bad));
  const missing = connectionStatus(); delete missing.capabilities.browser_submit;
  assert.throws(() => helper.validate(missing));
});

test('explicit connection refresh sends one GET and changes only global token/status, not recovery or buffers', async () => {
  const calls = [], helper = await connectionHarness(async (...args) => { calls.push(args); return connectionStatus(); });
  const retained = {endpoint: '/exact/original', wrapper: {schema_version: 1, admission_revision: 7,
    request: {idempotency_key: 'original', acknowledged: true, text: '  Café e\u0301 😀\r\n '}}};
  helper.state.retained = structuredClone(retained); helper.state.preview = {acknowledged: true};
  const before = structuredClone(helper.state), edits = new EditBuffers(), form = {};
  edits.changed(form); const edit = edits.capture(form);
  assert.equal(await helper.run(), true);
  assert.deepEqual(calls, [['/api/workspace/status']]);
  assert.deepEqual(helper.state, {...before, token: connectionStatus().boot_token, status: connectionStatus()});
  assert.deepEqual(helper.state.retained, retained); assert.deepEqual(edits.capture(form), edit); assert.equal(edits.dirty, true);
  assert.equal(helper.control.disabled, false); assert.equal(helper.pending(), false);
  assert.match(helper.notices.at(-1)[0], /separately/); assert.match(helper.notices.at(-1)[0], /No operation was resent/);
});

test('duplicate connection clicks while pending issue one GET without global busy or navigation locking', async () => {
  const wait = deferred(); let calls = 0;
  const helper = await connectionHarness(async () => { calls++; return wait.promise; });
  const pending = helper.run(); assert.equal(helper.pending(), true); assert.equal(helper.control.disabled, true);
  assert.equal(await helper.run(), false); assert.equal(calls, 1); assert.equal(helper.state.busy, true);
  wait.resolve(connectionStatus()); assert.equal(await pending, true); assert.equal(calls, 1); assert.equal(helper.pending(), false);
});

test('late connection refresh updates global credential without restoring the earlier owner/application view', async () => {
  const wait = deferred(), helper = await connectionHarness(() => wait.promise);
  const pending = helper.run(); helper.state.owner = application; helper.state.application = owner;
  helper.state.detail = {application_output: 22}; const before = structuredClone(helper.state);
  wait.resolve(connectionStatus()); assert.equal(await pending, true);
  assert.deepEqual(helper.state, {...before, token: connectionStatus().boot_token, status: connectionStatus()});
});

test('malformed or unavailable connection refresh preserves old token and all retained state with no retry', async () => {
  for (const result of [null, {...connectionStatus(), capabilities: {}}, Error('Synthetic unreadable status')]) {
    let calls = 0;
    const helper = await connectionHarness(async () => { calls++; if (result instanceof Error) throw result; return result; });
    const before = structuredClone(helper.state);
    assert.equal(await helper.run(), false); assert.deepEqual(helper.state, before); assert.equal(calls, 1);
    assert.equal(helper.control.disabled, false); assert.equal(helper.pending(), false);
    assert.match(helper.notices.at(-1)[0], /No operation was resent/); assert.equal(helper.notices.at(-1)[1], true);
  }
});

test('connection refresh is a no-op until initialization succeeds', async () => {
  const helper = await connectionHarness(() => { throw Error('Refresh before bootstrap'); }, {initialized: false});
  const before = structuredClone(helper.state); assert.equal(await helper.run(), false);
  assert.deepEqual(helper.state, before); assert.deepEqual(helper.notices, []); assert.equal(helper.pending(), false);
});

test('connection notification failure cannot misreport a valid credential update as failed or leave the button locked', async () => {
  for (const valid of [false, true]) {
    const helper = await connectionHarness(async () => valid ? connectionStatus() : null, {}, () => { throw Error('Detached notification view'); });
    assert.equal(await helper.run(), valid);
    assert.equal(helper.state.token, valid ? connectionStatus().boot_token : 'previous-token');
    assert.equal(helper.control.disabled, false); assert.equal(helper.pending(), false);
  }
});

test('persistent connection control is standalone and never routes refresh through bootstrap, mutation or paid actions', async () => {
  const app = await source(), start = app.indexOf("const connection = node('button', 'Refresh local connection'");
  assert.ok(start >= 0, 'persistent standalone button is created independently of mutable panes');
  const control = app.slice(start, app.indexOf('start();', start));
  assert.match(control, /connection\.onclick = refreshLocalConnection/); assert.match(control, /querySelector\('header'\)\.append\(connection\)/);
  assert.doesNotMatch(control, /data-action-control|dataset\.actionControl|reconcileOriginal|loadSelected|location\.reload/);
  const helper = app.slice(app.indexOf('async function refreshLocalConnection('), app.indexOf('function applicationMutationLocked('));
  assert.doesNotMatch(helper, /reconcileOriginal|writingRecovery|paidRegistry|disclosure|edits\.|context\(|loadSelected|start\(|operation\(|busy\(|localStorage|sessionStorage/);
  assert.match(app, /state\.initialized = true;[^\n]*connection\.disabled = false/);
});
