import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import http from 'node:http';
import { startServer } from '../src/server.mjs';

async function fixture(t, ai) {
  const directory = mkdtempSync(join(tmpdir(), 'copilot-http-'));
  const app = await startServer({ directory, port: 0, ai });
  t.after(async () => { await app.close(); rmSync(directory, { recursive: true, force: true }); });
  const base = 'http://127.0.0.1:' + app.server.address().port;
  const status = await (await fetch(base + '/api/status')).json();
  async function request(path, body, method = body === undefined ? 'GET' : 'POST', headers = {}) {
    const response = await fetch(base + path, { method, headers: { 'X-Copilot-Token': status.token, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
    return { status: response.status, body: await response.json() };
  }
  return { ...app, base, status, request };
}
async function profile(f, name = 'Synthetic candidate', sectors = ['tech']) {
  const response = await f.request('/api/profiles', { name, sectors }); assert.equal(response.status, 200); return response.body;
}
function input(sector = 'tech') {
  return { company: 'Synthetic Employer', role: sector === 'tech' ? 'Engineering Intern' : 'Finance Intern', sector, companyUrl: 'https://example.com/', url: 'https://example.com/careers/intern', jobDescription: 'A synthetic role requiring teamwork and clear communication.', questions: [{ id: 'why', text: 'Why this role?', maxWords: 100, maxChars: null }] };
}

test('bootstrap exposes configuration but no key/profile; host/origin/token guards fail closed', async t => {
  const f = await fixture(t);
  assert.equal(f.status.provider.configured, false); assert.equal(typeof f.status.token, 'string');
  assert.equal(JSON.stringify(f.status).includes('API_KEY'), false);
  assert.equal((await fetch(f.base + '/api/profiles')).status, 403);
  assert.equal((await fetch(f.base + '/api/status', { headers: { Origin: 'https://evil.example' } })).status, 403);
  assert.equal((await fetch(f.base + '/api/status', { headers: { Origin: 'null' } })).status, 403);
  // Native fetch normalizes Host in this runtime; raw HTTP actually exercises the guard.
  const hostileHostStatus = await new Promise((ok, reject) => {
    http.get(f.base + '/api/status', { headers: { Host: 'evil.example' } }, response => { response.resume(); ok(response.statusCode); }).on('error', reject);
  });
  assert.equal(hostileHostStatus, 403);
  assert.equal((await f.request('/api/profiles', undefined, 'GET', { 'X-Copilot-Token': 'bad' })).status, 403);
  const page = await fetch(f.base + '/');
  assert.equal(page.status, 200); assert.match(page.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  assert.equal((await fetch(f.base + '/.env')).status, 404);
});

test('real profiles start blank, isolate memory and reject cross-profile IDs', async t => {
  const f = await fixture(t); let a = await profile(f, 'Tech candidate'); const b = await profile(f, 'Finance candidate', ['finance']);
  assert.equal(a.cloudConsent, false); assert.equal(a.isDemo, false); assert.deepEqual(a.memories, []);
  a = (await f.request('/api/profiles/' + a.id + '/memories', { label: 'A real user-confirmed example', content: 'Synthetic example: wrote tests.', category: 'project' })).body;
  assert.equal(a.memories[0].status, 'pending');
  assert.equal((await f.request('/api/profiles/' + b.id + '/memories/' + a.memories[0].id + '/review', { action: 'confirm' })).status, 404);
  const bm = await f.request('/api/profiles/' + b.id); assert.deepEqual(bm.body.memories, []);
  const metadata = await f.request('/api/profiles'); assert.equal('memories' in metadata.body[0], false);
});

test('interview answers persist as pending memories and resume by canonical question ID', async t => {
  const f = await fixture(t); const p = await profile(f, 'Interview candidate', ['tech', 'finance']);
  const route = '/api/profiles/' + p.id;
  const interview = (await f.request(route + '/interview')).body;
  assert.ok(interview.questions.length >= 40);
  const result = await f.request(route + '/interview/answer', { questionId: interview.nextQuestion.id, answer: 'Synthetic experience entered by the user.' });
  assert.equal(result.status, 200); assert.ok(result.body.memories.every(m => m.status === 'pending'));
  const next = (await f.request(route + '/interview')).body;
  assert.equal(next.progress.answered, 1); assert.notEqual(next.nextQuestion.id, interview.nextQuestion.id);
  assert.equal((await f.request(route + '/interview/answer', { questionId: 'not-real', answer: 'hello' })).status, 400);
  await f.request(route + '/interview', { completed: true }, 'PATCH');
  assert.equal((await f.request(route + '/interview')).body.nextQuestion, null);
});

test('demo is separately synthetic and never injects facts into real profiles', async t => {
  const f = await fixture(t); const p = await profile(f);
  assert.equal((await f.request('/api/profiles/' + p.id + '/demo', {})).status, 404);
  const demo = await f.request('/api/demo', {});
  assert.equal(demo.status, 200); assert.equal(demo.body.isDemo, true); assert.equal(demo.body.applications.length, 2);
  assert.ok(demo.body.memories.every(m => m.source.kind === 'demo'));
  assert.deepEqual((await f.request('/api/profiles/' + p.id)).body.memories, []);
});

test('offline tech and finance applications draft, require review, then explicitly record submission', async t => {
  const f = await fixture(t);
  for (const sector of ['tech', 'finance']) {
    let p = await profile(f, 'Synthetic ' + sector, [sector]); const route = '/api/profiles/' + p.id;
    p = (await f.request(route + '/memories', { category: 'teamwork', label: 'Synthetic evidence', content: 'I helped a synthetic student team communicate clearly.' })).body;
    p = (await f.request(route + '/memories/' + p.memories[0].id + '/review', { action: 'confirm' })).body;
    p = (await f.request(route + '/applications', input(sector))).body;
    const appId = p.applications[0].id; const appRoute = route + '/applications/' + appId;
    p = (await f.request(appRoute + '/draft', { offline: true })).body;
    const draft = p.applications[0].draft; assert.equal(draft.mode, 'offline');
    const submittedFirst = await f.request(appRoute + '/record', { status: 'submitted', eventId: 'before-review', expectedDraftId: draft.id });
    assert.equal(submittedFirst.status, 409);
    assert.equal((await f.request(appRoute + '/record', { status: 'reviewed', eventId: 'review-1', expectedDraftId: 'unseen-draft' })).status, 409);
    const reviewed = await f.request(appRoute + '/record', { status: 'reviewed', eventId: 'review-1', expectedDraftId: draft.id });
    assert.equal(reviewed.status, 200);
    const historySize = reviewed.body.applications[0].history.length;
    const again = await f.request(appRoute + '/record', { status: 'reviewed', eventId: 'review-1', expectedDraftId: draft.id });
    assert.equal(again.body.applications[0].history.length, historySize);
    const submitted = await f.request(appRoute + '/record', { status: 'submitted', eventId: 'submit-1', expectedDraftId: draft.id });
    assert.equal(submitted.status, 200); assert.equal(submitted.body.applications[0].status, 'submitted');
    assert.equal(submitted.body.memories.filter(m => m.status === 'verified').length, 1);
  }
});

test('input validation rejects private URLs, injected questions, malformed payloads and oversized requests', async t => {
  const f = await fixture(t); const p = await profile(f); const route = '/api/profiles/' + p.id;
  assert.equal((await f.request(route + '/applications', { ...input(), companyUrl: 'http://127.0.0.1/admin' })).status, 400);
  assert.equal((await f.request(route + '/applications', { ...input(), questions: [{ id: 'a', text: 'A', maxWords: -1 }] })).status, 400);
  assert.equal((await f.request('/api/profiles', { name: 'A', sectors: ['bogus'] })).status, 400);
  assert.equal((await f.request('/api/profiles', { name: 'A'.repeat(1024 * 1024), sectors: ['tech'] })).status, 413);
  const bad = await fetch(f.base + '/api/profiles', { method: 'POST', headers: { 'X-Copilot-Token': f.status.token, 'Content-Type': 'application/json' }, body: '{bad' });
  assert.equal(bad.status, 400);
});

test('revoking persisted consent during research discards the in-flight result', async t => {
  let resolveResearch; let started;
  const entered = new Promise(ok => { started = ok; });
  const ai = { status: () => ({ configured: true, model: 'test' }), research: () => { started(); return new Promise(ok => { resolveResearch = ok; }); } };
  const f = await fixture(t, ai); let p = await profile(f); const route = '/api/profiles/' + p.id;
  p = (await f.request(route, { cloudConsent: true }, 'PATCH')).body;
  p = (await f.request(route + '/applications', input())).body;
  const appRoute = route + '/applications/' + p.applications[0].id;
  const running = f.request(appRoute + '/research', {}); await entered;
  await f.request(route, { cloudConsent: false }, 'PATCH');
  resolveResearch({ mode: 'cloud', status: 'researched' });
  const response = await running; assert.equal(response.status, 409);
  assert.equal((await f.request(route)).body.applications[0].research, null);
});

test('in-flight AI generation cannot overwrite a newer human edit or inherit its review', async t => {
  let release; let entered;
  const started = new Promise(ok => { entered = ok; });
  const draftData = text => ({ mode: 'offline', coverLetter: text, answers: [{ questionId: 'why', text: 'Synthetic answer.', evidenceIds: [], researchIds: [] }], evidenceIds: [], researchIds: [], missingFacts: [], warnings: [] });
  const ai = { status: () => ({ configured: true, model: 'mock' }), draft: () => { entered(); return new Promise(ok => { release = ok; }); } };
  const f = await fixture(t, ai); let p = await profile(f); const route = '/api/profiles/' + p.id;
  p = (await f.request(route + '/applications', input())).body;
  let a = p.applications[0];
  p = f.store.saveDraft(p.id, a.id, draftData('Original saved draft.'), { expectedBrainRevision: p.brainRevision, expectedInputRevision: a.inputRevision, expectedResearchId: null, expectedDraftId: null, expectedHistoryLength: 0 });
  a = p.applications[0]; const appRoute = route + '/applications/' + a.id;
  const running = f.request(appRoute + '/draft', {}); await started;
  const edited = await f.request(appRoute, { expectedDraftId: a.draft.id, draft: { coverLetter: 'Newer human-reviewed wording.' } }, 'PATCH');
  assert.equal(edited.status, 200);
  const currentId = edited.body.applications[0].draft.id;
  assert.equal((await f.request(appRoute + '/record', { status: 'reviewed', eventId: 'newer-review', expectedDraftId: currentId })).status, 200);
  release(draftData('Older AI response must be discarded.'));
  assert.equal((await running).status, 409);
  const current = (await f.request(route)).body.applications[0];
  assert.equal(current.draft.id, currentId);
  assert.equal(current.draft.coverLetter, 'Newer human-reviewed wording.');
  assert.equal(current.status, 'reviewed');
});

test('review-only during AI generation preserves the exact reviewed snapshot', async t => {
  let release; let entered; const started = new Promise(ok => { entered = ok; });
  const draft = { mode: 'offline', coverLetter: 'Original human-visible text.', answers: [{ questionId: 'why', text: 'A synthetic answer.', evidenceIds: [], researchIds: [] }], evidenceIds: [], researchIds: [], missingFacts: [], warnings: [] };
  const ai = { status: () => ({ configured: true, model: 'mock' }), draft: () => { entered(); return new Promise(ok => { release = ok; }); } };
  const f = await fixture(t, ai); let p = await profile(f); const route = '/api/profiles/' + p.id;
  p = (await f.request(route + '/applications', input())).body;
  const a = p.applications[0]; const appRoute = route + '/applications/' + a.id;
  p = f.store.saveDraft(p.id, a.id, draft, { expectedBrainRevision: p.brainRevision, expectedInputRevision: a.inputRevision, expectedResearchId: null, expectedDraftId: null, expectedHistoryLength: 0 });
  const id = p.applications[0].draft.id;
  const pending = f.request(appRoute + '/draft', {}); await started;
  assert.equal((await f.request(appRoute + '/record', { status: 'reviewed', eventId: 'review-during-generation', expectedDraftId: id })).status, 200);
  release({ ...draft, coverLetter: 'Unseen replacement.' });
  assert.equal((await pending).status, 409);
  const current = (await f.request(route)).body.applications[0];
  assert.equal(current.draft.id, id); assert.equal(current.status, 'reviewed');
});

test('each draft-stage checkpoint stops further cloud transmission after consent or source changes', async t => {
  for (const change of ['consent', 'brain', 'input', 'edit', 'review', 'research', 'delete']) {
    await t.test(change, async t => {
      let release; let enter; let transmissions = 0;
      const entered = new Promise(ok => { enter = ok; });
      const blocked = new Promise(ok => { release = ok; });
      const saved = { mode: 'offline', coverLetter: 'Previously saved synthetic draft.', answers: [{ questionId: 'why', text: 'Synthetic answer.', evidenceIds: [], researchIds: [] }], evidenceIds: [], researchIds: [], missingFacts: [], warnings: [] };
      const ai = {
        status: () => ({ configured: true, model: 'synthetic' }),
        draft: async (_profile, _application, options) => {
          for (let stage = 0; stage < 3; stage++) {
            await options.checkpoint();
            transmissions++;
            if (stage === 0) { enter(); await blocked; }
          }
          return { ...saved, coverLetter: 'Replacement that must never be saved.' };
        }
      };
      const f = await fixture(t, ai); let p = await profile(f); const route = '/api/profiles/' + p.id;
      p = (await f.request(route, { cloudConsent: true }, 'PATCH')).body;
      p = (await f.request(route + '/memories', { label: 'Synthetic pending evidence', content: 'I tested a synthetic project.', category: 'project' })).body;
      const memoryId = p.memories[0].id;
      p = (await f.request(route + '/applications', input())).body;
      const a = p.applications[0]; const appRoute = route + '/applications/' + a.id;
      p = f.store.saveDraft(p.id, a.id, saved, { expectedBrainRevision: p.brainRevision, expectedInputRevision: a.inputRevision, expectedResearchId: null, expectedDraftId: null, expectedHistoryLength: 0 });
      const savedId = p.applications[0].draft.id;
      const running = f.request(appRoute + '/draft', {}); await entered;
      let changed;
      if (change === 'consent') changed = await f.request(route, { cloudConsent: false }, 'PATCH');
      if (change === 'brain') changed = await f.request(route + '/memories/' + memoryId + '/review', { action: 'confirm' });
      if (change === 'input') changed = await f.request(appRoute, { role: 'Changed synthetic role' }, 'PATCH');
      if (change === 'edit') changed = await f.request(appRoute, { expectedDraftId: savedId, draft: { coverLetter: 'New human wording.' } }, 'PATCH');
      if (change === 'review') changed = await f.request(appRoute + '/record', { status: 'reviewed', eventId: 'during-stage', expectedDraftId: savedId });
      if (change === 'research') changed = { status: 200, body: f.store.saveResearch(p.id, a.id, { mode: 'offline', status: 'not_researched', summary: 'Synthetic changed research.' }, { expectedInputRevision: a.inputRevision }) };
      if (change === 'delete') changed = await f.request(route, {}, 'DELETE');
      assert.equal(changed.status, 200);
      release();
      const response = await running;
      assert.equal(response.status, change === 'delete' ? 404 : 409);
      assert.equal(transmissions, 1, 'no additional payload after the first in-flight stage');
      const current = await f.request(route);
      if (change === 'delete') assert.equal(current.status, 404);
      else {
        assert.equal(current.body.applications[0].draft.id, change === 'edit' ? changed.body.applications[0].draft.id : savedId);
        assert.equal(current.body.applications[0].draft.coverLetter, change === 'edit' ? 'New human wording.' : saved.coverLetter);
      }
    });
  }
});

test('research-stage checkpoint prevents normalization transmission after opt-out', async t => {
  let release; let enter; let transmissions = 0;
  const entered = new Promise(ok => { enter = ok; });
  const gate = new Promise(ok => { release = ok; });
  const ai = {
    status: () => ({ configured: true, model: 'synthetic' }),
    research: async (_application, { checkpoint }) => {
      await checkpoint(); transmissions++; enter(); await gate;
      await checkpoint(); transmissions++;
      return { mode: 'cloud', status: 'researched' };
    }
  };
  const f = await fixture(t, ai); let p = await profile(f); const route = '/api/profiles/' + p.id;
  p = (await f.request(route, { cloudConsent: true }, 'PATCH')).body;
  p = (await f.request(route + '/applications', input())).body;
  const running = f.request(route + '/applications/' + p.applications[0].id + '/research', {}); await entered;
  assert.equal((await f.request(route, { cloudConsent: false }, 'PATCH')).status, 200);
  release();
  assert.equal((await running).status, 409); assert.equal(transmissions, 1);
  assert.equal((await f.request(route)).body.applications[0].research, null);
});

test('failed quality pipeline keeps the previous saved draft and review snapshot', async t => {
  const ai = { status: () => ({ configured: true, model: 'synthetic' }), draft: async () => { throw Object.assign(new Error('Synthetic critique failed.'), { status: 502 }); } };
  const f = await fixture(t, ai); let p = await profile(f); const route = '/api/profiles/' + p.id;
  p = (await f.request(route + '/applications', input())).body;
  const a = p.applications[0]; const appRoute = route + '/applications/' + a.id;
  const saved = { mode: 'offline', coverLetter: 'Synthetic saved wording.', answers: [{ questionId: 'why', text: 'Synthetic answer.', evidenceIds: [], researchIds: [] }], evidenceIds: [], researchIds: [], missingFacts: [], warnings: [] };
  p = f.store.saveDraft(p.id, a.id, saved, { expectedBrainRevision: p.brainRevision, expectedInputRevision: a.inputRevision, expectedResearchId: null, expectedDraftId: null, expectedHistoryLength: 0 });
  const id = p.applications[0].draft.id;
  const reviewed = await f.request(appRoute + '/record', { status: 'reviewed', eventId: 'saved-review', expectedDraftId: id });
  assert.equal(reviewed.status, 200);
  const failed = await f.request(appRoute + '/draft', {});
  assert.equal(failed.status, 502); assert.match(failed.body.error, /critique failed/);
  assert.deepEqual((await f.request(route)).body.applications[0], reviewed.body.applications[0]);
});
