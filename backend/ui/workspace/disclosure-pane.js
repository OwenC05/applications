import {DisclosureController} from './disclosure.js';
import {node, section, text} from './dom.js';

const copy = value => structuredClone(value);
const clearOwned = element => {
  for (const child of [...element.children]) clearOwned(child);
  if ('value' in element) element.value = '';
  element.onclick = null; element.onchange = null; element.onsubmit = null;
  element.textContent = '';
};
let paneNumber = 0;
const details = (label, value) => {
  const result = node('details');
  result.append(node('summary', label), node('pre', JSON.stringify(value, null, 2)));
  return result;
};

// Local selected-data inspection and separately acknowledged paid operations.
// The root owns the paid controller and retained recovery capsule.
export function disclosurePane({api, owner, application, getCapture, isDirty, paidController, onSelectResult = async () => false, notify = () => {}}) {
  let retired = false, pending = null, preview = null, selection = null, operation = null, verifiedResult = null, paidFailure = null;
  const element = section('Selected-data disclosure', text('Inspection does not send data to the cloud. A separate unchecked acknowledgement and explicit launch are required for each new paid operation. A preview does not certify provider-key or remaining daily budget readiness.', 'warning'));
  element.className = 'application-writing';
  const model = node('input'); model.type = 'text'; model.value = ''; model.maxLength = 100; model.autocomplete = 'off';
  const ceiling = node('input'); ceiling.type = 'number'; ceiling.value = '2000'; ceiling.min = '1'; ceiling.max = '8000'; ceiling.step = '1';
  const labelled = (label, input) => { const result = node('label'); result.append(node('span', label), input); return result; };
  const status = text('Choose a saved packet or an unchanged saved local edit, enter your configured model identifier, then explicitly inspect.');
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const content = node('div'), generationReason = text(''), assessmentReason = text('');
  const generation = node('button', 'Inspect generation data and limits', 'secondary');
  const assessment = node('button', 'Inspect saved-edit assessment data and limits', 'secondary');
  const prefix = `selected-disclosure-${++paneNumber}`;
  for (const [kind, button, reason] of [['generation', generation, generationReason], ['assessment', assessment, assessmentReason]]) {
    button.type = 'button'; reason.id = `${prefix}-${kind}-reason`; button.setAttribute('aria-describedby', reason.id);
  }
  const acknowledge = node('input'); acknowledge.type = 'checkbox'; acknowledge.checked = false;
  const launch = node('button', 'Launch acknowledged paid operation'); launch.type = 'button';
  const launchReason = text('Inspect current selected data before acknowledging a paid operation.');
  launchReason.id = `${prefix}-launch-reason`; launch.setAttribute('aria-describedby', launchReason.id);
  const paidContent = node('div');
  const paidStatus = text('No retained paid operation. A fresh launch is a new operation, not recovery of a lost request.');
  paidStatus.setAttribute('role', 'status'); paidStatus.setAttribute('aria-live', 'polite');
  const actions = {};
  for (const [method, label] of [['refreshKnownJob','Read known job status'],['cancelKnownJob','Request known job cancellation'],['reconcileOriginal','Resend original acknowledged request (may first queue paid work)'],['inspectKnownResult','Inspect correlated saved result'],['refreshAdmission','Read current admission (does not resolve operation)'],['resolveKnown','Resolve known operation using fresh status and admission'],['handoffUnknown','Hand off unresolved operation for read-only navigation']]) {
    const button = node('button', label, 'secondary'); button.type = 'button'; actions[method] = button;
    button.onclick = () => paidAction(method);
  }
  const selectResult = node('button', 'Select inspected saved result'); selectResult.type = 'button';
  selectResult.onclick = async () => {
    if (retired || !verifiedResult || operation?.state !== 'known' || !operation.resultDraftId || operation.actionPending) return;
    const id = operation.resultDraftId;
    try { await onSelectResult({owner, application, draftId: id}); } catch { if (!retired) paidStatus.textContent = 'Saved result selection failed; no writing was replaced automatically.'; }
  };
  element.append(labelled('Provider model identifier', model), labelled('Maximum output tokens per call', ceiling),
    text('No compatible model is assumed. The output ceiling is configuration, not permission to spend or transmit.'), generation, generationReason, assessment, assessmentReason, status, content, labelled('I acknowledge this exact selected operation may transmit data and incur unknown charges', acknowledge), launch, launchReason, paidStatus, paidContent, ...Object.values(actions), selectResult);
  function blocked(kind) {
    if (retired) return 'This disclosure pane is retired.';
    let capture, dirty;
    try { capture = copy(getCapture()); dirty = isDirty(); } catch { return 'Writing state is unavailable. Refresh the saved inspection before proceeding.'; }
    if (!capture || typeof dirty !== 'boolean') return 'Writing state is unavailable.';
    if (capture.retired !== false) return 'The writing capture is retired or unavailable.';
    if (dirty) return 'Unsaved domain changes must be saved or discarded before inspection.';
    if (capture.saving !== false) return 'Wait for the local save to settle before inspection.';
    if (capture.uncertain !== false) return 'The saved edit outcome is unresolved. Inspection cannot resolve it.';
    if (capture.quarantined !== false) return 'This application is read-only with unresolved recovery. Disclosure inspection is unavailable.';
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(model.value)) return 'Enter a valid configured model identifier; no compatible default is assumed.';
    if (!/^\d+$/.test(ceiling.value) || !Number.isSafeInteger(Number(ceiling.value)) || Number(ceiling.value) < 1 || Number(ceiling.value) > 8000) return 'Maximum output tokens must be an integer from 1 to 8000.';
    if (kind === 'generation') {
      if (capture.packetInspection?.current !== true) return 'Inspect a selected current packet before requesting generation data.';
    } else if (capture.bufferMatchesSaved !== true || capture.draftInspection?.current !== true
      || capture.draftInspection?.publication?.origin !== 'edit'
      || capture.draftInspection?.draft?.assessment_state !== 'unassessed'
      || capture.draftInspection?.draft?.inventory_complete !== false
      || !Array.isArray(capture.draftInspection?.draft?.ledger) || capture.draftInspection.draft.ledger.length !== 0) {
      return 'Assessment inspection requires an unchanged, current, unassessed saved local edit; unsaved writing and other revision origins are not eligible.';
    }
    return {capture, options: {model: model.value, max_output_tokens: Number(ceiling.value)}};
  }
  function launchBlocked() {
    if (!paidController) return 'Paid launch remains unavailable: no root-owned launch controller.';
    if (!preview || !selection) return 'Explicitly inspect the current saved selection first.';
    const guard = blocked(preview.kind);
    if (typeof guard === 'string') return guard;
    if (JSON.stringify(guard.capture) !== JSON.stringify(selection.capture) || JSON.stringify(guard.options) !== JSON.stringify(selection.options)) return 'The selection or configuration changed. Explicitly inspect again.';
    if (operation) return 'Resolve the retained paid operation before beginning another.';
    if (!preview.admission || preview.admission.eligible !== true || preview.admission.reason_codes.length || preview.admission.admission_revision === Number.MAX_SAFE_INTEGER) return 'Admission is unavailable or ineligible. This inspection is read-only.';
    const purpose = preview.kind === 'generation' ? 'drafting' : 'assessment', consent = preview.state.consent;
    if (consent?.provider !== 'openai' || consent.granted !== true || !consent.purposes.includes(purpose)) return `Captured OpenAI ${purpose} purpose is not granted.`;
    const d = preview.disclosure, limits = d.configured_limits;
    if (!limits || limits.job_calls < d.maximum_calls || limits.daily_calls < d.maximum_calls || limits.job_tokens < d.conservative_reservation_units || limits.daily_tokens < d.conservative_reservation_units) return 'Configured ceilings do not cover the maximum operation reservation; this is not a remaining-usage or key check.';
    if (acknowledge.checked !== true) return 'Check the separate acknowledgement for this exact operation.';
    return null;
  }
  function syncPaid() {
    if (retired) return;
    if (preview && selection) {
      const guard = blocked(preview.kind);
      if (typeof guard === 'string' || JSON.stringify(guard.capture) !== JSON.stringify(selection.capture) || JSON.stringify(guard.options) !== JSON.stringify(selection.options)) {
        pending = null; preview = null; selection = null; acknowledge.checked = false; controller.invalidate(); clearOwned(content);
      }
    }
    const reason = launchBlocked(); launch.disabled = reason !== null; launchReason.textContent = reason ?? 'Explicit launch may incur charges and transmit selected and intermediate generated data. Server checks remain authoritative.';
    acknowledge.disabled = !preview || Boolean(operation);
    const known = operation?.state === 'known', unknown = operation?.state === 'unknown', active = operation?.actionPending === true || operation?.state === 'pending';
    for (const [method, button] of Object.entries(actions)) button.disabled = !paidController || !operation || active || (method === 'reconcileOriginal' || method === 'handoffUnknown' ? !unknown : !known);
    selectResult.disabled = !known || active || !verifiedResult || !operation.resultDraftId;
  }
  function sync() {
    for (const [kind, button, reason] of [['generation', generation, generationReason], ['assessment', assessment, assessmentReason]]) {
      const guard = blocked(kind); button.disabled = typeof guard === 'string';
      reason.textContent = typeof guard === 'string' ? guard : 'Available for read-only inspection; no launch permission is conferred.';
    }
    syncPaid();
  }
  function render(value) {
    const {kind, disclosure, state} = value;
    const consent = state.consent;
    const purpose = kind === 'generation' ? 'drafting' : 'assessment';
    const granted = consent?.provider === 'openai' && consent.granted === true && consent.purposes.includes(purpose);
    const consentCopy = !consent ? `OpenAI ${purpose} permission: absent (not granted).`
      : consent.provider !== 'openai' ? `Retained provider: ${consent.provider}. OpenAI ${purpose} permission: not granted; this other-provider grant is not applicable.`
        : `OpenAI ${purpose} permission: ${granted ? 'granted in the captured consent record' : 'not granted'}. This is not launch authorization.`;
    clearOwned(content);
    content.append(
      text(kind === 'generation' ? 'Generation: three to five calls, with at most one rewrite. Later stages also send intermediate generated text that does not exist yet.' : 'Saved-edit assessment: one critique call, no rewrite. Only the exact saved text shown below is assessed.'),
      text('This is selected data, not an exact future outgoing payload. Provider retention and charges may apply when you explicitly acknowledge and launch this operation.'),
      text(`Conservative reservation-unit bound: ${disclosure.conservative_reservation_units}. Reservation units are not exact tokens or a monetary price.`),
      text('Monetary cost: unknown, not zero. Pricing is unknown.'),
      text(consentCopy),
      text(!value.admission ? 'Admission unavailable: read-only inspection only.' : value.admission.eligible ? `Captured admission revision ${value.admission.admission_revision}: eligible at inspection time, not a key or remaining-usage check.` : `Captured admission blocks launch: ${value.admission.reason_codes.join(', ')}.`),
      text('Assessment consent is optional. If you want later assessment of a generated draft, grant the assessment purpose before generation. Adding it afterward changes the consent revision and stales retained drafts.'),
      details('Selected company, role, questions, constraints, evidence, criteria and gaps', disclosure.selected_context),
      ...(kind === 'assessment' ? [details('Exact saved edited text (not unsaved writing)', disclosure.input_draft)] : []),
      details('Configured reservation ceilings (not prices)', disclosure.configured_limits ?? 'Not configured'),
      details('Server hashes, versions and captured revisions (not client-verified semantic truth)', {
        disclosure_sha256: value.disclosure_sha256, selected_context_sha256: disclosure.selected_context_sha256,
        prompt_version: disclosure.prompt_version, output_schema_version: disclosure.output_schema_version,
        expected_revisions: disclosure.expected_revisions, model: disclosure.model, max_output_tokens: disclosure.max_output_tokens,
        parent_text_sha256: disclosure.parent_text_sha256, parent_ledger_sha256: disclosure.parent_ledger_sha256,
        parent_publication_sha256: disclosure.parent_publication_sha256,
      }));
  }
  const controller = new DisclosureController({api, owner, application, update(view, data, error) {
    if (retired || view !== 'preview') return;
    if (data == null && error == null) { clearOwned(content); return; }
    if (!pending) return;
    const active = pending;
    const guard = blocked(active.kind);
    if (typeof guard === 'string' || JSON.stringify(guard.capture) !== JSON.stringify(active.capture)
      || JSON.stringify(guard.options) !== JSON.stringify(active.options)) { invalidate(); return; }
    pending = null;
    if (error && !data) {
      clearOwned(content); status.textContent = 'Inspection failed. No launch was requested. Refresh the saved context and explicitly inspect again.';
      try { notify(status.textContent); } catch { /* Notification failure grants no authority. */ }
      return;
    }
    preview = copy(data); selection = copy(active); acknowledge.checked = false;
    render(data); status.textContent = error ? 'Selected-data inspection is read-only: admission could not be verified. No launch occurred.' : 'Selected-data inspection complete. No cloud send occurred. A new operation requires its own acknowledgement.';
    syncPaid();
  }});
  function inspect(kind) {
    if (retired) return;
    invalidate();
    const guard = blocked(kind);
    if (typeof guard === 'string') { status.textContent = guard; return; }
    pending = {...guard, kind};
    status.textContent = 'Inspecting local selected data and configured limits. No cloud transmission is requested.';
    if (kind === 'generation') controller.previewGeneration(guard.capture.packetInspection, guard.options);
    else controller.previewAssessment(guard.capture.draftInspection, guard.options);
  }
  function invalidate() {
    if (retired) return;
    pending = null; preview = null; selection = null; acknowledge.checked = false; controller.invalidate(); clearOwned(content); sync();
    status.textContent = 'Disclosure cleared. Explicitly inspect again for the current saved selection and configuration.';
  }
  function syncOperation() {
    if (retired) return;
    let next;
    try { next = copy(paidController?.snapshot() ?? null); } catch { paidStatus.textContent = 'Paid operation state is unavailable; inspect explicitly before proceeding.'; launch.disabled = true; return; }
    const resolved = operation !== null && next === null, began = operation === null && next !== null; operation = next;
    if (began) invalidate();
    if (resolved) { verifiedResult = null; clearOwned(paidContent); invalidate(); }
    const observations = [];
    if (operation) {
      observations.push(`Retained ${operation.kind} operation: ${operation.state}${operation.actionPending ? ' (action pending)' : ''}${operation.handedOff ? '; handed off for read-only navigation, not resolved' : ''}. Cancellation does not prove nontransmission or refund.`);
      if (operation.job) observations.push(`Job observation: ${operation.job.state}; stage: ${operation.job.stage}; cleanup pending: ${operation.job.cleanup_pending}.`);
      if (operation.admission) observations.push(`Admission observation: revision ${operation.admission.admission_revision}; eligible: ${operation.admission.eligible}; reasons: ${operation.admission.reason_codes.join(', ') || 'none'}.`);
      observations.push('Observations do not resolve or authorize a new operation. Use the explicit fresh-resolution action.');
    } else observations.push('No retained paid operation. Fresh inspection and acknowledgement start a NEW operation, not lost-request recovery.');
    if (paidFailure) observations.push(paidFailure);
    paidStatus.textContent = observations.join(' ');
    syncPaid();
  }
  function updatePaid(view, data, error) {
    if (retired) return;
    if (view === 'result' && !error && data) {
      verifiedResult = copy(data); clearOwned(paidContent);
      paidContent.append(text('Client correlation of server-verified publication, not independent browser hash verification or semantic approval.'), details('Correlated saved result (inspection only, never automatically selected)', verifiedResult));
    }
    if (error) paidFailure = 'Paid action could not be verified. The retained operation has not been cleared; use explicit recovery or status controls.';
    syncOperation();
  }
  async function paidAction(method) {
    if (retired || !paidController) return;
    syncOperation();
    if (actions[method]?.disabled) return;
    const target = paidController; paidFailure = null;
    syncOperation();
    try { await target[method](); } catch { if (!retired) paidFailure = 'Paid action failed; the original operation remains retained. No automatic retry occurred.'; }
    if (!retired) syncOperation();
  }
  launch.onclick = async () => {
    if (retired) return;
    syncOperation();
    const reason = launchBlocked();
    if (reason) { status.textContent = reason; if (preview && reason.includes('changed')) invalidate(); return; }
    const captured = copy(preview), target = paidController; paidFailure = null;
    invalidate();
    try { await target.launch(captured, true); } catch { if (!retired) paidFailure = 'Launch outcome could not be verified. Inspect the retained original operation; no automatic retry occurred.'; }
    if (!retired) syncOperation();
  };
  acknowledge.onchange = () => { if (!retired) syncPaid(); };
  const changed = () => invalidate();
  model.addEventListener('input', changed); ceiling.addEventListener('input', changed);
  generation.onclick = () => inspect('generation'); assessment.onclick = () => inspect('assessment');
  sync(); syncOperation();
  return {element, invalidate, syncOperation, updatePaid, retire() {
    if (retired) return;
    retired = true; pending = null; preview = null; selection = null; operation = null; verifiedResult = null; paidFailure = null; acknowledge.checked = false; controller.retire();
    model.removeEventListener('input', changed); ceiling.removeEventListener('input', changed);
    generation.onclick = null; assessment.onclick = null; model.value = ''; ceiling.value = '';
    clearOwned(element); getCapture = null; isDirty = null; notify = null; paidController = null; onSelectResult = null;
  }};
}
