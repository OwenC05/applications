// Workspace envelopes stay distinct from the preserved Node API.
export function createClient({fetchImpl = globalThis.fetch, token = () => ''} = {}) {
  return async function request(path, {method = 'GET', body, headers = {}} = {}) {
    const normalized = new URL(path, 'http://workspace.invalid');
    if (normalized.origin !== 'http://workspace.invalid'
      || !/^\/api\/(?:workspace|evidence)(?:\/|$)/.test(normalized.pathname)
      || !path.startsWith('/') || path.startsWith('//') || path.includes('\\')) {
      throw new Error('Only local workspace and evidence routes are supported.');
    }
    const upload = typeof FormData !== 'undefined' && body instanceof FormData;
    const response = await fetchImpl(path, {
      method, mode: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: {...headers, 'X-Evidence-Token': token(), ...(body !== undefined && !upload ? {'Content-Type': 'application/json'} : {})},
      body: body === undefined ? undefined : upload ? body : JSON.stringify(body),
    });
    let data;
    try { data = await response.json(); }
    catch { throw new Error('The local API returned an unreadable response. No success is claimed.'); }
    if (!response.ok) {
      const error = new Error(data?.error?.message || 'The local operation failed.');
      error.code = data?.error?.code || 'UNAVAILABLE';
      error.status = response.status;
      throw error;
    }
    return data;
  };
}

// Saved draft downloads have a narrower route and response contract than JSON IO.
// No caller-supplied method, headers, redirect target or filename is accepted.
export function createTextClient({fetchImpl = globalThis.fetch, token = () => ''} = {}) {
  return async function exportText(path) {
    const match = typeof path === 'string'
      && /^\/api\/workspace\/profiles\/([^/?#\\]+)\/applications\/([^/?#\\]+)\/drafts\/([^/?#\\]+)\/export$/.exec(path);
    let safe = Boolean(match);
    if (match) {
      try {
        safe = match.slice(1).every(segment => {
          const decoded = decodeURIComponent(segment);
          return decoded !== '.' && decoded !== '..' && !/[\u0000-\u001f\u007f/%\\?#]/u.test(decoded);
        });
      } catch { safe = false; }
    }
    if (!safe) throw new Error('Only local scoped draft exports are supported.');
    const response = await fetchImpl(path, {method: 'GET', mode: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: {'X-Evidence-Token': token()}});
    const mime = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
    if (!response.ok) {
      let data;
      if (mime === 'application/json') {
        try { data = await response.json(); } catch { /* Never expose an arbitrary response body. */ }
      }
      const error = new Error(typeof data?.error?.message === 'string' ? data.error.message : 'The saved draft export failed.');
      error.code = typeof data?.error?.code === 'string' ? data.error.code : 'UNAVAILABLE';
      error.status = response.status;
      throw error;
    }
    if (mime !== 'text/plain') throw new Error('The saved draft export did not return text/plain. No download is claimed.');
    try { return await response.text(); }
    catch { throw new Error('The saved draft export returned unreadable text. No download is claimed.'); }
  };
}

// Preserve captured application form objects; only our own saved output advances.
export function advanceApplicationOutput(application, published) {
  if (!application || application.profile_id !== published?.owner || application.application_id !== published?.application
    || typeof published.draftId !== 'string' || !published.draftId || typeof published.current !== 'boolean'
    || !Number.isSafeInteger(application.output_revision) || application.output_revision < 0) return false;
  const dimensions = ['metadata', 'facts', 'documents', 'consent', 'application_input', 'application_output', 'research'];
  const pre = published.preRevisions, post = published.postRevisions;
  const valid = vector => vector && vector.schema_version === 1 && Object.keys(vector).length === 8
    && dimensions.every(key => Number.isSafeInteger(vector[key]) && vector[key] >= 0);
  if (!valid(pre) || !valid(post) || !dimensions.every(key => post[key] === pre[key] + (key === 'application_output' ? 1 : 0))
    || post.application_output <= application.output_revision) return false;
  application.output_revision = post.application_output;
  return true;
}

// Explicit UI quarantine only: no durable storage, success inference or unlock.
export class WritingRecovery {
  #capsules = new Map();
  #key(owner, application) { return JSON.stringify([owner, application]); }
  retain(owner, application, capsule) {
    const operation = capsule?.operation, parent = operation?.parentCapture?.draft;
    if (!owner || !application || parent?.profile_id !== owner || parent?.application_id !== application
      || operation.parentDraftId !== parent.id || typeof operation.body?.idempotency_key !== 'string'
      || !Array.isArray(capsule.targets) || !capsule.targets.every(t => typeof t?.target === 'string' && typeof t.text === 'string')) return false;
    const key = this.#key(owner, application);
    // A second handoff cannot replace the unresolved logical operation.
    if (this.#capsules.has(key)) return false;
    this.#capsules.set(key, structuredClone(capsule));
    return true;
  }
  get(owner, application) {
    const value = this.#capsules.get(this.#key(owner, application));
    return value ? structuredClone(value) : null;
  }
  has(owner, application) { return this.#capsules.has(this.#key(owner, application)); }
  forgetOwner(owner) {
    for (const key of this.#capsules.keys()) if (JSON.parse(key)[0] === owner) this.#capsules.delete(key);
  }
  async forgetOwnerAfter(owner, action) {
    const result = await action();
    this.forgetOwner(owner);
    return result;
  }
  get size() { return this.#capsules.size; }
}

// An owner/view epoch handles navigation; a slot sequence also handles races
// between two source/citation/search selections within the same profile.
export class RequestGate {
  constructor() { this.epoch = 0; this.context = ''; this.slots = new Map(); }
  select(context) { this.context = context; this.epoch++; this.slots.clear(); }
  capture(slot) {
    const sequence = (this.slots.get(slot) || 0) + 1;
    this.slots.set(slot, sequence);
    return Object.freeze({context: this.context, epoch: this.epoch, slot, sequence});
  }
  current(ticket) {
    return ticket.context === this.context && ticket.epoch === this.epoch
      && this.slots.get(ticket.slot) === ticket.sequence;
  }
}

// Only the submitted edit generation may be acknowledged. Read-only actions
// have no ticket; they cannot erase another form's unsaved browser buffer.
export class EditBuffers {
  constructor() { this.forms = new Map(); }
  changed(form) {
    this.forms.set(form, {version: (this.forms.get(form)?.version || 0) + 1, dirty: true});
  }
  capture(form) { return form ? {form, version: this.forms.get(form)?.version || 0} : null; }
  acknowledge(ticket) {
    if (!ticket || (this.forms.get(ticket.form)?.version || 0) !== ticket.version) return false;
    this.forms.set(ticket.form, {version: ticket.version, dirty: false});
    return true;
  }
  get dirty() { return [...this.forms.values()].some(value => value.dirty); }
  forget(form) { this.forms.delete(form); }
  clear() { this.forms.clear(); }
}

export function evidenceDeletionNotice(result) {
  if (result?.state === 'pending') return 'Access revoked; external cleanup remains pending.';
  if (result?.state === 'complete') return 'Local source cleanup complete; not forensic erasure or deletion of external copies.';
  return 'Cleanup response is unrecognized; reload to inspect it. Cleanup completion is not claimed.';
}

export const codePoints = text => Array.from(text).length;
export function externalUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
