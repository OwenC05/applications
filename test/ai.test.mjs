import test from 'node:test';
import assert from 'node:assert/strict';
import { createAI } from '../src/ai.mjs';
const app = { id: 'app1', company: 'Example', role: 'Engineer', sector: 'tech', companyUrl: 'https://example.com', url: 'https://example.com/jobs/engineer', jobDescription: 'Python role', inputRevision: 1, questions: [{ id: 'q1', text: 'Why you?', maxWords: 20, maxChars: 150 }] };
const profile = { memories: [{ id: 'v1', status: 'verified', label: 'Project', category: 'tech', content: 'Built Python parser.' }, { id: 'pending1', status: 'pending', content: 'PENDING SECRET' }], interview: { answers: ['RAW PRIVATE'] }, writingPreferences: 'Concise' };
const url = 'https://example.com/jobs/engineer';
const textResponse = text => ({ status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text }] }] });
const researched = { roleEvidence: { company: 'Example', role: 'Engineer', sourceUrls: [url], supportExcerpt: 'Example Engineer role requires Python.' }, summary: 'Example builds tools and seeks Python skills.', companyFacts: [{ id: 'c1', text: 'Builds tools', sourceUrls: [url], supportExcerpt: 'Example builds tools.' }], requirements: [{ id: 'r1', text: 'Python', sourceUrls: [url], supportExcerpt: 'Example Engineer role requires Python.' }], hiringPriorities: [{ id: 'h1', text: 'Clear engineering', sourceUrls: [url], supportExcerpt: 'Example Engineer role requires Python.', inference: true, supportingClaimIds: ['r1'] }], unknowns: [] };
const searchResponse = { ...textResponse('Example builds tools. Example Engineer role requires Python.'), output: [{ type: 'web_search_call', action: { sources: [{ url, title: 'Engineer role' }] } }, ...textResponse('Example builds tools. Example Engineer role requires Python.').output] };
const draft = { coverLetter: 'I built a Python parser and am interested in the engineering role.', answers: [{ questionId: 'q1', text: 'I built a Python parser.', evidenceIds: ['v1'], researchIds: ['r1'] }], evidenceIds: ['v1'], researchIds: ['c1', 'r1'], missingFacts: [], warnings: [] };
const plan = { coverLetterApproach: 'Python parser fit', questionPlans: [{ questionId: 'q1', kind: 'other', approach: 'Match parser to Python role', evidenceIds: ['v1'], researchIds: ['r1'], missingFacts: [] }], missingFacts: [] };
const critique = { summary: 'Relevant and clear.', issues: [] };
function fixture(outputs, options = {}) {
  const calls = [];
  const ai = createAI({ apiKey: 'test-secret-key', clock: () => new Date('2026-10-05T12:00:00Z'), fetchImpl: async (endpoint, init) => { calls.push({ endpoint, body: JSON.parse(init.body), headers: init.headers }); const output = outputs.shift(); if (output instanceof Error) throw output; return output?.http ? { ok: false, status: output.http } : { ok: true, json: async () => output }; }, ...options });
  return { ai, calls };
}
async function researchedApp(ai) { return { ...app, research: { ...await ai.research(app, { consent: true }), inputRevision: 1 } }; }

test('no consent or no key means ZERO calls across every feature', async () => {
  for (const options of [{}, { apiKey: '' }]) {
    const { ai, calls } = fixture([], options);
    const actualConsent = options.apiKey === '' ? true : false;
    assert.equal((await ai.research(app, { consent: actualConsent })).status, 'not_researched');
    assert.equal((await ai.draft(profile, app, { consent: actualConsent })).mode, 'offline');
    const followup = await ai.followup(profile, { question: 'Project?', answer: 'Built parser', section: 'tech' }, { consent: actualConsent });
    assert.equal(followup.proposals[0].status, 'pending');
    assert.equal(calls.length, 0);
  }
});

test('two-stage research uses no profile, real sources, strict schema and no-tools normalization', async () => {
  const { ai, calls } = fixture([searchResponse, textResponse(JSON.stringify(researched))]);
  const result = await ai.research({ ...app, memories: profile.memories, secret: 'PRIVATE' }, { consent: true });
  assert.equal(result.status, 'researched');
  assert.equal(result.hiringPriorities[0].inference, true);
  assert.equal(result.sources[0].url, url);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body.store, false);
  assert.deepEqual(calls[0].body.tools[0].filters.allowed_domains, ['example.com']);
  assert.equal(calls[1].body.tools, undefined);
  assert.equal(calls[1].body.text.format.strict, true);
  assert.doesNotMatch(JSON.stringify(calls.map(call => call.body)), /PENDING SECRET|PRIVATE|RAW/);
});

test('research rejects unconsulted sources, spoofed hosts, excerpt fabrication and unsupported inference', async () => {
  for (const change of [value => value.companyFacts[0].sourceUrls = ['https://example.com/unconsulted'], value => value.companyFacts[0].sourceUrls = ['https://example.com.evil.test/jobs'], value => value.companyFacts[0].supportExcerpt = 'invented', value => value.hiringPriorities[0].supportingClaimIds = ['invented'], value => value.hiringPriorities[0].inference = false]) {
    const value = structuredClone(researched); change(value);
    const { ai } = fixture([searchResponse, textResponse(JSON.stringify(value))]);
    await assert.rejects(ai.research(app, { consent: true }), /unsupported|unconsulted|supporting/);
  }
});

test('missing actual role coverage is incomplete and blocks cloud draft', async () => {
  const partial = { ...researched, requirements: [], hiringPriorities: [], unknowns: ['Role unavailable'] };
  const { ai, calls } = fixture([searchResponse, textResponse(JSON.stringify(partial))]);
  const application = await researchedApp(ai);
  assert.equal(application.research.status, 'incomplete');
  await assert.rejects(ai.draft(profile, application, { consent: true }), /research is required/);
  assert.equal(calls.length, 2);
});

test('cloud drafting sends only verified context, checks actual question and evidence IDs', async () => {
  const { ai, calls } = fixture([searchResponse, textResponse(JSON.stringify(researched)), textResponse(JSON.stringify(plan)), textResponse(JSON.stringify(draft)), textResponse(JSON.stringify(critique))]);
  const application = await researchedApp(ai);
  const result = await ai.draft(profile, application, { consent: true });
  assert.equal(result.mode, 'cloud');
  assert.deepEqual(result.answers[0].evidenceIds, ['v1']);
  assert.doesNotMatch(calls[2].body.input, /PENDING SECRET|RAW PRIVATE|test-secret-key/);
  assert.match(calls[2].body.input, /Python parser/);
  for (const modify of [value => value.answers[0].evidenceIds = ['pending1'], value => value.answers[0].researchIds = ['imagined'], value => value.answers[0].questionId = 'unknown', value => value.answers = [], value => value.answers.push(value.answers[0])]) {
    const invalid = structuredClone(draft); modify(invalid);
    const { ai: invalidAI } = fixture([textResponse(JSON.stringify(plan)), textResponse(JSON.stringify(invalid))]);
    await assert.rejects(invalidAI.draft(profile, application, { consent: true }), /invalid|unverified|omitted/);
  }
});

test('stale research is blocked and limit overruns are flagged without truncation', async () => {
  const { ai } = fixture([searchResponse, textResponse(JSON.stringify(researched))]);
  const application = await researchedApp(ai);
  for (const research of [{ ...application.research, inputRevision: 2 }, { ...application.research, retrievedAt: '2026-09-01T00:00:00Z' }, { ...application.research, retrievedAt: 'invalid' }]) await assert.rejects(ai.draft(profile, { ...application, research }, { consent: true }), /Refresh research/);
  const tooLong = structuredClone(draft); tooLong.answers[0].text = 'word '.repeat(50);
  const { ai: draftingAI } = fixture([textResponse(JSON.stringify(plan)), textResponse(JSON.stringify(tooLong)), textResponse(JSON.stringify(critique)), textResponse(JSON.stringify(tooLong)), textResponse(JSON.stringify(critique))]);
  const result = await draftingAI.draft(profile, application, { consent: true });
  assert.equal(result.answers[0].text, tooLong.answers[0].text);
  assert.equal(result.warnings.filter(warning => warning.startsWith('LIMIT EXCEEDED:')).length, 2);
});

test('malformed, refused, incomplete and provider auth errors are bounded and sanitized', async () => {
  for (const output of [textResponse('not json'), textResponse('{}'), { status: 'incomplete' }, { output: [{ type: 'message', content: [{ type: 'refusal', refusal: 'no' }] }] }, { http: 401 }, Object.assign(new Error('test-secret-key private network'), { status: 502 })]) {
    const { ai } = fixture([output]);
    await assert.rejects(ai.followup(profile, { question: 'Project?', answer: 'Built parser', section: 'tech' }, { consent: true }), error => !error.message.includes('test-secret-key') && Boolean(error.status));
  }
});

test('cloud followup is adaptive and every proposal remains pending', async () => {
  const result = { question: 'How did you test the parser?', rationale: 'Explore the claimed project', proposals: [{ category: 'tech', label: 'Parser', content: 'Built a parser' }] };
  const { ai, calls } = fixture([textResponse(JSON.stringify(result))]);
  const followup = await ai.followup(profile, { question: 'Project?', answer: 'Built a parser', section: 'tech' }, { consent: true });
  assert.equal(followup.proposals[0].status, 'pending');
  assert.doesNotMatch(calls[0].body.input, /RAW PRIVATE|PENDING SECRET/);
});

test('public official URL validation blocks local/IP and accepts precise trusted ATS subdomain', async () => {
  const { ai, calls } = fixture([]);
  for (const companyUrl of ['http://example.com', 'https://localhost', 'https://127.0.0.1', 'https://[::1]', 'https://user:pass@example.com', 'https://example.com:8443']) await assert.rejects(ai.research({ ...app, companyUrl }, { consent: true }), /official|public/);
  assert.equal(calls.length, 0);
  const fixtureAI = fixture([searchResponse, textResponse(JSON.stringify(researched))]);
  await fixtureAI.ai.research({ ...app, url: 'https://example.myworkdayjobs.com/role' }, { consent: true });
  assert.deepEqual(fixtureAI.calls[0].body.tools[0].filters.allowed_domains, ['example.com', 'example.myworkdayjobs.com']);
});


test('consulted company-only or wrong-employer role evidence never becomes researched', async () => {
  for (const excerpt of ['Example builds tools.', 'DifferentCompany Engineer role requires Python.']) {
    const value = structuredClone(researched);
    value.roleEvidence.supportExcerpt = excerpt;
    const sourceResponse = { ...searchResponse, output: [searchResponse.output[0], ...textResponse(`Example builds tools. ${excerpt}`).output] };
    value.requirements[0].supportExcerpt = excerpt;
    value.hiringPriorities[0].supportExcerpt = excerpt;
    const { ai } = fixture([sourceResponse, textResponse(JSON.stringify(value))]);
    assert.equal((await ai.research(app, { consent: true })).status, 'incomplete');
  }
});

test('timeout is bounded even if transport ignores abort; auth/quota never retry', async () => {
  const { ai, calls } = fixture([], { timeoutMs: 5, fetchImpl: () => new Promise(() => {}) });
  await assert.rejects(ai.followup(profile, { question: 'Project?', answer: 'Parser' }, { consent: true }), /timed out/);
  for (const http of [401, 429]) {
    const value = fixture([{ http }]);
    await assert.rejects(value.ai.research(app, { consent: true }), /HTTP/);
    assert.equal(value.calls.length, 1);
  }
});

test('research normalization and followup honor checkpoint failures before transmission', async () => {
  const { ai, calls } = fixture([searchResponse]);
  let checks = 0;
  await assert.rejects(ai.research(app, { consent: true, checkpoint: () => { if (++checks === 2) throw Object.assign(new Error('Consent revoked'), { status: 409 }); } }), error => error.status === 409);
  assert.equal(calls.length, 1);
  await assert.rejects(ai.followup(profile, { question: 'Project?', answer: 'Parser' }, { consent: true, checkpoint: () => { throw Object.assign(new Error('Profile deleted'), { status: 404 }); } }), error => error.status === 404);
  assert.equal(calls.length, 1);
});
