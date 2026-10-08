import test from 'node:test';
import assert from 'node:assert/strict';
import { QUESTION_BANK, getInterviewQuestions, getInterviewProgress, getNextQuestion } from '../src/interview.mjs';
const profile = sectors => ({ sectors, interview: { answers: [], skippedQuestionIds: [], completed: false } });
test('substantive bank spans common, tech and finance and returns independent question objects', () => {
  assert.ok(QUESTION_BANK.length >= 40); assert.equal(new Set(QUESTION_BANK.map(q => q.id)).size, QUESTION_BANK.length);
  for (const q of QUESTION_BANK) { assert.ok(q.prompt.length > 40); assert.ok(q.hint.length > 30); assert.ok(q.section); }
  const tech = getInterviewQuestions(['tech']); const finance = getInterviewQuestions(['finance']);
  assert.ok(tech.some(q => q.sector === 'tech')); assert.ok(!tech.some(q => q.sector === 'finance'));
  assert.ok(finance.some(q => q.sector === 'finance')); assert.ok(!finance.some(q => q.sector === 'tech'));
  tech[0].prompt = 'Changed'; assert.notEqual(getInterviewQuestions(['tech'])[0].prompt, 'Changed');
});
test('resumes unanswered questions, tracks unique answered/skipped IDs and ignores out of sector history', () => {
  const p = profile(['tech']); const questions = getInterviewQuestions(p.sectors);
  p.interview.answers = [{ questionId: questions[0].id }, { questionId: questions[0].id }, { questionId: 'finance-risk' }];
  p.interview.skippedQuestionIds = [questions[0].id, questions[1].id, 'finance-risk'];
  assert.equal(getNextQuestion(p).id, questions[2].id);
  const progress = getInterviewProgress(p); assert.equal(progress.answered, 1); assert.equal(progress.skipped, 1); assert.equal(progress.remaining, questions.length - 2);
  assert.equal(progress.completed, false); assert.equal(progress.estimatedMinutes, 60);
});
test('early finish is allowed, reopening resumes and exhausted interview returns null', () => {
  const p = profile(['finance']); p.interview.completed = true; assert.equal(getNextQuestion(p), null); assert.equal(getInterviewProgress(p).completed, true);
  p.interview.completed = false; assert.ok(getNextQuestion(p)); p.interview.skippedQuestionIds = getInterviewQuestions(p.sectors).map(q => q.id);
  assert.equal(getNextQuestion(p), null); assert.equal(getInterviewProgress(p).percent, 100);
});
