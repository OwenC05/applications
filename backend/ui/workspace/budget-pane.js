import {node, section, text} from './dom.js';

const keys = ['daily_tokens', 'daily_calls', 'job_tokens', 'job_calls'];
const bounded = value => Number.isSafeInteger(value) && value >= 1 && value <= 100000000;
const validLimits = value => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every(key => bounded(value[key]))
  && value.job_tokens <= value.daily_tokens && value.job_calls <= value.daily_calls;
const validEnvelope = value => value && value.schema_version === 1
  && (value.configured_limits === null || validLimits(value.configured_limits))
  && value.reservation_units === 'conservative_input_bytes_plus_output_ceiling'
  && value.monetary_cost === null && value.pricing_status === 'unknown' && value.grants_cloud_consent === false;
const scrub = element => {
  for (const child of [...element.children]) scrub(child);
  if ('value' in element) element.value = '';
  if ('checked' in element) element.checked = false;
  element.onclick = null; element.onsubmit = null; element.textContent = '';
};

// Explicit local reservation configuration only; never launches or grants consent.
export function budgetPane({api, owner, edits, onConfigurationChanged = () => {}, notify = () => {}}) {
  let retired = false, pending = false, needsObservation = false, historicalUnknown = false;
  const path = `/api/workspace/profiles/${encodeURIComponent(owner)}/budget`;
  const element = section('Profile reservation limits',
    text('Reservation units are conservative input bytes plus output ceilings, not a money cap. Monetary cost: unknown, not zero. Limits grant neither cloud purpose consent nor provider-key readiness.'),
    text('These profile-wide limits apply to future reservation/send checks and can affect queued/running work. Configuration is last-writer-wins; other tabs or pending requests can change it.'));
  const form = node('form'), inputs = {};
  const labels = {daily_tokens: 'Daily reservation units', daily_calls: 'Daily provider calls', job_tokens: 'Per-job reservation units', job_calls: 'Per-job provider calls'};
  for (const key of keys) {
    const input = node('input'); input.type = 'number'; input.name = key; input.min = '1'; input.max = '100000000'; input.step = '1'; input.value = '';
    const label = node('label'); label.append(node('span', labels[key]), input); form.append(label); inputs[key] = input;
  }
  const acknowledgement = node('input'); acknowledgement.type = 'checkbox'; acknowledgement.checked = false;
  const ackLabel = node('label'); ackLabel.append(acknowledgement, node('span', 'I acknowledge these exact entered profile-wide limits can affect queued/running work, are reservation units not a money cap, and grant neither cloud purpose consent nor provider-key readiness.'));
  const refresh = node('button', 'Refresh observed reservation limits', 'secondary'); refresh.type = 'button';
  const save = node('button', 'Save acknowledged reservation limits'); save.type = 'button';
  const status = text('Explicitly refresh to observe configuration, or enter and acknowledge new limits. No configuration is assumed.');
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const observed = node('div'), warning = text('');
  form.append(ackLabel, refresh, save); element.append(form, status, observed, warning);
  const sync = () => {
    refresh.disabled = retired || pending; save.disabled = retired || pending || needsObservation;
    acknowledgement.disabled = retired || pending;
  };
  const safely = callback => { try { callback(); } catch { /* Observers cannot grant authority or interrupt cleanup. */ } };
  function changed() {
    if (retired) return;
    acknowledgement.checked = false; edits.changed(form);
    status.textContent = 'Entered limits changed. Fresh acknowledgement is required before saving.';
  }
  function acknowledgeChanged() {
    if (retired) return;
    if (pending) { acknowledgement.checked = false; return; }
    edits.changed(form); status.textContent = 'Acknowledgement changed. Saving requires valid exact entered limits.';
  }
  for (const input of Object.values(inputs)) input.addEventListener('input', changed);
  acknowledgement.addEventListener('change', acknowledgeChanged);
  form.onsubmit = event => event.preventDefault();
  function showObserved(value) {
    scrub(observed);
    observed.append(text('Observed reservation configuration (not a lasting guarantee):'),
      value === null ? text('Not configured') : node('pre', JSON.stringify(value, null, 2)));
  }
  async function request(kind) {
    if (retired || pending || (kind === 'save' && needsObservation)) return;
    let body, ticket;
    if (kind === 'save') {
      const values = Object.fromEntries(keys.map(key => [key, /^\d+$/.test(inputs[key].value) ? Number(inputs[key].value) : NaN]));
      if (!validLimits(values) || acknowledgement.checked !== true) {
        status.textContent = 'Enter positive integer limits up to 100000000, with per-job limits no greater than daily limits, and freshly acknowledge the exact values.'; return;
      }
      body = {...values, acknowledged: true}; ticket = edits.capture(form);
    }
    pending = true; acknowledgement.checked = false; sync();
    status.textContent = kind === 'save' ? 'Saving entered reservation limits. No cloud transmission is requested.' : 'Reading observed reservation configuration. Editable inputs are unchanged.';
    safely(onConfigurationChanged);
    // An observer may synchronously retire this pane during a privacy/context change.
    if (retired) return;
    try {
      const response = await (kind === 'save' ? api(path, {method: 'POST', body}) : api(path));
      if (retired) return;
      if (!validEnvelope(response) || (kind === 'save' && (!response.configured_limits
        || !keys.every(key => response.configured_limits[key] === body[key])))) throw new Error('Invalid observed configuration');
      showObserved(response.configured_limits);
      if (kind === 'save') {
        edits.acknowledge(ticket);
        status.textContent = 'Save request returned an observed configuration matching the submitted limits. This is not a lasting configuration or money/spend guarantee.';
      } else {
        needsObservation = false;
        status.textContent = 'Refresh returned an observed configuration. Entered values and edit generations are unchanged; this does not settle or cancel earlier writes.';
      }
      safely(() => notify(status.textContent));
    } catch {
      if (retired) return;
      if (kind === 'save') {
        needsObservation = true; historicalUnknown = true;
        status.textContent = 'Configuration outcome unknown. No retry or success is claimed. Explicitly refresh before any new acknowledged update.';
      } else status.textContent = 'Reservation configuration refresh failed. No configuration or success is claimed.';
      safely(() => notify(status.textContent));
    } finally {
      if (!retired) {
        pending = false;
        warning.textContent = historicalUnknown ? 'Earlier requests or other tabs may still change configuration. Refresh only observes current values; it does not establish whether an earlier write committed, failed or finished. A later acknowledged Save is a new last-writer-wins update, not reconciliation or retry.' : '';
        sync();
      }
    }
  }
  refresh.onclick = () => request('refresh'); save.onclick = () => request('save'); sync();
  return {element, retire() {
    if (retired) return;
    retired = true; pending = false;
    for (const input of Object.values(inputs)) input.removeEventListener('input', changed);
    acknowledgement.removeEventListener('change', acknowledgeChanged);
    edits.forget(form); scrub(element); api = null; edits = null; onConfigurationChanged = null; notify = null;
  }};
}
