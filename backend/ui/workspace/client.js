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
