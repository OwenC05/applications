const $ = (id) => document.getElementById(id);
let token = '', profile = '', epoch = 0, sources = [];
function notice(text) { $('notice').textContent = text; }
function node(tag, text, className) { const element = document.createElement(tag); if (text) element.textContent = text; if (className) element.className = className; return element; }
async function api(path, method = 'GET', body) {
  const headers = { 'x-evidence-token': token };
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(`/api/evidence${path}`, { method, headers, body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || 'Operation failed.');
  return data;
}
function path(tail = '') { if (!profile) throw new Error('Choose a profile first.'); return `/profiles/${profile}${tail}`; }
async function guarded(action) { try { await action(); } catch (error) { notice(error.message); } }
async function refresh() {
  const mark = epoch, id = profile;
  const [status, profiles] = await Promise.all([api('/status'), api('/profiles')]);
  if (mark !== epoch) return;
  token = status.token;
  $('profile').replaceChildren(new Option('Choose a profile', ''));
  for (const item of profiles) $('profile').append(new Option(`${item.name} · ${item.sectors.join(' / ')}`, item.id));
  $('profile').value = id;
  const prefix = 'uv run --directory backend python';
  $('commands').textContent = `${prefix} -m copilot.models setup\n${prefix} -m copilot index --profile ${id || 'PROFILE_ID'} --corpus facts\n${prefix} -m copilot index --profile ${id || 'PROFILE_ID'} --corpus documents`;
  notice(`${status.models_ready ? 'Models cached.' : 'Model setup required.'} Cloud disabled. ${status.pending_cleanup ? `${status.pending_cleanup} cleanup pending; run the cleanup command.` : ''}`);
  if (!id) { $('sources').replaceChildren(); $('facts').replaceChildren(); $('selection').replaceChildren(); return; }
  const [newSources, facts] = await Promise.all([api(path('/sources')), api(path('/facts'))]);
  if (mark !== epoch || id !== profile) return;
  sources = newSources;
  $('sources').replaceChildren(); $('selection').replaceChildren(); $('facts').replaceChildren();
  for (const source of sources) {
    const row = node('div', '', 'entry'), preview = node('button', `Inspect ${source.filename}`, 'secondary');
    preview.type = 'button';
    preview.onclick = () => guarded(async () => {
      const stamp = epoch, selected = profile;
      const units = await api(path(`/sources/${source.id}/units`));
      if (stamp !== epoch || selected !== profile) return;
      $('preview').textContent = units.map((unit) => `${unit.page ? `Page ${unit.page}` : 'Canonical text'}\n${unit.text}`).join('\n\n');
    });
    const remove = node('button', 'Delete source', 'secondary'); remove.type = 'button';
    remove.onclick = () => guarded(async () => {
      if (!confirm('Delete this source and revoke linked facts? Cleanup is application-level, not forensic erasure.')) return;
      const stamp = epoch, result = await api(path(`/sources/${source.id}`), 'DELETE');
      if (stamp !== epoch) return; $('preview').textContent = ''; $('results').replaceChildren(); $('inspection').hidden = true;
      await refresh(); notice(`Source access revoked. Cleanup ${result.state}.`);
    });
    row.append(preview, remove); $('sources').append(row);
    const label = node('label'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.value = source.id;
    label.append(checkbox, document.createTextNode(` ${source.filename}`)); $('selection').append(label);
  }
  for (const fact of facts.filter((item) => item.state === 'active')) {
    const row = node('div', '', 'entry'); row.append(node('p', fact.text));
    const remove = node('button', 'Remove reusable fact', 'secondary'); remove.type = 'button';
    remove.onclick = () => guarded(async () => {
      if (!confirm('Remove this reusable fact? The raw document, if any, remains available to document search.')) return;
      const stamp = epoch, result = await api(path(`/facts/${fact.id}`), 'DELETE');
      if (stamp !== epoch) return; $('results').replaceChildren(); $('inspection').hidden = true;
      await refresh(); notice(`Fact access revoked. Cleanup ${result.state}.`);
    }); row.append(remove); $('facts').append(row);
  }
}
$('profile').onchange = () => { epoch++; profile = $('profile').value; $('preview').textContent = ''; $('results').replaceChildren(); $('inspection').hidden = true; guarded(refresh); };
$('refresh').onclick = () => guarded(refresh);
$('create').onsubmit = (event) => { event.preventDefault(); guarded(async () => {
  const values = new FormData(event.target), sector = values.get('sector');
  const created = await api('/profiles', 'POST', { name: values.get('name'), sectors: sector === 'both' ? ['tech', 'finance'] : [sector] });
  epoch++; profile = created.id; event.target.reset(); await refresh();
}); };
$('upload').onsubmit = (event) => { event.preventDefault(); guarded(async () => {
  const stamp = epoch; await api(path('/sources'), 'POST', new FormData(event.target));
  if (stamp !== epoch) return; event.target.reset(); await refresh(); notice('Imported as raw evidence, not confirmed facts. Rebuild document indexes.');
}); };
$('fact').onsubmit = (event) => { event.preventDefault(); guarded(async () => {
  const values = new FormData(event.target), stamp = epoch;
  await api(path('/facts'), 'POST', { text: values.get('text'), confirmed: values.get('confirmed') === 'on' });
  if (stamp !== epoch) return; event.target.reset(); await refresh(); notice('Fact confirmed. Rebuild indexes before retrieval.');
}); };
$('search').onsubmit = (event) => { event.preventDefault(); guarded(async () => {
  const values = new FormData(event.target), stamp = epoch, id = profile, corpus = values.get('corpus');
  const source_ids = corpus === 'documents' ? [...$('selection').querySelectorAll('input:checked')].map((item) => item.value) : null;
  if (corpus === 'documents' && !source_ids.length) throw new Error('Select at least one raw document.');
  notice('Searching local evidence…'); $('results').replaceChildren(); $('inspection').hidden = true;
  const data = await api(path('/search'), 'POST', { corpus, query: values.get('query'), source_ids });
  if (stamp !== epoch || id !== profile) return;
  for (const hit of data.hits) {
    const row = node('div', '', 'entry'); row.append(node('blockquote', hit.citation.excerpt));
    const button = node('button', 'Inspect citation', 'secondary'); button.type = 'button';
    button.onclick = () => guarded(async () => {
      const selected = epoch; const { profile_id: ignored, ...citation } = hit.citation; void ignored;
      const resolved = await api(path('/citations'), 'POST', citation);
      if (selected !== epoch || id !== profile) return;
      const highlight = node('mark', resolved.excerpt);
      const canonical = Array.from(resolved.canonical_text);
      $('citation').replaceChildren(document.createTextNode(`Integrity: ${resolved.integrity}; semantic support: ${resolved.semantic_support}\n${resolved.page ? `Physical PDF page ${resolved.page}\n` : ''}\n${canonical.slice(0, resolved.start).join('')}`), highlight, document.createTextNode(canonical.slice(resolved.end).join('')));
      $('inspection').hidden = false;
    }); row.append(button); $('results').append(row);
  }
  notice(data.hits.length ? `${data.hits.length} exact quotations. Integrity is not independent truth verification.` : 'No selected eligible evidence. This is not a semantic answerability assessment.');
}); };
$('delete-profile').onclick = () => guarded(async () => {
  if (!confirm('Delete the selected profile and its evidence? External exports/backups and forensic traces are outside this cleanup.')) return;
  const stamp = epoch, result = await api(path(), 'DELETE'); if (stamp !== epoch) return;
  epoch++; profile = ''; $('preview').textContent = ''; $('results').replaceChildren(); $('inspection').hidden = true;
  await refresh(); notice(`Profile access revoked. Cleanup ${result.state}.`);
});
guarded(refresh);
