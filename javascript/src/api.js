/** Builds the public API around a platform (browser or node). One singleton client per bundle. */

import { Client } from './core.js';

/** token or full ping URL → full ping URL (needs the configured host when given a bare token) */
function pingBase(client, target) {
  const t = String(target || '').trim();
  if (/^https?:\/\//i.test(t)) return t.replace(/\/(start|fail|log|\d{1,3})?\/*$/i, '').replace(/\/+$/, '') + '/';
  if (!client || !client.endpoints) return null;
  const root = client.endpoints.events.replace(/\/ingest\/events\/$/, '');
  return `${root}/ping/${encodeURIComponent(t)}/`;
}

export function makeSdk(platform, installIntegrations) {
  let client = null;
  let installed = false;
  const sdk = {
    getClient: () => client,
    init(options = {}) {
      if (client) client.disabled = true; // a second init() replaces the first
      client = new Client(options, platform);
      if (!installed && installIntegrations) {
        installed = true;
        try { installIntegrations(sdk, platform); } catch (e) { client.log('integrations failed', e); }
      } else if (installIntegrations && platform.onReinit) platform.onReinit(sdk);
      if (platform.onInit) try { platform.onInit(client); } catch { /* ignore */ }
      return sdk.api;
    },
  };
  const c = () => client;

  /** Report a cron job run to Insight. status: 'success' | 'start' | 'fail' | 'log'. Never throws; resolves true if accepted. */
  async function checkIn(target, status = 'success', message) {
    try {
      const base = pingBase(client, target);
      if (!base) return false;
      const suffix = status === 'start' || status === 'fail' || status === 'log' ? status + '/' : ''; // the trailing slash matters: without it the server redirects, and some clients don't follow
      const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      const timer = ctl && setTimeout(() => ctl.abort(), 5000);
      if (timer && timer.unref) timer.unref();
      const f = (client && client.options.fetch) || platform.fetch;
      const res = await f(base + suffix, { method: message ? 'POST' : 'GET', body: message ? String(message).slice(0, 10000) : undefined, signal: ctl ? ctl.signal : undefined });
      if (timer) clearTimeout(timer);
      return res.status >= 200 && res.status < 300;
    } catch {
      return false;
    }
  }
  sdk.api = {
    checkIn,
    /** Run fn as a monitored cron job: start ping, then success (or fail with the error text, then rethrow). */
    async monitor(target, fn) {
      await checkIn(target, 'start');
      try {
        const r = await fn();
        await checkIn(target, 'success');
        return r;
      } catch (e) {
        await checkIn(target, 'fail', e && e.stack ? e.stack : String(e));
        throw e;
      }
    },
    init: (o) => sdk.init(o),
    captureException: (err, ctx) => (c() ? c().captureException(err, ctx) : null),
    captureMessage: (msg, level, ctx) => (c() ? c().captureMessage(msg, level, ctx) : null),
    addBreadcrumb: (b) => c() && c().addBreadcrumb(b),
    setUser: (u) => { if (c()) c().scope.user = u || null; },
    setTag: (k, v) => { if (c()) c().scope.tags[String(k)] = String(v); },
    setTags: (t) => { if (c()) for (const [k, v] of Object.entries(t || {})) c().scope.tags[String(k)] = String(v); },
    setExtra: (k, v) => { if (c()) c().scope.extra[String(k)] = v; },
    setFingerprint: (f) => { if (c()) c().scope.fingerprint = f; },
    withScope: (cb) => (c() ? c().withScope(cb) : cb({ setTag() {}, setUser() {}, setExtra() {}, setLevel() {} })),
    configureScope: (cb) => c() && cb(c().scope),
    flush: (timeout) => (c() ? c().flush(timeout) : Promise.resolve(true)),
    close: (timeout) => (c() ? c().close(timeout) : Promise.resolve(true)),
    isEnabled: () => !!c() && !c().disabled,
    getClient: () => c(),
    /** Wrap a function (or serverless handler): report anything it throws or rejects with, then rethrow. */
    wrap(fn, opts = {}) {
      const handle = (err) => { c() && c().captureException(err, opts.context); };
      return function wrapped(...args) {
        let r;
        try { r = fn.apply(this, args); } catch (e) { handle(e); if (opts.flush !== false && c()) c().flush(opts.timeout || 2000); throw e; } // sync stays sync; delivery continues in the background
        if (r && typeof r.then === 'function') {
          return r.then((v) => v, async (e) => { handle(e); if (opts.flush !== false && c()) await c().flush(opts.timeout || 2000); throw e; });
        }
        return r;
      };
    },
  };
  // Scope helpers are methods on Scope; give them friendlier names for withScope callbacks.
  return sdk;
}
