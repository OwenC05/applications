import { randomUUID } from 'node:crypto';
import { mkdirSync, openSync, closeSync, writeFileSync, readFileSync, fsyncSync, renameSync, unlinkSync, existsSync, chmodSync } from 'node:fs';
import { resolve, join } from 'node:path';

const copy = value => structuredClone(value);
const now = () => new Date().toISOString();
const fail = (message, status = 400) => { const error = new Error(message); error.status = status; throw error; };
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const string = (value, name, max = 100000, optional = false) => {
  if (optional && value === undefined) return '';
  if (typeof value !== 'string' || (!optional && !value.trim()) || value.length > max) fail(`Invalid ${name}`);
  return value;
};
const sectors = value => {
  if (!Array.isArray(value) || !value.length || value.some(item => !['tech', 'finance'].includes(item))) fail('Invalid sectors');
  return [...new Set(value)];
};
const only = (value, keys) => {
  if (!object(value) || Object.keys(value).some(key => !keys.includes(key))) fail('Unsupported fields');
};
const unique = values => [...new Set(values)];
const refs = (value, name) => {
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) fail(`Invalid ${name}`);
  return unique(value);
};
function questions(value = []) {
  if (!Array.isArray(value) || value.length > 100) fail('Invalid questions');
  const result = value.map(item => {
    only(item, ['id', 'text', 'maxWords', 'maxChars']);
    const limit = (n, label) => n == null ? null : Number.isSafeInteger(n) && n > 0 && n <= 100000 ? n : fail(`Invalid ${label}`);
    return { id: item.id === undefined ? randomUUID() : string(item.id, 'question id', 200), text: string(item.text, 'question', 10000), maxWords: limit(item.maxWords, 'maxWords'), maxChars: limit(item.maxChars, 'maxChars') };
  });
  if (new Set(result.map(item => item.id)).size !== result.length) fail('Duplicate question IDs');
  return result;
}
function source(value = { kind: 'manual' }) {
  only(value, ['kind', 'id', 'label']);
  return { kind: string(value.kind, 'source kind', 100), ...(value.id === undefined ? {} : { id: string(value.id, 'source id', 200) }), ...(value.label === undefined ? {} : { label: string(value.label, 'source label', 1000) }) };
}
function memory(input) {
  only(input, ['category', 'label', 'content', 'source', 'supersedes']);
  return { id: randomUUID(), category: string(input.category, 'category', 200), label: string(input.label, 'label', 1000), content: string(input.content, 'content'), status: 'pending', source: source(input.source), supersedes: input.supersedes == null ? null : string(input.supersedes, 'supersedes', 200), createdAt: now(), confirmedAt: null };
}
function quality(value, { questionIds, evidenceIds, researchIds, draft } = {}) {
  if (value == null) return value;
  only(value, ['version', 'status', 'checkedAt', 'rewriteCount', 'summary', 'questionPlans', 'issues']);
  if (value.version !== 1 || !['assessed', 'needs_work', 'stale'].includes(value.status) || ![0, 1].includes(value.rewriteCount) || typeof value.checkedAt !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value.checkedAt) || !Number.isFinite(Date.parse(value.checkedAt))) fail('Invalid quality assessment');
  string(value.summary, 'quality summary', 10000);
  if (!Array.isArray(value.questionPlans) || !Array.isArray(value.issues) || value.questionPlans.length > 100 || value.issues.length > 1000) fail('Invalid quality details');
  const check = (items, known, label) => {
    const checked = refs(items, label);
    if (checked.length !== items.length || checked.some(item => !item.trim() || (known && !known.has(item)))) fail(`Invalid quality ${label}`, 422);
  };
  const seen = new Set();
  for (const plan of value.questionPlans) {
    only(plan, ['questionId', 'kind', 'approach', 'evidenceIds', 'researchIds', 'missingFacts']);
    string(plan.questionId, 'quality question ID', 200); string(plan.approach, 'quality approach', 10000);
    if (seen.has(plan.questionId) || (questionIds && !questionIds.has(plan.questionId)) || !['factual', 'motivation', 'behavioral', 'technical', 'commercial', 'other'].includes(plan.kind)) fail('Invalid quality question plan', 422);
    seen.add(plan.questionId); check(plan.evidenceIds, evidenceIds, 'evidenceIds'); check(plan.researchIds, researchIds, 'researchIds'); check(plan.missingFacts, null, 'missingFacts');
  }
  if (questionIds && seen.size !== questionIds.size) fail('Every question requires a quality plan', 422);
  for (const issue of value.issues) {
    only(issue, ['target', 'category', 'severity', 'detail', 'evidenceIds', 'researchIds']);
    string(issue.target, 'quality target', 200); string(issue.detail, 'quality issue', 10000);
    if ((issue.target !== 'coverLetter' && !seen.has(issue.target)) || !['truthfulness', 'specificity', 'role_fit', 'question_coverage', 'clarity', 'format', 'voice'].includes(issue.category) || !['must_fix', 'improve'].includes(issue.severity)) fail('Invalid quality issue', 422);
    check(issue.evidenceIds, evidenceIds, 'evidenceIds'); check(issue.researchIds, researchIds, 'researchIds');
  }
  if (value.status === 'assessed' && (value.issues.length || value.questionPlans.some(plan => plan.missingFacts.length) || draft?.missingFacts.length || draft?.answers.some(answer => !answer.text.trim()))) fail('Unresolved gaps cannot be assessed', 422);
  return copy(value);
}
function staleDraft(draft, explanation) {
  if (!draft) return;
  draft.stale = true;
  if (draft.quality) { draft.quality.status = 'stale'; draft.quality.summary = explanation; }
}
function invalidate(profile, clear = false) {
  for (const app of profile.applications) {
    if (app.draft) { if (clear) app.draft = null; else staleDraft(app.draft, 'Editorial assessment is stale because the applicant brain or generation settings changed.'); }
    if (app.status === 'reviewed') app.status = 'draft';
  }
}
const currentCloudResearch = application => {
  const research = application.research;
  const timestamp = Date.parse(research?.retrievedAt);
  return research?.mode === 'cloud' && research.status === 'researched' && research.inputRevision === application.inputRevision && Number.isFinite(timestamp) && Date.now() - timestamp <= 7 * 86400000;
};
function appInput(input, existing = null) {
  const result = existing ? copy(existing) : {};
  for (const key of ['company', 'role', 'jobDescription']) if (!existing || key in input) result[key] = string(input[key], key, key === 'jobDescription' ? 100000 : 1000);
  if (!existing || 'sector' in input) { if (!['tech', 'finance'].includes(input.sector)) fail('Invalid sector'); result.sector = input.sector; }
  for (const key of ['location', 'url', 'companyUrl']) if (!existing || key in input) result[key] = string(input[key], key, 4000, true);
  if (!existing || 'questions' in input) result.questions = questions(input.questions);
  return result;
}
function validateState(state) {
  // Reject malformed storage instead of silently resetting or repairing user data.
  if (!object(state) || state.version !== 1 || !Array.isArray(state.profiles)) fail('Corrupt storage: invalid root', 500);
  const ids = new Set();
  const id = value => { if (typeof value !== 'string' || !value || ids.has(value)) fail('Corrupt storage: invalid or duplicate ID', 500); ids.add(value); };
  try {
    for (const p of state.profiles) {
      id(p.id); string(p.name, 'name', 200); sectors(p.sectors);
      if (typeof p.isDemo !== 'boolean' || typeof p.cloudConsent !== 'boolean' || typeof p.writingPreferences !== 'string' || !Number.isSafeInteger(p.revision) || p.revision < 0 || !Number.isSafeInteger(p.brainRevision) || p.brainRevision < 0 || !Array.isArray(p.memories) || !Array.isArray(p.applications) || !object(p.interview) || !Array.isArray(p.interview.answers) || !Array.isArray(p.interview.skippedQuestionIds) || typeof p.interview.completed !== 'boolean') fail('Invalid profile');
      refs(p.interview.skippedQuestionIds, 'skippedQuestionIds');
      for (const m of p.memories) { id(m.id); string(m.content, 'content'); string(m.label, 'label'); string(m.category, 'category'); source(m.source); if (!['pending', 'verified', 'superseded', 'rejected'].includes(m.status) || !(m.supersedes === null || typeof m.supersedes === 'string') || !(m.confirmedAt === null || typeof m.confirmedAt === 'string')) fail('Invalid memory status'); }
      for (const m of p.memories) if (m.supersedes && !p.memories.some(old => old.id === m.supersedes)) fail('Invalid supersession ownership');
      for (const a of p.interview.answers) { id(a.id); for (const k of ['questionId', 'question', 'section', 'answer']) string(a[k], k); }
      for (const a of p.applications) {
        id(a.id); appInput(a); if (!Number.isSafeInteger(a.inputRevision) || a.inputRevision < 0 || !['draft', 'reviewed', 'submitted'].includes(a.status) || !Array.isArray(a.history)) fail('Invalid application');
        if (a.research !== null) { if (!object(a.research) || !Number.isSafeInteger(a.research.inputRevision)) fail('Invalid research'); id(a.research.id); }
        const validateAnswers = answers => {
          const seen = new Set();
          for (const answer of answers) {
            if (!object(answer) || typeof answer.questionId !== 'string' || typeof answer.text !== 'string' || seen.has(answer.questionId)) fail('Invalid answer');
            seen.add(answer.questionId); refs(answer.evidenceIds, 'evidenceIds'); refs(answer.researchIds, 'researchIds');
          }
        };
        if (a.draft !== null) {
          if (!object(a.draft) || typeof a.draft.coverLetter !== 'string' || !Array.isArray(a.draft.answers) || typeof a.draft.stale !== 'boolean' || !Number.isSafeInteger(a.draft.brainRevision) || !Number.isSafeInteger(a.draft.inputRevision) || !['offline', 'cloud'].includes(a.draft.mode)) fail('Invalid draft');
          id(a.draft.id); validateAnswers(a.draft.answers); refs(a.draft.evidenceIds, 'evidenceIds'); refs(a.draft.researchIds, 'researchIds'); refs(a.draft.missingFacts, 'missingFacts'); refs(a.draft.warnings, 'warnings');
          const current = !a.draft.stale && a.draft.brainRevision === p.brainRevision && a.draft.inputRevision === a.inputRevision && a.draft.researchId === (a.research?.id ?? null);
          // Stale prose and historical snapshots may refer to a replaced question
          // or research brief. Their original IDs remain structurally validated.
          quality(a.draft.quality, { questionIds: new Set((current ? a.questions : a.draft.answers).map(q => q.id ?? q.questionId)), evidenceIds: current ? new Set(p.memories.filter(m => m.status === 'verified').map(m => m.id)) : undefined, researchIds: current ? new Set(['companyFacts', 'requirements', 'hiringPriorities'].flatMap(key => (a.research?.[key] ?? []).map(item => item.id))) : undefined, draft: a.draft });
          if (a.draft.quality && ((a.draft.mode !== 'cloud') || (!current && a.draft.quality.status !== 'stale'))) fail('Invalid quality freshness');
        }
        const events = new Set();
        for (const h of a.history) {
          if (!object(h) || !['reviewed', 'submitted'].includes(h.status) || typeof h.eventId !== 'string' || events.has(h.eventId) || typeof h.draftId !== 'string' || typeof h.coverLetter !== 'string' || !Array.isArray(h.answers)) fail('Invalid history');
          events.add(h.eventId); validateAnswers(h.answers); refs(h.evidenceIds, 'evidenceIds');
          quality(h.quality, { questionIds: new Set(h.answers.map(answer => answer.questionId)) });
        }
      }
    }
  } catch (error) { fail(`Corrupt storage: ${error.message}`, 500); }
}

export class BrainStore {
  #directory; #file; #lock; #lockToken; #state; #closed = false;
  constructor({ directory }) {
    this.#directory = resolve(string(directory, 'directory', 4000));
    mkdirSync(this.#directory, { recursive: true, mode: 0o700 });
    chmodSync(this.#directory, 0o700);
    this.#file = join(this.#directory, 'state.json');
    this.#lock = join(this.#directory, '.writer.lock');
    this.#lockToken = `${process.pid}:${randomUUID()}`;
    let handle;
    try { handle = openSync(this.#lock, 'wx', 0o600); writeFileSync(handle, this.#lockToken); closeSync(handle); }
    catch (error) { if (handle !== undefined) closeSync(handle); fail(`Data directory already owned or unavailable: ${error.code}`, 409); }
    try {
      this.#state = existsSync(this.#file) ? JSON.parse(readFileSync(this.#file, 'utf8')) : { version: 1, profiles: [] };
      validateState(this.#state);
      if (!existsSync(this.#file)) this.#persist(this.#state);
      else chmodSync(this.#file, 0o600);
    } catch (error) { this.close(); if (!error.status) error.status = 500; throw error; }
  }
  close() {
    if (this.#closed) return;
    if (existsSync(this.#lock) && readFileSync(this.#lock, 'utf8') === this.#lockToken) unlinkSync(this.#lock);
    this.#closed = true;
  }
  #ready() { if (this.#closed) fail('Store is closed', 503); }
  #persist(state) {
    const temp = join(this.#directory, `.state-${randomUUID()}.tmp`);
    let handle;
    try {
      handle = openSync(temp, 'wx', 0o600); writeFileSync(handle, JSON.stringify(state, null, 2)); fsyncSync(handle); closeSync(handle); handle = undefined;
      renameSync(temp, this.#file);
    } catch (error) {
      if (handle !== undefined) closeSync(handle);
      try { unlinkSync(temp); } catch { /* A failed write may not have created the temporary file. */ }
      error.status = 500; throw error;
    }
  }
  #profile(state, id) { const p = state.profiles.find(item => item.id === id); if (!p) fail('Profile not found', 404); return p; }
  #app(p, id) { const a = p.applications.find(item => item.id === id); if (!a) fail('Application not found', 404); return a; }
  #change(profileId, fn) {
    this.#ready(); const state = copy(this.#state); const p = this.#profile(state, profileId);
    const changed = fn(p);
    if (changed === false) return copy(p);
    p.revision++; p.updatedAt = now(); this.#persist(state); this.#state = state; return copy(p);
  }
  listProfiles() { this.#ready(); return this.#state.profiles.map(({ id, name, sectors, isDemo, cloudConsent, revision, brainRevision, createdAt, updatedAt }) => copy({ id, name, sectors, isDemo, cloudConsent, revision, brainRevision, createdAt, updatedAt })); }
  getProfile(id) { this.#ready(); return copy(this.#profile(this.#state, id)); }
  createProfile(input) {
    this.#ready(); only(input, ['name', 'sectors', 'isDemo']);
    if (input.isDemo !== undefined && typeof input.isDemo !== 'boolean') fail('Invalid isDemo');
    const timestamp = now(); const p = { id: randomUUID(), name: string(input.name, 'name', 200), sectors: sectors(input.sectors), isDemo: input.isDemo ?? false, cloudConsent: false, writingPreferences: '', revision: 0, brainRevision: 0, createdAt: timestamp, updatedAt: timestamp, memories: [], interview: { answers: [], skippedQuestionIds: [], completed: false }, applications: [] };
    const state = copy(this.#state); state.profiles.push(p); this.#persist(state); this.#state = state; return copy(p);
  }
  updateProfile(id, patch) {
    only(patch, ['name', 'sectors', 'cloudConsent', 'writingPreferences']);
    return this.#change(id, p => {
      if ('name' in patch) p.name = string(patch.name, 'name', 200);
      if ('sectors' in patch) p.sectors = sectors(patch.sectors);
      if ('cloudConsent' in patch) { if (typeof patch.cloudConsent !== 'boolean') fail('Invalid consent'); p.cloudConsent = patch.cloudConsent; }
      if ('writingPreferences' in patch) p.writingPreferences = string(patch.writingPreferences, 'writingPreferences', 10000, true);
      if (Object.keys(patch).length) { p.brainRevision++; invalidate(p); }
    });
  }
  deleteProfile(id) { this.#ready(); const state = copy(this.#state); this.#profile(state, id); state.profiles = state.profiles.filter(p => p.id !== id); this.#persist(state); this.#state = state; return { deleted: true }; }
  addMemory(id, input) { return this.#change(id, p => { const m = memory(input); if (m.supersedes && !p.memories.some(old => old.id === m.supersedes && old.status === 'verified')) fail('Superseded memory must be active verified evidence', 409); p.memories.push(m); }); }
  reviewMemory(id, memoryId, input) {
    only(input, ['action', 'content']);
    return this.#change(id, p => {
      const m = p.memories.find(item => item.id === memoryId); if (!m) fail('Memory not found', 404);
      if (m.status !== 'pending') fail('Only pending memories can be reviewed', 409);
      if (!['confirm', 'reject'].includes(input.action)) fail('Invalid review action');
      if (input.content !== undefined) m.content = string(input.content, 'content');
      if (input.action === 'reject') { m.status = 'rejected'; return; }
      if (m.supersedes) { const old = p.memories.find(item => item.id === m.supersedes && item.status === 'verified'); if (!old) fail('Correction conflicts with current evidence', 409); old.status = 'superseded'; }
      m.status = 'verified'; m.confirmedAt = now(); p.brainRevision++; invalidate(p);
    });
  }
  deleteMemory(id, memoryId) {
    return this.#change(id, p => {
      const m = p.memories.find(item => item.id === memoryId); if (!m) fail('Memory not found', 404);
      const removed = new Set([memoryId]);
      if (m.source.kind === 'interview' && m.source.id) {
        p.interview.answers = p.interview.answers.filter(a => a.id !== m.source.id);
        for (const other of p.memories) if (other.source.kind === 'interview' && other.source.id === m.source.id && other.status !== 'verified') removed.add(other.id);
      }
      p.memories = p.memories.filter(item => !removed.has(item.id));
      for (const item of p.memories) if (removed.has(item.supersedes)) item.supersedes = null;
      for (const app of p.applications) {
        // Forgetting conservatively erases all derived prose, since human edits may
        // contain the forgotten fact without a complete machine-readable mapping.
        app.draft = null; app.history = [];
        p.memories = p.memories.filter(item => !(item.source.kind === 'application' && item.source.id === app.id && item.status !== 'verified'));
        if (app.status === 'reviewed') app.status = 'draft';
      }
      p.brainRevision++;
    });
  }
  saveInterviewAnswer(id, input) {
    only(input, ['questionId', 'question', 'section', 'answer']);
    return this.#change(id, p => {
      const timestamp = now(); const answer = { id: randomUUID(), questionId: string(input.questionId, 'questionId', 200), question: string(input.question, 'question', 10000), section: string(input.section, 'section', 200), answer: string(input.answer, 'answer'), createdAt: timestamp, updatedAt: timestamp };
      const priorIds = new Set(p.interview.answers.filter(a => a.questionId === answer.questionId).map(a => a.id));
      const previous = p.memories.findLast(m => m.source.kind === 'interview' && priorIds.has(m.source.id) && m.status === 'verified');
      p.interview.answers.push(answer); p.interview.skippedQuestionIds = p.interview.skippedQuestionIds.filter(q => q !== answer.questionId);
      p.memories.push(memory({ category: answer.section, label: answer.question, content: answer.answer, source: { kind: 'interview', id: answer.id, label: answer.question }, supersedes: previous?.id ?? null }));
    });
  }
  setInterviewProgress(id, input) {
    only(input, ['skippedQuestionIds', 'completed']);
    return this.#change(id, p => {
      if ('skippedQuestionIds' in input) p.interview.skippedQuestionIds = refs(input.skippedQuestionIds, 'skippedQuestionIds');
      if ('completed' in input) { if (typeof input.completed !== 'boolean') fail('Invalid completed'); p.interview.completed = input.completed; }
    });
  }
  createApplication(id, input) {
    only(input, ['company', 'role', 'sector', 'location', 'url', 'companyUrl', 'jobDescription', 'questions']);
    return this.#change(id, p => { const timestamp = now(); p.applications.push({ ...appInput(input), id: randomUUID(), status: 'draft', inputRevision: 0, createdAt: timestamp, updatedAt: timestamp, research: null, draft: null, history: [] }); });
  }
  updateApplication(id, appId, patch) {
    only(patch, ['company', 'role', 'sector', 'location', 'url', 'companyUrl', 'jobDescription', 'questions', 'draft', 'expectedDraftId']);
    return this.#change(id, p => {
      const a = this.#app(p, appId); const fields = Object.keys(patch).filter(k => !['draft', 'expectedDraftId'].includes(k));
      if ('draft' in patch && fields.length) fail('Edit input and draft separately');
      if (fields.length) { Object.assign(a, appInput(patch, a)); a.inputRevision++; a.research = null; staleDraft(a.draft, 'Editorial assessment is stale because the role or application questions changed.'); a.status = 'draft'; }
      if ('draft' in patch) {
        if (!a.draft || patch.expectedDraftId !== a.draft.id) fail('Draft version changed', 409);
        only(patch.draft, ['coverLetter', 'answers']);
        const d = copy(a.draft);
        if ('coverLetter' in patch.draft) d.coverLetter = string(patch.draft.coverLetter, 'coverLetter', 100000, true);
        if ('answers' in patch.draft) {
          if (!Array.isArray(patch.draft.answers)) fail('Invalid answers');
          const edits = new Map();
          for (const edit of patch.draft.answers) { only(edit, ['questionId', 'text']); if (!a.questions.some(q => q.id === edit.questionId) || edits.has(edit.questionId)) fail('Invalid edited question ID'); edits.set(edit.questionId, string(edit.text, 'answer', 100000, true)); }
          d.answers = d.answers.map(answer => ({ ...answer, text: edits.get(answer.questionId) ?? answer.text }));
        }
        if (d.quality) { d.quality.status = 'stale'; d.quality.summary = 'Editorial assessment is stale because this prose was edited after the AI check. Generate again to reassess; human review remains required.'; }
        d.id = randomUUID(); d.createdAt = now(); d.humanEdited = true; a.draft = d; a.status = 'draft';
      }
      a.updatedAt = now();
    });
  }
  saveResearch(id, appId, research, { expectedInputRevision } = {}) {
    return this.#change(id, p => {
      const a = this.#app(p, appId); if (a.inputRevision !== expectedInputRevision) fail('Application changed during research', 409);
      if (!object(research) || !['offline', 'cloud'].includes(research.mode) || typeof research.status !== 'string') fail('Invalid research');
      a.research = { ...copy(research), id: randomUUID(), inputRevision: a.inputRevision };
      staleDraft(a.draft, 'Editorial assessment is stale because the company or role research changed.'); a.status = 'draft'; a.updatedAt = now();
    });
  }
  saveDraft(id, appId, draft, { expectedBrainRevision, expectedInputRevision, expectedResearchId, expectedDraftId, expectedHistoryLength } = {}) {
    return this.#change(id, p => {
      const a = this.#app(p, appId);
      if (p.brainRevision !== expectedBrainRevision || a.inputRevision !== expectedInputRevision || (a.research?.id ?? null) !== expectedResearchId || (a.draft?.id ?? null) !== expectedDraftId || a.history.length !== expectedHistoryLength) fail('Brain, application, research, draft or review history changed during generation', 409);
      only(draft, ['mode', 'coverLetter', 'answers', 'evidenceIds', 'researchIds', 'missingFacts', 'warnings', 'quality']);
      if (!['offline', 'cloud'].includes(draft.mode)) fail('Invalid draft mode');
      if (draft.mode === 'cloud' && !currentCloudResearch(a)) fail('Current sourced research required', 422);
      const active = new Set(p.memories.filter(m => m.status === 'verified').map(m => m.id));
      const researched = new Set(['companyFacts', 'requirements', 'hiringPriorities'].flatMap(key => (a.research?.[key] ?? []).map(item => item.id)));
      const checkRefs = (items, set, label) => { const result = refs(items, label); if (result.some(item => !set.has(item))) fail(`Invalid ${label}`, 422); return result; };
      if (!Array.isArray(draft.answers) || draft.answers.length !== a.questions.length) fail('Every application question requires an answer', 422);
      const seen = new Set(); const answers = draft.answers.map(answer => {
        only(answer, ['questionId', 'text', 'evidenceIds', 'researchIds']);
        if (!a.questions.some(q => q.id === answer.questionId) || seen.has(answer.questionId)) fail('Unknown or duplicate question ID', 422); seen.add(answer.questionId);
        return { questionId: answer.questionId, text: string(answer.text, 'answer', 100000, true), evidenceIds: checkRefs(answer.evidenceIds, active, 'evidenceIds'), researchIds: checkRefs(answer.researchIds, researched, 'researchIds') };
      });
      const assessment = quality(draft.quality, { questionIds: new Set(a.questions.map(q => q.id)), evidenceIds: active, researchIds: researched, draft });
      if (assessment && (draft.mode !== 'cloud' || assessment.status === 'stale')) fail('New quality assessment requires a current cloud draft', 422);
      if (assessment?.status === 'assessed' && a.questions.some(question => {
        const text = answers.find(answer => answer.questionId === question.id).text;
        return (question.maxWords && text.trim().split(/\s+/u).filter(Boolean).length > question.maxWords) || (question.maxChars && [...text].length > question.maxChars);
      })) fail('Over-limit answers cannot be assessed', 422);
      a.draft = { mode: draft.mode, coverLetter: string(draft.coverLetter, 'coverLetter', 100000, true), answers, evidenceIds: checkRefs(draft.evidenceIds, active, 'evidenceIds'), researchIds: checkRefs(draft.researchIds, researched, 'researchIds'), missingFacts: refs(draft.missingFacts, 'missingFacts'), warnings: refs(draft.warnings, 'warnings'), id: randomUUID(), brainRevision: p.brainRevision, inputRevision: a.inputRevision, researchId: a.research?.id ?? null, createdAt: now(), stale: false, humanEdited: false };
      if (draft.quality !== undefined) a.draft.quality = assessment;
      a.status = 'draft'; a.updatedAt = now();
    });
  }
  recordApplication(id, appId, input) {
    only(input, ['status', 'eventId', 'expectedDraftId']);
    if (!['reviewed', 'submitted'].includes(input.status)) fail('Invalid application event'); string(input.eventId, 'eventId', 200); string(input.expectedDraftId, 'expectedDraftId', 200);
    return this.#change(id, p => {
      const a = this.#app(p, appId); const duplicate = a.history.find(event => event.eventId === input.eventId);
      if (duplicate) { if (duplicate.status !== input.status || duplicate.draftId !== input.expectedDraftId) fail('Event ID already used', 409); return false; }
      const d = a.draft;
      if (!d || d.id !== input.expectedDraftId || d.stale || d.brainRevision !== p.brainRevision || d.inputRevision !== a.inputRevision || d.researchId !== (a.research?.id ?? null)) fail('Current nonstale draft required', 409);
      if (d.mode === 'cloud' && !currentCloudResearch(a)) fail('Refresh company research before reviewing or recording submission', 422);
      for (const question of a.questions) {
        const answer = d.answers.find(item => item.questionId === question.id);
        if (!answer || !answer.text.trim()) fail('Every required application question needs a nonempty answer', 422);
        if ((question.maxWords && answer.text.trim().split(/\s+/u).filter(Boolean).length > question.maxWords) || (question.maxChars && [...answer.text].length > question.maxChars)) fail('Answer exceeds declared limit', 422);
      }
      const reviewed = a.history.findLast(event => event.status === 'reviewed' && event.draftId === d.id);
      if (input.status === 'submitted' && (!reviewed || a.status !== 'reviewed')) fail('Review this exact draft before recording submitted', 409);
      a.history.push({ eventId: input.eventId, status: input.status, timestamp: now(), draftId: d.id, version: d.id, coverLetter: d.coverLetter, answers: copy(d.answers), evidenceIds: unique([...d.evidenceIds, ...d.answers.flatMap(answer => answer.evidenceIds)]), ...(d.quality === undefined ? {} : { quality: copy(d.quality) }) });
      a.status = input.status; a.updatedAt = now();
      if (input.status === 'reviewed' && !reviewed) {
        for (const answer of d.answers) if (answer.text.trim()) p.memories.push(memory({ category: 'application-answer', label: `${a.company}: ${a.questions.find(q => q.id === answer.questionId).text}`, content: answer.text, source: { kind: 'application', id: a.id, label: `Reviewed draft ${d.id}` } }));
      }
    });
  }
}
