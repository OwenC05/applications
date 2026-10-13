import {DraftBuffer} from './drafting.js';

const copy = value => structuredClone(value);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, names) => object(value) && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
const natural = value => Number.isSafeInteger(value) && value >= 0;
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const text = value => typeof value === 'string' && value.length > 0;
const dimensions = ['metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research'];
const states = ['queued', 'running', 'completed', 'cancelled', 'failed', 'indeterminate'];
const stages = ['queued', 'started', 'index_published', 'cancelled', 'research_published', 'packets_published', 'draft_published', 'assessment_published', 'drafting_failed', 'scope_forgotten', 'missing_scope', 'stale_inputs', 'lease_limit', 'provider_unknown', 'working'];
const reasons = ['integrity_blocked', 'provider_outcome_unresolved', 'active_operation', 'admission_exhausted'];
const failure = () => new Error('Paid operation could not be verified. Retain the original operation and inspect explicitly.');
const require = condition => { if (!condition) throw failure(); };
const same = (a, b) => {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => same(v, b[i]));
  if (!object(a) || !object(b)) return false;
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(key => Object.hasOwn(b, key) && same(a[key], b[key]));
};
const vector = value => require(exact(value, ['schema_version', ...dimensions]) && value.schema_version === 1 && dimensions.every(key => natural(value[key])));
const utc = value => value === null || typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);
const notify = (callback, ...args) => { try { callback?.(...args); } catch { /* View hooks cannot mutate lifecycle. */ } };

export function validateAdmission(value, owner, application) {
  const a = copy(value);
  require(uuid(owner) && uuid(application) && exact(a, ['schema_version', 'profile_id', 'application_id', 'admission_revision', 'eligible', 'reason_codes', 'head_job'])
    && a.schema_version === 1 && a.profile_id === owner && a.application_id === application && natural(a.admission_revision)
    && typeof a.eligible === 'boolean' && Array.isArray(a.reason_codes)
    && same(a.reason_codes, reasons.filter(reason => a.reason_codes.includes(reason))) && a.eligible === (a.reason_codes.length === 0));
  require(a.reason_codes.includes('admission_exhausted') === (a.admission_revision === Number.MAX_SAFE_INTEGER));
  if (a.head_job !== null) {
    const h = a.head_job;
    require(exact(h, ['schema_version', 'id', 'kind', 'state', 'captured_revision', 'consumed_revision']) && h.schema_version === 1
      && uuid(h.id) && ['draft', 'assess'].includes(h.kind) && states.includes(h.state) && natural(h.captured_revision)
      && natural(h.consumed_revision) && h.consumed_revision === h.captured_revision + 1 && h.consumed_revision <= a.admission_revision);
  }
  return a;
}
function validateJob(value, operation) {
  require(exact(value, ['schema_version', 'job']) && value.schema_version === 1);
  const j = value.job;
  require(exact(j, ['id', 'profile_id', 'application_id', 'kind', 'state', 'stage', 'revisions', 'fence', 'attempt_count', 'cancellation_requested', 'heartbeat_at', 'lease_expires_at', 'cleanup_pending'])
    && uuid(j.id) && j.profile_id === operation.owner && j.application_id === operation.application
    && j.kind === (operation.kind === 'generation' ? 'draft' : 'assess') && states.includes(j.state) && stages.includes(j.stage)
    && natural(j.fence) && natural(j.attempt_count) && j.attempt_count <= 5 && typeof j.cancellation_requested === 'boolean'
    && typeof j.cleanup_pending === 'boolean' && utc(j.heartbeat_at) && utc(j.lease_expires_at));
  vector(j.revisions);
  require(same(j.revisions, operation.capture.request.expected_revisions) && (!operation.job || operation.job.id === j.id)
    && (j.state !== 'running' || j.fence > 0 && j.lease_expires_at !== null));
  if (operation.job) require(j.fence >= operation.job.fence && j.attempt_count >= operation.job.attempt_count
    && (!['completed', 'cancelled', 'failed', 'indeterminate'].includes(operation.job.state) || j.state === operation.job.state));
  return copy(j);
}
function validateCapture(value, owner, application) {
  const c = copy(value);
  require(exact(c, ['kind', 'owner', 'application', 'admission', 'request', 'disclosure', 'disclosure_sha256', 'state', 'packetInspection', 'draftInspection'])
    && ['generation', 'assessment'].includes(c.kind) && c.owner === owner && c.application === application && digest(c.disclosure_sha256));
  c.admission = validateAdmission(c.admission, owner, application);
  require(c.admission.eligible && c.admission.admission_revision < Number.MAX_SAFE_INTEGER);
  const r = c.request, d = c.disclosure, s = c.state;
  const names = c.kind === 'generation' ? ['batch_id', 'batch_sha256'] : ['parent_text_sha256', 'parent_ledger_sha256', 'parent_publication_sha256'];
  require(exact(r, ['schema_version', 'expected_revisions', 'model', 'max_output_tokens', ...names]) && r.schema_version === 1
    && typeof r.model === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(r.model)
    && Number.isSafeInteger(r.max_output_tokens) && r.max_output_tokens >= 1 && r.max_output_tokens <= 8000);
  vector(r.expected_revisions);
  require(r.expected_revisions.application_output < Number.MAX_SAFE_INTEGER
    && exact(s, ['schema_version', 'profile_id', 'application_id', 'current_revisions', 'consent']) && s.schema_version === 1
    && s.profile_id === owner && s.application_id === application && same(s.current_revisions, r.expected_revisions));
  const consent = s.consent;
  require(exact(consent, ['provider', 'purposes', 'granted', 'revision']) && consent.provider === 'openai' && consent.granted === true
    && consent.revision === r.expected_revisions.consent && Array.isArray(consent.purposes) && new Set(consent.purposes).size === consent.purposes.length
    && consent.purposes.every(p => ['research', 'drafting', 'assessment'].includes(p)) && consent.purposes.includes(c.kind === 'generation' ? 'drafting' : 'assessment'));
  const calls = c.kind === 'generation' ? 5 : 1;
  require(object(d) && d.schema_version === 1 && d.model === r.model && d.max_output_tokens === r.max_output_tokens
    && same(d.expected_revisions, r.expected_revisions) && d.consent_revision === r.expected_revisions.consent
    && d.maximum_calls === calls && d.conservative_reservation_units === calls * (131072 + r.max_output_tokens)
    && text(d.prompt_version) && d.output_schema_version === 1 && digest(d.selected_context_sha256)
    && d.monetary_cost === null && d.pricing_status === 'unknown' && d.semantic_assessment === 'fallible_not_release_qualified');
  const limits = d.configured_limits;
  require(exact(limits, ['daily_tokens', 'daily_calls', 'job_tokens', 'job_calls'])
    && Object.values(limits).every(v => Number.isSafeInteger(v) && v > 0 && v <= 100_000_000)
    && limits.job_tokens <= limits.daily_tokens && limits.job_calls <= limits.daily_calls
    && limits.job_calls >= calls && limits.job_tokens >= d.conservative_reservation_units);
  require(object(d.selected_context) && text(d.selected_context.company) && text(d.selected_context.role)
    && ['tech', 'finance'].includes(d.selected_context.sector) && Array.isArray(d.selected_context.targets) && d.selected_context.targets.length > 0);
  if (c.kind === 'generation') {
    const p = c.packetInspection, b = p?.batch, publication = p?.publication;
    require(c.draftInspection === null && p?.schema_version === 1 && p.current === true && object(b) && b.schema_version === 1
      && uuid(b.id) && b.profile_id === owner && b.application_id === application && uuid(b.research_run_id)
      && b.id === r.batch_id && digest(r.batch_sha256) && p.batch_sha256 === r.batch_sha256
      && object(publication) && publication.schema_version === 1 && uuid(publication.id) && publication.profile_id === owner
      && publication.application_id === application && publication.batch_id === b.id && publication.batch_sha256 === r.batch_sha256 && digest(p.publication_sha256)
      && Array.isArray(b.canonical_questions) && Array.isArray(b.packets));
    const questions = [...b.canonical_questions, ...(b.cover_letter_target === null ? [] : [b.cover_letter_target])];
    const targets = questions.flatMap(question => {
      const matches = b.packets.filter(packet => packet.question?.id === question.id);
      require(matches.length <= 1);
      if (!matches.length) return [];
      require(same(matches[0].question, question) && uuid(matches[0].id));
      return [{question, packet_id: matches[0].id}];
    });
    require(same(d.selected_context.targets.map(t => ({question: t.question, packet_id: t.packet_id})), targets));
  } else {
    const p = c.draftInspection;
    new DraftBuffer(p);
    require(c.packetInspection === null && p.current === true && p.draft.profile_id === owner && p.draft.application_id === application
      && uuid(p.draft.id) && uuid(p.draft.research_run_id) && uuid(p.publication.id) && p.publication.origin === 'edit'
      && p.writing_targets.every(target => uuid(target.packet_id))
      && p.draft.assessment_state === 'unassessed' && p.draft.inventory_complete === false && p.draft.ledger.length === 0
      && same(p.draft.revisions, r.expected_revisions) && uuid(p.publication.batch_id) && digest(p.publication.batch_sha256)
      && same(d.selected_context.targets.map(t => ({question: t.question, packet_id: t.packet_id})), p.writing_targets)
      && same(d.input_draft, {schema_version: 1, targets: new DraftBuffer(p).targets().map(t => ({schema_version: 1, ...t}))}));
    for (const name of names) require(digest(r[name]) && r[name] === p[name.replace('parent_', '')] && d[name] === r[name]);
  }
  const batch = c.kind === 'generation' ? {id: r.batch_id, hash: r.batch_sha256} : {id: c.draftInspection.publication.batch_id, hash: c.draftInspection.publication.batch_sha256};
  require(d.batch_id === batch.id && d.batch_sha256 === batch.hash);
  return c;
}

// The WeakMap is module-private: consumers can inspect a copy but cannot settle or clear a capsule.
const registries = new WeakMap();
let registrySequence = 0;
const scopeKey = (owner, application) => JSON.stringify([owner, application]);
const stateOf = registry => { const state = registries.get(registry); require(state); return state; };
const find = (registry, token) => {
  if (!token) return null;
  const state = stateOf(registry), operation = state.operations.get(scopeKey(token.owner, token.application));
  return operation && same(operation.token, token) && (state.epochs.get(token.owner) || 0) === token.epoch ? operation : null;
};
const changed = (registry, operation) => notify(stateOf(registry).onChange, {owner: operation.owner, application: operation.application});
function begin(registry, operation) {
  const state = stateOf(registry), key = scopeKey(operation.owner, operation.application);
  require(!state.operations.has(key));
  const token = {registry: state.identity, owner: operation.owner, application: operation.application, epoch: state.epochs.get(operation.owner) || 0, sequence: ++state.sequence};
  state.resolutions.delete(key);
  state.operations.set(key, {...copy(operation), token, state: 'pending', job: null, result: null, admission: null, handedOff: false, action: null, actionSequence: 0, published: false});
  return copy(token);
}
function acquire(registry, token) {
  const op = find(registry, token); require(op && op.action === null);
  const ticket = ++op.actionSequence; op.action = ticket; changed(registry, op); return ticket;
}
const active = (registry, token, ticket) => { const op = find(registry, token); return op && op.action === ticket ? op : null; };
function summary(op) {
  return op ? copy({kind: op.kind, state: op.state, job: op.job, actionPending: op.action !== null, handedOff: op.handedOff,
    resultDraftId: op.result?.draft.id || null, admission: op.admission}) : null;
}
export class PaidLaunchRegistry {
  constructor({onChange} = {}) { registries.set(this, {identity: ++registrySequence, operations: new Map(), epochs: new Map(), resolutions: new Map(), controllers: new Map(), controllerSequence: 0, sequence: 0, onChange}); }
  get(owner, application) { const op = stateOf(this).operations.get(scopeKey(owner, application)); return op ? copy(op) : null; }
  has(owner, application) { return stateOf(this).operations.has(scopeKey(owner, application)); }
  get size() { return stateOf(this).operations.size; }
  handoff(token) {
    const op = find(this, token);
    if (!op || op.state !== 'unknown' || op.action !== null) return false;
    op.handedOff = true; changed(this, op); return true;
  }
  forgetOwner(owner) {
    const state = stateOf(this); state.epochs.set(owner, (state.epochs.get(owner) || 0) + 1);
    for (const [key, receipt] of state.resolutions) if (receipt.token.owner === owner) state.resolutions.delete(key);
    for (const [key, op] of state.operations) if (op.owner === owner) { state.operations.delete(key); changed(this, op); }
  }
}

function correlateResult(value, operation, job) {
  const v = copy(value), targets = new DraftBuffer(v).targets(), d = v.draft, p = v.publication, c = operation.capture;
  require(p.origin !== 'review'); // A local acknowledgement is never a paid job result.
  const pre = c.request.expected_revisions;
  const publicationFields = ['schema_version', 'profile_id', 'application_id', 'id', 'draft_id', 'intent_id', 'job_id', 'fence', 'batch_id', 'batch_sha256', 'text_sha256', 'ledger_sha256', 'request_sha256', 'disclosure_sha256', 'prompt_version', 'stage_names', 'reason_codes', 'style_notes'];
  if (operation.kind === 'assessment') publicationFields.push('origin', 'parent_draft_id', 'parent_text_sha256', 'parent_ledger_sha256', 'parent_publication_id', 'parent_publication_sha256', 'pre_revisions', 'edit_depth', 'created_at');
  require(exact(p, publicationFields));
  const context = c.disclosure.selected_context.targets.map(t => ({question: t.question, packet_id: t.packet_id}));
  const batchId = c.disclosure.batch_id, batchHash = c.disclosure.batch_sha256;
  require(v.schema_version === 1 && typeof v.current === 'boolean' && d.profile_id === operation.owner && d.application_id === operation.application
    && p.schema_version === 1 && uuid(d.id) && uuid(p.id) && uuid(p.intent_id) && p.job_id === job.id && p.fence === job.fence && p.fence > 0
    && p.batch_id === batchId && p.batch_sha256 === batchHash && p.disclosure_sha256 === c.disclosure_sha256
    && digest(p.request_sha256) && p.prompt_version === c.disclosure.prompt_version
    && same(d.revisions, {...pre, application_output: pre.application_output + 1}) && same(v.writing_targets, context)
    && d.assessment_state === 'assessed' && typeof d.inventory_complete === 'boolean'
    && Array.isArray(p.reason_codes) && p.reason_codes.every(x => typeof x === 'string') && Array.isArray(p.style_notes) && p.style_notes.every(x => typeof x === 'string'));
  if (operation.kind === 'generation') {
    require(!Object.hasOwn(p, 'origin') && !Object.hasOwn(p, 'pre_revisions') && job.kind === 'draft' && job.stage === 'draft_published'
      && d.research_run_id === c.packetInspection.batch.research_run_id && same(v.canonical_questions, c.packetInspection.batch.canonical_questions)
      && (same(p.stage_names, ['plan', 'draft', 'critique']) || same(p.stage_names, ['plan', 'draft', 'critique', 'rewrite', 'final_critique'])));
  } else {
    const parent = c.draftInspection;
    require(p.origin === 'assessment' && job.kind === 'assess' && job.stage === 'assessment_published'
      && natural(p.edit_depth) && p.edit_depth >= 1 && p.edit_depth <= 64 && p.created_at !== null && utc(p.created_at)
      && p.parent_draft_id === parent.draft.id && d.id !== parent.draft.id && p.parent_publication_id === parent.publication.id
      && p.parent_text_sha256 === parent.text_sha256 && p.parent_ledger_sha256 === parent.ledger_sha256
      && p.parent_publication_sha256 === parent.publication_sha256 && same(p.pre_revisions, pre)
      && d.research_run_id === parent.draft.research_run_id && same(v.canonical_questions, parent.canonical_questions)
      && same(p.stage_names, ['reassess']) && same(targets, new DraftBuffer(parent).targets()));
  }
  return v;
}
function immutableResult(a, b) {
  return same(a.draft, b.draft) && same(a.publication, b.publication) && a.publication_sha256 === b.publication_sha256
    && a.text_sha256 === b.text_sha256 && a.ledger_sha256 === b.ledger_sha256
    && same(a.writing_targets, b.writing_targets) && same(a.canonical_questions, b.canonical_questions);
}

export class PaidLaunchController {
  #api; #owner; #application; #registry; #update; #onPublished; #keyFactory; #base; #token; #epoch; #identity; #competingMutationLocked; #minimumAdmission = 0; #retired = false;
  constructor({api, owner, application, registry, update, onPublished, competingMutationLocked = () => false, keyFactory = () => crypto.randomUUID()}) {
    require(uuid(owner) && uuid(application)); stateOf(registry);
    this.#api = api; this.#owner = owner; this.#application = application; this.#registry = registry; this.#update = update;
    this.#onPublished = onPublished; this.#keyFactory = keyFactory; this.#competingMutationLocked = competingMutationLocked;
    this.#base = `/api/workspace/profiles/${encodeURIComponent(owner)}/applications/${encodeURIComponent(application)}`;
    const state = stateOf(registry);
    this.#identity = ++state.controllerSequence;
    state.controllers.set(scopeKey(owner, application), this.#identity);
    this.#epoch = state.epochs.get(owner) || 0;
    this.#token = registry.get(owner, application)?.token || null;
  }
  #emit(view, value, error = null) { if (!this.#retired) notify(this.#update, view, copy(value), error); }
  #operation() { return find(this.#registry, this.#token); }
  snapshot() { return summary(this.#operation()); }
  retire() {
    this.#retired = true; this.#update = null;
    const state = stateOf(this.#registry), key = scopeKey(this.#owner, this.#application);
    if (state.controllers.get(key) === this.#identity) state.controllers.delete(key);
  }
  #rearm(revision) {
    if (this.#token === null) return;
    const state = stateOf(this.#registry), key = scopeKey(this.#owner, this.#application), receipt = state.resolutions.get(key);
    require(!this.#retired && state.controllers.get(key) === this.#identity && !state.operations.has(key)
      && this.#epoch === (state.epochs.get(this.#owner) || 0) && receipt && same(receipt.token, this.#token)
      && revision >= receipt.admission_revision);
    this.#minimumAdmission = receipt.admission_revision; this.#token = null;
  }
  handoffUnknown() { if (this.#retired) return false; const ok = this.#registry.handoff(this.#token); this.#emit('operation', this.snapshot()); return ok; }
  async launch(value, acknowledged) {
    if (this.#retired) return false;
    try {
      require(this.#competingMutationLocked() === false);
      require(this.#epoch === (stateOf(this.#registry).epochs.get(this.#owner) || 0) && acknowledged === true && !this.#registry.has(this.#owner, this.#application));
      const capture = validateCapture(value, this.#owner, this.#application);
      this.#rearm(capture.admission.admission_revision);
      require(capture.admission.admission_revision >= this.#minimumAdmission);
      const key = this.#keyFactory();
      require(typeof key === 'string' && key.length > 0 && key.length <= 200);
      const endpoint = capture.kind === 'generation' ? `${this.#base}/drafts` : `${this.#base}/drafts/${encodeURIComponent(capture.draftInspection.draft.id)}/assessment`;
      const wrapper = {schema_version: 1, admission_revision: capture.admission.admission_revision,
        request: {...copy(capture.request), idempotency_key: key, disclosure_sha256: capture.disclosure_sha256, acknowledged: true}};
      this.#token = begin(this.#registry, {owner: this.#owner, application: this.#application, kind: capture.kind, capture, endpoint, wrapper});
      return await this.#sendOriginal(true);
    } catch { this.#emit('operation', this.snapshot(), failure()); return false; }
  }
  async #action(allowed, action, view = 'operation') {
    if (this.#retired) return false;
    let ticket;
    try {
      const op = this.#operation(); require(op && allowed(op));
      ticket = acquire(this.#registry, this.#token); this.#emit('operation', this.snapshot());
      if (!active(this.#registry, this.#token, ticket)) return false;
      await action(copy(op), ticket);
      return !!active(this.#registry, this.#token, ticket);
    } catch {
      if (ticket !== undefined && active(this.#registry, this.#token, ticket)) this.#emit(view, view === 'operation' ? this.snapshot() : null, failure());
      else if (ticket === undefined) this.#emit(view, view === 'operation' ? this.snapshot() : null, failure());
      return false;
    } finally {
      const op = ticket !== undefined && active(this.#registry, this.#token, ticket);
      if (op) { op.action = null; changed(this.#registry, op); this.#emit('operation', summary(op)); }
    }
  }
  async #sendOriginal(initial = false) {
    return this.#action(op => initial ? op.state === 'pending' : op.state === 'unknown', async (captured, ticket) => {
      let op = active(this.#registry, this.#token, ticket); if (!op) return;
      op.state = 'pending'; op.handedOff = false; changed(this.#registry, op); this.#emit('operation', summary(op));
      try {
        // Both notification hooks may synchronously forget this owner. Fence immediately before transport.
        if (!active(this.#registry, this.#token, ticket)) return;
        if (initial) require(this.#competingMutationLocked() === false);
        if (!active(this.#registry, this.#token, ticket)) return;
        const response = await this.#api(captured.endpoint, {method: 'POST', body: copy(captured.wrapper)});
        op = active(this.#registry, this.#token, ticket); if (!op) return;
        op.job = validateJob(copy(response), op); op.state = 'known';
      } catch {
        op = active(this.#registry, this.#token, ticket); if (!op) return;
        op.state = 'unknown'; throw failure();
      }
    });
  }
  reconcileOriginal() { return this.#sendOriginal(); }
  #jobPath(job) { return `/api/workspace/profiles/${encodeURIComponent(this.#owner)}/jobs/${encodeURIComponent(job.id)}`; }
  async #readJob(captured, ticket, cancel = false) {
    const response = await this.#api(`${this.#jobPath(captured.job)}${cancel ? '/cancel' : ''}`, ...(cancel ? [{method: 'POST', body: {schema_version: 1}}] : []));
    const op = active(this.#registry, this.#token, ticket); if (!op) return null;
    const job = validateJob(copy(response), op); op.job = job; return job;
  }
  refreshKnownJob() { return this.#action(op => op.state === 'known', (op, ticket) => this.#readJob(op, ticket)); }
  cancelKnownJob() { return this.#action(op => op.state === 'known', (op, ticket) => this.#readJob(op, ticket, true)); }
  async #readAdmission(ticket) {
    const result = await this.#api(`${this.#base}/drafting-admission`);
    const op = active(this.#registry, this.#token, ticket); if (!op) return null;
    const admission = validateAdmission(result, this.#owner, this.#application); op.admission = admission;
    this.#emit('admission', admission); return admission;
  }
  refreshAdmission() { return this.#action(() => true, (_op, ticket) => this.#readAdmission(ticket), 'admission'); }
  async #readResult(captured, ticket, job) {
    require(job?.state === 'completed' && job.stage !== 'provider_unknown');
    const list = copy(await this.#api(`${this.#base}/drafts`));
    if (!active(this.#registry, this.#token, ticket)) return null;
    require(exact(list, ['schema_version', 'drafts']) && list.schema_version === 1 && Array.isArray(list.drafts));
    const matches = list.drafts.filter(item => item?.publication?.job_id === job.id);
    require(matches.length === 1);
    const listed = correlateResult(matches[0], captured, job);
    const result = copy(await this.#api(`${this.#base}/drafts/${encodeURIComponent(listed.draft.id)}`));
    const op = active(this.#registry, this.#token, ticket); if (!op) return null;
    const verified = correlateResult(result, captured, job); require(immutableResult(listed, verified));
    if (op.result) require(immutableResult(op.result, verified));
    op.result = verified;
    // Root bookkeeping is independent of whether the originating pane still exists.
    if (!op.published) {
      op.published = true;
      notify(this.#onPublished, {owner: this.#owner, application: this.#application, draftId: verified.draft.id,
        preRevisions: copy(captured.capture.request.expected_revisions), postRevisions: copy(verified.draft.revisions), current: verified.current});
    }
    if (!active(this.#registry, this.#token, ticket)) return null;
    this.#emit('result', verified); return verified;
  }
  inspectKnownResult() {
    return this.#action(op => op.state === 'known', async (captured, ticket) => {
      const job = await this.#readJob(captured, ticket); if (job) await this.#readResult(captured, ticket, job);
    }, 'result');
  }
  async resolveKnown() {
    let removed = false;
    await this.#action(op => op.state === 'known', async (captured, ticket) => {
      const job = await this.#readJob(captured, ticket); if (!job) return;
      require(job.stage !== 'provider_unknown' && ['failed', 'cancelled', 'completed'].includes(job.state));
      if (job.state === 'completed' && !await this.#readResult(captured, ticket, job)) return;
      const admission = await this.#readAdmission(ticket); if (!admission) return;
      require(admission.eligible && admission.reason_codes.length === 0 && admission.admission_revision >= captured.wrapper.admission_revision + 1);
      const op = active(this.#registry, this.#token, ticket); if (!op) return;
      const state = stateOf(this.#registry), key = scopeKey(this.#owner, this.#application);
      // This receipt proves resolution, never mere absence; it contains no retained request or text.
      state.resolutions.set(key, {token: copy(op.token), admission_revision: admission.admission_revision});
      state.operations.delete(key); removed = true;
      changed(this.#registry, op);
      this.#emit('operation', null);
    });
    return removed;
  }
}
