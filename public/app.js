const $ = (s, root = document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ( {
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}
[c]));
const state = {
  status: null, profiles: [], profile: null, interview: null, busy: false, sector: 'all', contextVersion: 0, followupRequest: 0
};
const navigation = [['overview', '◈', 'Overview'], ['opportunities', '⌕', 'Opportunities'], ['interview', '◌', 'Interview'], ['brain', '◇', 'Brain'], ['applications', '▤', 'Applications'], ['settings', '⚙', 'Settings']];
const badge = (text, kind = '') => `<span class="badge ${esc(kind)}">${esc(text)}</span>`;
const btn = (label, action, id = '', cls = '') => `<button type="button" class="${cls}" data-action="${action}" data-id="${esc(id)}" ${state.busy?'disabled':''}>${esc(label)}</button>`;
const path = suffix => `/api/profiles/${encodeURIComponent(state.profile.id)}${suffix}`;
const date = value => value ? new Date(value).toLocaleString(): 'Not retrieved';
const safeUrl = value => {
  try {
    const u = new URL(value);
    return['https:', 'http:'].includes(u.protocol) ? u.href: null;
  } catch {
    return null;
  }
};
const link = (url, title) => safeUrl(url) ? `<a href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(title || url)} ↗</a>`: esc(title || url);
const memoryFacts = () => state.profile.memories.filter(m => m.status === 'verified');
function notify(message, error = false) {
  const el = $('#notice');
  el.textContent = message;
  el.className = error ? 'error': '';
}
async function api(url, method = 'GET', body) {
  if (method === 'DELETE' && body === undefined) body = {
  };
  const headers = {
    'X-Copilot-Token': state.status?.token || ''
  };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(url, {
    method, headers, body: body === undefined ? undefined: JSON.stringify(body)
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.message || data.error || `Request failed (${r.status})`);
  return data;
}
async function loadProfiles(preferred) {
  state.profiles = await api('/api/profiles');
  const id = preferred !== undefined ? preferred: state.profile?.id || localStorage.getItem('copilot-profile');
  state.profile = id && state.profiles.some(p => p.id === id) ? await api(`/api/profiles/${encodeURIComponent(id)}`): null;
  if (state.profile) localStorage.setItem('copilot-profile', state.profile.id);
  else localStorage.removeItem('copilot-profile');
  state.interview = null;
}
async function mutate(url, method, body, message) {
  await task(async() => {
    const result = await api(url, method, body);
    if (result?.id && result.memories) state.profile = result;
    notify(message);
  });
}
async function task(fn) {
  if (state.busy) return;
  state.busy = true;
  $('#profile-select').disabled = true;
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    await fn();
  } catch (error) {
    notify(error.message, true);
  } finally {
    state.busy = false;
    $('#profile-select').disabled = false;
    await render();
  }
}
function title(eyebrow, heading, intro) {
  return `<p class="eyebrow">${esc(eyebrow)}</p>
      <h1>${esc(heading)}</h1>
      <p class="intro">${esc(intro)}</p>`;
}
function profileCreation() {
  return `<section class="grid two">
      <form id="create-profile" class="card">
      <h2>Create your private workspace</h2>
      <p class="muted">Start blank. Your own answers become evidence only after you confirm them.</p>
      <label>Name<input name="name" required maxlength="100" placeholder="Your name or workspace name" autocomplete="off">
      </label>
      <div class="section-gap">
      <label>Application focus<select name="sectors">
      <option value="tech,finance">Technology & finance</option>
      <option value="tech">Technology</option>
      <option value="finance">Finance</option>
      </select>
      </label>
      </div>
      <button class="primary section-gap" type="submit">Create profile</button>
      </form>
      <section class="card">
      <h2>Explore without personal data</h2>
      <p class="muted">The demo creates a separate, clearly marked synthetic profile with example technology and finance opportunities. It never adds facts to your real profile.</p>${btn('Open synthetic demo','demo','','') }<div class="warning">Profiles separate your data inside this app; they are not accounts or protection from another user of this computer.</div>
      </section>
      </section>`;
}
function overview() {
  const p = state.profile;
  if (!p) return title('A BETTER START', 'Make your experience count.', 'A private workspace for considered applications—not an application spam engine.') + profileCreation();
  const facts = memoryFacts().length, pending = p.memories.filter(m => m.status === 'pending').length, apps = p.applications.length;
  return title('YOUR APPLICATION WORKSPACE', `Hello, ${p.name}.`, 'Know your story. Understand the employer. Put the two together—without inventing either.') + `<section class="hero card">
      <p class="eyebrow">THE NEXT GOOD STEP</p>
      <h2>${facts?'Turn real evidence into a thoughtful application.':'Build the foundation: your story, in your words.'}</h2>
      <p>Deep onboarding is designed for roughly an hour, but you can save, skip, stop early and come back whenever you like.</p>
      <div class="actions">
      <a class="button primary" href="#interview">${p.interview.answers.length?'Resume your interview':'Start your interview'}</a>
      <a class="button" href="#brain">Review ${pending} pending facts</a>
      </div>
      </section>
      <section class="grid section-gap">
      <div class="card">
      <p class="muted">Verified evidence</p>
      <div class="stat">${facts}</div>
      <p class="meta">Only confirmed facts inform drafts.</p>
      </div>
      <div class="card">
      <p class="muted">Opportunities saved</p>
      <div class="stat">${apps}</div>
      <p class="meta">Tech and finance, side by side.</p>
      </div>
      <div class="card">
      <p class="muted">Marked submitted by you</p>
      <div class="stat">${p.applications.filter(a=>a.status==='submitted').length}</div>
      <p class="meta">The app never submits externally.</p>
      </div>
      </section>
      <section class="grid two section-gap">
      <div class="card">
      <h2>From evidence to application</h2>
      <ol>
      <li>Tell your story and confirm new facts.</li>
      <li>Add the role and its actual questions.</li>
      <li>Research public company and role sources.</li>
      <li>Edit, review and submit yourself.</li>
      </ol>
      <a href="#opportunities">Find your next opportunity →</a>
      </div>
      <div class="card">
      <h2>A brain you control</h2>
      <p class="muted">Application edits may suggest new facts, but nothing quietly becomes true. Every proposal has a source and needs your approval.</p>
      <div class="pill-list">${badge(p.cloudConsent?'Cloud allowed on request':'Cloud off',p.cloudConsent?'verified':'offline')}${badge(p.sectors.join(' + '))}</div>
      <a href="#settings">Privacy & provider settings →</a>
      </div>
      </section>`;
}
function questionRow() {
  return `<div class="question-row">
      <label>Application question<textarea name="questionText" placeholder="Paste the exact question" required>
      </textarea>
      </label>
      <label>Word limit<input name="maxWords" type="number" min="1" max="20000" placeholder="None">
      </label>
      <label>Char limit<input name="maxChars" type="number" min="1" max="20000" placeholder="None">
      </label>
      <button type="button" data-action="remove-question" aria-label="Remove question">Remove</button>
      </div>`;
}
function opportunityForm() {
  return `<details class="card" ${state.profile.applications.length?'':'open'}>
      <summary>Add an opportunity · tech or finance</summary>
      <form id="create-application" class="section-gap">
      <div class="form-grid">
      <label>Company<input name="company" required maxlength="200" placeholder="Employer name">
      </label>
      <label>Role<input name="role" required maxlength="200" placeholder="Summer analyst / Software intern">
      </label>
      <label>Sector<select name="sector">
      <option value="tech">Technology</option>
      <option value="finance">Finance</option>
      </select>
      </label>
      <label>Location<input name="location" maxlength="200" placeholder="London / remote">
      </label>
      <label>Application / official role URL<input name="url" type="url" placeholder="https://…">
      </label>
      <label>Official company URL<input name="companyUrl" type="url" placeholder="https://company.example">
      </label>
      <label class="wide">Actual job description<textarea name="jobDescription" required rows="6" placeholder="Paste the employer’s role description, requirements and responsibilities.">
      </textarea>
      </label>
      </div>
      <label class="check section-gap">
      <input name="officialDomain" type="checkbox" required>
      <span>I have checked that the company URL, if provided, is the employer’s official domain. Research uses public employer/ATS sources, not private hiring-manager information.</span>
      </label>
      <h3 class="section-gap">Actual application questions</h3>
      <p class="meta">Add the questions you need to answer; include limits exactly as shown. A cover letter is drafted separately.</p>
      <div id="question-rows">
      </div>${btn('Add a question','add-question')}<div class="actions">
      <button class="primary" type="submit">Save opportunity</button>
      </div>
      </form>
      </details>`;
}
function appCard(a) {
  return `<article class="card">
      <div class="row spread">
      <p class="eyebrow">${esc(a.company)}</p>${badge(a.sector)}</div>
      <h3 class="section-gap">${esc(a.role)}</h3>
      <p class="meta">${esc(a.location || 'Location not specified')}</p>
      <div class="pill-list">${badge(a.status,a.status)}${a.draft?.stale?badge('Draft stale','stale'):''}${a.research?.status==='researched'?badge('Researched','verified'):badge('Research needed','pending')}</div>
      <a class="button" href="#applications/${encodeURIComponent(a.id)}">Open application →</a>
      </article>`;
}
function opportunities() {
  const apps = state.profile.applications.filter(a => state.sector === 'all' || a.sector === state.sector);
  return title('THE SHORTLIST', 'Opportunities worth your attention.', 'Add any technology or finance role from an employer’s website. This is your curated hub, not a promise of every available opening.') + opportunityForm() + `<div class="row spread section-gap">
      <h2>Your shortlist</h2>
      <label>Sector<select id="sector-filter">
      <option value="all" ${state.sector==='all'?'selected':''}>All sectors</option>
      <option value="tech" ${state.sector==='tech'?'selected':''}>Technology</option>
      <option value="finance" ${state.sector==='finance'?'selected':''}>Finance</option>
      </select>
      </label>
      </div>
      <div class="grid section-gap">${apps.map(appCard).join('')||'<p class="empty wide">No opportunities here yet. Add a role above—including its real questions.</p>'}</div>`;
}
function applications() {
  return title('YOUR APPLICATION PIPELINE', 'Thoughtful work, clearly tracked.', 'Reviewed is not submitted. Record submission only after you have completed it on the employer’s website.') + `<div class="grid">${state.profile.applications.map(appCard).join('')||'<div class="empty">No applications yet. <a href="#opportunities">Add an opportunity</a> to get started.</div>'}</div>`;
}
function interview() {
  const i = state.interview, p = state.profile;
  if (!i) return '<p>Loading interview…</p>';
  const answered = new Set(p.interview.answers.map(a => a.questionId)), skipped = p.interview.skippedQuestionIds || [];
  const total = i.questions.length, done = i.questions.filter(q => answered.has(q.id) || skipped.includes(q.id)).length;
  const q = i.nextQuestion;
  return title('DEEP ONBOARDING', 'A conversation with substance.', 'Around 45–60 minutes to uncover your strengths, motivations and concrete examples. No timer, no completion gate, no guessed answers.') + `<section class="card">
      <div class="row spread">
      <h2>${p.interview.completed?'Interview paused / finished early':'Your story, one question at a time'}</h2>${badge(`${done} / ${total} addressed`)}</div>
      <progress class="progress" max="${total}" value="${done}" aria-label="Interview progress">${done} / ${total}</progress>
      <p class="meta">${answered.size} answered · ${skipped.length} skipped · ${total-done} remaining. Every saved answer proposes pending evidence, not verified facts.</p>${p.interview.completed?`<p>You can use the workspace now and resume the remaining questions later.</p>${btn('Resume interview','resume-interview','','primary')} ${skipped.length?btn('Revisit skipped questions','revisit-skipped'):''}`:q?`<form id="interview-answer" data-question="${esc(q.id)}"><p class="eyebrow section-gap">${esc(q.section)} · ${esc(q.sector)}</p><h3 class="question">${esc(q.prompt)}</h3><p class="muted">${esc(q.hint)}</p><label>Your answer<textarea name="answer" rows="8" required placeholder="Be specific: what happened, what did you do, and what changed?"></textarea></label><div class="actions"><button class="primary" type="submit">Save & continue</button>${btn('Skip for now','skip-question',q.id)}${btn('Ask a deeper follow-up','followup',q.id)}</div><div id="followup-result" class="section-gap" aria-live="polite"></div></form><div class="divider"></div>${btn('Finish early / pause','finish-interview')} ${skipped.length?btn('Revisit skipped questions','revisit-skipped'):''}`:`<h3 class="section-gap">You’ve reached the end of this question bank.</h3><p>Review the pending facts before using them in applications.</p><a class="button primary" href="#brain">Review your brain</a> ${skipped.length?btn('Revisit skipped questions','revisit-skipped'):''}`}</section>
      <details class="card section-gap">
      <summary>Saved answers (${p.interview.answers.length})</summary>${p.interview.answers.map(a=>`<div class="history"><strong>${esc(a.question || a.questionId)}</strong><p class="pre">${esc(a.answer)}</p><span class="meta">${esc(date(a.updatedAt || a.createdAt))}</span></div>`).join('')||'<p class="muted">Your saved answers appear here.</p>'}</details>`;
}
function memoryCard(m) {
  const source = m.source || {
  };
  return `<article class="card memory">
      <div class="row spread">
      <h3>${esc(m.label)}</h3>${badge(m.status,m.status)}</div>
      <p class="pre">${esc(m.content)}</p>
      <p class="meta">${esc(m.category)} · Source: ${esc(source.label || source.kind || 'Manual entry')} ${source.id?`(${esc(source.id)})`:''}<br>Added ${esc(date(m.createdAt))}${m.supersedes?` · Proposed correction of ${esc(m.supersedes)}`:''}</p>
      <div class="actions">${m.status==='pending'?btn('Confirm fact','confirm-memory',m.id,'primary')+btn('Edit & confirm','edit-confirm-memory',m.id)+btn('Reject','reject-memory',m.id):m.status==='verified'?btn('Propose correction','correct-memory',m.id):''}${btn('Forget','forget-memory',m.id,'danger')}</div>
      </article>`;
}
function brain() {
  const p = state.profile, pending = p.memories.filter(m => m.status === 'pending'), verified = memoryFacts(), other = p.memories.filter(m => !['pending', 'verified'].includes(m.status));
  return title('YOUR EVIDENCE BANK', 'A brain built on confirmed facts.', 'New personal claims stay pending. Confirm only what is accurate; correcting a fact explicitly supersedes the old version and makes affected drafts stale.') + `<section class="grid two">
      <div>
      <h2>Pending confirmation ${badge(pending.length,'pending')}</h2>${pending.map(memoryCard).join('')||'<p class="empty">Your inbox is clear. Saved interview answers and application edits can propose new evidence here.</p>'}</div>
      <div>
      <h2>Verified evidence ${badge(verified.length,'verified')}</h2>${verified.map(memoryCard).join('')||'<p class="empty">No verified evidence yet. Answer some interview questions or add a fact, then confirm it.</p>'}</div>
      </section>
      <details class="card section-gap">
      <summary>Add a fact manually</summary>
      <form id="add-memory" class="stack section-gap">
      <label>Label<input name="label" required maxlength="200" placeholder="A specific achievement">
      </label>
      <label>Category<select name="category">
      <option>experience</option>
      <option>education</option>
      <option>achievement</option>
      <option>motivation</option>
      <option>skills</option>
      <option>preferences</option>
      </select>
      </label>
      <label>Fact / evidence<textarea name="content" required>
      </textarea>
      </label>
      <button class="primary" type="submit">Propose pending fact</button>
      </form>
      </details>
      <details class="card section-gap">
      <summary>Rejected and superseded history (${other.length})</summary>${other.map(memoryCard).join('')||'<p class="muted">None yet. These items never enter drafting context.</p>'}</details>`;
}
function claims(items, inferred = false) {
  return(items || []).map(c => `<div class="history">${inferred?badge('Inferred priority','pending'):''}<p>${esc(c.text)}</p>${c.supportExcerpt?`<p class="meta">Source support: “${esc(c.supportExcerpt)}”</p>`:''}${(c.sourceUrls||[]).map(u=>`<div class="meta">${link(u)}</div>`).join('')}</div>`).join('') || '<p class="meta">No supported claims recorded.</p>';
}
function researchPanel(a) {
  const r = a.research;
  return `<section class="card">
      <div class="row spread">
      <h2>Company & role research</h2>${badge(r?.status || 'Not researched',r?.status==='researched'?'verified':'pending')}</div>
      <p class="muted">What this employer publicly emphasizes—not a claim about a hiring manager’s private preferences.</p>${r?`<p>${esc(r.summary)}</p><p class="meta">Actually retrieved: ${esc(date(r.retrievedAt))} · ${esc(r.mode)} mode</p>${r.mode==='offline'?'<div class="warning">Offline: no web research was performed. Templates are generic and cannot be labelled employer-tailored.</div>':''}<details ${r.status==='researched'?'open':''}><summary>Company facts</summary>${claims(r.companyFacts)}</details><details><summary>Role requirements</summary>${claims(r.requirements)}</details><details><summary>Inferred hiring priorities</summary>${claims(r.hiringPriorities,true)}</details><h3 class="section-gap">Consulted sources</h3><ul class="source-list">${(r.sources||[]).map(s=>`<li>${link(s.url,s.title)}</li>`).join('')||'<li>No sources retrieved.</li>'}</ul>${r.unknowns?.length?`<h3>Unknowns / gaps</h3><ul>${r.unknowns.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}${warnings(r.warnings)}`:'<p class="empty">No research yet. Add the official company URL and role description, then request research. Cloud access requires your explicit consent and a configured server key.</p>'}<div class="actions">${btn(r?'Refresh research':'Research company & role','research',a.id,'primary')}<a href="#settings">Provider settings</a>
      </div>
      </section>`;
}
function warnings(values) {
  return values?.length ? `<div class="warning">
      <ul>${values.map(v=>`<li>${esc(v)}</li>`).join('')}</ul>
      </div>`: '';
}
const words = text => String(text || '').trim().split(/\s+/).filter(Boolean).length;
function counter(text, q = {
}) {
  const missing = Boolean(q.id) && !String(text || '').trim();
  const w = words(text), c = [...String(text || '')].length, over = (q.maxWords && w > q.maxWords) || (q.maxChars && c > q.maxChars);
  return `<div class="counter ${over||missing?'over':''}" data-question="${esc(q.id || '')}" data-max-words="${q.maxWords||''}" data-max-chars="${q.maxChars||''}">${w}${q.maxWords?` / ${q.maxWords}`:''} words · ${c}${q.maxChars?` / ${q.maxChars}`:''} characters${over?' · Over limit':''}${missing?' · Required answer missing':''}</div>`;
}
function qualityReferences(a, item) {
  const facts = memoryFacts();
  const research = [...(a.research?.companyFacts || []), ...(a.research?.requirements || []), ...(a.research?.hiringPriorities || [])];
  const evidence = (item.evidenceIds || []).map(id => facts.find(m => m.id === id));
  const sources = (item.researchIds || []).map(id => research.find(c => c.id === id));
  return `${evidence.length?`<p class="meta"><strong>Confirmed examples:</strong> ${evidence.map(m=>esc(m?.label || 'Evidence no longer active')).join(' · ')}</p>`:''}${sources.length?`<ul class="quality-references">${sources.map(c=>`<li>${esc(c?.text || 'Research no longer current')}</li>`).join('')}</ul>`:''}`;
}
function qualityPanel(a, unsaved = false) {
  const d = a.draft, q = d?.quality;
  if (!q || d.mode === 'offline') return `<section class="quality-panel section-gap"><h3>Editorial quality check</h3><p class="meta">${d.mode==='offline'?'Offline templates have no AI quality assessment.':'This version has no saved editorial assessment.'} Check role fit, specific motivation and real examples yourself.</p></section>`;
  const retrievedAt = Date.parse(a.research?.retrievedAt || '');
  const researchStale = !Number.isFinite(retrievedAt) || Date.now() - retrievedAt > 7 * 24 * 60 * 60 * 1000
    || retrievedAt > Date.now()
    || a.research?.status !== 'researched'
    || a.research?.inputRevision !== a.inputRevision
    || d.researchId !== a.research?.id;
  const stale = unsaved || d.stale || q.status === 'stale' || researchStale;
  const label = stale ? 'Assessment stale' : q.status === 'needs_work' ? 'Needs work' : 'Editorially assessed';
  const questionLabel = id => id === 'coverLetter' ? 'Cover letter' : a.questions.find(question => question.id === id)?.text || 'Question no longer current';
  const categories = {truthfulness:'Evidence / truthfulness',specificity:'Specificity',role_fit:'Role fit',question_coverage:'Answer coverage',clarity:'Clarity',format:'Format / limits',voice:'Natural voice'};
  return `<section class="quality-panel section-gap" aria-label="Advisory editorial quality assessment"><div class="row spread"><h3>Employer-specific quality check</h3>${badge(label,stale?'stale':q.status==='needs_work'?'pending':'verified')}</div><p class="meta">Advisory editorial feedback—not factual certification, a hiring probability or a replacement for your review.</p>${stale?'<div class="warning error">This assessment does not apply to the current text or inputs. Regenerate to reassess. The earlier plan and feedback below are historical context only.</div>':''}<p>${esc(q.summary)}</p><p class="meta">${stale?'Earlier check':'Checked'} ${esc(date(q.checkedAt))} · ${q.rewriteCount===1?'One automatic rewrite, then reassessed':'No automatic rewrite'}</p>${q.issues?.length?`<h4>Unresolved feedback${stale?' from the earlier check':''}</h4><ul class="quality-issues">${q.issues.map(issue=>`<li><strong>${esc(questionLabel(issue.target))}</strong><div>${badge(issue.severity==='must_fix'?'Must fix':'Improve',issue.severity==='must_fix'?'stale':'pending')} <span class="meta">${esc(categories[issue.category] || 'Editorial feedback')}</span></div><p>${esc(issue.detail)}</p>${qualityReferences(a,issue)}</li>`).join('')}</ul>`:'<p class="meta">No editorial issues were reported by this check. You still need to verify every claim and follow employer instructions.</p>'}<details class="section-gap"><summary>${stale?'Earlier':'Planned'} answer angles & evidence (${q.questionPlans?.length || 0})</summary>${(q.questionPlans || []).map(plan=>`<article class="history"><h4>${esc(questionLabel(plan.questionId))}</h4><p class="meta">${esc(plan.kind)} answer${plan.kind==='factual'?' · Direct facts, not employer flattery':''}</p><p>${esc(plan.approach)}</p>${qualityReferences(a,plan)}${plan.missingFacts?.length?`<div class="warning"><strong>Facts to supply—not invent</strong><ul>${plan.missingFacts.map(fact=>`<li>${esc(fact)}</li>`).join('')}</ul></div>`:''}</article>`).join('')}</details></section>`;
}
function draftPanel(a) {
  const d = a.draft, facts = memoryFacts();
  const over = d?.answers?.some(answer => {
    const q = a.questions.find(q => q.id === answer.questionId) || {
    };
    return(q.maxWords && words(answer.text) > q.maxWords) || (q.maxChars && [...answer.text].length > q.maxChars);
  });
  const missing = d ? a.questions.filter(q => !d.answers.find(answer => answer.questionId === q.id)?.text?.trim()).length : 0;
  return `<section class="card">
      <div class="row spread">
      <h2>Your application draft</h2>${d?badge(d.mode==='offline'?'Offline · generic template':'Cloud · researched draft',d.mode==='offline'?'offline':'verified'):badge('Not drafted')}</div>
      <p class="muted">Only active verified evidence is reusable. Company and role research shapes the answer angles and real examples—not guessed private hiring preferences.</p>
      <p class="meta">Cloud generation uses 3–5 model calls: plan → draft → critique, with at most one rewrite and a final critique. This adds latency and provider usage. Feedback is advisory; review all claims and employer AI instructions yourself.</p>
      <div class="actions">${btn('Generate researched draft','draft',a.id,'primary')}${btn('Create offline template','offline-draft',a.id)}</div>${d?`<div class="divider"></div>${d.stale?'<div class="warning error">This draft is stale. Your evidence, settings, research or role inputs changed. Generate a fresh draft before review.</div>':''}${d.mode==='offline'?'<div class="warning">Generic offline template—not web-researched or employer-tailored. Replace placeholders and review before use.</div>':''}<p class="meta">Version ${esc(d.id)} · created ${esc(date(d.createdAt))}</p>${qualityPanel(a)}${warnings(d.warnings)}${d.missingFacts?.length?`<div class="warning"><strong>Missing facts—do not guess</strong><ul>${d.missingFacts.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:''}<form id="edit-draft" data-id="${esc(a.id)}" data-draft="${esc(d.id)}"><label>Cover letter<textarea name="coverLetter" rows="12">${esc(d.coverLetter)}</textarea>${counter(d.coverLetter)}</label>${(a.questions||[]).map(q=>{const answer=d.answers.find(x=>x.questionId===q.id);return `<label class="section-gap">${esc(q.text)}<textarea name="draft-answer" data-question="${esc(q.id)}" rows="6">${esc(answer?.text||'')}</textarea>${counter(answer?.text,q)}</label>`;}).join('')}<div class="actions"><button class="primary" type="submit">Save edits (new draft version)</button>${btn('Copy saved text','copy-draft',a.id)}${btn('Download saved .txt','download-draft',a.id)}</div><p class="meta section-gap">Copy/download use the saved version. Save edits first. Every edit clears prior review approval.</p></form><details class="section-gap"><summary>Evidence used (${d.evidenceIds?.length||0})</summary>${(d.evidenceIds||[]).map(id=>{const m=facts.find(m=>m.id===id);return `<p class="meta"><strong>${esc(m?.label || id)}</strong>: ${esc(m?.content || 'Evidence no longer active')}</p>`;}).join('')||'<p class="muted">No verified personal evidence cited.</p>'}<p class="meta">Research claim IDs: ${esc((d.researchIds||[]).join(', ')||'None')}</p></details><div class="divider"></div>${missing?`<div class="warning error">${missing} required answer${missing===1?' is':'s are'} missing. Complete and save every application question before review.</div>`:''}${over?'<div class="warning error">At least one answer exceeds its declared limit. Edit and save before review.</div>':''}<div class="actions"><button type="button" class="primary" data-action="review-application" data-id="${esc(a.id)}" data-draft="${esc(d.id)}" ${d.stale||over||missing?'disabled':''}>Record human review</button><button type="button" data-action="submit-application" data-id="${esc(a.id)}" data-draft="${esc(d.id)}" ${d.stale||over||missing||a.status!=='reviewed'?'disabled':''}>I submitted this externally</button></div><p class="meta section-gap">Review creates a record of this exact draft; it does not verify new personal facts or submit anything. Recording submission requires that exact version to have been reviewed.</p>`:'<p class="empty section-gap">Research the role first for cloud drafting, or create a clearly labelled offline template. No draft is sent to an employer.</p>'}</section>`;
}
function applicationDetail(id) {
  const a = state.profile.applications.find(a => a.id === id);
  if (!a) return title('APPLICATION', 'Application not found.', 'Select an opportunity belonging to the current profile.') + '<a href="#applications">Back to applications</a>';
  return `<a href="#applications">← Applications</a>` + title(a.company, a.role, `${a.location || 'Location not specified'} · ${a.sector}`) + `<div class="pill-list">${badge(a.status,a.status)}${a.url?link(a.url,'Open employer application'):''}</div>
      <div class="grid two">
      <div class="stack">${researchPanel(a)}<details class="card">
      <summary>Role inputs & actual questions</summary>
      <form id="edit-application" data-id="${esc(a.id)}" class="stack section-gap">
      <label>Company<input name="company" value="${esc(a.company)}" required>
      </label>
      <label>Role<input name="role" value="${esc(a.role)}" required>
      </label>
      <label>Sector<select name="sector">
      <option value="tech" ${a.sector==='tech'?'selected':''}>Technology</option>
      <option value="finance" ${a.sector==='finance'?'selected':''}>Finance</option>
      </select>
      </label>
      <label>Location<input name="location" value="${esc(a.location)}">
      </label>
      <label>Role URL<input name="url" type="url" value="${esc(a.url)}">
      </label>
      <label>Official company URL<input name="companyUrl" type="url" value="${esc(a.companyUrl)}">
      </label>
      <label>Job description<textarea name="jobDescription" required rows="8">${esc(a.jobDescription)}</textarea>
      </label>
      <div>${(a.questions||[]).map(q=>`<div class="question-row" data-question="${esc(q.id)}"><label>Question<textarea name="questionText" required>${esc(q.text)}</textarea></label><label>Word limit<input name="maxWords" type="number" min="1" value="${esc(q.maxWords)}"></label><label>Char limit<input name="maxChars" type="number" min="1" value="${esc(q.maxChars)}"></label><button type="button" data-action="remove-question">Remove</button></div>`).join('')}</div>
      <div class="question-extra">
      </div>${btn('Add a question','add-edit-question')}<p class="meta">Saving input changes invalidates research and drafts.</p>
      <button type="submit">Save role inputs</button>
      </form>
      </details>
      <section class="card">
      <h2>Review & submission history</h2>${(a.history||[]).map(h=>`<details class="history"><summary>${esc(h.status)} · ${esc(date(h.timestamp || h.createdAt))}</summary><p class="meta">Draft: ${esc(h.draftId)}</p><p class="pre">${esc(h.coverLetter || h.approved?.coverLetter || '')}</p>${(h.answers || h.approved?.answers || []).map(x=>`<p class="pre">${esc(x.text)}</p>`).join('')}</details>`).join('')||'<p class="muted">No review or submission events yet.</p>'}</section>
      </div>
      <div>${draftPanel(a)}</div>
      </div>`;
}
function settings() {
  const p = state.profile;
  return title('PRIVACY & CONTROL', 'Your workspace, your rules.', 'Cloud processing is off until you explicitly opt in for this profile. Keys stay on the local server—not in this page or your brain.') + `<section class="grid two">
      <form id="settings" class="card stack">
      <h2>Profile settings</h2>
      <label>Name<input name="name" required value="${esc(p.name)}" maxlength="100">
      </label>
      <label>Application focus<select name="sectors">
      <option value="tech,finance" ${p.sectors.length===2?'selected':''}>Technology & finance</option>
      <option value="tech" ${p.sectors.length===1&&p.sectors[0]==='tech'?'selected':''}>Technology</option>
      <option value="finance" ${p.sectors.length===1&&p.sectors[0]==='finance'?'selected':''}>Finance</option>
      </select>
      </label>
      <label>Writing preferences<textarea name="writingPreferences" placeholder="Tone, style, phrases to avoid…">${esc(p.writingPreferences)}</textarea>
      </label>
      <label class="check">
      <input type="checkbox" name="cloudConsent" ${p.cloudConsent?'checked':''}>
      <span>I explicitly allow cloud AI requests for this profile when I request research, drafting or a follow-up. Relevant verified personal evidence may be sent for drafting; research uses public role/company inputs.</span>
      </label>
      <div class="warning">Local data is plaintext, not encrypted. Provider processing and retention policies still apply; disabling response storage does not guarantee zero retention. Opting out cannot undo earlier requests.</div>
      <button class="primary" type="submit">Save preferences & consent</button>
      </form>
      <div class="stack">
      <section class="card">
      <h2>Provider setup</h2>
      <p>${state.status.provider.configured?'Server key configured.':'No server key configured. Offline mode is available.'}</p>
      <p class="meta">Model: ${esc(state.status.provider.model || 'Not configured')}</p>
      <p>In <code>application-copilot/.env</code>, configure <code>OPENAI_API_KEY</code> and optionally <code>OPENAI_MODEL</code> using the server’s setup instructions, then restart the local server. Never paste a key into this application or commit it.</p>
      <p>
      <a href="https://developers.openai.com/api/docs/guides/your-data" target="_blank" rel="noopener noreferrer">Read provider data controls ↗</a>
      </p>
      </section>
      <section class="card">
      <h2>Export or erase this profile</h2>
      <p class="muted">Export includes your app-held memories, raw interview answers, opportunities, research, drafts and history. It contains personal data—store it carefully.</p>${btn('Download profile JSON','export-profile')}<div class="divider">
      </div>
      <p class="muted">Deleting this profile removes all its app-held records. It does not erase downloads, provider-held data, operating-system backups or other profiles.</p>${btn('Delete this profile','delete-profile',p.id,'danger')}</section>
      </div>
      </section>
      <details class="section-gap">
      <summary>Create another isolated profile or synthetic demo</summary>
      <div class="section-gap">${profileCreation()}</div>
      </details>`;
}
async function render() {
  const[view = 'overview', id] = location.hash.slice(1).split('/');
  $('#nav').innerHTML = navigation.map(([key, icon, label]) => `<a href="#${key}" class="${key===view?'active':''}" ${key===view?'aria-current="page"':''}>
      <span aria-hidden="true">${icon}</span>${label}</a>`).join('');
  $('#profile-select').innerHTML = '<option value="">Choose a profile</option>' + state.profiles.map(p => `<option value="${esc(p.id)}" ${p.id===state.profile?.id?'selected':''}>${esc(p.name)}${p.isDemo?' · SYNTHETIC DEMO':''}</option>`).join('');
  $('#provider').textContent = state.status?.provider.configured ? (state.profile?.cloudConsent ? 'Cloud available · opt-in enabled': 'Cloud configured · consent off'): 'Offline · no server key';
  $('#provider').className = 'badge ' + (state.status?.provider.configured && state.profile?.cloudConsent ? 'verified': 'offline');
  if (state.profile?.isDemo) $('#provider').textContent += ' · Synthetic demo';
  if (view === 'interview' && state.profile && !state.interview) {
    try {
      const profileId = state.profile.id;
      const version = state.contextVersion;
      const result = await api(path('/interview'));
      if (state.profile?.id !== profileId || state.contextVersion !== version) return;
      state.interview = result;
    } catch (e) {
      notify(e.message, true);
    }
  }
  const screens = {
    overview, opportunities, interview, brain, applications, settings
  };
  $('#main').innerHTML = !state.profile ? overview(): view === 'applications' && id ? applicationDetail(decodeURIComponent(id)): (screens[view] || overview)();
}
function applicationBody(form) {
  const data = new FormData(form);
  return {
    company: data.get('company').trim(), role: data.get('role').trim(), sector: data.get('sector'), location: data.get('location').trim(), url: data.get('url').trim(), companyUrl: data.get('companyUrl').trim(), jobDescription: data.get('jobDescription').trim(), questions: [...form.querySelectorAll('.question-row')].map(row => ( {
      ...(row.dataset.question ? {
        id: row.dataset.question
      }
      : {
      }), text: $('[name="questionText"]', row).value.trim(), maxWords: Number($('[name="maxWords"]', row).value) || null, maxChars: Number($('[name="maxChars"]', row).value) || null
    }))
  };
}
function draftText(a) {
  return `${a.role} — ${a.company}\n\n${a.draft.mode==='offline'?'OFFLINE GENERIC TEMPLATE — not employer researched\n\n':''}COVER LETTER\n\n${a.draft.coverLetter}\n\n${a.questions.map(q=>`${q.text}\n\n${a.draft.answers.find(x=>x.questionId===q.id)?.text || ''}`).join('\n\n')}`;
}
function download(content, name, type) {
  const url = URL.createObjectURL(new Blob([content], {
    type
  }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function memoryEditor(memory, confirmExisting = false) {
  const dialog = document.createElement('dialog');
  dialog.className = 'card';
  dialog.innerHTML = `<form id="memory-editor">
      <h2>${confirmExisting?'Edit & confirm this fact':'Propose a correction'}</h2>
      <p class="muted">${confirmExisting?'Confirming makes this version reusable.':'The old fact stays active until you explicitly confirm the pending correction.'}</p>
      <label>Fact<textarea name="content" rows="7" required>${esc(memory.content)}</textarea>
      </label>
      <div class="actions">
      <button class="primary" type="submit">${confirmExisting?'Confirm edited fact':'Save pending correction'}</button>
      <button type="button" data-close-dialog>Cancel</button>
      </div>
      </form>`;
  document.body.append(dialog);
  dialog.showModal();
  $('[data-close-dialog]', dialog).onclick = () => dialog.close();
  dialog.addEventListener('close', () => dialog.remove());
  $('#memory-editor', dialog).onsubmit = async e => {
    e.preventDefault();
    const content = new FormData(e.target).get('content').trim();
    if (!content) return;
    dialog.close();
    if (confirmExisting) await mutate(path(`/memories/${encodeURIComponent(memory.id)}/review`), 'POST', {
      action: 'confirm', content
    }, 'Edited fact confirmed. Affected drafts are stale.');
    else await mutate(path('/memories'), 'POST', {
      category: memory.category, label: memory.label, content, supersedes: memory.id
    }, 'Correction proposed. Confirm it in the pending inbox to supersede the old fact.');
  };
}
document.addEventListener('submit', async e => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;
  e.preventDefault();
  const data = new FormData(form);
  if (form.id === 'create-profile') {
    await task(async() => {
      const p = await api('/api/profiles', 'POST', {
        name: data.get('name').trim(), sectors: data.get('sectors').split(',')
      });
      await loadProfiles(p.id);
      location.hash = 'overview';
      notify('Blank profile created. Start with your own story.');
    });
  } else if (form.id === 'create-application') {
    await mutate(path('/applications'), 'POST', applicationBody(form), 'Opportunity saved. Research it before drafting.');
  } else if (form.id === 'edit-application') {
    await mutate(path(`/applications/${encodeURIComponent(form.dataset.id)}`), 'PATCH', applicationBody(form), 'Role inputs saved. Prior research and drafts must be refreshed.');
  } else if (form.id === 'interview-answer') {
    await task(async() => {
      state.profile = await api(path('/interview/answer'), 'POST', {
        questionId: form.dataset.question, answer: data.get('answer').trim()
      });
      state.interview = null;
      notify('Answer saved. New evidence is pending your confirmation.');
    });
  } else if (form.id === 'add-memory') {
    await mutate(path('/memories'), 'POST', {
      label: data.get('label').trim(), category: data.get('category'), content: data.get('content').trim()
    }, 'Fact proposed—please confirm it before reuse.');
  } else if (form.id === 'settings') {
    await task(async() => {
      state.profile = await api(path(''), 'PATCH', {
        name: data.get('name').trim(), sectors: data.get('sectors').split(','), writingPreferences: data.get('writingPreferences'), cloudConsent: data.has('cloudConsent')
      });
      await loadProfiles(state.profile.id);
      notify('Settings saved for this profile only.');
    });
  } else if (form.id === 'edit-draft') {
    const a = state.profile.applications.find(a => a.id === form.dataset.id);
    const answers = [...form.querySelectorAll('[name="draft-answer"]')].map(el => ( {
      questionId: el.dataset.question, text: el.value
    }));
    await mutate(path(`/applications/${encodeURIComponent(a.id)}`), 'PATCH', {
      expectedDraftId: form.dataset.draft, draft: {
        coverLetter: data.get('coverLetter'), answers
      }
    }, 'Edits saved as a new version. Review this version before recording submission.');
  }
});
document.addEventListener('click', async e => {
  const button = e.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const action = button.dataset.action, id = button.dataset.id;
  const mem = state.profile?.memories.find(m => m.id === id), a = state.profile?.applications.find(a => a.id === id);
  if (action === 'add-question') {
    $('#question-rows').insertAdjacentHTML('beforeend', questionRow());
    $('#question-rows').lastElementChild.querySelector('textarea').focus();
  } else if (action === 'add-edit-question') {
    const target = $('.question-extra');
    target.insertAdjacentHTML('beforeend', questionRow());
    target.lastElementChild.querySelector('textarea').focus();
  } else if (action === 'remove-question') {
    button.closest('.question-row').remove();
  } else if (action === 'demo') {
    await task(async() => {
      const p = await api('/api/demo', 'POST', {
      });
      await loadProfiles(p.id);
      location.hash = 'overview';
      notify('Opened a separate synthetic demo. Its facts are not real personal evidence.');
    });
  } else if (action === 'skip-question') {
    await task(async() => {
      state.profile = await api(path('/interview'), 'PATCH', {
        skippedQuestionIds: [...new Set([...state.profile.interview.skippedQuestionIds, id])]
      });
      state.interview = null;
      notify('Question skipped. You can return later.');
    });
  } else if (action === 'revisit-skipped') {
    await task(async() => {
      state.profile = await api(path('/interview'), 'PATCH', {
        skippedQuestionIds: [], completed: false
      });
      state.interview = null;
      notify('Skipped questions are available again. Saved answers remain intact.');
    });
  } else if (action === 'finish-interview' || action === 'resume-interview') {
    await task(async() => {
      state.profile = await api(path('/interview'), 'PATCH', {
        completed: action === 'finish-interview'
      });
      state.interview = null;
      notify(action === 'finish-interview' ? 'Interview saved and paused. You can continue using the workspace.': 'Interview resumed.');
    });
  } else if (action === 'followup') {
    const answer = $('#interview-answer [name="answer"]').value.trim();
    if (!answer) {
      notify('Write an answer first so the follow-up can deepen it.', true);
      return;
    }
    const profileId = state.profile.id;
    const version = state.contextVersion;
    const requestId = ++state.followupRequest;
    const originForm = $('#interview-answer');
    const stillCurrent = () => state.profile?.id === profileId
      && state.contextVersion === version
      && state.followupRequest === requestId
      && $('#interview-answer') === originForm
      && originForm.dataset.question === id;
    button.disabled = true;
    button.textContent = 'Preparing follow-up…';
    try {
      const f = await api(`/api/profiles/${encodeURIComponent(profileId)}/interview/followup`, 'POST', {
        questionId: id, answer
      });
      // A delayed response belongs only to the initiating profile, question and view.
      if (!stillCurrent()) return;
      $('#followup-result').innerHTML = `<div class="warning">
      <strong>Follow-up</strong>
      <p>${esc(f.question)}</p>
      <p class="meta">${esc(f.rationale)}</p>
      <p class="meta">Add your response to the answer above before saving. Follow-up output is not a confirmed personal fact.</p>
      </div>`;
      notify('Follow-up ready. Your unsaved answer is preserved.');
    } catch (err) {
      if (stillCurrent()) notify(err.message, true);
    } finally {
      if (stillCurrent()) {
        button.disabled = false;
        button.textContent = 'Ask a deeper follow-up';
      }
    }
  } else if (action === 'confirm-memory' || action === 'reject-memory') {
    await mutate(path(`/memories/${encodeURIComponent(id)}/review`), 'POST', {
      action: action === 'confirm-memory' ? 'confirm': 'reject'
    }, action === 'confirm-memory' ? 'Fact confirmed. Affected drafts are stale.': 'Proposal rejected; it will not enter drafting context.');
  } else if (action === 'edit-confirm-memory' || action === 'correct-memory') {
    memoryEditor(mem, action === 'edit-confirm-memory');
  } else if (action === 'forget-memory') {
    if (confirm('Forget this memory? This also removes linked source answers and application-answer proposals, and clears ALL saved drafts and approved history for this profile, because edited prose may contain the forgotten fact. Other verified memories remain. Downloads, provider data and backups are not erased.')) await mutate(path(`/memories/${encodeURIComponent(id)}`), 'DELETE', undefined, 'Memory forgotten and derived drafts/history cleared within this profile.');
  } else if (['research', 'draft', 'offline-draft'].includes(action)) {
    notify(action === 'research' ? 'Researching public employer and role sources…': action === 'draft' ? 'Planning examples, drafting and checking quality (3–5 cloud calls)…': 'Preparing your offline template…');
    await mutate(path(`/applications/${encodeURIComponent(id)}/${action==='research'?'research':'draft'}`), 'POST', action === 'offline-draft' ? {
      offline: true
    }
    : {
    }, action === 'research' ? 'Research request complete. Inspect status, sources, gaps and actual retrieval time.': 'Draft prepared. Inspect editorial feedback, evidence, wording and declared limits.');
  } else if (action === 'review-application' || action === 'submit-application') {
    if (draftChanged()) {
      notify('Save your edits before reviewing or recording submission.', true);
      return;
    }
    const submitted = action === 'submit-application';
    if (!confirm(submitted ? 'Have you submitted this exact reviewed version yourself on the employer’s website? This only records your action—it does not submit anything.': 'Have you checked this exact saved draft, all factual claims, source support, placeholders and answer limits? New claims still require separate brain confirmation.')) return;
    await mutate(path(`/applications/${encodeURIComponent(id)}/record`), 'POST', {
      status: submitted ? 'submitted': 'reviewed', eventId: crypto.randomUUID(), expectedDraftId: button.dataset.draft
    }, submitted ? 'Your external submission was recorded. No automated submission occurred.': 'Human review recorded for this exact draft version.');
  } else if (action === 'copy-draft') {
    try {
      await navigator.clipboard.writeText(draftText(a));
      notify('Saved draft copied.');
    } catch {
      notify('Clipboard unavailable; use Download saved .txt instead.', true);
    }
  } else if (action === 'download-draft') {
    download(draftText(a), `application-${a.id}.txt`, 'text/plain;charset=utf-8');
    notify('Saved draft downloaded.');
  } else if (action === 'export-profile') {
    await task(async() => {
      const data = await api(path('/export'));
      download(JSON.stringify(data, null, 2), `copilot-profile-${state.profile.id}.json`, 'application/json');
      notify('Profile export downloaded. This file contains personal data.');
    });
  } else if (action === 'delete-profile') {
    if (!confirm(`Permanently delete profile “${state.profile.name}” and all its app-held interviews, memories, opportunities, research, drafts and history? Other profiles, external downloads, provider data and OS backups are not deleted.`)) return;
    await task(async() => {
      await api(path(''), 'DELETE');
      state.profile = null;
      await loadProfiles();
      location.hash = 'overview';
      notify('Profile and its app-held records deleted.');
    });
  }
});
function draftChanged() {
  const f = $('#edit-draft');
  if (!f) return false;
  const a = state.profile.applications.find(a => a.id === f.dataset.id);
  return $('[name="coverLetter"]', f).value !== a.draft.coverLetter || [...f.querySelectorAll('[name="draft-answer"]')].some(el => el.value !== (a.draft.answers.find(x => x.questionId === el.dataset.question)?.text || ''));
}
document.addEventListener('input', e => {
  if (e.target instanceof HTMLTextAreaElement && e.target.nextElementSibling?.classList.contains('counter')) {
    const c = e.target.nextElementSibling;
    const q = {
      id: c.dataset.question, maxWords: Number(c.dataset.maxWords) || null, maxChars: Number(c.dataset.maxChars) || null
    };
    c.outerHTML = counter(e.target.value, q);
  }
  if (e.target.closest('#edit-draft')) {
    const form = $('#edit-draft');
    const application = state.profile.applications.find(a => a.id === form.dataset.id);
    const panel = form.parentElement.querySelector('.quality-panel');
    if (panel && application) panel.outerHTML = qualityPanel(application, draftChanged());
  }
});
document.addEventListener('change', async e => {
  if (e.target.id === 'profile-select') {
    state.contextVersion++;
    state.followupRequest++;
    notify('');
    await task(async() => {
      state.profile = null;
      state.interview = null;
      await loadProfiles(e.target.value || null);
      location.hash = 'overview';
      notify(state.profile ? 'Profile switched. Data stays isolated.': 'Choose or create a profile.');
    });
  } else if (e.target.id === 'sector-filter') {
    state.sector = e.target.value;
    await render();
  }
});
window.addEventListener('hashchange', () => {
  state.contextVersion++;
  state.followupRequest++;
  render().then(() => $('#main').focus( {
    preventScroll: true
  }));
});
async function boot() {
  try {
    state.status = await api('/api/status');
    await loadProfiles();
    await render();
  } catch (e) {
    notify(`Cannot connect to the local server: ${e.message}`, true);
    $('#main').innerHTML = '<h1>Workspace unavailable</h1><p>Start the local Application Copilot server, then reload this page. Existing saved data has not been replaced.</p>';
  }
}
boot();
