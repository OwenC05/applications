import assert from 'node:assert/strict';
import test from 'node:test';
import {createTextClient} from '../backend/ui/workspace/client.js';

const path = '/api/workspace/profiles/owner/applications/app/drafts/draft/export';
const response = (overrides = {}) => ({ok: true, status: 200,
  headers: {get: () => 'text/plain; charset=utf-8'}, text: async () => 'e\u0301 😀\r\n', ...overrides});

test('saved-text client is fixed GET with token, same-origin, no-store and no redirects', async () => {
  let sent;
  const client = createTextClient({token: () => 'synthetic', fetchImpl: async (...args) => {
    sent = args; return response();
  }});
  assert.equal(await client(path), 'e\u0301 😀\r\n');
  assert.deepEqual(sent, [path, {method: 'GET', mode: 'same-origin', redirect: 'error', cache: 'no-store',
    headers: {'X-Evidence-Token': 'synthetic'}}]);
});

test('only literal scoped draft export paths can transmit', async () => {
  let calls = 0;
  const client = createTextClient({fetchImpl: async () => { calls++; return response(); }});
  for (const invalid of [null, {}, '//attacker.invalid' + path, 'https://attacker.invalid' + path,
    '/api/profiles', path + '?token=x', path + '#x', path + '/', path.replace('/drafts/', '/jobs/'),
    path.replace('/draft/', '/../'), path.replace('/draft/', '/%2e%2e/'),
    path.replace('/draft/', '/%2f/'), path.replace('/draft/', '/%5c/'), path.replace('/draft/', '/%252f/'),
    path.replace('/draft/', '/dr\\aft/'), '/api/workspace/profiles/owner/export']) {
    await assert.rejects(client(invalid), /Only local scoped draft exports/);
  }
  assert.equal(calls, 0);
  assert.equal(await client(path.replace('/owner/', '/owner%20name/')), 'e\u0301 😀\r\n');
  assert.equal(calls, 1);
});

test('successful non-text responses and unreadable text never claim export success', async () => {
  for (const result of [response({headers: {get: () => 'text/html'}}),
    response({headers: {get: () => null}}), response({text: async () => { throw Error('secret raw content'); }})]) {
    const client = createTextClient({fetchImpl: async () => result});
    await assert.rejects(client(path), error => !error.message.includes('secret raw content') && /unreadable|text\/plain/.test(error.message));
  }
});

test('structured export errors preserve status and code without retry or reading arbitrary HTML', async () => {
  let calls = 0;
  const client = createTextClient({fetchImpl: async () => {
    calls++; return response({ok: false, status: 409, headers: {get: () => 'application/json'},
      json: async () => ({error: {code: 'CONFLICT', message: 'Inspect current source'}})});
  }});
  await assert.rejects(client(path), error => error.status === 409 && error.code === 'CONFLICT');
  assert.equal(calls, 1);
  const html = createTextClient({fetchImpl: async () => response({ok: false, status: 500,
    headers: {get: () => 'text/html'}, text: async () => { throw Error('Must not read HTML'); }})});
  await assert.rejects(html(path), error => error.status === 500 && !error.message.includes('HTML'));
});
