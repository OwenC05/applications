import {ReviewController, ReviewRegistry} from './review.js';
import {node, section, text} from './dom.js';

const warnings = [
  'Writing only: this records your statement about this exact saved writing, not approval of the whole application.',
  'Automated support is fallible. Read the excerpts and citations; they are not proof of truth.',
  'Not fact confirmation: this action does not confirm new personal facts.',
  'Manual fields and attachments are excluded and have not been reviewed by this action.',
  'No browser authorization: this does not authorize filling or submitting an application.',
];
let paneNumber = 0;
function clearOwned(element) {
  for (const child of [...element.children]) clearOwned(child);
  if ('value' in element) element.value = '';
  if ('checked' in element) element.checked = false;
  element.onclick = null; element.onchange = null;
  element.textContent = '';
}
function exact(label, value) {
  const box = node('div'); box.append(node('h3', label), node('pre', value)); return box;
}
function detail(label, value) {
  const box = node('details'); box.append(node('summary', label), node('pre', JSON.stringify(value, null, 2))); return box;
}

// Local writing-only review. The controller owns immutable captures and correlation;
// the root owns editor preservation, selection, privacy and composed mutation locks.
export function reviewPane({api, owner, application, registry = new ReviewRegistry(), getContext,
  beforeDispatch, onPublished, keyFactory, selectResult = async () => false,
  captureWriting = () => null, freezeWriting = () => false,
  confirmHandoff = () => globalThis.confirm?.('Keep the unresolved review and current writing in page memory, and make this application read-only? Reloading or closing loses this recovery.') === true}) {
  let retired = false, current = null, shownPreview = null, selecting = false, localError = '';
  const prefix = `writing-review-${++paneNumber}`;
  const element = section('Review exact saved writing', text('This local action records a human statement about saved writing only. It is not fact confirmation, whole-application approval or browser permission.', 'warning'));
  element.className = 'application-writing';
  const status = text('Inspect unchanged, eligible saved writing before acknowledging a review.');
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); status.setAttribute('aria-atomic', 'true');
  const reason = text(''); reason.id = `${prefix}-reason`;
  const error = text(''); error.id = `${prefix}-error`;
  const content = node('div'), resultContent = node('div');
  const makeButton = label => { const b = node('button', label, 'secondary'); b.type = 'button'; b.setAttribute('aria-describedby', `${reason.id} ${error.id}`); return b; };
  const inspect = makeButton('Inspect writing for human review');
  const acknowledge = node('input'); acknowledge.type = 'checkbox'; acknowledge.checked = false;
  acknowledge.setAttribute('aria-describedby', reason.id);
  const acknowledgement = node('label'); acknowledgement.append(acknowledge, node('span', 'I have reviewed this exact saved writing and understand these limits.'));
  const record = makeButton('Record my writing review');
  const replay = makeButton('Resend exact review request to resolve outcome (may record it now)');
  const handoff = makeButton('Keep unresolved review and writing for read-only navigation');
  const selection = makeButton('Inspect recorded revision');
  element.append(inspect, reason, status, error, content, acknowledgement, record, resultContent, replay, handoff, selection);

  function blocked() {
    if (retired) return 'This writing review panel is retired.';
    let context;
    try { context = getContext(); } catch { return 'Writing state is unavailable. Refresh the saved inspection.'; }
    if (!context || context.owner !== owner || context.application !== application) return 'The writing scope changed. Inspect the selected application again.';
    if (context.dirty !== false) return 'Save or discard unsaved writing and domain changes before inspection.';
    if (context.busy !== false) return 'Wait for the current application action to settle.';
    if (context.competingMutationLocked !== false) return 'Another retained operation locks application mutations; resolve it first.';
    const w = context.writing;
    if (!w || w.retired !== false) return 'Current saved writing is unavailable or retired.';
    if (w.saving !== false) return 'Wait for the local writing save to settle.';
    if (w.uncertain !== false) return 'The local edit outcome is unresolved.';
    if (w.quarantined !== false) return 'This writing is read-only with retained recovery.';
    if (w.bufferMatchesSaved !== true) return 'Inspect unchanged saved writing; unsaved writing cannot be reviewed.';
    if (w.draftInspection?.current !== true) return 'The saved writing is stale. Refresh its inspection.';
    const manualIds = new Set((w.draftInspection.manual_requirements ?? []).map(item => item.question_id));
    const requiredManual = (w.draftInspection.canonical_questions ?? []).filter(q => q.required === true && manualIds.has(q.id));
    if (requiredManual.length) return `Required manual questions block this writing review: ${requiredManual.map(q => q.text).join('; ')}. These require separate manual handling.`;
    if (w.draftInspection?.review_eligible !== true) return 'The saved writing is not eligible for human writing review. Required manual questions, unresolved support or a non-eligible revision can block review.';
    return null;
  }
  function showDisclosure(d) {
    clearOwned(content);
    const warningList = node('ol'); warningList.className = 'warning';
    for (const warning of warnings) warningList.append(node('li', warning));
    content.append(warningList);
    if (d.cover_letter_target === null) content.append(text('Cover letter: not configured.'));
    else {
      content.append(exact('Exact saved cover letter', d.cover_letter), detail('Canonical cover-letter question and limits', d.cover_letter_target));
      if (d.cover_letter === '') content.append(text('The configured saved cover letter is blank.'));
    }
    for (const answer of d.answers) {
      const question = d.canonical_questions.find(q => q.id === answer.question_id);
      content.append(exact(`Exact saved answer: ${question?.text ?? answer.question_id}`, answer.text), detail(`Canonical question and limits: ${answer.question_id}`, question), text(`Packet: ${answer.packet_id}`));
    }
    content.append(node('h3', 'Automated support ledger — fallible, not verified truth'));
    if (!d.ledger.length) content.append(text('No support ledger entries.'));
    for (const span of d.ledger) content.append(exact(`${span.target}: ${span.assessment} (${span.kind}; ${span.integrity})`, span.excerpt), detail('Exact span and citations', span));
    content.append(node('h3', 'Excluded manual fields and attachments'));
    if (!d.manual_requirements.length) content.append(text('No canonical manual requirements in this disclosure. Attachments remain outside this review.'));
    for (const item of d.manual_requirements) {
      const q = [...d.canonical_questions, ...(d.cover_letter_target ? [d.cover_letter_target] : [])].find(q => q.id === item.question_id);
      content.append(text(`${q?.text ?? item.question_id} — ${q?.required === true ? 'Required manual item' : 'Optional manual item'}: excluded from this writing review.`), text(item.reason), detail('Canonical excluded question', q));
    }
    content.append(node('h3', 'Style notes (advisory)'));
    for (const note of d.style_notes) content.append(text(note));
    if (!d.style_notes.length) content.append(text('No style notes.'));
    content.append(detail('Exact source identity, hashes and revision vectors', {
      profile_id: d.profile_id, application_id: d.application_id, parent_draft_id: d.parent_draft_id,
      parent_publication_id: d.parent_publication_id, parent_publication_sha256: d.parent_publication_sha256,
      batch_id: d.batch_id, batch_sha256: d.batch_sha256, research_run_id: d.research_run_id,
      text_sha256: d.text_sha256, ledger_sha256: d.ledger_sha256,
      pre_revisions: d.pre_revisions, post_revisions: d.post_revisions,
    }));
  }
  function sync() {
    if (retired) return;
    const operation = current?.operation;
    const ownLocked = registry.isMutationLocked(owner, application);
    const reasonText = blocked();
    reason.textContent = reasonText ?? (ownLocked ? 'Resolve this retained writing review before starting another.' : 'Review applies only to the exact inspected saved writing, not newer typing.');
    inspect.disabled = Boolean(reasonText || ownLocked || current?.previewPending || selecting);
    acknowledge.disabled = !current?.preview || Boolean(reasonText || ownLocked || current?.previewPending || selecting);
    record.disabled = acknowledge.disabled || acknowledge.checked !== true;
    replay.hidden = operation?.state !== 'uncertain'; replay.disabled = operation?.state !== 'uncertain' || operation.actionPending || selecting;
    handoff.hidden = operation?.state !== 'uncertain' || operation?.quarantined === true;
    handoff.disabled = operation?.state !== 'uncertain' || operation.actionPending || operation.quarantined || selecting;
    selection.hidden = operation?.state !== 'recorded' || !operation?.result;
    selection.disabled = operation?.state !== 'recorded' || !operation?.result || operation.actionPending || selecting;
    error.textContent = localError || current?.error || '';
  }
  function update(snapshot) {
    if (retired) return;
    current = snapshot; acknowledge.checked = false;
    if (snapshot.preview !== shownPreview) {
      // The token, not a pane-owned disclosure, is record authority.
      if (!snapshot.preview || shownPreview?.token !== snapshot.preview.token) {
        clearOwned(content);
        if (snapshot.preview) showDisclosure(snapshot.preview.envelope.disclosure);
      }
      shownPreview = snapshot.preview;
    }
    const op = snapshot.operation;
    let message = snapshot.previewPending ? 'Inspecting exact saved writing…' : snapshot.preview ? 'Read the complete exact writing, support and exclusions, then acknowledge separately.' : 'Inspect unchanged, eligible saved writing before acknowledging a review.';
    if (op?.state === 'sending') message = 'Recording the acknowledged saved writing review. Newer editor typing is not part of this request.';
    if (op?.state === 'uncertain') message = 'The writing review outcome is unresolved. An error is not proof of rollback. Only explicit exact replay can resolve this operation.';
    if (op?.state === 'recorded') message = `Writing review recorded for saved revision ${op.result.draft.id}. ${op.result.current ? 'Current saved revision.' : 'Historical saved revision.'} Writing only; no facts or browser actions authorized.${op.quarantined ? ' Read-only recovery remains quarantined.' : ' Select the recorded revision separately; your editor was not replaced.'}`;
    // Do not repeat an unchanged live-region announcement on lock synchronization.
    if (status.textContent !== message) status.textContent = message;
    clearOwned(resultContent);
    if (op?.quarantined) resultContent.append(text('Read-only handoff is retained in page memory. Closing or reloading loses recovery; inspection does not adopt writing or remove quarantine.', 'warning'));
    sync();
  }
  const controller = new ReviewController({api, owner, application, registry, getContext, beforeDispatch, onPublished, keyFactory, update});
  current = controller.snapshot(); update(current);
  inspect.onclick = async () => {
    if (retired) return;
    localError = ''; acknowledge.checked = false;
    if (blocked() || registry.isMutationLocked(owner, application)) { sync(); return; }
    await controller.preview();
  };
  acknowledge.onchange = () => { if (!retired) sync(); };
  record.onclick = async () => {
    if (retired) return;
    const token = current?.preview?.token, acknowledged = acknowledge.checked === true;
    localError = ''; acknowledge.checked = false; sync();
    if (!token || !acknowledged || blocked()) return;
    await controller.record(token, true);
  };
  replay.onclick = async () => {
    if (retired || current?.operation?.state !== 'uncertain' || current.operation.actionPending) return;
    localError = ''; acknowledge.checked = false; await controller.replayOriginal();
  };
  handoff.onclick = () => {
    if (retired || current?.operation?.state !== 'uncertain' || current.operation.actionPending || current.operation.quarantined) return;
    localError = ''; acknowledge.checked = false;
    try {
      if (confirmHandoff() !== true || retired) return;
      const ticket = captureWriting();
      if (!ticket || !controller.handoffUncertain(ticket, freezeWriting)) localError = 'Read-only handoff was not secured. Current typing and the unresolved operation must remain on this application.';
    } catch { localError = 'Read-only handoff failed; current writing has not been released for navigation.'; }
    sync();
  };
  selection.onclick = async () => {
    const operation = current?.operation;
    if (retired || selecting || operation?.state !== 'recorded' || !operation.result || operation.actionPending) return;
    selecting = true; localError = ''; sync();
    try { await selectResult(operation.result.draft.id, {readOnly: operation.quarantined === true}); }
    catch { if (!retired) localError = 'Recorded revision inspection failed. Current editor writing was not automatically replaced.'; }
    finally { selecting = false; if (!retired) sync(); }
  };
  return {
    element,
    invalidate() { if (retired) return; localError = ''; acknowledge.checked = false; controller.invalidate(); },
    syncLocks() {
      if (retired) return;
      if (current?.preview && blocked()) { acknowledge.checked = false; controller.invalidate(); return; }
      const next = controller.snapshot(), a = current?.operation, b = next.operation;
      if (next.preview?.token !== current?.preview?.token || next.previewPending !== current?.previewPending
        || next.error !== current?.error || a?.state !== b?.state || a?.quarantined !== b?.quarantined
        || a?.actionPending !== b?.actionPending || a?.result?.draft?.id !== b?.result?.draft?.id) update(next);
      else sync();
    },
    retire() { if (retired) return; retired = true; controller.retire(); current = null; shownPreview = null; clearOwned(element); },
  };
}
