import http from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { BrainStore } from './store.mjs';
import { getInterviewQuestions, getInterviewProgress, getNextQuestion } from './interview.mjs';
import { createAI } from './ai.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PUBLIC = resolve(ROOT, 'public');
const MAX_BODY = 1024 * 1024;
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };
function fail(status, message) { throw Object.assign(new Error(message), { status }); }
function text(value, name, max = 20000, required = true) {
  if (value === undefined && !required) return '';
  if (typeof value !== 'string' || value.length > max || (required && !value.trim())) fail(400, name + ' must be valid text (maximum ' + max + ' characters).');
  return value.trim();
}
function sectors(value) {
  if (!Array.isArray(value) || !value.length || value.some(v => !['tech', 'finance'].includes(v))) fail(400, 'Choose tech, finance, or both.');
  return [...new Set(value)];
}
function publicUrl(value, name) {
  if (!value) return '';
  let url; try { url = new URL(text(value, name, 2000)); } catch { fail(400, name + ' must be a public HTTP(S) URL.'); }
  const host = url.hostname.toLowerCase();
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || /^(localhost|127\.|0\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host) || host.includes(':') || host.endsWith('.localhost') || !host.includes('.')) fail(400, name + ' must be a public employer URL, not a local/private address.');
  return url.href;
}
function questions(value = []) {
  if (!Array.isArray(value) || value.length > 40) fail(400, 'Supply at most 40 application questions.');
  const seen = new Set();
  return value.map((q, index) => {
    const id = text(q.id || 'question-' + (index + 1), 'Question ID', 100);
    if (seen.has(id)) fail(400, 'Question IDs must be unique.'); seen.add(id);
    const result = { id, text: text(q.text, 'Question', 4000), maxWords: null, maxChars: null };
    for (const key of ['maxWords', 'maxChars']) {
      if (q[key] !== null && q[key] !== undefined && q[key] !== '') {
        if (!Number.isInteger(q[key]) || q[key] < 1 || q[key] > 20000) fail(400, 'Question limits must be positive integers.');
        result[key] = q[key];
      }
    }
    return result;
  });
}
function applicationInput(input, partial = false) {
  const result = {};
  for (const [key, max] of [['company', 200], ['role', 300], ['jobDescription', 40000], ['location', 300]]) {
    if (!partial || key in input) result[key] = text(input[key], key, max, key !== 'location');
  }
  if (!partial || 'sector' in input) {
    if (!['tech', 'finance'].includes(input.sector)) fail(400, 'Application sector must be tech or finance.');
    result.sector = input.sector;
  }
  for (const key of ['url', 'companyUrl']) if (!partial || key in input) result[key] = publicUrl(input[key], key);
  if (!partial || 'questions' in input) result.questions = questions(input.questions);
  return result;
}
async function readJson(req) {
  if (!String(req.headers['content-type'] || '').toLowerCase().startsWith('application/json')) fail(415, 'Use application/json.');
  if (Number(req.headers['content-length'] || 0) > MAX_BODY) { req.resume(); fail(413, 'Request is too large.'); }
  const chunks = []; let size = 0; let oversized = false;
  await new Promise((ok, reject) => {
    req.on('data', chunk => { size += chunk.length; if (size > MAX_BODY) oversized = true; if (!oversized) chunks.push(chunk); });
    req.on('end', ok); req.on('error', reject);
  });
  if (oversized) fail(413, 'Request is too large.');
  let body;
  try { body = JSON.parse(Buffer.concat(chunks).toString() || '{}'); } catch { fail(400, 'Invalid JSON.'); }
  if (!body || typeof body !== 'object' || Array.isArray(body)) fail(400, 'Expected a JSON object.');
  return body;
}
function send(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(data));
}
function safeError(error, apiKey) {
  let message = error?.message || 'Request failed.';
  if (apiKey) message = message.split(apiKey).join('[redacted]');
  return message.replace(/sk-[A-Za-z0-9_-]+/g, '[redacted]').slice(0, 800);
}
function findApplication(profile, id) {
  const app = profile.applications.find(item => item.id === id);
  if (!app) fail(404, 'Application not found in this profile.');
  return app;
}

export function createAppServer({ directory = process.env.COPILOT_DATA_DIR || resolve(ROOT, '.data'), ai, apiKey = process.env.OPENAI_API_KEY || '', model = process.env.OPENAI_MODEL || 'gpt-6-astra' } = {}) {
  const store = new BrainStore({ directory });
  const provider = ai || createAI({ apiKey, model });
  const token = randomBytes(32).toString('hex');
  const busy = new Set();
  let closed = false;
  const server = http.createServer(async (req, res) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'");
    try {
      const port = server.address()?.port;
      const hosts = new Set(['127.0.0.1:' + port, 'localhost:' + port]);
      if (!hosts.has(req.headers.host)) fail(403, 'Invalid local host.');
      if (req.headers.origin) {
        let origin; try { origin = new URL(req.headers.origin).origin; } catch { fail(403, 'Invalid request origin.'); }
        if (!new Set([...hosts].map(host => 'http://' + host)).has(origin)) fail(403, 'Cross-origin requests are not permitted.');
      }
      if (req.headers['sec-fetch-site'] === 'cross-site') fail(403, 'Cross-site requests are not permitted.');
      const path = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
      if (path === '/api/status' && req.method === 'GET') return send(res, 200, { token, provider: provider.status(), version: '0.1.0' });
      if (path.startsWith('/api/')) {
        const given = req.headers['x-copilot-token'];
        if (typeof given !== 'string' || Buffer.byteLength(given) !== Buffer.byteLength(token) || !timingSafeEqual(Buffer.from(given), Buffer.from(token))) fail(403, 'Local session token required. Refresh this page.');
        const body = ['POST', 'PATCH', 'DELETE'].includes(req.method) ? await readJson(req) : {};
        return send(res, 200, await dispatch(req.method, path, body));
      }
      if (req.method !== 'GET' && req.method !== 'HEAD') fail(405, 'Method not allowed.');
      const file = resolve(PUBLIC, '.' + (path === '/' ? '/index.html' : path));
      if (!file.startsWith(PUBLIC + sep) || !MIME[extname(file)]) fail(404, 'Not found.');
      let content; try { content = readFileSync(file); } catch { fail(404, 'Not found.'); }
      res.writeHead(200, { 'Content-Type': MIME[extname(file)], 'Cache-Control': 'no-cache' });
      res.end(req.method === 'HEAD' ? undefined : content);
    } catch (error) {
      if (!res.headersSent) send(res, Number.isInteger(error.status) ? error.status : 500, { error: safeError(error, apiKey) });
      else res.end();
    }
  });
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.on('close', () => { if (!closed) { closed = true; store.close(); } });

  async function runAI(profileId, appId, kind, task) {
    const key = profileId + ':' + (appId || 'interview') + ':' + kind;
    if (busy.has(key)) fail(409, 'This request is already running.');
    if (busy.size >= 3) fail(429, 'Too many AI requests. Wait for the current ones to finish.');
    busy.add(key);
    try { return await task(); } finally { busy.delete(key); }
  }
  function revalidate(profileId, previous, appId, application, { personal = false, draft = false } = {}) {
    const current = store.getProfile(profileId);
    if (previous.cloudConsent && !current.cloudConsent) fail(409, 'Cloud consent changed while the request was running. Result discarded.');
    if (personal && (current.brainRevision !== previous.brainRevision || current.writingPreferences !== previous.writingPreferences)) fail(409, 'Your verified brain or writing preferences changed. Generate again using current evidence.');
    if (appId) {
      const live = findApplication(current, appId);
      if (live.inputRevision !== application.inputRevision) fail(409, 'The application changed while the request was running. Result discarded.');
      if (draft && ((live.research?.id ?? null) !== (application.research?.id ?? null) || (live.draft?.id ?? null) !== (application.draft?.id ?? null) || live.history.length !== application.history.length)) fail(409, 'The research, draft or review changed while the request was running. Generate again from the current version.');
    }
    return current;
  }
  function cloudCheckpoint(profileId, previous, appId, application, options) {
    // Called immediately before EACH provider transmission, not just after the
    // whole pipeline. Opt-out or an intervening edit stops subsequent stages.
    return () => {
      const current = revalidate(profileId, previous, appId, application, options);
      if (!previous.cloudConsent || !current.cloudConsent) fail(403, 'Explicit profile consent is required before cloud processing.');
    };
  }
  async function dispatch(method, path, body) {
    if (path === '/api/profiles') {
      if (method === 'GET') return store.listProfiles();
      if (method === 'POST') return store.createProfile({ name: text(body.name, 'Name', 100), sectors: sectors(body.sectors) });
    }
    if (path === '/api/demo' && method === 'POST') return createDemo();
    const match = /^\/api\/profiles\/([^/]+)(?:\/(.*))?$/.exec(path);
    if (!match) fail(404, 'API route not found.');
    const [, profileId, tail = ''] = match;
    const profile = store.getProfile(profileId);
    if (!tail) {
      if (method === 'GET') return profile;
      if (method === 'DELETE') return store.deleteProfile(profileId);
      if (method === 'PATCH') {
        const patch = {};
        if ('name' in body) patch.name = text(body.name, 'Name', 100);
        if ('sectors' in body) patch.sectors = sectors(body.sectors);
        if ('cloudConsent' in body) { if (typeof body.cloudConsent !== 'boolean') fail(400, 'Consent must be a boolean.'); patch.cloudConsent = body.cloudConsent; }
        if ('writingPreferences' in body) patch.writingPreferences = text(body.writingPreferences, 'Writing preferences', 3000, false);
        return store.updateProfile(profileId, patch);
      }
    }
    if (tail === 'export' && method === 'GET') return profile;
    if (tail === 'interview') {
      if (method === 'GET') return { questions: getInterviewQuestions(profile.sectors), progress: getInterviewProgress(profile), nextQuestion: getNextQuestion(profile) };
      if (method === 'PATCH') {
        const allowed = new Set(getInterviewQuestions(profile.sectors).map(q => q.id));
        const patch = {};
        if ('skippedQuestionIds' in body) {
          if (!Array.isArray(body.skippedQuestionIds) || body.skippedQuestionIds.some(id => !allowed.has(id))) fail(400, 'Unknown interview question.');
          patch.skippedQuestionIds = [...new Set(body.skippedQuestionIds)];
        }
        if ('completed' in body) { if (typeof body.completed !== 'boolean') fail(400, 'Completion must be boolean.'); patch.completed = body.completed; }
        return store.setInterviewProgress(profileId, patch);
      }
    }
    if (tail === 'interview/answer' || tail === 'interview/followup') {
      if (method !== 'POST') fail(405, 'Method not allowed.');
      const question = getInterviewQuestions(profile.sectors).find(q => q.id === body.questionId);
      if (!question) fail(400, 'Unknown interview question.');
      const answer = text(body.answer, 'Answer', 20000);
      if (tail.endsWith('/answer')) return store.saveInterviewAnswer(profileId, { questionId: question.id, question: question.prompt, section: question.section, answer });
      return runAI(profileId, null, 'followup', async () => {
        const result = await provider.followup(profile, { question: question.prompt, answer, section: question.section }, { consent: profile.cloudConsent, checkpoint: cloudCheckpoint(profileId, profile, null, null, { personal: true }) });
        revalidate(profileId, profile, null, null, { personal: true });
        return result;
      });
    }
    if (tail === 'memories' && method === 'POST') return store.addMemory(profileId, { category: text(body.category || 'experience', 'Category', 100), label: text(body.label, 'Label', 300), content: text(body.content, 'Memory', 20000), source: { kind: 'user', label: 'Entered explicitly by this profile' }, supersedes: body.supersedes || null });
    const memoryMatch = /^memories\/([^/]+)(\/review)?$/.exec(tail);
    if (memoryMatch) {
      const [, memoryId, review] = memoryMatch;
      if (method === 'DELETE' && !review) return store.deleteMemory(profileId, memoryId);
      if (method === 'POST' && review) {
        if (!['confirm', 'reject'].includes(body.action)) fail(400, 'Choose confirm or reject.');
        const patch = { action: body.action };
        if ('content' in body) patch.content = text(body.content, 'Memory', 20000);
        return store.reviewMemory(profileId, memoryId, patch);
      }
    }
    if (tail === 'applications' && method === 'POST') return store.createApplication(profileId, applicationInput(body));
    const appMatch = /^applications\/([^/]+)(?:\/(research|draft|record))?$/.exec(tail);
    if (appMatch) {
      const [, appId, action] = appMatch;
      const application = findApplication(profile, appId);
      if (!action && method === 'PATCH') {
        const patch = applicationInput(body, true);
        if ('draft' in body) {
          if (!body.draft || typeof body.draft !== 'object') fail(400, 'Invalid draft edit.');
          patch.expectedDraftId = text(body.expectedDraftId, 'Expected draft ID', 200);
          patch.draft = {};
          if ('coverLetter' in body.draft) patch.draft.coverLetter = text(body.draft.coverLetter, 'Cover letter', 30000, false);
          if ('answers' in body.draft) {
            if (!Array.isArray(body.draft.answers) || body.draft.answers.length > 40) fail(400, 'Invalid draft answers.');
            patch.draft.answers = body.draft.answers.map(a => ({ questionId: text(a.questionId, 'Question ID', 100), text: text(a.text, 'Answer', 30000, false) }));
          }
        }
        return store.updateApplication(profileId, appId, patch);
      }
      if (method === 'POST' && action === 'record') return store.recordApplication(profileId, appId, { status: body.status, eventId: text(body.eventId, 'Event ID', 200), expectedDraftId: text(body.expectedDraftId, 'Expected draft ID', 200) });
      if (method === 'POST' && action === 'research') return runAI(profileId, appId, action, async () => {
        const result = await provider.research(application, { consent: profile.cloudConsent, checkpoint: cloudCheckpoint(profileId, profile, appId, application) });
        revalidate(profileId, profile, appId, application);
        return store.saveResearch(profileId, appId, result, { expectedInputRevision: application.inputRevision });
      });
      if (method === 'POST' && action === 'draft') return runAI(profileId, appId, action, async () => {
        if ('offline' in body && typeof body.offline !== 'boolean') fail(400, 'Offline choice must be boolean.');
        const guards = { personal: true, draft: true };
        const result = await provider.draft(profile, application, { consent: profile.cloudConsent, offline: body.offline === true, checkpoint: cloudCheckpoint(profileId, profile, appId, application, guards) });
        revalidate(profileId, profile, appId, application, guards);
        return store.saveDraft(profileId, appId, result, { expectedBrainRevision: profile.brainRevision, expectedInputRevision: application.inputRevision, expectedResearchId: application.research?.id ?? null, expectedDraftId: application.draft?.id ?? null, expectedHistoryLength: application.history.length });
      });
    }
    fail(404, 'API route not found.');
  }
  function createDemo() {
    let profile = store.createProfile({ name: 'Demo candidate — synthetic', sectors: ['tech', 'finance'], isDemo: true });
    for (const item of [
      { category: 'project', label: 'Synthetic engineering example', content: 'In a synthetic university project, I built a Python data pipeline and added automated validation tests.' },
      { category: 'teamwork', label: 'Synthetic collaboration example', content: 'In a synthetic student team, I coordinated weekly progress updates and resolved conflicting priorities.' },
      { category: 'finance', label: 'Synthetic finance example', content: 'In a synthetic investment society exercise, I compared company earnings and explained valuation assumptions.' }
    ]) {
      profile = store.addMemory(profile.id, { ...item, source: { kind: 'demo', label: 'Synthetic fixture; not real experience' } });
      const memory = profile.memories.at(-1);
      profile = store.reviewMemory(profile.id, memory.id, { action: 'confirm' });
    }
    for (const role of [
      { company: 'Example Technology — synthetic', role: 'Software Engineering Summer Intern', sector: 'tech', jobDescription: 'Synthetic role: collaborate on a software team, write reliable Python code, test changes, and explain technical decisions.' },
      { company: 'Example Capital — synthetic', role: 'Investment Research Summer Intern', sector: 'finance', jobDescription: 'Synthetic role: analyze company performance, communicate valuation assumptions, collaborate with analysts, and demonstrate genuine interest in financial markets.' }
    ]) profile = store.createApplication(profile.id, { ...role, location: 'London', url: '', companyUrl: '', questions: [{ id: 'motivation', text: 'Why are you interested in this role?', maxWords: 200, maxChars: null }, { id: 'teamwork', text: 'Describe a time you worked effectively in a team.', maxWords: 250, maxChars: null }] });
    return profile;
  }
  return { server, store, provider, close: () => new Promise((ok, reject) => { if (!server.listening) { if (!closed) store.close(); closed = true; ok(); } else server.close(error => error ? reject(error) : ok()); }) };
}

export async function startServer(options = {}) {
  const app = createAppServer(options);
  try {
    await new Promise((ok, reject) => { app.server.once('error', reject); app.server.listen(options.port ?? Number(process.env.PORT || 3000), '127.0.0.1', ok); });
    return app;
  } catch (error) { await app.close(); throw error; }
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  startServer().then(app => {
    console.log('Application Copilot is running at http://localhost:' + app.server.address().port);
    console.log('Local data only by default. Cloud AI requires a server-side key and per-profile opt-in.');
    for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => { await app.close(); process.exit(0); });
  }).catch(error => { console.error('Startup failed: ' + safeError(error, process.env.OPENAI_API_KEY)); process.exitCode = 1; });
}
