import {RequestGate, externalUrl} from './client.js';
import {node, section, text, link} from './dom.js';

const encoded = value => encodeURIComponent(value);
// Each request captures its route before awaiting. Retiring a pane denies both
// successes and errors without aborting or pretending to cancel durable work.
export class ActivityController {
  constructor({api, owner, application, update}) {
    this.api = api; this.update = update; this.gate = new RequestGate();
    this.base = `/api/workspace/profiles/${encoded(owner)}`;
    this.research = `${this.base}/applications/${encoded(application)}/research`;
    this.runId = ''; this.retire();
  }
  retire() { this.gate.select('activity'); this.runId = ''; }
  async request(slot, path, options, view = slot) {
    const ticket = this.gate.capture(slot);
    try {
      const result = await this.api(path, options);
      if (this.gate.current(ticket)) this.update(view, result, null);
    } catch (error) { if (this.gate.current(ticket)) this.update(view, null, error); }
  }
  jobs() { return this.request('jobs', `${this.base}/jobs`); }
  status(id) { return this.request('jobs', `${this.base}/jobs/${encoded(id)}`); }
  cancel(id) { return this.request('jobs', `${this.base}/jobs/${encoded(id)}/cancel`, {method: 'POST', body: {schema_version: 1}}); }
  usage() { return this.request('usage', `${this.base}/usage`); }
  history() {
    this.runId = ''; this.gate.capture('source');
    return this.request('run', this.research, undefined, 'history');
  }
  run(id) {
    this.runId = id; this.gate.capture('source');
    return this.request('run', `${this.research}/${encoded(id)}`);
  }
  source(id) {
    const path = `${this.research}/${encoded(this.runId)}/sources/${encoded(id)}`;
    return this.request('source', path);
  }
}

export const visibleJobs = (jobs, application) => jobs.filter(job => job.application_id === application || (job.application_id == null && job.kind === 'index'));
const known = value => value ?? 'unknown';
export function jobNotice(job) {
  const cancellation = job.state === 'cancelled' ? 'Terminal cancellation recorded.' : job.cancellation_requested ? 'Cancellation requested; not terminal until the job state records it.' : 'No cancellation request recorded.';
  return `${job.kind} · ${job.id} · state: ${job.state} · stage: ${known(job.stage)}. ${cancellation} Already transmitted work cannot be recalled. Attempts: ${known(job.attempt_count)} · heartbeat: ${known(job.heartbeat_at)} · lease expiry: ${known(job.lease_expires_at)} · cleanup pending: ${known(job.cleanup_pending)} · fence: ${known(job.fence)} · captured revisions: ${JSON.stringify(job.revisions ?? null)}.`;
}
export function usageNotice(usage = {}) {
  return `Profile-wide accounting, not this application's price. Reserved tokens: ${known(usage.reserved_tokens)} · reserved calls: ${known(usage.reserved_calls)} · reported tokens: ${known(usage.reported_tokens)} · unknown attempts: ${known(usage.unknown_attempts)}. Monetary cost: unknown (not zero); pricing is unknown. Reservations and reports are separate, not additive billing totals.`;
}
export function canonicalPreview(data, limit = 12000) {
  const points = Array.from(data.unit?.text ?? '');
  return `${points.slice(0, limit).join('')}\n\n${points.length > limit ? 'Truncated: showing' : 'Showing'} ${Math.min(limit, points.length)} of ${points.length} canonical code points.`;
}

// The controls are created once. Refresh only updates activity-owned text/results,
// never the application input/history forms or their edit-buffer bookkeeping.
export function activityPane({api, owner, application}) {
  const pane = section('Application activity — static inspection only', text('Explicit refresh; no background polling. No research launch, paid retry, drafting, assessment, review or browser action is enabled. Stored hashes and eligibility are not semantic proof.', 'warning'));
  pane.classList.add('application-activity');
  const notices = {};
  for (const key of ['jobs', 'usage', 'history', 'run', 'source']) { notices[key] = text(''); notices[key].setAttribute('role', 'status'); }
  const control = (label, action) => { const result = node('button', label, 'secondary'); result.type = 'button'; result.onclick = action; return result; };
  const jobs = section('Selected application jobs and related profile index jobs', notices.jobs);
  const jobRows = new Map();
  const usage = section('Profile-wide usage', notices.usage);
  const research = section('Static research history and current head', notices.history);
  const runLabel = node('label', 'Inspect research run');
  const runSelect = node('select'); runLabel.append(runSelect);
  const runInfo = node('pre', 'Refresh history to inspect the current head.');
  const sourceLabel = node('label', 'Inspect stored source');
  const sourceSelect = node('select'); sourceLabel.append(sourceSelect);
  const sourceMeta = node('div'), sourceText = node('pre', 'No canonical source selected.');
  const clearSource = () => { sourceSelect.replaceChildren(); sourceSelect.disabled = true; sourceMeta.replaceChildren(); sourceText.textContent = 'No canonical source selected.'; notices.source.textContent = ''; };
  const clearRun = () => { runInfo.textContent = 'No run loaded. Historical complete state alone is not current eligibility.'; notices.run.textContent = ''; clearSource(); };
  const option = (value, label) => { const result = node('option', label); result.value = value; return result; };
  const showJob = job => {
    let row = jobRows.get(job.id);
    if (!row) {
      const entry = node('div', '', 'entry'), detail = text('');
      const status = control('Refresh job status', () => controller.status(job.id));
      const cancel = control('Request cancellation', () => controller.cancel(job.id));
      entry.append(detail, status, cancel); jobs.append(entry);
      row = {entry, detail, status, cancel}; jobRows.set(job.id, row);
    }
    row.detail.textContent = `${job.application_id == null ? 'Related profile-wide index job. ' : 'Selected application. '}${jobNotice(job)}`;
    row.cancel.disabled = job.cancellation_requested || !['queued', 'running'].includes(job.state);
  };
  const controller = new ActivityController({api, owner, application, update(view, data, error) {
    notices[view].textContent = error ? `Inspection failed: ${error.message}. Refresh explicitly; no success is claimed.` : '';
    if (error) return;
    if (view === 'jobs') {
      const filtered = visibleJobs(data.jobs || (data.job ? [data.job] : []), application);
      if (data.jobs) {
        const ids = new Set(filtered.map(job => job.id));
        for (const [id, row] of jobRows) if (!ids.has(id)) { row.entry.remove(); jobRows.delete(id); }
      }
      filtered.forEach(showJob);
      notices.jobs.textContent = jobRows.size ? 'Current durable states shown; refresh explicitly for changes.' : 'No selected-application or related profile index jobs.';
    } else if (view === 'usage') notices.usage.textContent = usageNotice(data.usage);
    else if (view === 'history') {
      clearRun();
      runSelect.replaceChildren(option('', 'Choose a historical run'));
      for (const run of data.runs) runSelect.append(option(run.id, `${run.id} · historical ${run.state}${run.id === data.current_run_id ? ' · current head' : ''}`));
      runSelect.disabled = !data.runs.length;
      notices.history.textContent = `Current head: ${data.current_run_id ?? 'none'}. Historical state is separate from current eligibility. No fallback to an older complete run.`;
      if (data.current_run_id) { runSelect.value = data.current_run_id; controller.run(data.current_run_id); }
    } else if (view === 'run') {
      clearSource();
      runInfo.textContent = `Historical state: ${data.run.state}\nCurrent eligibility (server): ${data.eligibility.eligible}\nAcquired: ${data.run.acquired_at}\nExpires: ${data.run.expires_at}\nGaps: ${data.run.gaps.length ? data.run.gaps.join('; ') : 'none recorded'}\nCaptured revisions: ${JSON.stringify(data.run.revisions)}\nClosed, incomplete, stale or non-current research is not silently replaced by an older run. Eligibility is not semantic support.`;
      sourceSelect.append(option('', 'Choose canonical source'));
      for (const source of data.sources) sourceSelect.append(option(source.id, `${source.purpose} · ${source.provenance} · ${source.id}`));
      sourceSelect.disabled = !data.sources.length;
    } else if (view === 'source') {
      sourceMeta.replaceChildren(text(`Provenance: ${data.source.provenance} · purpose: ${data.source.purpose} · role state: ${data.source.role_state} · acquired: ${data.source.acquired_at} · extraction: ${data.source.extraction_version}`), text(`Stored original SHA-256: ${data.source.original_sha256}`), text(`Stored canonical SHA-256: ${data.source.canonical_sha256} · unit hash: ${data.unit.text_sha256}`), text(`Identity spans (code points): ${JSON.stringify(data.identity_spans)}. Stored provenance/hashes are not semantic verification.`));
      for (const [label, value] of [['Original public URL', data.source.original_url], ['Final public URL', data.source.final_url]]) {
        const url = externalUrl(value); if (url) sourceMeta.append(link(url, label));
      }
      sourceText.textContent = canonicalPreview(data);
    }
  }});
  jobs.prepend(control('Refresh jobs', () => controller.jobs()));
  usage.append(control('Refresh profile usage', () => controller.usage()));
  research.append(control('Refresh research history/current head', () => { clearRun(); runSelect.disabled = true; controller.history(); }), runLabel, notices.run, runInfo, sourceLabel, notices.source, sourceMeta, sourceText);
  runSelect.disabled = true; sourceSelect.disabled = true;
  runSelect.onchange = () => { clearRun(); if (runSelect.value) controller.run(runSelect.value); else { controller.gate.capture('run'); controller.gate.capture('source'); } };
  sourceSelect.onchange = () => { sourceMeta.replaceChildren(); sourceText.textContent = 'No canonical source loaded.'; notices.source.textContent = ''; if (sourceSelect.value) controller.source(sourceSelect.value); else controller.gate.capture('source'); };
  pane.append(jobs, usage, research);
  return {element: pane, retire: () => controller.retire()};
}
