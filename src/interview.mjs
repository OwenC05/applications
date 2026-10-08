// Deep evidence discovery is resumable and optional, never a timed eligibility gate.
const common = [
  ['direction', 'Career direction', 'What kinds of work energize you, and which roles are you considering?', 'Describe the actual tasks you enjoy, not only job titles.'],
  ['motivation', 'Career direction', 'Why are you interested in these sectors, and what experiences shaped that interest?', 'Name a concrete moment, project, conversation or course.'],
  ['values', 'Career direction', 'What matters most to you in an employer and working environment?', 'Consider learning, responsibility, impact, culture and trade-offs.'],
  ['availability', 'Practical details', 'What are your internship availability dates and any scheduling constraints?', 'Use exact dates where known; say explicitly what is uncertain.'],
  ['locations', 'Practical details', 'Which locations and working arrangements can you realistically consider?', 'Separate preferences from genuine constraints.'],
  ['education', 'Education', 'What are you studying, where, and when do you expect to graduate?', 'Include qualification names and dates you can verify.'],
  ['grades', 'Education', 'Which grades, qualifications or academic distinctions would you want an employer to know?', 'Do not estimate scores; skip anything you do not want to store.'],
  ['coursework', 'Education', 'Which course or assignment best demonstrates skills relevant to your target roles?', 'Explain the task, your contribution and an observable result.'],
  ['experience', 'Experience', 'Walk through your work experience and the responsibilities you personally held.', 'Include paid work, volunteering or caring responsibilities if relevant.'],
  ['achievement', 'Experience', 'What achievement are you most proud of, and what did you personally do to make it happen?', 'Distinguish your work from the team outcome.'],
  ['impact', 'Experience', 'Describe an example where your work improved an outcome in a measurable way.', 'Give the baseline, result and how the measurement was obtained.'],
  ['project', 'Experience', 'Describe a project from start to finish: problem, decisions, contribution and result.', 'Use a real project; incomplete projects can still supply useful evidence.'],
  ['leadership', 'Collaboration', 'When have you guided a group or taken responsibility without formal authority?', 'What did you do, how many people were involved, and what changed?'],
  ['teamwork', 'Collaboration', 'Describe a time you contributed effectively to a team with different strengths.', 'Explain coordination and your individual contribution.'],
  ['conflict', 'Collaboration', 'Tell me about a disagreement and how you handled it.', 'Explain the competing views and whether the resolution worked.'],
  ['communication', 'Collaboration', 'When have you explained a complex idea to someone unfamiliar with the subject?', 'How did you adapt your explanation and check understanding?'],
  ['feedback', 'Growth', 'What difficult feedback have you received, and what changed afterward?', 'A specific change is more useful than saying you welcome feedback.'],
  ['failure', 'Growth', 'Describe a mistake or setback, your responsibility for it, and what you learned.', 'Avoid disguising a strength as a weakness; focus on recovery.'],
  ['learning', 'Growth', 'How have you learned a difficult skill independently?', 'Describe your approach, practice and evidence of progress.'],
  ['pressure', 'Judgement', 'Tell me about competing deadlines and how you decided what to prioritize.', 'What did you deprioritize or communicate to others?'],
  ['ambiguity', 'Judgement', 'When have you worked with incomplete information or an unclear brief?', 'Explain what you investigated and what assumptions you tested.'],
  ['ethics', 'Judgement', 'Describe a real situation where doing the right thing was inconvenient.', 'Do not disclose confidential or identifying information about others.'],
  ['initiative', 'Judgement', 'When did you notice a problem nobody had asked you to solve?', 'What action did you take and what happened?'],
  ['interests', 'Personal story', 'What interests or activities outside coursework reveal something meaningful about you?', 'Describe commitment or curiosity rather than listing hobbies.'],
  ['distinctive', 'Personal story', 'What combination of experiences or perspectives makes your contribution distinctive?', 'Ground this in examples rather than unsupported adjectives.'],
  ['writing', 'Personal story', 'How would you like your application writing to sound, and what language should it avoid?', 'Give tone preferences. These are not new factual achievements.'],
  ['gaps', 'Personal story', 'What parts of your experience are difficult to explain or need careful context?', 'Share only what you are comfortable storing; never invent a replacement story.'],
  ['evidence', 'Personal story', 'Which public portfolios, projects, awards or references support your strongest claims?', 'Share links or descriptions, not passwords or private documents.'],
];
const tech = [
  ['technical-project', 'Technology', 'Describe your strongest technical project and the problem it solved.', 'Include your role, technologies, users and actual outcome.'],
  ['architecture', 'Technology', 'What important design trade-off have you made in a technical project?', 'Explain the alternatives and why the constraints favored your choice.'],
  ['debugging', 'Technology', 'Walk through a difficult bug you diagnosed and fixed.', 'Explain reproduction, investigation and how you verified the fix.'],
  ['testing', 'Technology', 'How have you tested software or checked the reliability of a technical result?', 'Describe concrete checks and a defect they caught.'],
  ['code-quality', 'Technology', 'Describe a time you improved maintainability or collaborated on code.', 'Consider reviews, documentation, version control and simplifying code.'],
  ['technical-skills', 'Technology', 'Which technical tools can you use independently, and which are still developing?', 'For each important skill, provide a project-based example.'],
  ['users', 'Technology', 'When have user feedback or accessibility needs changed your technical work?', 'Describe the feedback and the change, including limits of your knowledge.'],
  ['data', 'Technology', 'Describe an analysis, model or data task you completed and how you checked its conclusions.', 'Include data limitations and avoid claiming causation without evidence.'],
  ['security', 'Technology', 'How have you considered privacy, security or misuse in a project?', 'Acknowledge what you did not assess; do not disclose vulnerabilities or secrets.'],
  ['technical-curiosity', 'Technology', 'What technical topic have you explored recently, and why does it interest you?', 'Explain what you actually tried or learned, not just a trend name.'],
];
const finance = [
  ['finance-direction', 'Finance', 'Which finance functions interest you, and what do you understand about their daily work?', 'Distinguish banking, markets, investing, risk, accounting and other areas.'],
  ['commercial-awareness', 'Finance', 'Describe a recent business or market development you followed and your interpretation.', 'Separate known facts, your interpretation and unanswered questions.'],
  ['financial-analysis', 'Finance', 'Tell me about financial analysis or quantitative work you have actually done.', 'Include sources, assumptions, methods and limitations.'],
  ['valuation', 'Finance', 'Have you evaluated a business or investment? Explain your approach and conclusions.', 'A coursework or society example is valid; do not imply professional experience.'],
  ['finance-tools', 'Finance', 'Which spreadsheets, accounting concepts or analytical tools have you used independently?', 'Give a specific task and explain how you checked the output.'],
  ['risk', 'Finance', 'Describe a decision where you weighed potential gains against downside risk.', 'Explain uncertainty and safeguards, not only the final result.'],
  ['client-service', 'Finance', 'When have you understood another person’s needs and delivered accurate, reliable service?', 'Customer-facing non-finance experience counts.'],
  ['accuracy', 'Finance', 'Tell me about catching an error or maintaining accuracy under pressure.', 'Describe the checks you used and the impact.'],
  ['investment-thesis', 'Finance', 'What business or sector would you like to investigate further, and what questions would you ask?', 'A reasoned research question is better than unsupported investment certainty.'],
  ['finance-exposure', 'Finance', 'Which events, societies, courses or conversations informed your finance interests?', 'Clarify participation versus leadership and what you learned.'],
];
export const QUESTION_BANK = Object.freeze([
  ...common.map(([id, section, prompt, hint]) => Object.freeze({ id: `common-${id}`, section, prompt, hint, sector: 'common' })),
  ...tech.map(([id, section, prompt, hint]) => Object.freeze({ id: `tech-${id}`, section, prompt, hint, sector: 'tech' })),
  ...finance.map(([id, section, prompt, hint]) => Object.freeze({ id: `finance-${id}`, section, prompt, hint, sector: 'finance' })),
]);
export function getInterviewQuestions(sectors = []) {
  return QUESTION_BANK.filter(q => q.sector === 'common' || sectors.includes(q.sector)).map(q => ({ ...q }));
}
export function getInterviewProgress(profile) {
  const questions = getInterviewQuestions(profile.sectors);
  const allowed = new Set(questions.map(q => q.id));
  const answered = new Set(profile.interview.answers.map(a => a.questionId).filter(id => allowed.has(id)));
  const skipped = new Set(profile.interview.skippedQuestionIds.filter(id => allowed.has(id) && !answered.has(id)));
  const total = questions.length; const done = answered.size + skipped.size;
  return { total, answered: answered.size, skipped: skipped.size, remaining: total - done, percent: total ? Math.round(done / total * 100) : 0, completed: profile.interview.completed, estimatedMinutes: 60 };
}
export function getNextQuestion(profile) {
  if (profile.interview.completed) return null;
  const done = new Set([...profile.interview.answers.map(a => a.questionId), ...profile.interview.skippedQuestionIds]);
  return getInterviewQuestions(profile.sectors).find(q => !done.has(q.id)) ?? null;
}
