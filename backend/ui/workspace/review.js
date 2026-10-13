import {DraftBuffer, validateReviewDisclosure} from './drafting.js';

const copy = value => structuredClone(value);
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const exact = (v, names) => object(v) && Object.keys(v).length === names.length && names.every(k => Object.hasOwn(v, k));
const same = (a, b) => a === b || (Array.isArray(a) || Array.isArray(b)
  ? Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => same(v, b[i]))
  : object(a) && object(b) && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.hasOwn(b, k) && same(a[k], b[k])));
const require = condition => { if (!condition) throw new Error('The exact writing review could not be verified. Inspect saved writing again or resolve the retained request.'); };
const digest = v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
const registries = new WeakMap();
const key = (owner, application) => JSON.stringify([owner, application]);
const state = registry => { const s = registries.get(registry); require(s); return s; };
const entry = (registry, owner, application) => state(registry).entries.get(key(owner, application));
const summary = op => op ? copy({owner: op.owner, application: op.application, parentDraftId: op.parent.draft.id,
  state: op.state, quarantined: op.quarantined, actionPending: op.action !== null, result: op.result, published: op.published}) : null;
const live = (registry, op, action) => entry(registry, op.owner, op.application) === op && op.action === action
  && (state(registry).epochs.get(op.owner) || 0) === op.epoch;
const changed = (registry, op) => state(registry).onChange?.({owner: op.owner, application: op.application});

export class ReviewRegistry {
  constructor({onChange} = {}) { registries.set(this, {entries: new Map(), epochs: new Map(), onChange}); }
  get size() { return state(this).entries.size; }
  snapshot(owner, application) { return summary(entry(this, owner, application)); }
  recovery(owner, application) { return copy(entry(this, owner, application)?.recovery ?? null); }
  isMutationLocked(owner, application) { const op = entry(this, owner, application); return Boolean(op && (op.state !== 'recorded' || op.quarantined)); }
  blocksNavigation(owner, application) { const op = entry(this, owner, application); return Boolean(op && (op.action || op.state === 'sending' || op.state === 'uncertain' && !op.quarantined)); }
  needsUnloadWarning() { return [...state(this).entries.values()].some(op => op.state !== 'recorded' || op.quarantined); }
  forgetOwner(owner) {
    const s = state(this); s.epochs.set(owner, (s.epochs.get(owner) || 0) + 1);
    for (const [k, op] of s.entries) if (op.owner === owner) { s.entries.delete(k); try { changed(this, op); } catch { /* Privacy deletion must finish. */ } }
  }
}
function parentRequest(parent) {
  return {schema_version: 1, expected_revisions: copy(parent.draft.revisions), parent_text_sha256: parent.text_sha256,
    parent_ledger_sha256: parent.ledger_sha256, parent_publication_sha256: parent.publication_sha256};
}
function envelope(value, parent) {
  require(exact(value, ['schema_version', 'disclosure', 'disclosure_sha256']) && value.schema_version === 1 && digest(value.disclosure_sha256));
  const d = validateReviewDisclosure(value.disclosure), p = parent.publication;
  require(d.profile_id === parent.draft.profile_id && d.application_id === parent.draft.application_id
    && d.parent_draft_id === parent.draft.id && d.parent_publication_id === p.id && d.parent_publication_sha256 === parent.publication_sha256
    && d.batch_id === p.batch_id && d.batch_sha256 === p.batch_sha256 && d.research_run_id === parent.draft.research_run_id
    && same(d.pre_revisions, parent.draft.revisions) && d.text_sha256 === parent.text_sha256 && d.ledger_sha256 === parent.ledger_sha256
    && d.cover_letter === parent.draft.cover_letter && same(d.answers, parent.draft.answers) && same(d.ledger, parent.draft.ledger)
    && same(d.canonical_questions, parent.canonical_questions) && same(d.manual_requirements, parent.manual_requirements)
    && same(d.style_notes, p.style_notes));
  const manual = new Set(d.manual_requirements.map(m => m.question_id));
  const qs = [...d.canonical_questions, ...(d.cover_letter_target === null ? [] : [d.cover_letter_target])].filter(q => q.type === 'writing' && !manual.has(q.id));
  require(same(parent.writing_targets.map(t => t.question), qs));
  return {schema_version: 1, disclosure: d, disclosure_sha256: value.disclosure_sha256};
}
function correlate(value, op) {
  new DraftBuffer(value);
  const d = value.draft, p = value.publication;
  require(d.profile_id === op.owner && d.application_id === op.application && d.id !== op.parent.draft.id && p.origin === 'review'
    && p.parent_draft_id === op.parent.draft.id && p.parent_publication_id === op.parent.publication.id
    && same(p.request, op.body) && same(p.disclosure, op.envelope.disclosure)
    && same(value.writing_targets, op.parent.writing_targets)
    && d.assessment_state === op.parent.draft.assessment_state && d.inventory_complete === op.parent.draft.inventory_complete);
  return {owner: op.owner, application: op.application, draftId: d.id, preRevisions: copy(op.body.expected_revisions), postRevisions: copy(d.revisions), current: value.current};
}

export class ReviewController {
  #api; #owner; #application; #registry; #context; #before; #update; #published; #key;
  #epoch = 0; #sequence = 0; #preview = null; #pending = false; #error = null; #retired = false;
  constructor({api, owner, application, registry, getContext, beforeDispatch, update, onPublished, keyFactory = () => crypto.randomUUID()}) {
    this.#api = api; this.#owner = owner; this.#application = application; this.#registry = registry;
    this.#context = getContext; this.#before = beforeDispatch; this.#update = update; this.#published = onPublished; this.#key = keyFactory;
    state(registry);
  }
  #emit() { if (!this.#retired) this.#update?.(this.snapshot()); }
  snapshot() {
    if (this.#preview && this.#preview.privacyEpoch !== (state(this.#registry).epochs.get(this.#owner) || 0)) this.#preview = null;
    const preview = this.#preview ? {...copy({envelope: this.#preview.envelope, parentCapture: this.#preview.parent}), token: this.#preview.token} : null;
    return {preview, operation: this.#registry.snapshot(this.#owner, this.#application), error: this.#error, previewPending: this.#pending, retired: this.#retired};
  }
  invalidate() { this.#epoch++; this.#sequence++; this.#preview = null; this.#pending = false; this.#error = null; this.#emit(); }
  retire() { this.#retired = true; this.invalidate(); }
  #capture() {
    const c = copy(this.#context()), w = c?.writing;
    require(!this.#retired && c.owner === this.#owner && c.application === this.#application && c.dirty === false && c.busy === false
      && c.competingMutationLocked === false && w && w.bufferMatchesSaved === true && w.saving === false && w.uncertain === false
      && w.quarantined === false && w.retired === false && Number.isSafeInteger(w.epoch) && !this.#registry.isMutationLocked(this.#owner, this.#application));
    const p = w.draftInspection; new DraftBuffer(p);
    require(p.current === true && p.review_eligible === true && p.draft.profile_id === this.#owner && p.draft.application_id === this.#application
      && (!Object.hasOwn(p.publication, 'origin') || p.publication.origin === 'assessment')
      && p.draft.assessment_state === 'assessed' && p.draft.inventory_complete === true);
    return c;
  }
  #base(parent) { return `/api/workspace/profiles/${encodeURIComponent(this.#owner)}/applications/${encodeURIComponent(this.#application)}/drafts/${encodeURIComponent(parent.draft.id)}/review`; }
  async preview() {
    const seq = ++this.#sequence, epoch = this.#epoch, privacyEpoch = state(this.#registry).epochs.get(this.#owner) || 0;
    this.#preview = null; this.#error = null; this.#pending = true;
    let captured;
    try {
      captured = this.#capture(); this.#emit();
      const beforeTransport = this.#capture();
      require(seq === this.#sequence && epoch === this.#epoch && privacyEpoch === (state(this.#registry).epochs.get(this.#owner) || 0)
        && !this.#retired && same(captured, beforeTransport));
      const parent = captured.writing.draftInspection, result = await this.#api(`${this.#base(parent)}/preview`, {method: 'POST', body: parentRequest(parent)});
      if (seq !== this.#sequence || epoch !== this.#epoch || this.#retired) return false;
      const current = this.#capture();
      require(seq === this.#sequence && epoch === this.#epoch && privacyEpoch === (state(this.#registry).epochs.get(this.#owner) || 0)
        && !this.#retired && same(captured, current));
      this.#preview = {token: Object.freeze({}), envelope: envelope(result, parent), parent: copy(parent), context: captured, epoch, privacyEpoch};
      this.#pending = false; this.#emit(); return true;
    } catch (error) {
      if (seq === this.#sequence && epoch === this.#epoch && !this.#retired) { this.#pending = false; this.#preview = null; this.#error = error.message; this.#emit(); }
      return false;
    }
  }
  async record(token, acknowledged) {
    let op;
    try {
      const p = this.#preview;
      require(acknowledged === true && p && token === p.token && p.epoch === this.#epoch && p.privacyEpoch === (state(this.#registry).epochs.get(this.#owner) || 0) && same(p.context, this.#capture()));
      const id = this.#key(); require(typeof id === 'string' && id.length > 0 && Array.from(id).length <= 200);
      const current = this.#capture();
      require(this.#preview === p && p.epoch === this.#epoch && p.privacyEpoch === (state(this.#registry).epochs.get(this.#owner) || 0)
        && !this.#retired && same(p.context, current));
      const s = state(this.#registry);
      op = {owner: this.#owner, application: this.#application, parent: copy(p.parent), envelope: copy(p.envelope), path: this.#base(p.parent),
        body: {...parentRequest(p.parent), idempotency_key: id, disclosure_sha256: p.envelope.disclosure_sha256, acknowledged: true},
        context: copy(p.context), epoch: p.privacyEpoch, state: 'sending', action: Object.freeze({}), result: null, published: false, quarantined: false, recovery: null};
      s.entries.set(key(this.#owner, this.#application), op);
      this.#preview = null; this.#pending = false; this.#epoch++; this.#sequence++;
    } catch { return false; }
    return this.#dispatch(op, false);
  }
  async replayOriginal() {
    const op = entry(this.#registry, this.#owner, this.#application);
    if (this.#retired || !op || op.state !== 'uncertain' || op.action) return false;
    op.action = Object.freeze({}); op.state = 'sending'; this.#preview = null; this.#error = null;
    return this.#dispatch(op, true);
  }
  async #dispatch(op, replay) {
    const action = op.action; let sent = false;
    try {
      changed(this.#registry, op); this.#emit(); this.#before?.();
      require(live(this.#registry, op, action) && !this.#retired);
      if (!replay) require(same(op.context, copy(this.#context())));
      // The injected context callback above can synchronously forget or retire.
      require(live(this.#registry, op, action) && !this.#retired);
      sent = true;
      const value = await this.#api(op.path, {method: 'POST', body: copy(op.body)});
      if (!live(this.#registry, op, action)) return false;
      const publication = correlate(value, op);
      op.result = copy(value); op.state = 'recorded'; op.action = null;
      if (!op.published) { op.published = true; try { this.#published?.(copy(publication)); } catch { /* Correlated outcome remains recorded. */ } }
      if (entry(this.#registry, op.owner, op.application) !== op) return false;
      this.#error = null; try { changed(this.#registry, op); } catch { /* View callbacks cannot undo settlement. */ } this.#emit(); return true;
    } catch (error) {
      if (!live(this.#registry, op, action)) return false;
      op.action = null;
      if (sent || replay) op.state = 'uncertain'; else state(this.#registry).entries.delete(key(op.owner, op.application));
      this.#error = error?.message || 'The writing review outcome is unknown.';
      try { changed(this.#registry, op); } catch { /* Recovery remains available. */ }
      try { this.#emit(); } catch { /* Recovery remains available. */ } return false;
    }
  }
  handoffUncertain(writingTicket, freezeWriting) {
    const op = entry(this.#registry, this.#owner, this.#application), epoch = state(this.#registry).epochs.get(this.#owner) || 0;
    if (this.#retired || !op || op.state !== 'uncertain' || op.action || op.quarantined) return false;
    try {
      require(exact(writingTicket, ['epoch', 'capsule']) && Number.isSafeInteger(writingTicket.epoch));
      const c = copy(writingTicket.capsule), context = copy(this.#context());
      require(context.owner === this.#owner && context.application === this.#application && context.writing?.epoch === writingTicket.epoch
        && same(context.writing.draftInspection, c.baselineInspection));
      require(exact(c, ['schema_version', 'owner', 'application', 'baselineInspection', 'targets']) && c.schema_version === 1 && c.owner === this.#owner && c.application === this.#application);
      const b = new DraftBuffer(c.baselineInspection), saved = b.targets();
      require(c.baselineInspection.draft.profile_id === this.#owner && c.baselineInspection.draft.application_id === this.#application && Array.isArray(c.targets)
        && c.targets.length === saved.length && c.targets.every((t, i) => exact(t, ['target', 'text']) && t.target === saved[i].target && typeof t.text === 'string'));
      require(freezeWriting(writingTicket.epoch) === true && entry(this.#registry, this.#owner, this.#application) === op
        && epoch === (state(this.#registry).epochs.get(this.#owner) || 0) && op.state === 'uncertain' && !op.action);
      op.recovery = c; op.quarantined = true;
      try { changed(this.#registry, op); this.#emit(); } catch { /* Secured recovery cannot be undone by view hooks. */ } return true;
    } catch { return false; }
  }
}
