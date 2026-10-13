import {createClient, createTextClient, advanceApplicationOutput, WritingRecovery, EditBuffers, RequestGate, externalUrl, evidenceDeletionNotice} from './client.js';
import {node, field, section, text, link} from './dom.js';
import {writingQuestions, sourcePreview} from './questions.js';
import {activityPane} from './activity.js';
import {draftingPane} from './drafting-pane.js';
import {disclosurePane} from './disclosure-pane.js';
import {reviewPane} from './review-pane.js';
import {ReviewRegistry} from './review.js';
import {budgetPane} from './budget-pane.js';
import {PaidLaunchRegistry, PaidLaunchController} from './paid-launch.js';

let activity = null;
let writing = null;
let disclosure = null;
let review = null;
let budget = null;
let paidController = null;
let applicationLocks = null;
let connectionPending = false;
function retireDisclosure() {
  review?.retire(); review = null;
  disclosure?.retire(); disclosure = null;
  paidController?.retire(); paidController = null;
}
function retireBudget() { budget?.retire(); budget = null; }
function retireActivity() {
  applicationLocks = null;
  activity?.retire(); activity = null;
  writing?.retire(); writing = null;
  retireDisclosure();
  retireBudget();
}

const $ = id => document.getElementById(id);
const routes = ['overview', 'opportunities', 'interview', 'brain', 'evidence', 'applications', 'settings'];
const gate = new RequestGate();
const edits = new EditBuffers();
const writingRecovery = new WritingRecovery();
const state = {token: '', status: null, initialized: false, profiles: [], owner: '', detail: null, route: 'overview', busy: false, get dirty() { return edits.dirty; }, question: '', application: ''};
const api = createClient({token: () => state.token});
const paidRegistry = new PaidLaunchRegistry({onChange: ({owner, application}) => {
  if (state.owner !== owner || state.application !== application) return;
  applicationLocks?.(); writing?.syncLocks(); disclosure?.syncOperation(); review?.syncLocks();
}});
const reviewRegistry = new ReviewRegistry({onChange: ({owner, application}) => {
  if (state.owner !== owner || state.application !== application) return;
  applicationLocks?.(); writing?.syncLocks(); disclosure?.syncOperation(); review?.syncLocks();
}});
const workspace = (tail = '') => `/api/workspace/profiles/${encodeURIComponent(state.owner)}${tail}`;
const evidence = (tail = '') => `/api/evidence/profiles/${encodeURIComponent(state.owner)}${tail}`;
const revisions = () => state.detail.profile.revisions;
const values = form => Object.fromEntries(new FormData(form));

function notify(message, error = false) {
  $('notice').textContent = message;
  $('notice').className = error ? 'error' : '';
}
function invalidateDisclosureInput(event) {
  if (review?.element.contains(event.target)) return;
  if (disclosure && !disclosure.element.contains(event.target)) disclosure.invalidate();
  review?.invalidate();
}
function exactEnvelope(value, names) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
}
function canonicalIdentifier(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
}
function recognizedPrivacyDeletion(kind, value) {
  if (kind === 'workspace') return exactEnvelope(value, ['schema_version', 'deleted', 'cleanup_pending'])
    && value.schema_version === 1 && value.deleted === true && typeof value.cleanup_pending === 'boolean';
  return kind === 'evidence' && exactEnvelope(value, ['ticket_id', 'state', 'scope']) && canonicalIdentifier(value.ticket_id)
    && value.state === 'pending' && value.scope === 'access_revoked_cleanup_pending';
}
function cloudRevocationState(value, owner, application) {
  const dimensions = ['metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research'];
  const vector = value?.current_revisions, consent = value?.consent;
  if (!exactEnvelope(value, ['schema_version', 'profile_id', 'application_id', 'current_revisions', 'consent'])
    || value.schema_version !== 1 || value.profile_id !== owner || value.application_id !== application
    || !exactEnvelope(vector, ['schema_version', ...dimensions]) || vector.schema_version !== 1
    || !dimensions.every(name => Number.isSafeInteger(vector[name]) && vector[name] >= 0)
    || !(consent === null ? vector.consent === 0
      : exactEnvelope(consent, ['provider', 'purposes', 'granted', 'revision']) && typeof consent.provider === 'string'
        && consent.provider.length > 0 && consent.provider.length <= 100 && typeof consent.granted === 'boolean'
        && Number.isSafeInteger(consent.revision) && consent.revision === vector.consent
        && Array.isArray(consent.purposes) && consent.purposes.length > 0
        && new Set(consent.purposes).size === consent.purposes.length
        && consent.purposes.every(purpose => ['research', 'drafting', 'assessment'].includes(purpose)))) {
    throw new Error('Current consent state was not recognized. No revocation was sent; inspect the current scope.');
  }
  return structuredClone(value);
}
function recognizedCloudRevocation(value, owner, previousRevision) {
  const timestamp = value?.disclosed_at;
  return exactEnvelope(value, ['schema_version', 'profile_id', 'id', 'provider', 'purposes', 'granted', 'revision', 'disclosed_at'])
    && value.schema_version === 1 && value.profile_id === owner && canonicalIdentifier(value.id) && value.provider === 'openai'
    && value.granted === false && Number.isSafeInteger(value.revision) && value.revision === previousRevision + 1
    && Array.isArray(value.purposes) && value.purposes.length === 3
    && ['research', 'drafting', 'assessment'].every((purpose, index) => value.purposes[index] === purpose)
    && typeof timestamp === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(timestamp)
    && Number.isFinite(Date.parse(timestamp)) && new Date(timestamp).toISOString().slice(0, 19) === timestamp.slice(0, 19);
}
function validateConnectionStatus(value) {
  const capabilities = ['profile_management', 'interview', 'proposals', 'application_management', 'research', 'drafting', 'browser_fill', 'browser_submit'];
  if (!exactEnvelope(value, ['schema_version', 'boot_token', 'capabilities', 'cloud_key_configured']) || value.schema_version !== 1
    || typeof value.boot_token !== 'string' || value.boot_token.length < 1 || value.boot_token.length > 256
    || typeof value.cloud_key_configured !== 'boolean' || !exactEnvelope(value.capabilities, ['schema_version', ...capabilities])
    || value.capabilities.schema_version !== 1 || value.capabilities.profile_management !== true
    || !capabilities.every(name => typeof value.capabilities[name] === 'boolean')) {
    throw new Error('Local connection status was not recognized.');
  }
  return structuredClone(value);
}
async function refreshLocalConnection() {
  if (state.initialized !== true || connectionPending) return false;
  connectionPending = true; connection.disabled = true;
  try {
    const status = validateConnectionStatus(await api('/api/workspace/status'));
    state.token = status.boot_token; state.status = status;
    try { notify('Local connection refreshed; recovery memory retained. Choose reconciliation or status separately. No operation was resent.'); }
    catch { /* View feedback cannot change a validated connection update. */ }
    return true;
  } catch {
    try { notify('Local connection could not be refreshed. No operation was resent; the previous connection and retained recovery remain unchanged.', true); }
    catch { /* Retained connection/recovery is independent of view feedback. */ }
    return false;
  } finally {
    connectionPending = false; connection.disabled = state.initialized !== true;
  }
}
function applicationMutationLocked(owner, application) {
  return writingRecovery.has(owner, application) || paidRegistry.has(owner, application) || reviewRegistry.isMutationLocked(owner, application);
}
function writingBlocksNavigation() {
  if (reviewRegistry.blocksNavigation(state.owner, state.application)) {
    notify('A writing review is pending or unresolved. Resolve its exact original request or secure a read-only handoff before changing scope. Reloading loses page memory and is not rollback.', true);
    return true;
  }
  if (writing?.unresolved) {
    notify('A writing save is pending or its outcome is unresolved. Resolve the exact saved edit in place before changing scope or reloading. Closing/reloading the page can lose the in-memory operation; no rollback is claimed.', true);
    return true;
  }
  const paid = paidController?.snapshot();
  if (paid?.state === 'pending' || paid?.state === 'unknown' && paid.handedOff !== true) {
    notify('A paid request is pending or its outcome is unknown. Keep the original request; reconcile it explicitly or use the settled unknown read-only handoff before navigating. Reload/close loses page memory, not server history.', true);
    return true;
  }
  return false;
}
async function revokeCloudPurposes(owner, application) {
  const base = `/api/workspace/profiles/${encodeURIComponent(owner)}`;
  const stateUrl = `${base}/applications/${encodeURIComponent(application)}/drafting-state`, consentUrl = `${base}/consent`;
  if (state.owner === owner) { disclosure?.invalidate(); review?.invalidate(); }
  const captured = cloudRevocationState(await api(stateUrl), owner, application);
  let result;
  try {
    result = await api(consentUrl, {method: 'POST', body: {expected_consent_revision: captured.current_revisions.consent,
      provider: 'openai', purposes: ['research', 'drafting', 'assessment'], granted: false}});
  } catch (error) {
    throw Object.assign(new Error('Revocation outcome could not be verified. Inspect consent explicitly; cancellation, non-transmission and refund are not assumed.'), {status: error.status});
  }
  if (!recognizedCloudRevocation(result, owner, captured.current_revisions.consent)) {
    throw new Error('Revocation outcome is not recognized. Inspect consent explicitly; cancellation, non-transmission and refund are not assumed.');
  }
  // Consent is profile-wide; invalidate a new same-owner preview even when
  // navigation has retired the originating action's UI ticket.
  if (state.owner === owner) { disclosure?.invalidate(); review?.invalidate(); }
  return result;
}
async function forgetWritingAfter(kind, action) {
  const owner = state.owner;
  const result = await writingRecovery.forgetOwnerAfter(owner, async () => {
    const response = await action();
    if (!recognizedPrivacyDeletion(kind, response)) throw new Error('Deletion outcome is not recognized. Recovery remains retained; inspect before assuming deletion succeeded.');
    paidRegistry.forgetOwner(owner);
    reviewRegistry.forgetOwner(owner);
    return response;
  });
  // Privacy cleanup survives stale UI tickets, without touching another owner.
  if (state.owner === owner) { writing?.retire(); writing = null; retireDisclosure(); retireBudget(); }
  return result;
}
function context() {
  retireActivity();
  gate.select(`${state.owner}:${state.route}:${state.application}`);
  $('main').replaceChildren(text('Loading the selected local scope…'));
  edits.clear();
}
function busy(value) {
  state.busy = value;
  for (const control of document.querySelectorAll('[data-action-control]')) control.disabled = value || control.dataset.writingLocked === 'true';
  applicationLocks?.(); writing?.syncLocks(); disclosure?.syncOperation(); review?.syncLocks();
}
function button(label, action, className = '') {
  const result = node('button', label, className);
  result.type = 'button'; result.dataset.actionControl = ''; result.disabled = state.busy;
  result.onclick = action; return result;
}
function form(label, submit, ...children) {
  const result = node('form');
  const control = button(label); control.type = 'submit'; control.onclick = null;
  result.append(...children, control);
  result.oninput = () => edits.changed(result);
  result.onsubmit = event => { event.preventDefault(); submit(result); };
  return result;
}
function offerReload(message, error = false) {
  notify(message, error);
  $('notice').append(button('Reload selected data', () => {
    if (writingBlocksNavigation()) return;
    if (state.dirty && !confirm('Discard unsaved edits and reload the current server revisions?')) return;
    context(); reloadWithMessage('Current revisions reloaded. Re-enter only the edits you want to keep.');
  }, 'secondary'));
}
async function operation(action, after, submittedForm = null, preserveView = false) {
  if (state.busy) return;
  const ticket = gate.capture('mutation');
  const submitted = edits.capture(submittedForm);
  busy(true);
  try {
    const result = await action();
    if (!gate.current(ticket)) return;
    edits.acknowledge(submitted);
    if (state.dirty && !preserveView) {
      offerReload('The submitted action completed locally. Newer or other-form edits remain unsaved; reload explicitly to reconcile the saved state.');
      return;
    }
    if (after) await after(result);
    if (gate.current(ticket) && !after) notify('Saved locally.');
  } catch (error) {
    if (gate.current(ticket)) {
      if (error.status === 409) offerReload(`${error.message}. Your local edits remain; reload and reconcile before retrying.`, true);
      else notify(error.message, true);
    }
  } finally { busy(false); }
}
async function loadSelected() {
  const ticket = gate.capture('detail');
  const owner = state.owner;
  try {
    const [list, detail] = await Promise.all([api('/api/workspace/profiles'), owner ? api(workspace()) : null]);
    if (!gate.current(ticket)) return false;
    if (state.dirty) {
      offerReload('Local edits changed while data was loading. They remain unsaved; reload explicitly when ready.');
      return false;
    }
    state.profiles = list.profiles; state.detail = detail;
    $('profile').replaceChildren(new Option('Choose a local profile', ''));
    for (const item of state.profiles) $('profile').append(new Option(`${item.name} · ${item.sectors.join(' / ')}`, item.profile_id));
    $('profile').value = owner;
    const rendered = await render();
    return rendered && gate.current(ticket);
  } catch (error) { if (gate.current(ticket)) notify(error.message, true); return false; }
}
async function reloadWithMessage(message) {
  const ticket = gate.capture('feedback');
  if (await loadSelected() && gate.current(ticket)) notify(message);
}
async function refreshAfter() { await reloadWithMessage('Saved locally. Current revisions reloaded.'); }
function heading(label) { $('main').replaceChildren(node('h1', label)); }
function profileForm() {
  return form('Create blank local profile', element => {
    const data = values(element);
    operation(() => api('/api/workspace/profiles', {method: 'POST', body: {name: data.name, sectors: data.sector === 'both' ? ['tech', 'finance'] : [data.sector]}}), async created => {
      state.owner = created.profile.profile_id; state.question = ''; state.application = '';
      localStorage.setItem('copilot-python-profile', state.owner); context(); await reloadWithMessage('Blank profile created locally. No facts or cloud consent were invented.');
    }, element);
  }, field('name', 'Profile name', {required: true, maxLength: 200}), field('sector', 'Sectors', {options: [['both', 'Tech + finance'], ['tech', 'Tech'], ['finance', 'Finance']]}));
}
function overview() {
  heading('Your application workspace');
  $('main').append(section('Private, evidence-led preparation', text('This preview uses the Python data services. Inspect saved research, evidence packets, drafts and job/usage history; save immutable local writing edits and export saved text. Applications include explicitly acknowledged generation and unchanged saved-edit assessment, with retained in-page recovery controls. Human writing review is a separate local acknowledgement, not factual verification or browser permission. Research launch and supervised browser actions are not wired here. Nothing is submitted for you.'), text('Each friend should run an independent local installation. Profile selection is not account authentication.', 'muted')));
  if (!state.detail) { $('main').append(section('Start with your own experience', profileForm())); return; }
  const detail = state.detail;
  $('main').append(section('Next steps', text(`${detail.interview_answers.length} interview answers · ${detail.proposals.filter(p => p.status === 'pending').length} pending proposals · ${detail.applications.length} applications`), text('Save a story, confirm only accurate canonical facts, and add the exact employer questions. You can skip onboarding or finish early.'), link('#interview', 'Continue interview'), text('Evidence and typed personal values are stored separately. Uploading, approving wording or recording an outcome does not confirm a new fact.', 'muted')));
}
async function interview() {
  heading('Interview');
  const ticket = gate.capture('interview');
  const data = await api(workspace('/interview'));
  if (!gate.current(ticket)) return;
  const answered = new Set(data.answers.map(answer => answer.question_id));
  const skipped = new Set(data.progress.skipped_question_ids);
  const next = data.questions.find(q => !answered.has(q.id) && !skipped.has(q.id));
  const current = data.questions.find(q => q.id === state.question) || next || data.questions[0];
  if (!current) { $('main').append(text('No applicable interview questions.')); return; }
  state.question = current.id;
  const selection = field('question_id', 'Question', {value: current.id, options: data.questions.map(q => [q.id, `${answered.has(q.id) ? 'Answered · ' : skipped.has(q.id) ? 'Skipped · ' : ''}${q.prompt}`])});
  selection.querySelector('select').oninput = event => event.stopPropagation();
  selection.querySelector('select').onchange = event => {
    if (state.dirty && !confirm('Discard the unsaved answer and change questions?')) { event.target.value = current.id; return; }
    state.question = event.target.value; context(); loadSelected();
  };
  const old = data.answers.find(answer => answer.question_id === current.id);
  const editor = form('Save answer and continue', element => {
    const input = values(element);
    operation(() => api(workspace('/interview/answers'), {method: 'POST', body: {expected_metadata_revision: revisions().metadata, question_id: current.id, answer: input.answer}}), async () => {
      state.question = ''; await reloadWithMessage('Saved locally as a pending proposal. Confirm accurate wording in Brain before factual reuse.');
    }, element);
  }, selection, text(current.section, 'muted'), text(current.hint), field('answer', current.prompt, {type: 'textarea', value: old?.answer || '', required: true, maxLength: 20000, rows: 7}));
  const progress = (skip, completed) => {
    if (state.dirty && !confirm('Discard the unsaved answer and save interview progress?')) return;
    operation(() => api(workspace('/interview'), {method: 'PATCH', body: {expected_metadata_revision: revisions().metadata, skipped_question_ids: [...skip], completed}}), async () => { state.question = ''; await reloadWithMessage(completed ? 'Interview finished. You can reopen it later.' : 'Progress saved.'); }, editor);
  };
  const actions = node('div', '', 'row');
  if (!answered.has(current.id)) actions.append(button('Skip this question', () => progress(new Set([...skipped, current.id]), false), 'secondary'));
  actions.append(button(data.progress.completed ? 'Reopen interview' : 'Finish early', () => progress(skipped, !data.progress.completed), 'secondary'));
  $('main').append(section('Your experience, at your pace', text(`${answered.size} answered · ${skipped.size} skipped · ${data.questions.length} applicable questions. The full bank has 48 common/tech/finance questions.`), text('45–60 minutes is a target, not a timer or a barrier. Adaptive cloud follow-ups are not connected in this preview.', 'muted'), editor, actions));
}
async function brain() {
  heading('Brain');
  $('main').append(section('Propose a reusable fact', text('All new wording is pending until you explicitly confirm it. A student exercise is not a real client deal; metrics need your own evidence.'), form('Create pending proposal', element => operation(() => api(workspace('/proposals'), {method: 'POST', body: {expected_metadata_revision: revisions().metadata, text: values(element).text}}), refreshAfter, element), field('text', 'Proposed canonical wording', {type: 'textarea', required: true, maxLength: 20000}))));
  const inbox = section('Proposal inbox');
  for (const proposal of state.detail.proposals) {
    const entry = node('div', '', 'entry');
    entry.append(text(`${proposal.status} · origin: ${proposal.origin}`, 'muted'));
    if (proposal.status === 'pending') {
      const review = form('Confirm this wording', element => {
        const data = values(element);
        if (!data.confirmed) return notify('Explicit confirmation is required.', true);
        operation(() => api(workspace(`/proposals/${proposal.id}/review`), {method: 'POST', body: {expected_metadata_revision: revisions().metadata, expected_facts_revision: revisions().facts, action: 'confirm', confirmed: true, text: data.text}}), refreshAfter, element);
      }, field('text', 'Canonical wording to confirm', {type: 'textarea', value: proposal.text, required: true, maxLength: 20000}), field('confirmed', 'I confirm this exact wording is accurate and reusable.', {type: 'checkbox', required: true}));
      entry.append(review, button('Reject proposal', () => operation(() => api(workspace(`/proposals/${proposal.id}/review`), {method: 'POST', body: {expected_metadata_revision: revisions().metadata, expected_facts_revision: revisions().facts, action: 'reject'}}), refreshAfter), 'secondary'));
    } else entry.append(text(proposal.text));
    entry.append(button('Forget proposal and linked content', () => {
      if (!confirm('Forget this proposal, linked raw answer lineage and confirmed derivatives, and discard unsaved edits in this view? Unrelated saved facts remain; external backups are outside deletion.')) return;
      operation(() => forgetWritingAfter('workspace', () => api(workspace(`/proposals/${proposal.id}`), {method: 'DELETE', body: {expected_metadata_revision: revisions().metadata, expected_facts_revision: revisions().facts}})), async result => { context(); await reloadWithMessage(result.cleanup_pending ? 'Access revoked; external cleanup remains pending.' : 'Linked local content forgotten.'); }, null, true);
    }, 'danger'));
    inbox.append(entry);
  }
  if (!state.detail.proposals.length) inbox.append(text('No proposals yet. Save an interview answer or add a real experience.'));
  $('main').append(inbox);
  const ticket = gate.capture('facts');
  const data = await api(workspace('/facts'));
  if (!gate.current(ticket)) return;
  const confirmed = section('Confirmed by you');
  for (const record of data.facts) confirmed.append(text(record.fact.text), text(`Canonical fact ${record.fact.id} · ${record.confirmation?.origin || record.fact.provenance}. Confirmation is not independent verification.`, 'muted'));
  if (!data.facts.length) confirmed.append(text('No confirmed story evidence yet.'));
  $('main').append(confirmed);
}
function applicationForm(application = null) {
  return form(application ? 'Save changed application inputs' : 'Add exact application', element => {
    if (application && applicationMutationLocked(application.profile_id, application.application_id)) return notify('This application is read-only for this page session. Its unresolved writing or paid operation must not be replaced by input changes.', true);
    const data = values(element);
    let questions;
    try { questions = writingQuestions(data.questions, application?.questions, {max_words: data.max_words, max_chars: data.max_chars}); }
    catch (error) { return notify(error.message, true); }
    const body = {company: data.company, role: data.role, sector: data.sector, vacancy_url: data.vacancy_url, company_url: data.company_url || null, location: data.location, job_description: data.job_description, official_domains: data.domains.split(',').map(value => value.trim()).filter(Boolean), questions};
    if (body.official_domains.length && !data.confirmed_domains) return notify('Confirm the exact official domains; no domain is guessed for you.', true);
    if (application) Object.assign(body, {expected_input_revision: application.input_revision, expected_output_revision: application.output_revision});
    else body.expected_metadata_revision = revisions().metadata;
    operation(() => api(workspace(application ? `/applications/${application.application_id}` : '/applications'), {method: application ? 'PATCH' : 'POST', body}), async saved => { state.application = saved.application_id; state.route = 'applications'; location.hash = 'applications'; context(); await reloadWithMessage('Application inputs saved. Input changes invalidate later derived research and drafts.'); }, element);
  }, field('company', 'Company', {value: application?.company, required: true, maxLength: 300}), field('role', 'Exact role', {value: application?.role, required: true, maxLength: 300}), field('sector', 'Sector', {value: application?.sector || state.detail.profile.sectors[0], options: [['tech', 'Tech'], ['finance', 'Finance']]}), field('vacancy_url', 'Exact HTTPS vacancy URL', {type: 'url', value: application?.vacancy_url, required: true}), field('company_url', 'Official HTTPS company URL', {type: 'url', value: application?.company_url}), field('domains', 'Confirmed exact official domains, comma separated (optional)', {value: application?.official_domains.join(', ')}), field('confirmed_domains', 'I confirm these are the employer/approved vacancy domains.', {type: 'checkbox'}), field('location', 'Location', {value: application?.location, maxLength: 300}), field('job_description', 'Pasted role description — untrusted context, not independent company evidence', {type: 'textarea', value: application?.job_description, maxLength: 100000}), field('questions', 'Actual writing questions, one per line', {type: 'textarea', value: application?.questions.map(q => q.text).join('\n'), maxLength: 100000}), field('max_words', 'Optional default word limit for each question', {type: 'number'}), field('max_chars', 'Optional default code-point character limit for each question', {type: 'number'}), text('This preview editor supports writing questions. Individual mixed controls and richer per-question editing remain part of later UI/form integration.', 'muted'));
}
async function applications(create = false) {
  heading(create ? 'Opportunities' : 'Applications');
  if (create) { $('main').append(section('Track a real tech or finance vacancy', applicationForm())); return; }
  const list = section('Your applications');
  for (const application of state.detail.applications) list.append(button(`${application.company} · ${application.role}`, () => {
    if (writingBlocksNavigation()) return;
    if (state.dirty && !confirm('Discard unsaved edits and select another application?')) return;
    state.application = application.application_id; context(); loadSelected();
  }, 'secondary'));
  if (!state.detail.applications.length) list.append(text('No applications yet.'), link('#opportunities', 'Add an exact vacancy'));
  $('main').append(list);
  const application = state.detail.applications.find(item => item.application_id === state.application);
  if (!application) return;
  const inputForm = applicationForm(application), appForms = [inputForm], lockNotice = text('', 'warning');
  const lockForms = () => {
    const locked = applicationMutationLocked(application.profile_id, application.application_id);
    lockNotice.textContent = locked ? 'Retained writing, paid or review operation: application inputs/history and new writing saves are locked. Inspect, export, reconcile/status/cancel, revoke cloud purposes or delete this scope explicitly. No rollback, non-transmission or refund is assumed.' : '';
    for (const form of appForms) for (const control of form.querySelectorAll('input,textarea,select,button')) {
      control.dataset.writingLocked = locked ? 'true' : 'false'; control.disabled = state.busy || locked;
    }
  };
  applicationLocks = lockForms;
  lockForms();
  const url = externalUrl(application.vacancy_url);
  const owner = application.profile_id, appId = application.application_id;
  const revoke = button('Revoke all cloud purposes', () => operation(() => revokeCloudPurposes(owner, appId), result => {
    if (state.owner === owner && state.detail) {
      state.detail.consent = result;
      if (result.revision > revisions().consent) revisions().consent = result.revision;
    }
    notify('All three cloud purposes were recorded revoked. Future dispatch remains server-checked; already transmitted data, cancellation, refund and employer actions are not assumed.');
  }, null, true), 'danger');
  $('main').append(section(`${application.company} · ${application.role}`, ...(url ? [link(url, 'Inspect the public vacancy')] : []), text('Inspect stored research and packets, then separately acknowledge generation or unchanged saved-edit assessment. Local edits create unassessed immutable revisions. Inspect exact saved writing before separately recording a local human review. This does not verify facts or authorize browser actions. Research launch, form filling and submission remain unavailable.', 'warning'), inputForm, lockNotice, revoke));
  activity = activityPane({api, owner: state.owner, application: application.application_id});
  $('main').append(activity.element);
  writing = draftingPane({api, exportText: createTextClient({token: () => state.token}),
    owner: state.owner, application: application.application_id, edits, notify,
    recovery: writingRecovery.get(state.owner, application.application_id),
    readOnlyWriting: reviewRegistry.recovery(owner, appId),
    isSelectionLocked: () => reviewRegistry.isMutationLocked(owner, appId),
    isMutationLocked: () => applicationMutationLocked(application.profile_id, application.application_id),
    onHandoff: capsule => {
      if (state.owner !== application.profile_id || state.application !== application.application_id) return false;
      const retained = writingRecovery.retain(application.profile_id, application.application_id, capsule);
      if (retained) lockForms();
      return retained;
    },
    onCaptureChange: () => { disclosure?.invalidate(); review?.invalidate(); },
    onPublished: published => {
      if (state.owner === published.owner && state.application === published.application) { advanceApplicationOutput(application, published); review?.invalidate(); }
    }});
  writing.element.classList.add('application-writing');
  $('main').append(writing.element);
  paidController = new PaidLaunchController({api, owner, application: appId, registry: paidRegistry,
    competingMutationLocked: () => state.owner !== owner || state.application !== appId || state.busy || writingRecovery.has(owner, appId) || reviewRegistry.isMutationLocked(owner, appId),
    update: (view, data, error) => disclosure?.updatePaid(view, data, error),
    onPublished: published => {
      if (state.owner !== published.owner) return;
      const target = state.detail?.applications.find(item => item.application_id === published.application);
      if (target) advanceApplicationOutput(target, published);
      if (state.application === published.application) review?.invalidate();
    }});
  disclosure = disclosurePane({api, owner: state.owner, application: application.application_id,
    getCapture: () => writing?.capture(), isDirty: () => state.dirty, isBusy: () => state.busy,
    isMutationLocked: () => reviewRegistry.isMutationLocked(owner, appId), paidController, notify,
    onSelectResult: async ({owner, application, draftId}) => {
      if (state.owner !== owner || state.application !== application || !writing) return false;
      return writing.selectSavedResult(draftId);
    }});
  disclosure.element.classList.add('application-writing');
  $('main').append(disclosure.element);
  review = reviewPane({api, owner, application: appId, registry: reviewRegistry,
    getContext: () => state.owner !== owner || state.application !== appId ? null : ({owner, application: appId,
      writing: writing?.capture(), dirty: state.dirty, busy: state.busy,
      competingMutationLocked: writingRecovery.has(owner, appId) || paidRegistry.has(owner, appId)}),
    beforeDispatch: () => {
      if (state.owner !== owner || state.application !== appId || !writing?.fenceExternalDispatch()) throw new Error('The selected writing scope changed.');
      disclosure?.invalidate(); applicationLocks?.(); writing?.syncLocks();
    },
    onPublished: published => {
      if (state.owner !== published.owner) return;
      const target = state.detail?.applications.find(item => item.application_id === published.application);
      if (target) advanceApplicationOutput(target, published);
    },
    captureWriting: () => writing?.captureReadOnlyWriting(),
    freezeWriting: epoch => state.owner === owner && state.application === appId && writing?.freezeReadOnlyWriting(epoch) === true,
    selectResult: async (id, {readOnly}) => {
      if (state.owner !== owner || state.application !== appId || !writing) return false;
      return readOnly ? writing.inspectSavedResult(id) : writing.selectSavedResult(id);
    }});
  $('main').append(review.element);
  disclosure.syncOperation(); writing.syncLocks(); review.syncLocks();
  const ticket = gate.capture('history');
  const data = await api(workspace(`/applications/${application.application_id}/history`));
  if (!gate.current(ticket)) return;
  const history = section('Feedback and user-reported outcomes', text('Recording an outcome is not a browser submission or an observed employer receipt. Feedback creates a pending memory proposal.'));
  for (const event of data.history) history.append(text(`${event.kind} · ${event.recorded_at}`, 'muted'), text(event.text));
  const feedbackForm = form('Record local feedback', element => {
    if (applicationMutationLocked(application.profile_id, application.application_id)) return notify('History writes are locked while this application has unresolved writing or paid work.', true);
    const data = values(element);
    operation(() => api(workspace(`/applications/${application.application_id}/history`), {method: 'POST', body: {expected_input_revision: application.input_revision, expected_output_revision: application.output_revision, event_id: crypto.randomUUID(), kind: data.kind, text: data.text}}), refreshAfter, element);
  }, field('kind', 'Event kind', {options: [['feedback', 'Feedback'], ['user_reported_submitted', 'I report that I submitted manually']]}), field('text', 'Your own feedback or outcome statement', {type: 'textarea', required: true, maxLength: 20000}));
  appForms.push(feedbackForm); lockForms(); history.append(feedbackForm);
  history.append(button('Delete application and linked feedback content', () => {
    if (!confirm('Delete this application, history and dependent confirmed feedback facts, and discard unsaved edits in this view? Independently confirmed unrelated saved facts remain.')) return;
    operation(() => forgetWritingAfter('workspace', () => api(workspace(`/applications/${application.application_id}`), {method: 'DELETE', body: {expected_input_revision: application.input_revision, expected_output_revision: application.output_revision}})), async result => {
      state.application = ''; context(); await reloadWithMessage(result.cleanup_pending ? 'Application access revoked; external cleanup remains pending.' : 'Application deleted locally.');
    }, null, true);
  }, 'danger'));
  $('main').append(history);
}
async function inspectEvidence() {
  heading('Evidence');
  const ticket = gate.capture('evidence');
  const owner = state.owner;
  const sources = await api(evidence('/sources'));
  if (!gate.current(ticket)) return;
  const upload = form('Import selected document', element => {
    const file = element.querySelector('input').files[0];
    if (!file || file.size > 10 * 1024 * 1024) return notify('Select a TXT, Markdown or text PDF up to 10 MiB.', true);
    operation(() => api(evidence('/sources'), {method: 'POST', body: new FormData(element)}), refreshAfter, element);
  }, field('file', 'TXT, Markdown or text-based PDF (10 MiB maximum)', {type: 'file', required: true}), text('Uploading never confirms facts. Inspect canonical text, then propose accurate reusable wording in Brain.', 'muted'));
  const preview = node('pre', 'Choose a source to inspect canonical text.');
  const selection = node('div');
  const sourceSection = section('Import and inspect', upload);
  for (const source of sources) {
    sourceSection.append(button(`Inspect ${source.filename}`, async () => {
      const mark = gate.capture('source-preview');
      try {
        const units = await api(evidence(`/sources/${source.id}/units`));
        if (!gate.current(mark)) return;
        preview.textContent = sourcePreview(units);
      } catch (error) { if (gate.current(mark)) notify(error.message, true); }
    }, 'secondary'), button(`Delete ${source.filename}`, () => {
      if (!confirm('Delete the original source, revoke linked facts/proposals and discard unsaved file selections in this view? External backups and forensic traces are outside this operation.')) return;
      operation(() => forgetWritingAfter('evidence', () => api(evidence(`/sources/${source.id}`), {method: 'DELETE', body: {}})), async result => { context(); await reloadWithMessage(evidenceDeletionNotice(result)); }, null, true);
    }, 'danger'), link(evidence(`/sources/${source.id}/original`), `Download original ${source.filename}`));
    selection.append(field('source_id', source.filename, {type: 'checkbox'}));
    selection.lastElementChild.querySelector('input').value = source.id;
  }
  sourceSection.append(preview);
  $('main').append(sourceSection, section('Prepare indexes explicitly', text('Both dense Chroma and BM25 must be current. Missing/stale branches fail closed. These terminal operations are not proof of Docker qualification.'), node('pre', `uv run --directory backend python -m copilot.models setup\nuv run --directory backend python -m copilot index --profile ${owner} --corpus facts\nuv run --directory backend python -m copilot index --profile ${owner} --corpus documents\nuv run --directory backend python -m copilot cleanup`)));
  const results = node('div'), inspection = node('pre', 'Choose a quotation to inspect citation integrity.');
  const search = form('Retrieve exact quotations', async element => {
    const data = values(element), mark = gate.capture('search');
    gate.capture('citation'); results.replaceChildren(); inspection.textContent = 'Waiting for a current quotation.';
    const sourceIds = data.corpus === 'documents' ? [...selection.querySelectorAll('input:checked')].map(input => input.value) : null;
    if (sourceIds && !sourceIds.length) return notify('Choose at least one raw source explicitly.', true);
    notify('Retrieving local quotations; relevance is not truth.');
    try {
      const response = await api(evidence('/search'), {method: 'POST', body: {corpus: data.corpus, query: data.query, source_ids: sourceIds}});
      if (!gate.current(mark)) return;
      for (const hit of response.hits) {
        const row = node('div', '', 'entry'); row.append(node('blockquote', hit.citation.excerpt));
        row.append(button('Inspect exact citation', async () => {
          const stamp = gate.capture('citation');
          const {profile_id: ignored, ...citation} = hit.citation; void ignored;
          try {
            const resolved = await api(evidence('/citations'), {method: 'POST', body: citation});
            if (!gate.current(stamp)) return;
            const canonical = Array.from(resolved.canonical_text), start = Math.max(0, resolved.start - 400), end = Math.min(canonical.length, resolved.end + 400);
            inspection.replaceChildren(document.createTextNode(`Integrity: ${resolved.integrity}; semantic support: ${resolved.semantic_support}\n${resolved.page ? `Physical PDF page ${resolved.page}\n` : ''}Record: ${resolved.record_id}\n\n${start ? '…' : ''}${canonical.slice(start, resolved.start).join('')}`), node('mark', resolved.excerpt), document.createTextNode(`${canonical.slice(resolved.end, end).join('')}${end < canonical.length ? '…' : ''}`));
          } catch (error) { if (gate.current(stamp)) notify(error.message, true); }
        }, 'secondary')); results.append(row);
      }
      if (!response.hits.length) results.append(text('No quotations returned. No supported answer is inferred.'));
      notify(`Retrieved ${response.hits.length} local quotation(s). Relevance is not factual support; inspect citation integrity separately.`);
    } catch (error) { if (gate.current(mark)) notify(error.message, true); }
  }, field('corpus', 'Evidence corpus', {options: [['facts', 'Confirmed story facts'], ['documents', 'Explicitly selected raw documents']]}), field('query', 'Retrieval question (128-token limit)', {required: true, maxLength: 4000}), selection);
  search.oninput = null; // A transient retrieval query is not an unsaved profile edit.
  $('main').append(section('Search and inspect', text('Dense vectors + BM25 → RRF → reranking. Integrity and semantic support are different; these are quotations, not generated answers.'), search, results, inspection));
}
function settings() {
  heading('Settings and privacy');
  budget = budgetPane({api, owner: state.owner, edits,
    onConfigurationChanged: () => { disclosure?.invalidate(); review?.invalidate(); }, notify});
  $('main').append(budget.element);
  $('main').append(section('Writing preferences', form('Save explicit preferences', element => operation(() => api(workspace(), {method: 'PATCH', body: {expected_metadata_revision: revisions().metadata, writing_preferences: values(element).preferences}}), refreshAfter, element), field('preferences', 'Your preferred voice and writing style', {type: 'textarea', value: state.detail.profile.writing_preferences, maxLength: 10000}))));
  const consent = state.detail.consent;
  const purposes = ['research', 'drafting', 'assessment'];
  $('main').append(section('Cloud purposes', text('Paid generation and saved-edit assessment each require a separate selected-data preview and unchecked-by-default operation acknowledgement in Applications. Research launch is not connected. A key must be configured server-side, never in this page. Cloud transmission can incur charges and is not zero provider retention. If later edit assessment is wanted, explicitly grant both drafting and assessment before generation. Assessment remains optional; adding it afterward stales existing drafts and requires fresh generation.'), form('Save explicit purpose decision', element => {
    const data = new FormData(element), selected = purposes.filter(purpose => data.has(purpose));
    if (!selected.length) return notify('Choose at least one purpose for this consent or revocation record.', true);
    operation(() => api(workspace('/consent'), {method: 'POST', body: {expected_consent_revision: revisions().consent, provider: 'openai', purposes: selected, granted: data.has('granted')}}), refreshAfter, element);
  }, ...purposes.map(purpose => field(purpose, purpose, {type: 'checkbox', value: consent?.purposes.includes(purpose)})), field('granted', 'I explicitly grant these purposes. Unchecked records revocation.', {type: 'checkbox', value: consent?.granted}))));
  const typed = section('Personal values — separate from story evidence', text('Contact, identity, eligibility and declarations are never inferred. These values are not automatically authorized for transmission.'));
  for (const item of state.detail.typed_values) typed.append(text(`${item.kind} · ${item.field}: ${item.value}`), text(`Purpose: ${item.purpose} · ${item.application_id ? 'one application' : 'profile'} scope`, 'muted'), button('Delete this personal value', () => {
    if (!confirm('Delete this stored personal value and discard unsaved settings edits in this view?')) return;
    operation(() => forgetWritingAfter('workspace', () => api(workspace(`/typed-values/${item.id}`), {method: 'DELETE', body: {expected_metadata_revision: revisions().metadata}})), async () => { context(); await refreshAfter(); }, null, true);
  }, 'danger'));
  typed.append(form('Store explicitly confirmed personal value', element => {
    const data = values(element);
    operation(() => api(workspace('/typed-values'), {method: 'POST', body: {expected_metadata_revision: revisions().metadata, kind: data.kind, field: data.field, value: data.value, purpose: data.purpose, jurisdiction: data.jurisdiction || null, application_id: data.application_id || null, explicitly_confirmed: data.confirmed === 'on'}}), refreshAfter, element);
  }, field('kind', 'Value kind', {options: ['contact', 'identity', 'eligibility', 'demographic', 'declaration'].map(value => [value, value])}), field('field', 'Field name', {required: true, maxLength: 200}), field('value', 'Exact user-supplied value', {required: true, maxLength: 20000}), field('purpose', 'Purpose for storing this value', {required: true, maxLength: 1000}), field('jurisdiction', 'Jurisdiction, if relevant'), field('application_id', 'Application scope', {options: [['', 'Profile only'], ...state.detail.applications.map(application => [application.application_id, `${application.company} · ${application.role}`])]}), field('confirmed', 'I explicitly confirm this value and purpose.', {type: 'checkbox', required: true})));
  $('main').append(typed);
  const importer = section('Explicit legacy profile import', text('Choose one Node profile export, up to 1 MiB. Dry-run, then confirm the exact hash. No .data/.env discovery or merging; old cloud consent and review badges are not current approvals.'));
  const importForm = form('Dry-run selected export', async element => {
    const file = element.querySelector('input').files[0];
    if (!file || file.size > 1024 * 1024) return notify('Choose one profile export up to 1 MiB.', true);
    operation(() => api('/api/workspace/import/legacy/dry-run', {method: 'POST', body: new FormData(element)}), async summary => {
      importer.append(node('pre', JSON.stringify(summary, null, 2)), button('Confirm exact selected-byte import', () => {
        if (!confirm(`Import these exact selected bytes, SHA-256 ${summary.source_sha256}? New cloud consent remains off.`)) return;
        const selected = new FormData(); selected.append('file', file);
        operation(() => api('/api/workspace/import/legacy/commit', {method: 'POST', body: selected, headers: {'X-Confirm-Legacy-Import': 'true', 'X-Legacy-Source-Sha256': summary.source_sha256}}), async result => {
          state.owner = result.profile_ids[0]; state.application = ''; state.question = ''; localStorage.setItem('copilot-python-profile', state.owner); context(); await reloadWithMessage('Selected legacy profile imported. Original Node storage was not changed.');
        });
      }));
      notify('Dry-run only. No profile was imported.');
    }, null, true);
  }, field('file', 'One selected profile JSON export', {type: 'file', required: true}));
  importForm.oninput = null; // Selected bytes are a read-only dry-run input, not a saved settings edit.
  importer.append(importForm);
  $('main').append(importer);
  $('main').append(section('Local data controls', text('Local storage is plaintext. JSON export is not a complete blob backup or qualified restore. Provider-held data, employer autosaves, downloaded exports and external backups cannot be recalled.'), button('Export owned profile JSON', () => operation(() => api(workspace('/export')), async data => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    const anchor = link(url, 'Download private export'); anchor.download = `copilot-profile-${state.owner}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); notify('Private saved-state JSON export created. Unsaved browser edits are not exported. Store it securely.');
  }, null, true), 'secondary'), button('Delete selected profile and derived data', () => {
    if (!confirm('Revoke this profile, delete its app-held data and discard unsaved browser edits? Cleanup may remain pending; external copies and forensic traces are excluded.')) return;
    operation(() => forgetWritingAfter('workspace', () => api(workspace(), {method: 'DELETE', body: {}})), async result => {
      state.owner = ''; state.detail = null; state.application = ''; state.question = ''; localStorage.removeItem('copilot-python-profile'); context(); await reloadWithMessage(result.cleanup_pending ? 'Profile access revoked; external cleanup remains pending.' : 'Profile deleted locally.');
    }, null, true);
  }, 'danger')));
}
async function render() {
  if (state.dirty) return false;
  retireActivity();
  edits.clear();
  // Re-rendering the same owner/view also retires earlier pane requests.
  for (const slot of ['interview', 'facts', 'history', 'evidence', 'source-preview', 'search', 'citation']) gate.capture(slot);
  const ticket = gate.capture('render');
  try {
    for (const anchor of $('navigation').querySelectorAll('a')) {
      if (anchor.hash === `#${state.route}`) anchor.setAttribute('aria-current', 'page');
      else anchor.removeAttribute('aria-current');
    }
    if (!state.detail && state.route !== 'overview') { heading('Choose a local profile'); $('main').append(profileForm()); }
    else if (state.route === 'overview') overview();
    else if (state.route === 'interview') await interview();
    else if (state.route === 'brain') await brain();
    else if (state.route === 'evidence') await inspectEvidence();
    else if (state.route === 'opportunities') await applications(true);
    else if (state.route === 'applications') await applications();
    else settings();
    if (gate.current(ticket)) {
      busy(state.busy);
      if (!$('main').contains(document.activeElement)) $('main').focus();
      return true;
    }
    return false;
  } catch (error) { if (gate.current(ticket)) notify(error.message, true); return false; }
}
function navigate() {
  if (!state.initialized) return; // Keep latest hash; bootstrap owns its lifetime.
  const route = routes.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  if (route === state.route) return;
  if (writingBlocksNavigation()) { location.hash = state.route; return; }
  if (state.dirty && !confirm('Discard unsaved changes and change views?')) { location.hash = state.route; return; }
  state.route = route; context(); loadSelected();
}
for (const route of routes) $('navigation').append(link(`#${route}`, route.charAt(0).toUpperCase() + route.slice(1)));
document.querySelector('.skip').onclick = event => { event.preventDefault(); $('main').focus(); };
$('profile').onchange = event => {
  if (!state.initialized) return;
  if (writingBlocksNavigation()) { event.target.value = state.owner; return; }
  if (state.dirty && !confirm('Discard unsaved changes and switch profiles?')) { event.target.value = state.owner; return; }
  state.owner = event.target.value; state.detail = null; state.question = ''; state.application = '';
  if (state.owner) localStorage.setItem('copilot-python-profile', state.owner); else localStorage.removeItem('copilot-python-profile');
  context(); notify('Selected profile scope changed. Previous profile content cleared.'); loadSelected();
};
$('main').addEventListener('input', invalidateDisclosureInput);
window.addEventListener('hashchange', navigate);
window.addEventListener('beforeunload', event => { if (state.dirty || writingRecovery.size || paidRegistry.size || reviewRegistry.needsUnloadWarning()) { event.preventDefault(); event.returnValue = ''; } });
const connection = node('button', 'Refresh local connection', 'secondary');
connection.type = 'button'; connection.disabled = true; connection.onclick = refreshLocalConnection;
document.querySelector('header').append(connection);
async function start() {
  const ticket = gate.capture('bootstrap');
  try {
    const status = await api('/api/workspace/status');
    if (!gate.current(ticket)) return;
    if (!status.boot_token || status.capabilities?.profile_management !== true) throw new Error('Workspace data services are not available. No fallback success is claimed.');
    state.status = status; state.token = status.boot_token;
    const list = await api('/api/workspace/profiles');
    if (!gate.current(ticket)) return;
    const remembered = localStorage.getItem('copilot-python-profile');
    state.owner = list.profiles.some(profile => profile.profile_id === remembered) ? remembered : '';
    state.route = routes.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
    state.initialized = true; $('profile').disabled = false; connection.disabled = false;
    context(); await reloadWithMessage('Local preview ready. Inspect saved research, packets and writing; separately acknowledge paid generation/assessment, save local edits or export saved text. Exact writing review is available; browser filling and submission remain unavailable.');
  } catch (error) {
    if (gate.current(ticket)) {
      notify(error.message, true);
      $('notice').append(button('Retry local initialization', start, 'secondary'));
    }
  }
}
start();
