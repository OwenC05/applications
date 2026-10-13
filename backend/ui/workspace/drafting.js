import {RequestGate} from './client.js';

const copy = value => structuredClone(value);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const identifier = value => typeof value === 'string' && value.length > 0;
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const revisions = ['metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research'];
const same = (a, b) => {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => same(v, b[i]));
  if (!object(a) || !object(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key => Object.hasOwn(b, key) && same(a[key], b[key]));
};
const require = condition => { if (!condition) throw new Error('The authoritative saved draft is inconsistent; editing is unavailable.'); };
function validateVector(value) {
  require(object(value) && value.schema_version === 1 && Object.keys(value).length === 8
    && revisions.every(key => Number.isSafeInteger(value[key]) && value[key] >= 0));
}
const exact = (value, names) => object(value) && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const natural = value => Number.isSafeInteger(value) && value >= 0;
const utc = value => typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);
const strings = value => Array.isArray(value) && value.every(item => typeof item === 'string');
const questionFields = ['schema_version', 'id', 'text', 'type', 'required', 'max_words', 'max_chars', 'options', 'constraint_origin'];
function reviewQuestion(q) {
  require(exact(q, questionFields) && q.schema_version === 1 && identifier(q.id) && Array.from(q.id).length <= 200
    && identifier(q.text) && Array.from(q.text).length <= 10000 && ['writing', 'text', 'select', 'radio', 'checkbox', 'file', 'sensitive'].includes(q.type)
    && typeof q.required === 'boolean' && [q.max_words, q.max_chars].every(n => n === null || Number.isSafeInteger(n) && n > 0)
    && strings(q.options) && ['user', 'vacancy', 'form'].includes(q.constraint_origin));
}
function reviewReference(r, d) {
  require(exact(r, ['schema_version', 'profile_id', 'kind', 'span', 'generation_id', 'application_id', 'research_run_id'])
    && r.schema_version === 1 && r.profile_id === d.profile_id && ['confirmed_fact', 'employer'].includes(r.kind) && uuid(r.generation_id)
    && (r.application_id === null || r.application_id === d.application_id)
    && (r.research_run_id === null || r.research_run_id === d.research_run_id));
  require(r.kind === 'employer' ? r.application_id === d.application_id && r.research_run_id === d.research_run_id : r.research_run_id === null);
  const s = r.span;
  require(exact(s, ['schema_version', 'record_id', 'unit_id', 'start', 'end', 'text_sha256', 'excerpt']) && s.schema_version === 1
    && uuid(s.record_id) && (s.unit_id === null || uuid(s.unit_id)) && natural(s.start) && Number.isSafeInteger(s.end) && s.end > s.start
    && identifier(s.excerpt) && Array.from(s.excerpt).length === s.end - s.start && digest(s.text_sha256));
}
// Structural correlation only: the backend reconstructs canonical hashes and the
// original evidence graph. Browser equality is neither entailment nor approval.
function validateReview(value) {
  const d = value.draft, p = value.publication, r = p.request, disclosure = p.disclosure, snapshot = value.review_snapshot;
  require(exact(value, ['schema_version', 'draft', 'publication', 'publication_sha256', 'text_sha256', 'ledger_sha256', 'current', 'reason_codes', 'canonical_questions', 'writing_targets', 'manual_requirements', 'semantic_assessment', 'writing_review_ready', 'reviewed_current', 'review_snapshot', 'review_eligible', 'browser_eligible', 'capabilities']));
  require(exact(d, ['schema_version', 'profile_id', 'application_id', 'id', 'revisions', 'research_run_id', 'cover_letter', 'answers', 'ledger', 'assessment_state', 'inventory_complete', 'created_at'])
    && [d.profile_id, d.application_id, d.id, d.research_run_id].every(uuid) && utc(d.created_at)
    && d.assessment_state === 'assessed' && d.inventory_complete === true && Array.from(d.cover_letter).length <= 100000);
  require(exact(p, ['schema_version', 'profile_id', 'application_id', 'origin', 'id', 'draft_id', 'parent_draft_id', 'parent_text_sha256', 'parent_ledger_sha256', 'parent_publication_id', 'parent_publication_sha256', 'batch_id', 'batch_sha256', 'pre_revisions', 'edit_depth', 'created_at', 'request', 'request_sha256', 'disclosure', 'review_snapshot_id', 'review_snapshot_sha256', 'text_sha256', 'ledger_sha256', 'reason_codes', 'style_notes'])
    && p.schema_version === 1 && p.origin === 'review' && [p.id, p.parent_draft_id, p.parent_publication_id, p.batch_id, p.review_snapshot_id].every(uuid)
    && p.parent_draft_id !== d.id && p.parent_publication_id !== p.id && utc(p.created_at) && p.created_at === d.created_at
    && natural(p.edit_depth) && p.edit_depth <= 64 && Array.isArray(p.reason_codes) && p.reason_codes.length === 0 && strings(p.style_notes)
    && [p.parent_text_sha256, p.parent_ledger_sha256, p.parent_publication_sha256, p.batch_sha256, p.request_sha256, p.review_snapshot_sha256].every(digest));
  validateVector(p.pre_revisions);
  require(p.pre_revisions.application_output < Number.MAX_SAFE_INTEGER
    && same(d.revisions, {...p.pre_revisions, application_output: p.pre_revisions.application_output + 1})
    && p.parent_text_sha256 === value.text_sha256 && p.parent_ledger_sha256 === value.ledger_sha256);
  require(exact(r, ['schema_version', 'expected_revisions', 'parent_text_sha256', 'parent_ledger_sha256', 'parent_publication_sha256', 'idempotency_key', 'disclosure_sha256', 'acknowledged'])
    && r.schema_version === 1 && same(r.expected_revisions, p.pre_revisions) && r.parent_text_sha256 === p.parent_text_sha256
    && r.parent_ledger_sha256 === p.parent_ledger_sha256 && r.parent_publication_sha256 === p.parent_publication_sha256
    && identifier(r.idempotency_key) && Array.from(r.idempotency_key).length <= 200 && digest(r.disclosure_sha256) && r.acknowledged === true);
  require(exact(disclosure, ['schema_version', 'profile_id', 'application_id', 'operation', 'parent_draft_id', 'parent_publication_id', 'parent_publication_sha256', 'batch_id', 'batch_sha256', 'research_run_id', 'pre_revisions', 'post_revisions', 'text_sha256', 'ledger_sha256', 'cover_letter', 'answers', 'ledger', 'canonical_questions', 'cover_letter_target', 'manual_requirements', 'style_notes', 'warnings'])
    && disclosure.schema_version === 1 && disclosure.operation === 'human_writing_review' && disclosure.profile_id === d.profile_id && disclosure.application_id === d.application_id
    && disclosure.parent_draft_id === p.parent_draft_id && disclosure.parent_publication_id === p.parent_publication_id && disclosure.parent_publication_sha256 === p.parent_publication_sha256
    && disclosure.batch_id === p.batch_id && disclosure.batch_sha256 === p.batch_sha256 && disclosure.research_run_id === d.research_run_id
    && same(disclosure.pre_revisions, p.pre_revisions) && same(disclosure.post_revisions, d.revisions)
    && disclosure.text_sha256 === value.text_sha256 && disclosure.ledger_sha256 === value.ledger_sha256
    && disclosure.cover_letter === d.cover_letter && same(disclosure.answers, d.answers) && same(disclosure.ledger, d.ledger)
    && same(disclosure.canonical_questions, value.canonical_questions) && same(disclosure.manual_requirements, value.manual_requirements)
    && same(disclosure.style_notes, p.style_notes)
    && same(disclosure.warnings, ['WRITING_ONLY', 'AUTOMATED_SUPPORT_FALLIBLE', 'NOT_FACT_CONFIRMATION', 'MANUAL_AND_ATTACHMENTS_NOT_REVIEWED', 'NO_BROWSER_AUTHORIZATION']));
  require(exact(snapshot, ['schema_version', 'profile_id', 'application_id', 'id', 'draft_id', 'text_sha256', 'ledger_sha256', 'revisions', 'research_run_id', 'disclosure_sha256', 'reviewed_at'])
    && snapshot.schema_version === 1 && snapshot.profile_id === d.profile_id && snapshot.application_id === d.application_id && snapshot.id === p.review_snapshot_id
    && snapshot.draft_id === d.id && snapshot.text_sha256 === value.text_sha256 && snapshot.ledger_sha256 === value.ledger_sha256
    && same(snapshot.revisions, d.revisions) && snapshot.research_run_id === d.research_run_id && snapshot.disclosure_sha256 === r.disclosure_sha256
    && snapshot.reviewed_at === p.created_at && utc(snapshot.reviewed_at));
  require(strings(value.reason_codes) && typeof value.writing_review_ready === 'boolean' && value.writing_review_ready === value.current
    && typeof value.reviewed_current === 'boolean' && value.reviewed_current === value.current && value.review_eligible === false && value.browser_eligible === false
    && value.semantic_assessment === 'fallible_not_release_qualified'
    && exact(value.capabilities, ['generation', 'edit', 'reassess', 'review']) && Object.values(value.capabilities).every(flag => flag === true));
  require(Array.isArray(value.canonical_questions) && value.canonical_questions.length <= 12 && Array.isArray(value.manual_requirements));
  const questionIds = new Set();
  for (const q of value.canonical_questions) { reviewQuestion(q); require(q.id !== 'cover_letter' && !questionIds.has(q.id)); questionIds.add(q.id); }
  if (disclosure.cover_letter_target !== null) {
    reviewQuestion(disclosure.cover_letter_target);
    require(disclosure.cover_letter_target.id === 'cover_letter' && disclosure.cover_letter_target.type === 'writing' && disclosure.cover_letter_target.max_words !== null);
  }
  const allQuestions = [...value.canonical_questions, ...(disclosure.cover_letter_target === null ? [] : [disclosure.cover_letter_target])];
  const manualQuestionIds = value.manual_requirements.map(m => m?.question_id);
  const expectedQuestions = allQuestions.filter(q => q.type === 'writing' && !manualQuestionIds.includes(q.id));
  require(same(value.writing_targets.map(t => t.question), expectedQuestions));
  for (const t of value.writing_targets) require(exact(t, ['question', 'packet_id']) && uuid(t.packet_id));
  for (const a of d.answers) require(exact(a, ['schema_version', 'question_id', 'text', 'packet_id']) && Array.from(a.text).length <= 100000);
  const manualIds = new Set();
  for (const m of value.manual_requirements) {
    require(exact(m, ['schema_version', 'question_id', 'reason']) && m.schema_version === 1 && identifier(m.question_id) && Array.from(m.question_id).length <= 200
      && identifier(m.reason) && !manualIds.has(m.question_id) && allQuestions.some(q => q.id === m.question_id && !q.required));
    manualIds.add(m.question_id);
  }
  require(allQuestions.every(q => q.type === 'writing' || manualIds.has(q.id)));
  const texts = new Map(d.answers.map(a => [a.question_id, a.text])); texts.set('cover_letter', d.cover_letter);
  for (const q of expectedQuestions) {
    const content = texts.get(q.id);
    // Match Python str.split whitespace, not JavaScript's different FEFF set.
    const words = content.split(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u).filter(Boolean).length;
    require((!q.required || words > 0) && (q.max_words === null || words <= q.max_words)
      && (q.max_chars === null || Array.from(content).length <= q.max_chars));
  }
  for (const span of d.ledger) {
    require(exact(span, ['schema_version', 'target', 'start', 'end', 'excerpt', 'kind', 'assessment', 'citations', 'integrity']) && span.schema_version === 1
      && texts.has(span.target) && natural(span.start) && Number.isSafeInteger(span.end) && span.end > span.start && identifier(span.excerpt)
      && Array.from(texts.get(span.target)).slice(span.start, span.end).join('') === span.excerpt && Array.from(span.excerpt).length === span.end - span.start
      && ['personal', 'employer', 'inference'].includes(span.kind) && span.assessment === 'supported' && span.integrity === 'valid' && Array.isArray(span.citations) && span.citations.length > 0);
    for (const ref of span.citations) { reviewReference(ref, d); require(span.kind === 'inference' || ref.kind === (span.kind === 'personal' ? 'confirmed_fact' : 'employer')); }
  }
}

function validateInspection(value) {
  require(object(value) && value.schema_version === 1 && typeof value.current === 'boolean');
  const d = value.draft, p = value.publication;
  require(object(d) && d.schema_version === 1 && object(p) && identifier(d.id) && identifier(p.id)
    && identifier(d.profile_id) && identifier(d.application_id) && identifier(d.research_run_id)
    && p.profile_id === d.profile_id && p.application_id === d.application_id && p.draft_id === d.id);
  require(!Object.hasOwn(p, 'origin') || ['edit', 'assessment', 'review'].includes(p.origin));
  validateVector(d.revisions);
  require(digest(value.text_sha256) && digest(value.ledger_sha256) && digest(value.publication_sha256)
    && p.text_sha256 === value.text_sha256 && p.ledger_sha256 === value.ledger_sha256);
  require(Array.isArray(d.answers) && typeof d.cover_letter === 'string' && Array.isArray(d.ledger)
    && ['unassessed', 'assessed', 'stale'].includes(d.assessment_state) && typeof d.inventory_complete === 'boolean'
    && Array.isArray(value.writing_targets) && value.writing_targets.length > 0);
  const ids = new Set(); let answerIndex = 0, cover = false;
  const targets = value.writing_targets.map((target, index) => {
    require(object(target) && identifier(target.packet_id) && object(target.question));
    const q = target.question;
    require(q.schema_version === 1 && identifier(q.id) && identifier(q.text) && q.type === 'writing'
      && typeof q.required === 'boolean' && ['user', 'vacancy', 'form'].includes(q.constraint_origin)
      && Array.isArray(q.options) && q.options.every(option => typeof option === 'string')
      && [q.max_words, q.max_chars].every(limit => limit === null || Number.isSafeInteger(limit) && limit > 0)
      && !ids.has(q.id));
    ids.add(q.id);
    if (q.id === 'cover_letter') {
      require(index === value.writing_targets.length - 1); cover = true;
      return {target: q.id, text: d.cover_letter};
    }
    const answer = d.answers[answerIndex++];
    require(object(answer) && answer.schema_version === 1 && answer.question_id === q.id
      && answer.packet_id === target.packet_id && typeof answer.text === 'string');
    return {target: q.id, text: answer.text};
  });
  require(answerIndex === d.answers.length && (cover || d.cover_letter === ''));
  if (p.origin === 'review') validateReview(value);
  else require((!Object.hasOwn(value, 'review_snapshot') || value.review_snapshot === null)
    && (!Object.hasOwn(value, 'reviewed_current') || value.reviewed_current === false));
  return targets;
}

// Saved bytes remain authoritative until an actual input event supplies new text.
export class DraftBuffer {
  #inspection; #saved; #texts;
  constructor(inspection) {
    this.#inspection = copy(inspection);
    this.#saved = validateInspection(this.#inspection);
    this.#texts = copy(this.#saved);
  }
  input(target, text) {
    const item = this.#texts.find(value => value.target === target);
    require(item && typeof text === 'string'); item.text = text;
  }
  targets() { return copy(this.#texts); }
  matchesSaved() { return same(this.#texts, this.#saved); }
  editRequest(idempotencyKey) {
    require(this.#inspection.current === true && typeof idempotencyKey === 'string' && idempotencyKey.length > 0 && idempotencyKey.length <= 200);
    const parent = this.#inspection;
    return {schema_version: 1, expected_revisions: copy(parent.draft.revisions), parent_text_sha256: parent.text_sha256,
      parent_ledger_sha256: parent.ledger_sha256, parent_publication_sha256: parent.publication_sha256,
      idempotency_key: idempotencyKey, draft: {schema_version: 1, targets: this.#texts.map(value => ({schema_version: 1, ...value}))}};
  }
}

function correlate(inspection, operation, owner, application) {
  const parent = operation.parentCapture, body = operation.body;
  validateInspection(inspection);
  const d = inspection.draft, p = inspection.publication;
  require(d.profile_id === owner && d.application_id === application && d.id !== parent.draft.id
    && p.origin === 'edit' && p.parent_draft_id === operation.parentDraftId
    && p.parent_text_sha256 === parent.text_sha256 && p.parent_ledger_sha256 === parent.ledger_sha256
    && p.parent_publication_id === parent.publication.id && p.parent_publication_sha256 === parent.publication_sha256
    && same(p.request, body) && same(p.pre_revisions, body.expected_revisions)
    && same(inspection.writing_targets, parent.writing_targets)
    && d.research_run_id === parent.draft.research_run_id
    && same(d.revisions, {...body.expected_revisions, application_output: body.expected_revisions.application_output + 1})
    && d.assessment_state === 'unassessed' && d.inventory_complete === false && d.ledger.length === 0);
  require(same(validateInspection(inspection), body.draft.targets.map(({target, text}) => ({target, text}))));
  return {owner, application, draftId: d.id, preRevisions: copy(body.expected_revisions), postRevisions: copy(d.revisions), current: inspection.current};
}

// Independent read slots, plus a baseline generation, fence cross-slot mutation races.
export class DraftingController {
  #api; #exportText; #update; #owner; #application; #base; #gate = new RequestGate();
  #retired = false; #packet = ''; #draft = ''; #baseline = 0;
  constructor({api, exportText, owner, application, update}) {
    this.#api = api; this.#exportText = exportText; this.#update = update;
    this.#owner = owner; this.#application = application;
    this.#base = `/api/workspace/profiles/${encodeURIComponent(owner)}/applications/${encodeURIComponent(application)}`;
  }
  #invalidate(...slots) { slots.forEach(slot => this.#gate.capture(slot)); }
  retire() { this.#retired = true; this.#gate.select('retired'); }
  adoptDraft(id) {
    if (this.#retired) return;
    this.#draft = id; this.#baseline++; this.#invalidate('draft-detail', 'edit', 'export');
  }
  #selectDraft(id) { if (id !== this.#draft) this.adoptDraft(id); }
  #selectPacket(id) {
    if (id !== this.#packet) { this.#packet = id; this.#invalidate('packet-detail', 'packet-question'); }
  }
  async #request(slot, path, io = this.#api, validate = () => {}) {
    if (this.#retired) return;
    const ticket = this.#gate.capture(slot), baseline = this.#baseline, selected = this.#draft;
    const current = () => !this.#retired && this.#gate.current(ticket)
      && (!['draft-detail', 'export'].includes(slot) || baseline === this.#baseline && selected === this.#draft);
    try {
      const data = await io(path);
      if (current()) { validate(data); this.#update(slot, data, null); }
    }
    catch (error) { if (current()) this.#update(slot, null, error); }
  }
  packets() { return this.#request('packet-list', `${this.#base}/packets`); }
  packet(id) {
    if (this.#retired) return;
    this.#selectPacket(id);
    if (id === '') { this.#invalidate('packet-detail', 'packet-question'); return; }
    return this.#request('packet-detail', `${this.#base}/packets/${encodeURIComponent(id)}`);
  }
  packetQuestion(batch, id) {
    if (this.#retired) return;
    this.#selectPacket(batch);
    if (batch === '' || id === '') { this.#invalidate('packet-question'); return; }
    return this.#request('packet-question', `${this.#base}/packets/${encodeURIComponent(batch)}/questions/${encodeURIComponent(id)}`);
  }
  drafts() { return this.#request('draft-list', `${this.#base}/drafts`); }
  draft(id) {
    if (this.#retired) return;
    this.#selectDraft(id);
    if (id === '') { this.#invalidate('draft-detail', 'edit', 'export'); return; }
    return this.#request('draft-detail', `${this.#base}/drafts/${encodeURIComponent(id)}`, this.#api, data => {
      validateInspection(data);
      require(data.draft.id === id && data.draft.profile_id === this.#owner && data.draft.application_id === this.#application);
    });
  }
  exportDraft(id) {
    if (!this.#retired) this.#selectDraft(id);
    return this.#request('export', `${this.#base}/drafts/${encodeURIComponent(id)}/export`, this.#exportText);
  }
  async saveEdit(parentCapture, body) {
    if (this.#retired) return;
    const operation = {parentDraftId: parentCapture?.draft?.id, parentCapture: copy(parentCapture), body: copy(body)};
    const parent = operation.parentCapture;
    try {
      const buffer = new DraftBuffer(parent);
      for (const value of operation.body.draft.targets) buffer.input(value.target, value.text);
      require(same(buffer.editRequest(operation.body.idempotency_key), operation.body)
        && parent.draft.profile_id === this.#owner && parent.draft.application_id === this.#application);
    } catch (error) { this.#update('edit', {operation: copy(operation)}, error); return; }
    if (!this.#draft) this.adoptDraft(operation.parentDraftId);
    if (this.#draft !== operation.parentDraftId) return;
    this.#invalidate('draft-detail', 'export');
    const ticket = this.#gate.capture('edit'), baseline = this.#baseline;
    const current = () => !this.#retired && this.#gate.current(ticket) && baseline === this.#baseline && this.#draft === operation.parentDraftId;
    try {
      const inspection = await this.#api(`${this.#base}/drafts/${encodeURIComponent(operation.parentDraftId)}/edits`, {method: 'POST', body: copy(operation.body)});
      if (!current()) return;
      const published = correlate(inspection, operation, this.#owner, this.#application);
      this.#invalidate('draft-detail', 'export');
      this.#update('edit', {inspection: copy(inspection), operation: copy(operation), published}, null);
    } catch (error) {
      if (!current()) return;
      // An API inspection can fail after commit. Never infer rollback or retry here.
      const reported = error instanceof Error && Object.isExtensible(error) ? error
        : Object.assign(new Error(error?.message || 'The saved edit outcome is unknown.'), {status: error?.status, code: error?.code});
      reported.uncertain = true;
      this.#invalidate('draft-detail', 'export');
      this.#update('edit', {operation: copy(operation)}, reported);
    }
  }
}
