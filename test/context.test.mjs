import test from 'node:test';
import assert from 'node:assert/strict';
import { buildContext } from '../src/context.mjs';

test('context contains only verified evidence and explicit preference, never history or secrets', () => {
  const profile = { writingPreferences: 'Concise', apiKey: 'SECRET', interview: { answers: ['RAW SECRET'] }, memories: [
    { id: 'v', status: 'verified', label: 'Project', category: 'tech', content: 'Built Python parser' },
    ...['pending', 'rejected', 'superseded'].map(status => ({ id: status, status, content: `PRIVATE ${status}` })),
  ], applications: [{ history: [{ answers: ['HISTORY SECRET'] }] }] };
  const context = buildContext(profile, { company: 'Example', role: 'Python engineer', sector: 'tech', jobDescription: 'Python', questions: [] });
  assert.deepEqual(context.evidence.map(item => item.id), ['v']);
  assert.equal(context.writingPreferences, 'Concise');
  assert.doesNotMatch(JSON.stringify(context), /PRIVATE|SECRET|apiKey|history|interview/);
});

test('relevant active evidence precedes unrelated evidence and retrieval is bounded', () => {
  const memories = Array.from({ length: 30 }, (_, i) => ({ id: `${i}`, status: 'verified', label: 'Example', category: 'common', content: i === 29 ? 'Financial valuation model' : 'Financial experience' }));
  const context = buildContext({ memories }, { role: 'Financial analyst', sector: 'finance', jobDescription: 'valuation', questions: [] });
  assert.equal(context.evidence.length, 24);
  assert.equal(context.evidence[0].id, '29');
});


test('irrelevant verified facts and stopword-only overlaps are not sent', () => {
  const context = buildContext({ memories: [{ id: 'birthday', status: 'verified', label: 'Personal', category: 'identity', content: 'My birthday is in June and I live with family.' }] }, { role: 'Engineer', sector: 'tech', jobDescription: 'You will work with our team and build systems', questions: [{ id: 'q', text: 'Why?', apiKey: 'secret', maxWords: 50 }] });
  assert.deepEqual(context.evidence, []);
  assert.doesNotMatch(JSON.stringify(context), /birthday|secret|apiKey/);
});


test('actual application question terms retrieve otherwise unrelated verified evidence', () => {
  const context = buildContext({ memories: [{ id: 'grade', status: 'verified', label: 'Degree grade', category: 'education', content: 'First-class degree' }] }, { role: 'Engineer', sector: 'tech', jobDescription: 'Build systems', questions: [{ id: 'q', text: 'What is your degree grade?' }] });
  assert.deepEqual(context.evidence.map(item => item.id), ['grade']);
});

test('sourced role criteria retrieve confirmed examples absent from pasted role text', () => {
  const memories = [{ id: 'fit', status: 'verified', category: 'experience', label: 'School activity', content: 'Resolved disagreement through mediation' }, { id: 'pending', status: 'pending', content: 'Mediation private' }];
  const context = buildContext({ memories }, { role: 'Engineer', sector: 'tech', jobDescription: 'Build systems', questions: [], research: { requirements: [{ text: 'Mediation' }], hiringPriorities: [{ text: 'Resolve disagreement' }] } });
  assert.deepEqual(context.evidence.map(item => item.id), ['fit']);
});
