import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync, writeFileSync, renameSync, mkdirSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { BrainStore } from '../src/store.mjs';
const fixture = t => { const directory = mkdtempSync(join(tmpdir(), 'copilot-store-')); const store = new BrainStore({ directory }); t.after(() => { store.close(); rmSync(directory, { recursive: true, force: true }); }); return { directory, store }; };
const person = store => store.createProfile({ name: 'Synthetic Person', sectors: ['tech', 'finance'] });
const addFact = (store, id, content = 'Built a synthetic calculator') => {
  let p = store.addMemory(id, { category: 'project', label: 'Project', content }); const m = p.memories.at(-1);
  return store.reviewMemory(id, m.id, { action: 'confirm' });
};
const role = (store, id) => store.createApplication(id, { company: 'Synthetic Company', role: 'Intern', sector: 'finance', jobDescription: 'Analyze data', questions: [{ id: 'why', text: 'Why this role?', maxWords: 10, maxChars: 100 }] });
const draft = (p, text = 'Built a synthetic calculator') => ({ mode: 'offline', coverLetter: 'Synthetic letter', answers: [{ questionId: 'why', text, evidenceIds: [p.memories.find(m => m.status === 'verified').id], researchIds: [] }], evidenceIds: [], researchIds: [], missingFacts: [], warnings: [] });
const save = (store, p, content) => {
  const a = p.applications[0]; return store.saveDraft(p.id, a.id, content ?? draft(p), { expectedBrainRevision: p.brainRevision, expectedInputRevision: a.inputRevision, expectedResearchId: a.research?.id ?? null, expectedDraftId: a.draft?.id ?? null, expectedHistoryLength: a.history.length });
};
const rejected = (fn, status) => assert.throws(fn, error => error.status === status);

test('profiles are isolated, return copies, persist privately and survive restart', t => {
  const { directory, store } = fixture(t); const a = person(store); const b = person(store);
  addFact(store, a.id); assert.equal(store.getProfile(b.id).memories.length, 0);
  const copy = store.getProfile(a.id); copy.memories[0].content = 'tampered'; assert.notEqual(store.getProfile(a.id).memories[0].content, 'tampered');
  assert.equal('memories' in store.listProfiles()[0], false);
  assert.equal(statSync(join(directory, 'state.json')).mode & 0o777, 0o600);
  store.close(); const reopened = new BrainStore({ directory }); t.after(() => reopened.close()); assert.equal(reopened.getProfile(a.id).memories.length, 1);
});
test('exclusive writer fails explicitly within and across processes', t => {
  const { directory } = fixture(t); rejected(() => new BrainStore({ directory }), 409);
  const moduleUrl = new URL('../src/store.mjs', import.meta.url).href;
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', `import {BrainStore} from ${JSON.stringify(moduleUrl)}; try {new BrainStore({directory:${JSON.stringify(directory)}}); process.exit(9)} catch(e) {process.exit(e.status===409?0:8)}`], { windowsHide: true });
  assert.equal(child.status, 0);
});
test('malformed storage fails visibly and retains bytes', t => {
  const { directory, store } = fixture(t); store.close(); const file = join(directory, 'state.json');
  for (const content of ['{broken', JSON.stringify({ version: 1, profiles: [{}] }), JSON.stringify({ version: 8, profiles: [] })]) {
    writeFileSync(file, content); assert.throws(() => new BrainStore({ directory })); assert.equal(readFileSync(file, 'utf8'), content);
  }
});
test('failed persistence leaves live state unchanged and no temp files', t => {
  const { directory, store } = fixture(t); const p = person(store); const before = store.getProfile(p.id);
  const file = join(directory, 'state.json'); const backup = join(directory, 'state.backup'); renameSync(file, backup); mkdirSync(file);
  rejected(() => store.updateProfile(p.id, { name: 'Must not commit' }), 500); assert.deepEqual(store.getProfile(p.id), before);
  assert.equal(readdirSync(directory).some(name => name.endsWith('.tmp')), false);
  rmSync(file, { recursive: true }); renameSync(backup, file);
  store.close(); const reopened = new BrainStore({ directory }); t.after(() => reopened.close()); assert.deepEqual(reopened.getProfile(p.id), before);
});
test('pending facts cannot silently replace verified evidence; explicit corrections supersede', t => {
  const { store } = fixture(t); let p = addFact(store, person(store).id); const first = p.memories[0]; const revision = p.brainRevision;
  p = store.addMemory(p.id, { category: 'project', label: 'Correction', content: 'Built a different synthetic tool', supersedes: first.id });
  assert.equal(p.brainRevision, revision); assert.equal(p.memories[0].status, 'verified');
  p = store.reviewMemory(p.id, p.memories[1].id, { action: 'confirm', content: 'Corrected explicitly' });
  assert.equal(p.memories[0].status, 'superseded'); assert.equal(p.memories[1].status, 'verified'); assert.equal(p.brainRevision, revision + 1);
  rejected(() => store.reviewMemory(p.id, first.id, { action: 'confirm' }), 409);
});
test('rejected proposals are not verified, scope checks prevent cross-profile lookups', t => {
  const { store } = fixture(t); let a = person(store); const b = person(store);
  a = store.addMemory(a.id, { category: 'skill', label: 'Skill', content: 'Synthetic skill' });
  rejected(() => store.reviewMemory(b.id, a.memories[0].id, { action: 'confirm' }), 404);
  a = store.reviewMemory(a.id, a.memories[0].id, { action: 'reject' }); assert.equal(a.brainRevision, 0); assert.equal(a.memories[0].status, 'rejected');
  a = role(store, a.id); rejected(() => store.updateApplication(b.id, a.applications[0].id, { role: 'Other role' }), 404);
});
test('interview persists raw answers and pending proposals, reanswers never overwrite verified facts', t => {
  const { directory, store } = fixture(t); let p = person(store);
  const input = { questionId: 'common-project', question: 'Describe a project', section: 'Experience', answer: 'A synthetic answer' };
  p = store.setInterviewProgress(p.id, { skippedQuestionIds: [input.questionId] }); p = store.saveInterviewAnswer(p.id, input);
  assert.deepEqual(p.interview.skippedQuestionIds, []); assert.equal(p.memories[0].status, 'pending'); assert.equal(p.brainRevision, 0);
  p = store.reviewMemory(p.id, p.memories[0].id, { action: 'confirm' }); p = store.saveInterviewAnswer(p.id, { ...input, answer: 'A changed synthetic answer' });
  assert.equal(p.interview.answers.length, 2); assert.equal(p.memories[0].status, 'verified'); assert.equal(p.memories[1].status, 'pending'); assert.equal(p.memories[1].supersedes, p.memories[0].id);
  p = store.saveInterviewAnswer(p.id, { ...input, answer: 'Third synthetic version' });
  assert.equal(p.memories[2].supersedes, p.memories[0].id);
  store.close(); const reopened = new BrainStore({ directory }); t.after(() => reopened.close()); assert.deepEqual(reopened.getProfile(p.id), p);
});
test('draft concurrency rejects brain, inputs, research changes and foreign/pending evidence', t => {
  const { store } = fixture(t); let p = role(store, addFact(store, person(store).id).id); const a = p.applications[0];
  rejected(() => store.saveDraft(p.id, a.id, draft(p), { expectedBrainRevision: -1, expectedInputRevision: 0, expectedResearchId: null, expectedDraftId: null, expectedHistoryLength: 0 }), 409);
  const invalid = draft(p); invalid.answers[0].evidenceIds = ['foreign']; rejected(() => save(store, p, invalid), 422);
  p = store.addMemory(p.id, { category: 'skill', label: 'Pending', content: 'Unconfirmed synthetic skill' }); invalid.answers[0].evidenceIds = [p.memories.at(-1).id]; rejected(() => save(store, p, invalid), 422);
  p = save(store, p); p = store.saveResearch(p.id, a.id, { mode: 'offline', status: 'not_researched' }, { expectedInputRevision: 0 }); assert.equal(p.applications[0].draft.stale, true);
  rejected(() => store.saveDraft(p.id, a.id, draft(p), { expectedBrainRevision: p.brainRevision, expectedInputRevision: 0, expectedResearchId: null, expectedDraftId: p.applications[0].draft.id, expectedHistoryLength: 0 }), 409);
  p = store.updateApplication(p.id, a.id, { role: 'Changed role' }); assert.equal(p.applications[0].research, null); assert.equal(p.applications[0].inputRevision, 1);
  rejected(() => store.saveResearch(p.id, a.id, { mode: 'offline', status: 'not_researched' }, { expectedInputRevision: 0 }), 409);
});
test('new verified fact and generation settings stale existing drafts', t => {
  const { store } = fixture(t); let p = save(store, role(store, addFact(store, person(store).id).id));
  p = addFact(store, p.id, 'Another synthetic fact'); assert.equal(p.applications[0].draft.stale, true);
  p = save(store, p); p = store.updateProfile(p.id, { writingPreferences: 'Concise' }); assert.equal(p.applications[0].draft.stale, true);
});
test('generation cannot overwrite a draft created or edited after its request snapshot', t => {
  const { store } = fixture(t); let p = role(store, addFact(store, person(store).id).id);
  const pendingSnapshot = p;
  p = save(store, p);
  rejected(() => save(store, pendingSnapshot), 409);
  const beforeEdit = p; const a = p.applications[0];
  p = store.updateApplication(p.id, a.id, { expectedDraftId: a.draft.id, draft: { coverLetter: 'Preserve these human edits' } });
  rejected(() => save(store, beforeEdit), 409);
  assert.equal(store.getProfile(p.id).applications[0].draft.coverLetter, 'Preserve these human edits');
  rejected(() => store.saveDraft(p.id, a.id, draft(p), { expectedBrainRevision: p.brainRevision, expectedInputRevision: a.inputRevision, expectedResearchId: null }), 409);
});
test('generation cannot overwrite an unchanged draft reviewed or submitted during generation', t => {
  const { store } = fixture(t); let p = save(store, role(store, addFact(store, person(store).id).id));
  const beforeReview = p; const a = p.applications[0];
  p = store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: 'async-review', expectedDraftId: a.draft.id });
  assert.equal(p.applications[0].draft.id, a.draft.id);
  rejected(() => save(store, beforeReview), 409);
  const beforeSubmit = p;
  p = store.recordApplication(p.id, a.id, { status: 'submitted', eventId: 'async-submit', expectedDraftId: a.draft.id });
  rejected(() => save(store, beforeSubmit), 409);
  assert.equal(store.getProfile(p.id).applications[0].status, 'submitted');
  rejected(() => store.saveDraft(p.id, a.id, draft(p), { expectedBrainRevision: p.brainRevision, expectedInputRevision: a.inputRevision, expectedResearchId: null, expectedDraftId: a.draft.id }), 409);
});
test('review uses immutable snapshots, creates pending facts, is idempotent and submission needs exact review', t => {
  const { store } = fixture(t); let p = save(store, role(store, addFact(store, person(store).id).id)); let a = p.applications[0]; const d = a.draft.id;
  rejected(() => store.recordApplication(p.id, a.id, { status: 'submitted', eventId: 'too-early', expectedDraftId: d }), 409);
  const event = { status: 'reviewed', eventId: 'review-1', expectedDraftId: d };
  p = store.recordApplication(p.id, a.id, event); assert.equal(p.memories.at(-1).status, 'pending'); assert.equal(p.brainRevision, 1);
  const reviewed = p; assert.deepEqual(store.recordApplication(p.id, a.id, event), reviewed);
  rejected(() => store.recordApplication(p.id, a.id, { ...event, status: 'submitted' }), 409);
  p = store.updateApplication(p.id, a.id, { expectedDraftId: d, draft: { answers: [{ questionId: 'why', text: 'Edited synthetic prose' }] } }); a = p.applications[0];
  assert.notEqual(a.draft.id, d); assert.equal(a.status, 'draft'); assert.equal(a.history[0].answers[0].text, 'Built a synthetic calculator');
  rejected(() => store.updateApplication(p.id, a.id, { expectedDraftId: d, draft: { coverLetter: 'stale edit' } }), 409);
  rejected(() => store.recordApplication(p.id, a.id, { status: 'submitted', eventId: 'submit-1', expectedDraftId: a.draft.id }), 409);
  p = store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: 'review-2', expectedDraftId: a.draft.id });
  p = store.recordApplication(p.id, a.id, { status: 'submitted', eventId: 'submit-2', expectedDraftId: a.draft.id }); assert.equal(p.applications[0].status, 'submitted');
});
test('review rejects stale UI and over-limit answers without truncating meaning', t => {
  const { store } = fixture(t); let p = role(store, addFact(store, person(store).id).id);
  p = save(store, p, draft(p, 'one two three four five six seven eight nine ten eleven')); const a = p.applications[0];
  rejected(() => store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: 'long', expectedDraftId: a.draft.id }), 422);
  rejected(() => store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: 'wrong', expectedDraftId: 'wrong-version' }), 409);
  assert.equal(store.getProfile(p.id).applications[0].history.length, 0);
});
test('incomplete drafts remain editable but required answers cannot be reviewed or submitted blank', t => {
  const { store } = fixture(t); let p = role(store, addFact(store, person(store).id).id);
  for (const text of ['', '  \n\t  ']) {
    p = save(store, p, draft(p, text)); const a = p.applications[0];
    assert.equal(a.draft.answers[0].text, text);
    rejected(() => store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: `blank-review-${text.length}`, expectedDraftId: a.draft.id }), 422);
    rejected(() => store.recordApplication(p.id, a.id, { status: 'submitted', eventId: `blank-submit-${text.length}`, expectedDraftId: a.draft.id }), 422);
    assert.equal(store.getProfile(p.id).applications[0].history.length, 0);
  }
  const a = p.applications[0];
  p = store.updateApplication(p.id, a.id, { expectedDraftId: a.draft.id, draft: { answers: [{ questionId: 'why', text: 'A completed answer' }] } });
  p = store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: 'completed-review', expectedDraftId: p.applications[0].draft.id });
  assert.equal(p.applications[0].status, 'reviewed');
});
test('forget clears source-linked answer and derived prose, not other verified memories', t => {
  const { store } = fixture(t); let p = person(store);
  p = store.saveInterviewAnswer(p.id, { questionId: 'common-project', question: 'Project?', section: 'Experience', answer: 'Sensitive synthetic answer' }); const memoryId = p.memories[0].id;
  p = store.reviewMemory(p.id, memoryId, { action: 'confirm' }); p = addFact(store, p.id, 'Other synthetic evidence'); p = save(store, role(store, p.id));
  p = store.recordApplication(p.id, p.applications[0].id, { status: 'reviewed', eventId: 'review', expectedDraftId: p.applications[0].draft.id });
  p = store.deleteMemory(p.id, memoryId); assert.equal(p.interview.answers.length, 0); assert.equal(p.applications[0].draft, null); assert.deepEqual(p.applications[0].history, []); assert.equal(p.memories.length, 1); assert.equal(p.memories[0].content, 'Other synthetic evidence');
});
test('cloud draft requires valid recent sourced research, question IDs and references validated', t => {
  const { store } = fixture(t); let p = role(store, addFact(store, person(store).id).id); const cloud = { ...draft(p), mode: 'cloud' };
  rejected(() => save(store, p, cloud), 422);
  p = store.saveResearch(p.id, p.applications[0].id, { mode: 'cloud', status: 'researched', retrievedAt: new Date().toISOString(), companyFacts: [{ id: 'company-1' }], requirements: [], hiringPriorities: [] }, { expectedInputRevision: 0 });
  cloud.researchIds = ['company-1']; p = save(store, p, cloud); assert.equal(p.applications[0].draft.mode, 'cloud');
  const realNow = Date.now; const currentTime = realNow();
  try {
    Date.now = () => currentTime + 8 * 86400000;
    rejected(() => store.recordApplication(p.id, p.applications[0].id, { status: 'reviewed', eventId: 'expired-review', expectedDraftId: p.applications[0].draft.id }), 422);
  } finally { Date.now = realNow; }
  const wrong = draft(p); wrong.answers[0].questionId = 'unknown'; rejected(() => save(store, p, wrong), 422);
  p = store.saveResearch(p.id, p.applications[0].id, { mode: 'cloud', status: 'researched', retrievedAt: '2020-01-01', companyFacts: [], requirements: [], hiringPriorities: [] }, { expectedInputRevision: 0 }); rejected(() => save(store, p, { ...draft(p), mode: 'cloud' }), 422);
});
test('strict mutation fields and full profile deletion', t => {
  const { store } = fixture(t); const p = person(store);
  rejected(() => store.updateProfile(p.id, { memories: [] }), 400); rejected(() => store.addMemory(p.id, { category: 'x', label: 'x', content: 'x', status: 'verified' }), 400);
  assert.deepEqual(store.deleteProfile(p.id), { deleted: true }); rejected(() => store.getProfile(p.id), 404); assert.deepEqual(store.listProfiles(), []);
});

const qualityDraft = (store, p) => {
  p = store.saveResearch(p.id, p.applications[0].id, { mode: 'cloud', status: 'researched', retrievedAt: new Date().toISOString(), companyFacts: [{ id: 'company-1' }], requirements: [], hiringPriorities: [] }, { expectedInputRevision: 0 });
  const content = { ...draft(p), mode: 'cloud', quality: { version: 1, status: 'assessed', checkedAt: new Date().toISOString(), rewriteCount: 1, summary: 'Editorial checks passed; human review remains required.', questionPlans: [{ questionId: 'why', kind: 'motivation', approach: 'Connect the real project to the researched role.', evidenceIds: [p.memories[0].id], researchIds: ['company-1'], missingFacts: [] }], issues: [] } };
  return { p, content };
};
test('quality metadata roundtrips and legacy absent/null drafts remain loadable', t => {
  const { store, directory } = fixture(t); let p = role(store, addFact(store, person(store).id).id);
  p = save(store, p); assert.equal(p.applications[0].draft.quality, undefined);
  p = save(store, p, { ...draft(p), quality: null }); assert.equal(p.applications[0].draft.quality, null);
  const prepared = qualityDraft(store, p); p = save(store, prepared.p, prepared.content);
  assert.deepEqual(p.applications[0].draft.quality, prepared.content.quality);
  prepared.content.quality.summary = 'Caller tampering'; assert.notEqual(store.getProfile(p.id).applications[0].draft.quality.summary, 'Caller tampering');
  store.close(); const reopened = new BrainStore({ directory }); t.after(() => reopened.close()); assert.deepEqual(reopened.getProfile(p.id), p);
});
test('quality schema rejects unsupported status, fields, references and misleading assessed states', t => {
  const { store } = fixture(t); const prepared = qualityDraft(store, role(store, addFact(store, person(store).id).id));
  const invalid = [q => q.status = 'excellent', q => q.version = 2, q => q.rewriteCount = 2, q => q.checkedAt = 'yesterday', q => q.score = 100, q => q.questionPlans[0].kind = 'flattery', q => q.questionPlans[0].questionId = 'foreign', q => q.questionPlans.push(structuredClone(q.questionPlans[0])), q => q.questionPlans[0].evidenceIds = ['foreign'], q => q.questionPlans[0].researchIds = ['foreign'], q => q.questionPlans[0].missingFacts = ['Missing real example'], q => q.issues = [{ target: 'why', category: 'specificity', severity: 'improve', detail: 'Too generic', evidenceIds: [], researchIds: [] }]];
  for (const mutate of invalid) { const content = structuredClone(prepared.content); mutate(content.quality); assert.throws(() => save(store, prepared.p, content)); }
  for (const patch of [{ target: 'foreign' }, { category: 'odds' }, { severity: 'minor' }, { evidenceIds: ['foreign'] }, { researchIds: ['foreign'] }]) {
    const content = structuredClone(prepared.content); content.quality.status = 'needs_work'; content.quality.issues = [{ target: 'why', category: 'specificity', severity: 'improve', detail: 'Too generic', evidenceIds: [], researchIds: [], ...patch }]; assert.throws(() => save(store, prepared.p, content));
  }
  const content = structuredClone(prepared.content); content.missingFacts = ['Need confirmed result']; assert.throws(() => save(store, prepared.p, content));
  content.missingFacts = []; content.answers[0].text = 'one two three four five six seven eight nine ten eleven'; assert.throws(() => save(store, prepared.p, content));
  assert.equal(store.getProfile(prepared.p.id).applications[0].draft, null);
});
test('human edits stale quality, cannot patch it and review snapshots stay immutable', t => {
  const { store, directory } = fixture(t); const prepared = qualityDraft(store, role(store, addFact(store, person(store).id).id)); let p = save(store, prepared.p, prepared.content); let a = p.applications[0];
  p = store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: 'quality-review', expectedDraftId: a.draft.id });
  const snapshot = structuredClone(p.applications[0].history[0].quality);
  rejected(() => store.updateApplication(p.id, a.id, { expectedDraftId: a.draft.id, draft: { quality: { status: 'assessed' } } }), 400);
  p = store.updateApplication(p.id, a.id, { expectedDraftId: a.draft.id, draft: { coverLetter: 'My edited letter' } }); a = p.applications[0];
  assert.equal(a.draft.quality.status, 'stale'); assert.match(a.draft.quality.summary, /edit/i); assert.notEqual(a.draft.id, p.applications[0].history[0].draftId); assert.deepEqual(a.history[0].quality, snapshot);
  a.history[0].quality.summary = 'Tampered'; assert.deepEqual(store.getProfile(p.id).applications[0].history[0].quality, snapshot);
  store.close(); const reopened = new BrainStore({ directory }); t.after(() => reopened.close()); assert.equal(reopened.getProfile(p.id).applications[0].draft.quality.status, 'stale');
});
test('brain, role and research changes invalidate editorial quality without rewriting history', t => {
  for (const change of ['brain', 'role', 'research']) {
    const { store, directory } = fixture(t); const prepared = qualityDraft(store, role(store, addFact(store, person(store).id).id)); let p = save(store, prepared.p, prepared.content); const a = p.applications[0];
    p = store.recordApplication(p.id, a.id, { status: 'reviewed', eventId: 'before-change', expectedDraftId: a.draft.id });
    if (change === 'brain') p = addFact(store, p.id, 'New verified experience');
    if (change === 'role') p = store.updateApplication(p.id, a.id, { questions: [{ id: 'changed', text: 'A different question' }] });
    if (change === 'research') p = store.saveResearch(p.id, a.id, { mode: 'offline', status: 'not_researched' }, { expectedInputRevision: 0 });
    assert.equal(p.applications[0].draft.stale, true); assert.equal(p.applications[0].draft.quality.status, 'stale'); assert.equal(p.applications[0].history[0].quality.status, 'assessed');
    store.close(); const reopened = new BrainStore({ directory }); t.after(() => reopened.close()); assert.equal(reopened.getProfile(p.id).applications[0].draft.quality.status, 'stale');
  }
});
test('corrupt persisted quality fails closed without resetting storage', t => {
  const { store, directory } = fixture(t); const prepared = qualityDraft(store, role(store, addFact(store, person(store).id).id)); save(store, prepared.p, prepared.content); store.close();
  const file = join(directory, 'state.json'); const state = JSON.parse(readFileSync(file, 'utf8')); state.profiles[0].applications[0].draft.quality.questionPlans[0].evidenceIds = ['foreign']; const bytes = JSON.stringify(state); writeFileSync(file, bytes);
  rejected(() => new BrainStore({ directory }), 500); assert.equal(readFileSync(file, 'utf8'), bytes);
});
test('unresolved editorial issues remain needs_work and pending facts cannot enter plans', t => {
  const { store, directory } = fixture(t); const prepared = qualityDraft(store, role(store, addFact(store, person(store).id).id));
  let p = store.addMemory(prepared.p.id, { category: 'result', label: 'Unconfirmed result', content: 'A synthetic unconfirmed achievement' });
  const invalid = structuredClone(prepared.content); invalid.quality.questionPlans[0].evidenceIds = [p.memories.at(-1).id]; rejected(() => save(store, p, invalid), 422);
  const content = structuredClone(prepared.content); content.quality.status = 'needs_work'; content.quality.questionPlans[0].missingFacts = ['What was your own contribution?']; content.quality.issues = [{ target: 'why', category: 'truthfulness', severity: 'must_fix', detail: 'Confirm the personal contribution before making this claim.', evidenceIds: [], researchIds: ['company-1'] }];
  p = save(store, p, content); assert.equal(p.applications[0].draft.quality.status, 'needs_work');
  store.close(); const reopened = new BrainStore({ directory }); t.after(() => reopened.close()); assert.deepEqual(reopened.getProfile(p.id).applications[0].draft.quality, content.quality);
});
