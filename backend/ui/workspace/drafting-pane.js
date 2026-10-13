import {DraftingController, DraftBuffer} from './drafting.js';
import {node, section, text} from './dom.js';

const copy = value => structuredClone(value);
const normalized = value => Array.isArray(value) ? value.map(normalized) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, normalized(value[key])])) : value;
const same = (left, right) => JSON.stringify(normalized(left)) === JSON.stringify(normalized(right));
const exactKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && same(Object.keys(value).sort(), [...keys].sort());
const json = value => JSON.stringify(value ?? null, null, 2);
const button = (label, action) => {
  const result = node('button', label, 'secondary'); result.type = 'button'; result.onclick = action; return result;
};
const option = (id, label) => { const result = node('option', label); result.value = id; return result; };

// Saved offsets are Python/Unicode code points, never textarea UTF-16 offsets.
export function savedSpan(draft, span) {
  const value = span.target === 'cover_letter' ? draft.cover_letter : draft.answers?.find(answer => answer.question_id === span.target)?.text;
  if (typeof value !== 'string' || !Number.isInteger(span.start) || !Number.isInteger(span.end)
    || span.start < 0 || span.end <= span.start || span.end > Array.from(value).length) return null;
  const excerpt = Array.from(value).slice(span.start, span.end).join('');
  return excerpt === span.excerpt ? excerpt : null;
}
export function writingWarning(question, value) {
  const points = Array.from(value).length, words = value.trim() ? value.trim().split(/\s+/u).length : 0;
  const warnings = [];
  if (question.required && !value.trim()) warnings.push('Required writing is blank.');
  if (question.max_chars != null && points > question.max_chars) warnings.push('Character limit exceeded.');
  if (question.max_words != null && words > question.max_words) warnings.push('Word limit exceeded.');
  return `${points} Unicode characters (code points) · ${words} words. ${warnings.join(' ')} Local save remains available; this is not application readiness.`;
}

// Controls and editor nodes survive unrelated refreshes. Only an intentional
// selection/adoption replaces the baseline; a save acknowledgement never does.
export function draftingPane({api, exportText, owner, application, edits, notify = () => {}, confirmDiscard = globalThis.confirm,
  keyFactory = () => crypto.randomUUID(), onPublished = () => {}, recovery = null, onHandoff = () => false, onCaptureChange = () => {}, isMutationLocked = () => false}) {
  let retired = false, inspection = null, buffer = null, saving = false, pending = null, savedChild = null, uncertain = false;
  let selectedDraft = '', selectedPacket = '', ownDirty = false, handedOff = false;
  let epoch = 0, packetInspection = null, observer = onCaptureChange;
  function capture() {
    return {epoch, packetInspection: retired ? null : copy(packetInspection),
      draftInspection: retired || uncertain || handedOff || savedChild || !buffer ? null : copy(inspection),
      bufferMatchesSaved: !retired && buffer?.matchesSaved() === true,
      saving, uncertain, quarantined: handedOff, retired};
  }
  function changed() {
    epoch++;
    try { observer?.(capture()); } catch { /* Inspection observers cannot interrupt local writing or privacy cleanup. */ }
  }
  const controls = new Map();
  const pane = section('Saved evidence and application writing', text('Local saved inspection, immutable editing and saved-text export. Separately acknowledged generation and unchanged saved-edit assessment are below; no review action, autofill or submission is connected.', 'warning'));
  const packetStatus = text('Refresh to inspect saved packets.'), draftStatus = text('Refresh to inspect saved drafts.'), editStatus = text(''), exportStatus = text('');
  for (const status of [packetStatus, draftStatus, editStatus, exportStatus]) { status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); }
  const packetSelect = node('select'), questionSelect = node('select'), draftSelect = node('select');
  const labelled = (title, control) => { const label = node('label'); label.append(node('span', title), control); return label; };
  const packetInfo = node('pre', 'No packet selected.'), questionInfo = node('div'), draftInfo = node('div'), savedPreview = node('div'), ledger = node('div');
  const form = node('form'), editor = node('div'); form.onsubmit = event => event.preventDefault();
  const save = button('Save new local revision', () => submit(false));
  const resolve = button('Resolve saved edit outcome', () => submit(true));
  const handoff = button('Continue read-only with unresolved outcome', () => {
    if (retired || saving || !uncertain || handedOff || !pending || !buffer) return;
    if (!confirmDiscard('Continue read-only after retaining the exact unresolved operation and current writing in memory? This does not establish rollback or non-commit. Reloading or closing this page loses this recovery; only the same captured operation may be replayed.')) return;
    const capsule = {operation: {parentDraftId: pending.parentCapture.draft.id, parentCapture: copy(pending.parentCapture), body: copy(pending.body)}, targets: buffer.targets()};
    let retained = false;
    try { retained = onHandoff(copy(capsule)) === true; } catch { /* Failed retention grants no navigation or mutation authority. */ }
    if (!retained) { editStatus.textContent = 'Recovery was not retained. The outcome remains unresolved; all writing and the exact operation are unchanged.'; return; }
    handedOff = true;
    for (const {input} of controls.values()) input.disabled = true;
    editStatus.textContent = 'Recovery retained in memory. This application stays read-only, including after a correlated replay. No rollback or non-commit is inferred; reload/close loses recovery.';
    sync();
  });
  const adopt = button('Inspect/adopt saved revision', () => {
    if (retired || isMutationLocked() || !savedChild || saving || !discard()) return;
    const child = savedChild;
    savedChild = null; pending = null; uncertain = false;
    controller.adoptDraft(child.draft.id); selectedDraft = child.draft.id;
    if (![...draftSelect.children].some(item => item.value === selectedDraft)) draftSelect.append(option(selectedDraft, `${selectedDraft} · saved local revision`));
    draftSelect.value = selectedDraft; renderDraft(child); sync();
  });
  const discardButton = button('Discard this writing buffer and inspect selected saved revision', () => {
    if (saving || !discard()) return;
    pending = null; uncertain = false; savedChild = null;
    if (inspection) renderDraft(inspection);
    if (selectedDraft) controller.draft(selectedDraft);
    sync();
  });
  const download = button('Export SAVED revision (.txt)', () => {
    if (!selectedDraft || retired || saving) return;
    exportStatus.textContent = ownDirty ? 'Exporting the SAVED revision, not your unsaved writing. Downloads are outside managed forgetting.' : 'Exporting saved text. Downloads are outside managed forgetting.';
    controller.exportDraft(selectedDraft);
  });
  function discard() {
    // An unresolved commit cannot be abandoned into a new logical save. Only
    // exact-operation reconciliation can restore mutation authority.
    if (uncertain || handedOff) {
      editStatus.textContent = 'Resolve the saved edit outcome before discarding, adopting or switching drafts. The exact captured operation and all newer typing are retained.';
      return false;
    }
    if ((ownDirty || uncertain || pending) && !confirmDiscard('Discard only this writing buffer? A save may already have occurred. Other forms are unchanged.')) return false;
    edits.acknowledge(edits.capture(form)); ownDirty = false; return true;
  }
  function sync(emitCapture = true) {
    save.disabled = retired || isMutationLocked() || handedOff || saving || uncertain || Boolean(savedChild) || !buffer || inspection?.current !== true || !ownDirty;
    resolve.disabled = retired || saving || !uncertain || !pending;
    adopt.disabled = retired || isMutationLocked() || handedOff || saving || uncertain || !savedChild;
    discardButton.disabled = retired || handedOff || saving || uncertain || (!ownDirty && !uncertain && !savedChild);
    download.disabled = retired || saving || !selectedDraft;
    draftSelect.disabled = retired || saving || (!selectedDraft && draftSelect.children.length <= 1);
    handoff.disabled = retired || saving || !uncertain || handedOff || !pending || !buffer;
    if (emitCapture) changed();
  }
  function assessmentNotice() {
    if (!inspection) return '';
    if (ownDirty || saving || uncertain || savedChild) return 'Unsaved or unresolved writing: the saved assessment does not apply to this editor.';
    return `Saved assessment: ${inspection.draft.assessment_state}. Inventory complete: ${inspection.draft.inventory_complete}. Automated support is fallible, not release qualified. Any writing acknowledgement applies only to its exact saved revision, not facts, manual answers, attachments or browser authorization.`;
  }
  function updateCounters() {
    for (const target of inspection?.writing_targets ?? []) {
      const entry = controls.get(target.question.id);
      if (entry) entry.counter.textContent = writingWarning(target.question, buffer.targets().find(item => item.target === target.question.id).text);
    }
  }
  function renderSaved(data) {
    draftInfo.replaceChildren(text(`Revision ${data.draft.id} · server-current at last inspection: ${data.current === true}. Currentness is not semantic support.`),
      text(`Saved assessment: ${data.draft.assessment_state} · inventory complete: ${data.draft.inventory_complete}. Exact saved writing only; automated support is fallible. No fact confirmation, manual-answer or attachment review, or browser authorization.`),
      text(`Reasons: ${(data.reason_codes ?? []).join(', ') || 'none recorded'}`), text(`Editorial notes: ${(data.publication?.style_notes ?? []).join('; ') || 'none recorded'}`));
    savedPreview.replaceChildren(node('h3', 'Saved text — separate from unsaved writing'));
    for (const answer of data.draft.answers ?? []) savedPreview.append(node('h4', answer.question_id), node('pre', answer.text));
    if (data.writing_targets?.some(target => target.question.id === 'cover_letter')) savedPreview.append(node('h4', 'Cover letter'), node('pre', data.draft.cover_letter));
    ledger.replaceChildren(node('h3', 'Saved support ledger — offsets do not apply to unsaved writing'));
    for (const span of data.draft.ledger ?? []) {
      const excerpt = savedSpan(data.draft, span);
      const entry = section(`${span.target} · ${span.kind} · ${span.assessment}`, text(`Code-point range [${span.start}, ${span.end}) · integrity: ${span.integrity}. Integrity is not entailment.`));
      entry.append(excerpt === null ? text('Warning: saved span does not match the exact saved text.', 'warning') : node('pre', excerpt), node('pre', json(span.citations)));
      ledger.append(entry);
    }
    if (!data.draft.ledger?.length) ledger.append(text('No saved support spans.'));
    ledger.append(text('Manual requirements are unresolved here.'), node('pre', json(data.manual_requirements)));
  }
  function renderDraft(data) {
    buffer = null; controls.clear(); editor.replaceChildren();
    if (!data?.draft || data.draft.profile_id !== owner || data.draft.application_id !== application) {
      inspection = null; draftInfo.replaceChildren(text('Saved inspection has invalid application ownership. No editor authority is available.', 'warning'));
      savedPreview.replaceChildren(); ledger.replaceChildren(); sync(); return;
    }
    inspection = copy(data); renderSaved(data);
    try { buffer = new DraftBuffer(data); }
    catch { editStatus.textContent = 'Authoritative writing targets or bindings are invalid/missing. Editing is disabled; saved text remains inspectable.'; sync(); return; }
    for (const target of inspection.writing_targets) {
      const label = node('label'), input = node('textarea'), counter = text('', 'muted');
      input.rows = 7; input.value = buffer.targets().find(item => item.target === target.question.id).text;
      input.disabled = handedOff || data.current !== true;
      label.append(node('span', target.question.text), input, counter); editor.append(label);
      controls.set(target.question.id, {input, counter});
      const onInput = () => {
        if (retired || handedOff) return;
        buffer.input(target.question.id, input.value); edits.changed(form); ownDirty = true;
        if (buffer.matchesSaved() && !saving && !uncertain && !savedChild) { edits.acknowledge(edits.capture(form)); ownDirty = false; }
        editStatus.textContent = assessmentNotice(); updateCounters(); sync();
      };
      controls.get(target.question.id).onInput = onInput;
      input.addEventListener('input', onInput);
    }
    updateCounters(); editStatus.textContent = assessmentNotice(); sync();
  }
  async function submit(replay) {
    if (retired || saving || (!replay && isMutationLocked()) || (replay ? !uncertain || !pending : save.disabled)) return;
    if (!replay) {
      pending = {parentCapture: copy(inspection), body: buffer.editRequest(keyFactory()), generation: edits.capture(form)};
    }
    if (replay && handedOff) {
      // History inspection has its own selection; replay always returns the
      // controller to the immutable original parent without rebasing writing.
      selectedDraft = pending.parentCapture.draft.id;
      if (![...draftSelect.children].some(item => item.value === selectedDraft)) draftSelect.append(option(selectedDraft, `${selectedDraft} · retained recovery parent`));
      draftSelect.value = selectedDraft; controller.adoptDraft(selectedDraft);
    }
    saving = true; editStatus.textContent = 'Saving captured writing. New typing is preserved; no success is claimed yet.'; sync();
    await controller.saveEdit(pending.parentCapture, pending.body);
    // A retired/selection-suppressed request intentionally gives no UI update.
  }
  function setList(select, entries, id, label, chosen) {
    select.replaceChildren(option('', label));
    for (const entry of entries) select.append(option(id(entry), `${id(entry)} · current at last inspection: ${entry.current === true}`));
    // Never silently select the newest revision or erase an active selection.
    if (chosen && !entries.some(entry => id(entry) === chosen)) select.append(option(chosen, `${chosen} · retained selection`));
    select.value = chosen; select.disabled = !entries.length && !chosen;
  }
  const controller = new DraftingController({api, exportText, owner, application, update(view, data, error) {
    if (retired) return;
    if (view === 'edit') {
      saving = false;
      if (error) {
        uncertain = true;
        editStatus.textContent = `Save outcome unresolved${error.status ? ` (HTTP ${error.status})` : ''}: ${error.message}. A revision may already have been saved. Your writing is retained. Resolve only the exact captured operation; no automatic retry or rollback is claimed.`;
        sync(); return;
      }
      savedChild = copy(data.inspection); uncertain = false;
      onPublished(data.published);
      if (!handedOff && edits.acknowledge(pending.generation)) {
        ownDirty = false; inspection = copy(savedChild); buffer = new DraftBuffer(inspection);
        selectedDraft = inspection.draft.id; controller.adoptDraft(selectedDraft);
        if (![...draftSelect.children].some(item => item.value === selectedDraft)) draftSelect.append(option(selectedDraft, `${selectedDraft} · saved local revision`));
        draftSelect.value = selectedDraft;
        // Do not replace textarea nodes/value: preserves current focus and caret.
        for (const {input} of controls.values()) input.disabled = inspection.current !== true;
        renderSaved(inspection); savedChild = null; pending = null;
        editStatus.textContent = `${assessmentNotice()} New local revision saved; it is unassessed.`;
      } else {
        ownDirty = true; renderSaved(savedChild);
        editStatus.textContent = handedOff
          ? `Reconciled captured revision ${savedChild.draft.id}; retained writing is unchanged. This application is still read-only; no new save or baseline adoption is enabled.`
          : `Captured writing saved as ${savedChild.draft.id}; newer typing is retained on the old baseline. Inspect/adopt explicitly before another save.`;
      }
      sync(); return;
    }
    if (error) {
      const status = view.startsWith('packet') ? packetStatus : view === 'export' ? exportStatus : draftStatus;
      status.textContent = `Inspection failed: ${error.message}. No success is claimed; refresh explicitly.`; return;
    }
    if (view === 'packet-list') setList(packetSelect, data.batches ?? [], item => item.batch.id, 'Choose saved packet batch', selectedPacket);
    if (view === 'draft-list') { setList(draftSelect, data.drafts ?? [], item => item.draft.id, 'Choose saved draft revision', selectedDraft); sync(); }
    if (view === 'packet-detail') {
      if (data?.batch?.id !== selectedPacket || data.batch.profile_id !== owner || data.batch.application_id !== application) {
        packetInspection = null; packetStatus.textContent = 'Packet inspection does not match the selected application batch.';
        changed(); return;
      }
      packetInspection = copy(data);
      packetStatus.textContent = `Server-current at last inspection: ${data.current === true}. Semantic support: ${data.semantic_support}. Stored integrity is not semantic support.`;
      packetInfo.textContent = json({reasons: data.reason_codes, manual_requirements: data.batch.manual_requirements, hiring_criteria: data.batch.hiring_criteria, note: 'Criteria may be inferred employer priorities, not personal facts or hiring-manager knowledge.'});
      questionSelect.replaceChildren(option('', 'Choose canonical question'));
      for (const question of [...data.batch.canonical_questions, ...(data.batch.cover_letter_target ? [data.batch.cover_letter_target] : [])]) questionSelect.append(option(question.id, question.text));
      questionSelect.disabled = false;
      changed();
    }
    if (view === 'packet-question') {
      questionInfo.replaceChildren(node('h3', data.question.text), node('pre', json(data.question)), text('Personal evidence, employer evidence, inference and gaps remain distinct. Stored excerpts/IDs are literal, not verified semantic support.'), node('h4', 'Personal evidence — confirmed-fact references'), node('pre', json(data.packet?.facts ?? [])), node('h4', 'Employer evidence — separate corpus'), node('pre', json(data.packet?.employer ?? [])), node('h4', 'Criteria / inferred priorities (IDs, not personal facts)'), node('pre', json(data.packet?.criteria ?? [])), node('h4', 'Gaps and omissions'), node('pre', json({gaps: data.packet?.gaps ?? [], omitted: data.packet?.omitted ?? [], truncated: data.packet?.truncated})), text('Manual requirement'), node('pre', json(data.manual_requirement)));
    }
    if (view === 'draft-detail') {
      try {
        if (data?.draft?.id !== selectedDraft || data.draft.profile_id !== owner || data.draft.application_id !== application) throw new Error('Saved inspection does not match the selected application revision.');
        new DraftBuffer(data);
      } catch {
        draftStatus.textContent = 'Saved inspection is malformed or does not match the selected revision. It cannot be adopted or supply editor authority; existing writing is unchanged.';
        sync(); return;
      }
      if (ownDirty || saving || uncertain || savedChild || handedOff) {
        savedChild = copy(data); renderSaved(savedChild);
        draftStatus.textContent = 'Refreshed saved inspection is available separately. Your writing and baseline are unchanged; explicit discard/adoption is required.';
        sync(); return;
      }
      renderDraft(data);
    }
    if (view === 'export') {
      const blob = new Blob([data], {type: 'text/plain;charset=utf-8'}), url = URL.createObjectURL(blob), anchor = node('a', 'Saved text download');
      try { anchor.href = url; anchor.download = 'application-draft.txt'; pane.append(anchor); anchor.click(); }
      finally { anchor.remove(); URL.revokeObjectURL(url); }
      exportStatus.textContent = 'Saved revision downloaded, not unsaved writing. Downloads are outside managed forgetting.';
    }
  }});
  packetSelect.onchange = () => {
    selectedPacket = packetSelect.value; questionInfo.replaceChildren(); packetInfo.textContent = 'No packet loaded.';
    packetInspection = null; changed();
    questionSelect.replaceChildren(); questionSelect.disabled = true; controller.packet(selectedPacket);
  };
  questionSelect.onchange = () => { questionInfo.replaceChildren(); controller.packetQuestion(selectedPacket, questionSelect.value); };
  draftSelect.onchange = () => {
    if (saving || (!handedOff && !discard())) { draftSelect.value = selectedDraft; return; }
    if (handedOff) {
      selectedDraft = draftSelect.value;
      controller.draft(selectedDraft); sync(); return;
    }
    selectedDraft = draftSelect.value; inspection = null; buffer = null; pending = null; savedChild = null; uncertain = false;
    editor.replaceChildren(); savedPreview.replaceChildren(); ledger.replaceChildren(); draftInfo.replaceChildren();
    controller.draft(selectedDraft); sync();
  };
  for (const select of [packetSelect, questionSelect, draftSelect]) select.disabled = true;
  const packets = section('Saved question evidence', button('Refresh saved packet list', () => { packetInspection = null; changed(); controller.packets(); }), labelled('Saved packet batch', packetSelect),
    button('Refresh selected packet inspection', () => { packetInspection = null; changed(); if (selectedPacket) controller.packet(selectedPacket); }), packetStatus, packetInfo, labelled('Canonical packet question', questionSelect), questionInfo);
  const drafts = section('Saved application writing', button('Refresh saved draft list', () => controller.drafts()), labelled('Saved draft revision', draftSelect), button('Refresh selected saved inspection', () => { changed(); if (selectedDraft) controller.draft(selectedDraft); }), draftStatus, draftInfo);
  form.append(editor, editStatus, save, resolve, handoff, adopt, discardButton);
  drafts.append(form, download, exportStatus, savedPreview, ledger); pane.append(packets, drafts); sync();
  if (recovery !== null) {
    handedOff = true;
    try {
      const capsule = copy(recovery), operation = capsule.operation;
      if (!exactKeys(capsule, ['operation', 'targets']) || !exactKeys(operation, ['parentDraftId', 'parentCapture', 'body'])
        || operation.parentCapture?.draft?.profile_id !== owner || operation.parentCapture.draft.application_id !== application
        || operation.parentDraftId !== operation.parentCapture.draft.id) throw new Error('Invalid recovery scope');
      const captured = new DraftBuffer(operation.parentCapture);
      for (const target of operation.body.draft.targets) captured.input(target.target, target.text);
      if (!same(captured.editRequest(operation.body.idempotency_key), operation.body)) throw new Error('Invalid recovery operation');
      const current = new DraftBuffer(operation.parentCapture), targetIds = current.targets().map(item => item.target);
      if (!Array.isArray(capsule.targets) || !same(capsule.targets.map(item => item.target), targetIds)
        || !capsule.targets.every(item => exactKeys(item, ['target', 'text']) && typeof item.text === 'string')) throw new Error('Invalid recovery writing');
      for (const target of capsule.targets) current.input(target.target, target.text);
      selectedDraft = operation.parentDraftId; controller.adoptDraft(selectedDraft);
      renderDraft(operation.parentCapture); buffer = current;
      for (const target of current.targets()) controls.get(target.target).input.value = target.text;
      ownDirty = !current.matchesSaved();
      pending = {parentCapture: copy(operation.parentCapture), body: copy(operation.body), generation: null}; uncertain = true;
      draftSelect.append(option(selectedDraft, `${selectedDraft} · retained read-only recovery`)); draftSelect.value = selectedDraft; draftSelect.disabled = false;
      updateCounters();
      editStatus.textContent = 'Retained read-only recovery. Captured writing remains available even if saved inspection is stale. Resolve only the exact captured operation; no automatic request, rollback or non-commit is inferred. Reload/close loses recovery.';
    } catch {
      inspection = null; buffer = null; pending = null; uncertain = false;
      editor.replaceChildren(); savedPreview.replaceChildren(); ledger.replaceChildren(); draftInfo.replaceChildren();
      editStatus.textContent = 'Invalid recovery was rejected. No captured private writing or mutation authority is available.';
    }
    sync();
  }
  async function selectSavedResult(id) {
    if (retired || saving || uncertain || handedOff || typeof id !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id) || !discard()) return false;
    selectedDraft = id; controller.adoptDraft(id);
    if (![...draftSelect.children].some(item => item.value === id)) draftSelect.append(option(id, `${id} · explicitly selected saved result`));
    draftSelect.value = id; inspection = null; buffer = null; pending = null; savedChild = null;
    editor.replaceChildren(); savedPreview.replaceChildren(); ledger.replaceChildren(); draftInfo.replaceChildren(); sync();
    await controller.draft(id);
    return !retired && selectedDraft === id && inspection?.draft?.id === id && buffer !== null;
  }
  return {element: pane, capture, syncLocks() { sync(false); }, selectSavedResult, get unresolved() { return saving || (uncertain && !handedOff); }, retire() {
    if (retired) return;
    retired = true; controller.retire();
    for (const {input, onInput} of controls.values()) {
      input.removeEventListener('input', onInput); input.value = ''; input.disabled = true;
    }
    controls.clear(); inspection = null; buffer = null; pending = null; savedChild = null; recovery = null;
    saving = false; uncertain = false; ownDirty = false; selectedDraft = ''; selectedPacket = '';
    packetInspection = null; changed(); observer = null;
    edits.forget(form);
    // Clear detached controls and text as well as the visible subtree. A retained
    // old element must not expose prior-owner prose after successful forgetting.
    const clear = element => {
      for (const child of [...element.children]) clear(child);
      if ('value' in element) element.value = '';
      element.onclick = null; element.onchange = null; element.onsubmit = null;
      element.textContent = '';
    };
    clear(pane); pane.append(text('Writing pane retired.'));
  }};
}
