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

// Read-only selected-data inspection. This pane has no enqueue, acknowledgement,
// key, budget-write or browser-authorization path.
export function disclosurePane({api, owner, application, getCapture, isDirty, notify = () => {}}) {
  let retired = false, pending = null;
  const element = section('Selected-data disclosure', text('Inspection does not send data to the cloud. Paid launch remains unavailable here. A preview does not certify provider-key, purpose-consent or budget readiness.', 'warning'));
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
  element.append(labelled('Provider model identifier', model), labelled('Maximum output tokens per call', ceiling),
    text('No compatible model is assumed. The output ceiling is configuration, not permission to spend or transmit.'), generation, generationReason, assessment, assessmentReason, status, content);
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
  function sync() {
    for (const [kind, button, reason] of [['generation', generation, generationReason], ['assessment', assessment, assessmentReason]]) {
      const guard = blocked(kind); button.disabled = typeof guard === 'string';
      reason.textContent = typeof guard === 'string' ? guard : 'Available for read-only inspection; no launch permission is conferred.';
    }
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
      text('This is selected data, not an exact future outgoing payload. Provider retention and charges may apply if a later separately authorized launch is enabled.'),
      text(`Conservative reservation-unit bound: ${disclosure.conservative_reservation_units}. Reservation units are not exact tokens or a monetary price.`),
      text('Monetary cost: unknown, not zero. Pricing is unknown.'),
      text(consentCopy),
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
    if (error) {
      clearOwned(content); status.textContent = 'Inspection failed. No launch was requested. Refresh the saved context and explicitly inspect again.';
      try { notify(status.textContent); } catch { /* Notification failure grants no authority. */ }
      return;
    }
    render(data); status.textContent = 'Selected-data inspection complete. No cloud send or launch authorization occurred.';
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
    pending = null; controller.invalidate(); clearOwned(content); sync();
    status.textContent = 'Disclosure cleared. Explicitly inspect again for the current saved selection and configuration.';
  }
  const changed = () => invalidate();
  model.addEventListener('input', changed); ceiling.addEventListener('input', changed);
  generation.onclick = () => inspect('generation'); assessment.onclick = () => inspect('assessment');
  sync();
  return {element, invalidate, retire() {
    if (retired) return;
    retired = true; pending = null; controller.retire();
    model.removeEventListener('input', changed); ceiling.removeEventListener('input', changed);
    generation.onclick = null; assessment.onclick = null; model.value = ''; ceiling.value = '';
    clearOwned(element); getCapture = null; isDirty = null; notify = null;
  }};
}
