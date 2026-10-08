import assert from 'node:assert/strict';
import test from 'node:test';
import {EditBuffers, RequestGate, codePoints, createClient, externalUrl, evidenceDeletionNotice} from '../backend/ui/workspace/client.js';
import {sourcePreview, writingQuestions} from '../backend/ui/workspace/questions.js';

test('workspace request uses explicit envelope and boot token without Node routes', async () => {
  let sent;
  const client = createClient({token: () => 'synthetic-token', fetchImpl: async (...args) => {
    sent = args;
    return {ok: true, json: async () => ({schema_version: 1, profiles: []})};
  }});
  assert.deepEqual(await client('/api/workspace/profiles', {method: 'POST', body: {name: 'Synthetic'}}), {schema_version: 1, profiles: []});
  assert.equal(sent[1].headers['X-Evidence-Token'], 'synthetic-token');
  assert.equal(sent[1].headers['Content-Type'], 'application/json');
  assert.equal(sent[1].redirect, 'error');
  assert.equal(sent[1].mode, 'same-origin');
  assert.equal(sent[1].body, '{"name":"Synthetic"}');
});

test('client rejects external and Node URLs without transmitting', async () => {
  let calls = 0;
  const client = createClient({fetchImpl: async () => { calls++; }});
  for (const path of ['https://attacker.invalid/api/workspace', '//attacker.invalid', '/api/profiles', '/api/workspace\\evil', '/api/workspace/../../api/profiles', '/api/workspace/%2e%2e/%2e%2e/api/profiles']) {
    await assert.rejects(client(path), /Only local/);
  }
  assert.equal(calls, 0);
});

test('stale CAS errors remain visible and are never automatically retried', async () => {
  let calls = 0;
  const client = createClient({fetchImpl: async () => { calls++; return {ok: false, status: 409, json: async () => ({error: {code: 'CONFLICT', message: 'Reload before editing'}})}; }});
  await assert.rejects(client('/api/workspace/profiles/example', {method: 'PATCH', body: {}}), error => error.status === 409 && error.code === 'CONFLICT');
  assert.equal(calls, 1);
});

test('unreadable local responses do not leak arbitrary response bodies or claim success', async () => {
  const client = createClient({fetchImpl: async () => ({ok: true, json: async () => { throw new Error('private response text'); }})});
  await assert.rejects(client('/api/workspace/status'), error => /unreadable/.test(error.message) && !error.message.includes('private response'));
});

test('owner changes invalidate delayed successes and errors', () => {
  const gate = new RequestGate();
  gate.select('owner-a:brain');
  const old = gate.capture('load');
  gate.select('owner-b:brain');
  assert.equal(gate.current(old), false);
  assert.equal(gate.current(gate.capture('load')), true);
});

test('same-owner question context advance retires a pending mutation callback', () => {
  const gate = new RequestGate();
  gate.select('owner-a:interview');
  const savingA = gate.capture('mutation');
  gate.select('owner-a:interview');
  assert.equal(gate.current(savingA), false);
  assert.equal(gate.current(gate.capture('interview')), true);
});

test('same-profile reversed pane requests cannot replace newer selection', () => {
  const gate = new RequestGate();
  gate.select('owner-a:evidence');
  const first = gate.capture('source');
  const unrelated = gate.capture('citation');
  const second = gate.capture('source');
  assert.equal(gate.current(first), false);
  assert.equal(gate.current(second), true);
  assert.equal(gate.current(unrelated), true);
  gate.select('owner-a:evidence');
  assert.equal(gate.current(second), false);
});

test('Unicode counters use exact code points without normalization', () => {
  assert.equal(codePoints('🚀'), 1);
  assert.equal(codePoints('e\u0301'), 2);
  assert.equal(codePoints('é'), 1);
});

test('source links reject executable and credential-bearing URLs', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,evil', 'http://example.invalid', 'https://user:secret@example.invalid', 'not a URL']) assert.equal(externalUrl(url), null);
  assert.equal(externalUrl('https://example.invalid/role'), 'https://example.invalid/role');
});

test('preview question edits preserve optionality, type, limits and origin when unchanged', () => {
  const previous = [{schema_version: 1, id: 'q-existing', text: 'Why this role?', type: 'text', required: false, max_words: 50, max_chars: 500, options: [], constraint_origin: 'employer'}];
  assert.deepEqual(writingQuestions('Why this role?', previous, {max_words: '', max_chars: ''}), previous);
  const changed = writingQuestions('Why this role?', previous, {max_words: '100', max_chars: ''});
  assert.equal(changed[0].id, 'q-existing');
  assert.equal(changed[0].required, false);
  assert.equal(changed[0].type, 'text');
  assert.equal(changed[0].max_words, 100);
  assert.equal(changed[0].max_chars, 500);
  assert.equal(changed[0].constraint_origin, 'user');
  assert.throws(() => writingQuestions('Changed wording only', previous), /stable IDs/);
});

test('new questions get distinct IDs, explicit user constraints and exact Unicode text', () => {
  let counter = 0;
  const result = writingQuestions('Why 🚀?\n\nTell us about e\u0301.', [], {max_words: '50', max_chars: '100'}, () => `q-${++counter}`);
  assert.deepEqual(result.map(question => question.id), ['q-1', 'q-2']);
  assert.equal(result[1].text, 'Tell us about e\u0301.');
  assert.ok(result.every(question => question.required === true && question.constraint_origin === 'user' && question.max_words === 50));
});

test('preview refuses mixed controls and malformed limits before sending', () => {
  for (const type of ['select', 'file', 'sensitive']) assert.throws(() => writingQuestions('Question', [{type}]), /cannot replace/);
  assert.throws(() => writingQuestions('Question\nPart two', [{type: 'writing', text: 'Question\nPart two'}]), /multiline questions/);
  for (const max_words of ['0', '-1', '1.5', 'NaN', '9007199254740992']) assert.throws(() => writingQuestions('Question', [], {max_words}), /positive whole/);
});

test('document preview bounds all units together without splitting code points', () => {
  const preview = sourcePreview([{text: '🚀'.repeat(4)}, {page: 2, text: 'e\u0301'.repeat(4)}, {page: 3, text: 'omitted text'}], 7);
  assert.ok(preview.includes('🚀🚀🚀🚀'));
  assert.ok(preview.includes('Physical PDF page 2\ne\u0301e'));
  assert.ok(preview.includes('7 canonical code points across all units'));
  assert.ok(!preview.includes('omitted text'));
  assert.equal(sourcePreview([{text: 'literal <script> text'}], 100), 'Canonical text\nliteral <script> text');
});

test('existing question removal and equal-count reorder never transfer identity or constraints', () => {
  const previous = [{id: 'required', text: 'Required?', type: 'writing', required: true, max_words: 50}, {id: 'optional', text: 'Optional?', type: 'text', required: false, max_words: 100}];
  for (const changed of ['Optional?', 'Optional?\nRequired?', 'Replacement?\nOptional?']) assert.throws(() => writingQuestions(changed, previous), /stable IDs/);
  assert.deepEqual(writingQuestions('Required?\nOptional?', previous).map(q => [q.id, q.required, q.max_words]), [['required', true, 50], ['optional', false, 100]]);
});

test('a successful save acknowledges only its submitted unchanged buffer', () => {
  const edits = new EditBuffers(), one = {}, other = {};
  edits.changed(one); edits.changed(other);
  const submitted = edits.capture(one);
  assert.equal(edits.acknowledge(submitted), true);
  assert.equal(edits.dirty, true);
  assert.equal(edits.acknowledge(edits.capture(other)), true);
  assert.equal(edits.dirty, false);
});

test('typing during a save remains dirty and prevents destructive refresh', () => {
  const edits = new EditBuffers(), form = {};
  edits.changed(form); const submitted = edits.capture(form);
  edits.changed(form);
  assert.equal(edits.acknowledge(submitted), false);
  assert.equal(edits.dirty, true);
  assert.equal(edits.acknowledge(null), false);
  assert.equal(edits.dirty, true);
  edits.clear(); assert.equal(edits.dirty, false);
});

test('evidence deletion notices distinguish physical cleanup from revoked access', () => {
  assert.match(evidenceDeletionNotice({state: 'pending'}), /Access revoked.*cleanup remains pending/);
  assert.match(evidenceDeletionNotice({state: 'complete'}), /cleanup complete.*not forensic/);
  assert.match(evidenceDeletionNotice({}), /completion is not claimed/);
});
