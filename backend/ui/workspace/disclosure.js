import {DraftBuffer} from './drafting.js';
import {validateAdmission} from './paid-launch.js';

const copy = value => structuredClone(value);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = value => typeof value === 'string' && value.length > 0;
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const keys = (value, names) => object(value) && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
const natural = value => Number.isSafeInteger(value) && value >= 0;
const dimensions = ['metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research'];
const require = condition => { if (!condition) throw new Error('Selected-data disclosure is inconsistent or no longer current; inspect the selection again.'); };
const same = (a, b) => {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => same(v, b[i]));
  if (!object(a) || !object(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key => Object.hasOwn(b, key) && same(a[key], b[key]));
};
function vector(value) {
  require(object(value) && value.schema_version === 1 && Object.keys(value).length === 8 && dimensions.every(key => natural(value[key])));
}
function question(value) {
  require(keys(value, ['schema_version', 'id', 'text', 'type', 'required', 'max_words', 'max_chars', 'options', 'constraint_origin']) && value.schema_version === 1 && text(value.id) && text(value.text)
    && ['writing', 'text', 'select', 'radio', 'checkbox', 'file', 'sensitive'].includes(value.type)
    && typeof value.required === 'boolean' && ['user', 'vacancy', 'form'].includes(value.constraint_origin)
    && Array.isArray(value.options) && value.options.every(option => typeof option === 'string')
    && [value.max_words, value.max_chars].every(limit => limit === null || Number.isSafeInteger(limit) && limit > 0));
}
function stateCapture(value, owner, application) {
  require(object(value) && Object.keys(value).length === 5 && value.schema_version === 1
    && value.profile_id === owner && value.application_id === application);
  vector(value.current_revisions);
  const consent = value.consent;
  if (consent === null) require(value.current_revisions.consent === 0);
  else require(object(consent) && Object.keys(consent).length === 4 && text(consent.provider) && consent.provider.length <= 100
    && typeof consent.granted === 'boolean' && natural(consent.revision) && consent.revision === value.current_revisions.consent
    && Array.isArray(consent.purposes) && consent.purposes.length > 0 && new Set(consent.purposes).size === consent.purposes.length
    && consent.purposes.every(purpose => ['research', 'drafting', 'assessment'].includes(purpose)));
}
function optionsCapture(value) {
  require(object(value) && Object.keys(value).length === 2 && typeof value.model === 'string'
    && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(value.model)
    && Number.isSafeInteger(value.max_output_tokens) && value.max_output_tokens >= 1 && value.max_output_tokens <= 8000);
}
function packetTargets(value, owner, application) {
  require(object(value) && value.schema_version === 1 && value.current === true && digest(value.batch_sha256) && digest(value.publication_sha256)
    && value.review_eligible === false && value.browser_eligible === false);
  const batch = value.batch;
  require(object(batch) && batch.schema_version === 1 && text(batch.id) && batch.profile_id === owner && batch.application_id === application
    && text(batch.research_run_id) && Array.isArray(batch.canonical_questions) && Array.isArray(batch.packets) && Array.isArray(batch.hiring_criteria));
  vector(batch.revisions);
  const publication = value.publication;
  require(object(publication) && publication.schema_version === 1 && text(publication.id)
    && publication.profile_id === owner && publication.application_id === application
    && publication.batch_id === batch.id && publication.batch_sha256 === value.batch_sha256);
  const ordered = [...batch.canonical_questions];
  require(ordered.every(q => q.id !== 'cover_letter'));
  if (batch.cover_letter_target !== null) {
    require(object(batch.cover_letter_target) && batch.cover_letter_target.id === 'cover_letter' && batch.cover_letter_target.type === 'writing');
    ordered.push(batch.cover_letter_target);
  }
  ordered.forEach(question);
  require(new Set(ordered.map(q => q.id)).size === ordered.length);
  const packetIds = new Set(), packetQuestions = new Set();
  batch.packets.forEach(packet => {
    require(object(packet) && packet.schema_version === 1 && text(packet.id) && !packetIds.has(packet.id)
      && packet.profile_id === owner && packet.application_id === application && packet.research_run_id === batch.research_run_id
      && object(packet.question) && packet.question.type === 'writing' && !packetQuestions.has(packet.question.id)
      && ordered.some(q => same(q, packet.question))
      && Array.isArray(packet.facts) && Array.isArray(packet.employer) && Array.isArray(packet.criteria) && Array.isArray(packet.gaps)
      && packet.gaps.every(gap => typeof gap === 'string'));
    packetIds.add(packet.id); packetQuestions.add(packet.question.id);
    for (const [key, kind] of [['facts', 'confirmed_fact'], ['employer', 'employer']]) {
      packet[key].forEach(ref => require(object(ref) && ref.kind === kind && ref.profile_id === owner
        && (kind !== 'employer' || ref.application_id === application && ref.research_run_id === batch.research_run_id)
        && object(ref.span) && typeof ref.span.excerpt === 'string'));
    }
  });
  require(new Set(batch.hiring_criteria.map(c => c.id)).size === batch.hiring_criteria.length);
  batch.hiring_criteria.forEach(c => require(object(c) && text(c.id) && text(c.text) && typeof c.inferred === 'boolean' && Array.isArray(c.evidence) && c.evidence.length > 0
    && c.evidence.every(ref => object(ref) && ref.kind === 'employer' && ref.profile_id === owner
      && ref.application_id === application && ref.research_run_id === batch.research_run_id
      && object(ref.span) && typeof ref.span.excerpt === 'string')));
  return ordered.flatMap(q => {
    const packet = batch.packets.find(p => p.question.id === q.id);
    if (!packet) return [];
    require(new Set(packet.criteria).size === packet.criteria.length);
    const criteria = packet.criteria.map(id => {
      const criterion = batch.hiring_criteria.find(c => c.id === id); require(criterion); return criterion;
    }).filter(c => c.evidence.every(ref => packet.employer.some(selected => same(ref, selected))))
      .map(({id, text, inferred}) => ({id, text, inferred}));
    return [{question: q, packet_id: packet.id, packet, criteria}];
  });
}
function selectedContext(value, targets) {
  require(keys(value, ['company', 'role', 'sector', 'targets']) && text(value.company) && text(value.role) && ['tech', 'finance'].includes(value.sector)
    && Array.isArray(value.targets) && value.targets.length === targets.length && targets.length > 0);
  value.targets.forEach((target, i) => {
    const expected = targets[i];
    require(keys(target, ['question', 'packet_id', 'facts', 'employer', 'criteria', 'gaps']) && same(target.question, expected.question) && target.packet_id === expected.packet_id);
    for (const [key, kind] of [['facts', 'confirmed_fact'], ['employer', 'employer']]) {
      require(Array.isArray(target[key]));
      target[key].forEach(ref => require(keys(ref, ['id', 'kind', 'excerpt']) && digest(ref.id) && ref.kind === kind && typeof ref.excerpt === 'string'));
      if (expected.packet) require(target[key].length === expected.packet[key].length
        && target[key].every((ref, index) => ref.excerpt === expected.packet[key][index].span.excerpt));
    }
    require(Array.isArray(target.criteria) && target.criteria.every(c => keys(c, ['id', 'text', 'inferred']) && text(c.id) && text(c.text) && typeof c.inferred === 'boolean')
      && new Set(target.criteria.map(c => c.id)).size === target.criteria.length
      && Array.isArray(target.gaps) && target.gaps.every(gap => typeof gap === 'string'));
    if (expected.packet) require(same(target.criteria, expected.criteria) && same(target.gaps, expected.packet.gaps));
  });
}
function correlate(value, kind, request, targets, batch, savedTargets) {
  require(keys(value, ['schema_version', 'review_eligible', 'browser_eligible', 'disclosure', 'disclosure_sha256']) && value.schema_version === 1 && value.review_eligible === false && value.browser_eligible === false && digest(value.disclosure_sha256));
  const d = value.disclosure, calls = kind === 'generation' ? 5 : 1;
  require(object(d) && d.schema_version === 1 && d.batch_id === batch.id && d.batch_sha256 === batch.hash
    && same(d.expected_revisions, request.expected_revisions) && d.model === request.model && d.max_output_tokens === request.max_output_tokens
    && d.consent_revision === request.expected_revisions.consent && d.maximum_calls === calls
    && d.conservative_reservation_units === calls * (131072 + request.max_output_tokens)
    && text(d.reservation_unit_description) && text(d.prompt_version) && d.output_schema_version === 1
    && digest(d.selected_context_sha256) && d.monetary_cost === null && d.pricing_status === 'unknown'
    && d.semantic_assessment === 'fallible_not_release_qualified');
  if (d.configured_limits !== null) require(object(d.configured_limits) && Object.keys(d.configured_limits).length === 4
    && ['daily_tokens', 'daily_calls', 'job_tokens', 'job_calls'].every(key => Number.isSafeInteger(d.configured_limits[key]) && d.configured_limits[key] > 0 && d.configured_limits[key] <= 100_000_000)
    && d.configured_limits.job_tokens <= d.configured_limits.daily_tokens
    && d.configured_limits.job_calls <= d.configured_limits.daily_calls);
  selectedContext(d.selected_context, targets);
  if (kind === 'assessment') {
    for (const key of ['parent_text_sha256', 'parent_ledger_sha256', 'parent_publication_sha256']) require(d[key] === request[key]);
    require(same(d.input_draft, {schema_version: 1, targets: savedTargets.map(target => ({schema_version: 1, ...target}))}));
  }
}

// A single generation fences all read-only preview operations, including the GET/POST boundary.
export class DisclosureController {
  #api; #owner; #application; #update; #base; #generation = 0; #retired = false;
  constructor({api, owner, application, update}) {
    this.#api = api; this.#owner = owner; this.#application = application; this.#update = update;
    this.#base = `/api/workspace/profiles/${encodeURIComponent(owner)}/applications/${encodeURIComponent(application)}`;
  }
  invalidate() {
    if (this.#retired) return;
    this.#generation++; this.#update('preview', null, null);
  }
  retire() { this.#retired = true; this.#generation++; this.#update = null; }
  previewGeneration(packetInspection, options = {}) { return this.#preview('generation', packetInspection, options); }
  previewAssessment(draftInspection, options = {}) { return this.#preview('assessment', draftInspection, options); }
  async #preview(kind, inspection, options) {
    if (this.#retired) return;
    const generation = ++this.#generation;
    const current = () => !this.#retired && generation === this.#generation;
    try {
      const capture = copy(inspection), settings = copy(options);
      optionsCapture(settings);
      let targets, batch, request, savedTargets, path;
      if (kind === 'generation') {
        targets = packetTargets(capture, this.#owner, this.#application);
        require(targets.length > 0);
        batch = {id: capture.batch.id, hash: capture.batch_sha256};
        path = `${this.#base}/drafts/preview`;
      } else {
        const buffer = new DraftBuffer(capture), d = capture.draft, p = capture.publication;
        require(capture.current === true && d.profile_id === this.#owner && d.application_id === this.#application
          && p.origin === 'edit' && d.assessment_state === 'unassessed' && d.ledger.length === 0 && d.inventory_complete === false
          && text(p.batch_id) && digest(p.batch_sha256));
        targets = capture.writing_targets; savedTargets = buffer.targets();
        batch = {id: p.batch_id, hash: p.batch_sha256};
        request = {schema_version: 1, expected_revisions: copy(d.revisions), parent_text_sha256: capture.text_sha256,
          parent_ledger_sha256: capture.ledger_sha256, parent_publication_sha256: capture.publication_sha256, ...settings};
        path = `${this.#base}/drafts/${encodeURIComponent(d.id)}/assessment/preview`;
      }
      const state = copy(await this.#api(`${this.#base}/drafting-state`));
      if (!current()) return;
      stateCapture(state, this.#owner, this.#application);
      if (kind === 'generation') request = {schema_version: 1, batch_id: batch.id, batch_sha256: batch.hash,
        expected_revisions: copy(state.current_revisions), ...settings};
      else require(same(state.current_revisions, request.expected_revisions));
      let admission = null, admissionError = null;
      try { admission = validateAdmission(copy(await this.#api(`${this.#base}/drafting-admission`)), this.#owner, this.#application); }
      catch { admissionError = new Error('Admission could not be verified. This preview is read-only; paid launch is unavailable.'); }
      if (!current()) return;
      const result = copy(await this.#api(path, {method: 'POST', body: copy(request)}));
      if (!current()) return;
      correlate(result, kind, request, targets, batch, savedTargets);
      this.#update('preview', {kind, owner: this.#owner, application: this.#application, admission: copy(admission), request: copy(request), disclosure: result.disclosure, disclosure_sha256: result.disclosure_sha256, state,
        packetInspection: kind === 'generation' ? capture : null, draftInspection: kind === 'assessment' ? capture : null}, admissionError);
    } catch (error) {
      if (current()) this.#update('preview', null, error);
    }
  }
}
