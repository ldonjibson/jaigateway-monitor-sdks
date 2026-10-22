const BLOCK = /^(authorization|cookie|set-cookie|x-api-key|proxy-authorization|x-csrf-token)$/i;

export const SENSITIVE_PARAM = /^(token|access_token|id_token|refresh_token|key|api_key|apikey|secret|password|passwd|pwd|auth|authorization|code|session|sid|signature|sig|otp)$/i;

/** URL without the fragment, with secret-looking query values removed. */
export function safeUrl(u) {
  try {
    const url = new URL(u, 'http://x.invalid');
    const abs = /^[a-z][a-z0-9+.-]*:/i.test(String(u));
    for (const k of [...url.searchParams.keys()]) if (SENSITIVE_PARAM.test(k)) url.searchParams.set(k, '[Filtered]');
    url.hash = '';
    return abs ? url.toString() : url.pathname + url.search;
  } catch {
    return String(u).split('#')[0].slice(0, 500);
  }
}

export function scrubHeaders(h) {
  const out = {};
  if (!h) return out;
  for (const [k, v] of Object.entries(h)) if (!BLOCK.test(k)) out[k] = Array.isArray(v) ? v.join(', ') : String(v);
  return out;
}

export class Scope {
  constructor() {
    this.user = null;
    this.tags = {};
    this.extra = {};
    this.breadcrumbs = [];
    this.level = null;
    this.request = null;
    this.fingerprint = null;
    this.txn = null;
  }
  clone() {
    const s = new Scope();
    s.user = this.user ? { ...this.user } : null;
    s.tags = { ...this.tags };
    s.extra = { ...this.extra };
    s.breadcrumbs = this.breadcrumbs.slice();
    s.level = this.level;
    s.request = this.request ? { ...this.request } : null;
    s.fingerprint = this.fingerprint;
    s.txn = this.txn; // a child scope is still inside the same request
    return s;
  }
  setTag(k, v) { this.tags[String(k)] = String(v); return this; }
  setTags(t) { for (const [k, v] of Object.entries(t || {})) this.setTag(k, v); return this; }
  setExtra(k, v) { this.extra[String(k)] = v; return this; }
  setUser(u) { this.user = u || null; return this; }
  setLevel(l) { this.level = l; return this; }
  setFingerprint(f) { this.fingerprint = f; return this; }
  setRequest(r) { this.request = r; return this; }
  clear() {
    this.user = null; this.tags = {}; this.extra = {}; this.breadcrumbs = []; this.level = null; this.request = null; this.fingerprint = null;
  }
  addBreadcrumb(b, max) {
    if (max <= 0) return;
    this.breadcrumbs.push(b);
    if (this.breadcrumbs.length > max) this.breadcrumbs.splice(0, this.breadcrumbs.length - max);
  }
}
