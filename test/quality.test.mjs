import test from 'node:test';
import assert from 'node:assert/strict';
import { createAI } from '../src/ai.mjs';
const profile = { memories: [{ id: 'e', status: 'verified', category: 'tech', label: 'Parser', content: 'Built Python parser.' }] };
const application = { company: 'Example', role: 'Engineer', sector: 'tech', jobDescription: 'Python', inputRevision: 1, questions: [{ id: 'q', text: 'Why Example?', maxWords: 30 }], research: { mode: 'cloud', status: 'researched', inputRevision: 1, retrievedAt: '2026-10-05T12:00:00Z', companyFacts: [{ id: 'c', text: 'Builds developer tools' }], requirements: [{ id: 'r', text: 'Python' }], hiringPriorities: [] } };
const plan = { coverLetterApproach: 'Parser relevant to tools', questionPlans: [{ questionId: 'q', kind: 'motivation', approach: 'Connect parser to developer tools', evidenceIds: ['e'], researchIds: ['c', 'r'], missingFacts: [] }], missingFacts: [] };
const draft = { coverLetter: 'I built a Python parser.', answers: [{ questionId: 'q', text: 'My parser experience matches your developer tools work.', evidenceIds: ['e'], researchIds: ['c', 'r'] }], evidenceIds: ['e'], researchIds: ['c', 'r'], missingFacts: [], warnings: [] };
const clear = { summary: 'Specific and supported.', issues: [] };
const issue = { target: 'q', category: 'specificity', severity: 'improve', detail: 'Replace generic prestige praise with developer tools motivation.', evidenceIds: ['e'], researchIds: ['c'] };
function fixture(outputs) {
  const calls = [];
  const ai = createAI({ apiKey: 'synthetic', clock: () => new Date('2026-10-05T12:00:00Z'), fetchImpl: async (_, init) => { calls.push(JSON.parse(init.body)); return { ok: true, json: async () => ({ status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text: JSON.stringify(outputs.shift()) }] }] }) }; } });
  return { ai, calls };
}
test('plan chooses evidence and employer angle; separate critique sees original context and exact draft', async () => {
  const { ai, calls } = fixture([plan, draft, clear]);
  const result = await ai.draft(profile, application, { consent: true });
  assert.equal(calls.length, 3);
  assert.equal(result.quality.status, 'assessed');
  assert.equal(result.quality.rewriteCount, 0);
  assert.deepEqual(result.quality.questionPlans, plan.questionPlans);
  const critique = JSON.parse(calls[2].input);
  assert.deepEqual(critique.draft.answers, draft.answers);
  assert.equal(critique.context.evidence[0].id, 'e');
});
test('generic motivation causes one rewrite and final critique assesses rewritten prose', async () => {
  const revised = { ...draft, answers: [{ ...draft.answers[0], text: 'I built a Python parser; Example developer tooling gives that experience a concrete fit.' }] };
  const { ai, calls } = fixture([plan, draft, { summary: 'Too generic.', issues: [issue] }, revised, clear]);
  const result = await ai.draft(profile, application, { consent: true });
  assert.equal(calls.length, 5);
  assert.equal(result.quality.rewriteCount, 1);
  assert.equal(result.quality.status, 'assessed');
  assert.deepEqual(result.quality.issues, []);
  assert.equal(JSON.parse(calls[4].input).draft.answers[0].text, revised.answers[0].text);
});
test('unresolved gaps and deterministic format issues cannot be waved through by critique', async () => {
  const missing = { ...plan, missingFacts: ['Confirm actual availability'] };
  const long = { ...draft, answers: [{ ...draft.answers[0], text: 'word '.repeat(40) }] };
  const { ai, calls } = fixture([missing, long, clear, long, clear]);
  const result = await ai.draft(profile, application, { consent: true });
  assert.equal(calls.length, 5);
  assert.equal(result.quality.status, 'needs_work');
  assert.ok(result.quality.issues.some(item => item.category === 'format'));
  assert.ok(result.quality.issues.some(item => item.category === 'truthfulness'));
  assert.ok(result.missingFacts.includes('Confirm actual availability'));
});
test('unknown refs and duplicate/missing plans, critique targets and rewritten refs fail closed', async () => {
  for (const outputs of [
    [{ ...plan, questionPlans: [] }],
    [{ ...plan, questionPlans: [plan.questionPlans[0], plan.questionPlans[0]] }],
    [{ ...plan, questionPlans: [{ ...plan.questionPlans[0], evidenceIds: ['pending'] }] }],
    [plan, draft, { ...clear, issues: [{ ...issue, target: 'unknown' }] }],
    [plan, draft, { ...clear, issues: [{ ...issue, researchIds: ['invented'] }] }],
    [plan, draft, { ...clear, issues: [issue] }, { ...draft, evidenceIds: ['invented'] }],
  ]) {
    const { ai } = fixture(outputs);
    await assert.rejects(ai.draft(profile, application, { consent: true }), /invalid|unverified|omitted/);
  }
});
test('checkpoint precedes each cloud request, retains status and stops next transmission', async () => {
  const { ai, calls } = fixture([plan, draft, clear]);
  let checks = 0;
  await assert.rejects(ai.draft(profile, application, { consent: true, checkpoint: () => { if (++checks === 2) throw Object.assign(new Error('Consent revoked'), { status: 409 }); } }), error => error.status === 409 && error.message === 'Consent revoked');
  assert.equal(checks, 2);
  assert.equal(calls.length, 1);
});
test('factual plans allow direct answers without employer citations; offline has no quality claim', async () => {
  const factual = { ...plan, questionPlans: [{ ...plan.questionPlans[0], kind: 'factual', researchIds: [], approach: 'Answer directly' }] };
  const direct = { ...draft, answers: [{ ...draft.answers[0], researchIds: [] }] };
  const { ai, calls } = fixture([factual, direct, clear]);
  assert.equal((await ai.draft(profile, application, { consent: true })).quality.questionPlans[0].kind, 'factual');
  const count = calls.length;
  assert.equal((await ai.draft(profile, application, { consent: true, offline: true })).quality, undefined);
  assert.equal(calls.length, count);
});

test('final unresolved critique is retained and no second rewrite occurs', async () => {
  const finalIssue = { ...issue, category: 'role_fit', severity: 'must_fix', detail: 'Actual role duties are still not addressed.' };
  const { ai, calls } = fixture([plan, draft, { summary: 'Generic.', issues: [issue] }, draft, { summary: 'Still weak role fit.', issues: [finalIssue] }]);
  const result = await ai.draft(profile, application, { consent: true });
  assert.equal(calls.length, 5);
  assert.equal(result.quality.status, 'needs_work');
  assert.deepEqual(result.quality.issues, [finalIssue]);
  assert.equal(result.quality.summary, 'Still weak role fit.');
});
test('every draft stage has a checkpoint, including rewrite and final critique', async () => {
  for (let blockedAt = 1; blockedAt <= 5; blockedAt++) {
    const { ai, calls } = fixture([plan, draft, { summary: 'Generic.', issues: [issue] }, draft, clear]);
    let checks = 0;
    await assert.rejects(ai.draft(profile, application, { consent: true, checkpoint: async () => { if (++checks === blockedAt) throw Object.assign(new Error('Snapshot changed'), { status: 409 }); } }), error => error.status === 409);
    assert.equal(calls.length, blockedAt - 1);
  }
});
test('critique severity/category enums and revised question coverage fail closed', async () => {
  for (const outputs of [
    [plan, draft, { ...clear, issues: [{ ...issue, severity: 'perfect' }] }],
    [plan, draft, { ...clear, issues: [{ ...issue, category: 'hiring_probability' }] }],
    [plan, draft, { ...clear, issues: [issue] }, { ...draft, answers: [] }],
    [plan, draft, { ...clear, issues: [issue] }, { ...draft, answers: [{ ...draft.answers[0], researchIds: ['imagined'] }] }],
  ]) {
    const { ai } = fixture(outputs);
    await assert.rejects(ai.draft(profile, application, { consent: true }), /invalid|omitted/);
  }
});
