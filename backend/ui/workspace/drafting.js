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
function validateInspection(value) {
  require(object(value) && value.schema_version === 1 && typeof value.current === 'boolean');
  const d = value.draft, p = value.publication;
  require(object(d) && d.schema_version === 1 && object(p) && identifier(d.id) && identifier(p.id)
    && identifier(d.profile_id) && identifier(d.application_id) && identifier(d.research_run_id)
    && p.profile_id === d.profile_id && p.application_id === d.application_id && p.draft_id === d.id);
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
