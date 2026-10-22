/**
 * The platform-independent client: configuration, scope, event building and the delivery queue.
 * A "platform" object supplies what differs between browsers and Node (see browser.js / node.js).
 */

import { Scope, safeUrl } from './scope.js';
import { parseStack } from './stack.js';

export const SDK_VERSION = '1.6.0';

const LEVELS = ['fatal', 'error', 'warning', 'info', 'debug'];
const MAX_BATCH = 20;
const MAX_QUEUE = 100;
const MAX_ATTEMPTS = 5;
const MAX_PAUSE_S = 3600;

/** host / endpoint → the events URL, tolerant of trailing slashes and of a host that already ends in /api. */
export function resolveEndpoints(o) {
  if (o.endpoint) {
    const events = String(o.endpoint).replace(/\/+$/, '') + '/';
    return { events, sourcemaps: events.replace(/\/events\/$/, '/sourcemaps/'), metrics: events.replace(/\/events\/$/, '/metrics/'), sessions: events.replace(/\/events\/$/, '/sessions/'), replay: events.replace(/\/events\/$/, '/replay/') };
  }
  let host = String(o.host || '').trim().replace(/\/+$/, '');
  if (!host) return null;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(host)) host = 'https://' + host;
  const root = /\/api$/i.test(host) ? host : host + '/api';
  return { events: `${root}/ingest/events/`, sourcemaps: `${root}/ingest/sourcemaps/`, metrics: `${root}/ingest/metrics/`, sessions: `${root}/ingest/sessions/`, replay: `${root}/ingest/replay/` };
}

const clip = (s, n) => (typeof s === 'string' && s.length > n ? s.slice(0, n) : s);
const isErrorLike = (e) => e instanceof Error || (e && typeof e === 'object' && typeof e.message === 'string' && typeof e.stack === 'string');

function stringify(v) {
  if (typeof v === 'string') return v;
  try { return JSON.stringify(v); } catch { return String(v); }
}

function matches(list, text) {
  return (list || []).some((p) => (p instanceof RegExp ? p.test(text) : text.includes(String(p))));
}

export class Client {
  /**
   * @param {object} options  see index.d.ts
   * @param {object} platform { name, fetch, getStore?, runInScope?, now?, onFlushNeeded?, serverName?, defaults?(options)→partial event }
   */
  constructor(options, platform) {
    this.platform = platform;
    this.options = {
      sampleRate: 1,
      maxBreadcrumbs: 100,
      maxEventsPerMinute: 60,
      sendDefaultPii: false,
      debug: false,
      ignoreErrors: [/^Script error\.?$/, /ResizeObserver loop/],
      batchDelayMs: platform.name === 'node' ? 100 : 500,
      ...options,
    };
    this.globalScope = new Scope();
    this.endpoints = resolveEndpoints(this.options);
    this.queue = []; // { event, attempts }
    this.inflight = 0;
    this.timer = null;
    this.pausedUntil = 0;
    this.disabled = !this.options.apiKey || !this.endpoints;
    this.warned = new Set();
    this.recent = [];
    this.seen = new WeakSet();
    this.dropped = 0;
    this.sent = 0;
    this.waiters = [];
    this.sdkVersion = SDK_VERSION;
    if (!this.options.apiKey) this.warnOnce('config', 'Insight: no apiKey given, events will not be sent.');
    else if (!this.endpoints) this.warnOnce('config', 'Insight: no host (or endpoint) given, events will not be sent.');
  }

  // ---- logging ------------------------------------------------------------------------------
  log(...a) {
    if (this.options.debug) try { (this.platform.rawConsole || console).log('[insight]', ...a); } catch { /* ignore */ }
  }
  warnOnce(key, msg) {
    if (this.warned.has(key)) return;
    this.warned.add(key);
    try { (this.platform.rawConsole || console).warn(msg); } catch { /* ignore */ }
  }

  // ---- scope --------------------------------------------------------------------------------
  get scope() {
    return (this.platform.getStore && this.platform.getStore()) || this.currentScope || this.globalScope;
  }
  withScope(cb) {
    const scope = this.scope.clone();
    if (this.platform.runInScope) return this.platform.runInScope(scope, () => cb(scope));
    const prev = this.currentScope;
    this.currentScope = scope; // browsers have one logical thread of control: sync callbacks only
    try { return cb(scope); } finally { this.currentScope = prev; }
  }
  addBreadcrumb(b) {
    const crumb = { timestamp: new Date().toISOString(), category: 'custom', level: 'info', ...b };
    if (typeof this.options.beforeBreadcrumb === 'function') {
      try { if (!(b = this.options.beforeBreadcrumb(crumb))) return; Object.assign(crumb, b); } catch { /* keep going */ }
    }
    crumb.message = clip(crumb.message, 500);
    this.scope.addBreadcrumb(crumb, this.options.maxBreadcrumbs);
  }

  // ---- capture ------------------------------------------------------------------------------
  captureException(err, ctx = {}) {
    try {
      if (err && typeof err === 'object') {
        if (this.seen.has(err)) return null; // already reported (e.g. by a middleware and a global handler)
        this.seen.add(err);
      }
      return this.enqueueEvent(this.buildEvent(err, ctx, ctx.level || this.scope.level || 'error'), err);
    } catch (e) {
      this.log('captureException failed', e);
      return null;
    }
  }

  captureMessage(message, level = 'info', ctx = {}) {
    try {
      return this.enqueueEvent(this.buildEvent(String(message), ctx, level, true), message);
    } catch (e) {
      this.log('captureMessage failed', e);
      return null;
    }
  }

  buildEvent(err, ctx, level, isMessage = false) {
    const o = this.options;
    const scope = this.scope;
    const event = {
      event_type: 'error',
      action: isMessage ? 'message' : 'exception',
      status: 'error',
      level: LEVELS.includes(level) ? level : 'error',
      timestamp: new Date().toISOString(),
      platform: this.platform.name === 'node' ? 'node' : 'javascript',
      sdk: { name: 'insight.javascript', version: SDK_VERSION },
    };
    if (o.release) event.release = String(o.release);
    if (o.environment) event.environment = String(o.environment);
    const server = o.serverName || (this.platform.serverName && this.platform.serverName());
    if (server) event.server_name = server;

    if (isMessage) {
      event.description = clip(String(err), 255);
    } else {
      const ex = this.describeError(err);
      event.exception = ex.exception;
      event.description = clip(ex.exception.value ? `${ex.exception.type}: ${ex.exception.value}` : ex.exception.type, 255);
      if (ex.chain.length) event.extra = { exception_chain: ex.chain };
    }

    const user = ctx.user || scope.user;
    if (user) event.user = user;
    const tags = { ...scope.tags, ...(ctx.tags || {}) };
    if (Object.keys(tags).length) event.tags = tags;
    const extra = { ...(event.extra || {}), ...scope.extra, ...(ctx.extra || {}) };
    if (Object.keys(extra).length) event.extra = extra;
    if (scope.txn && scope.txn.trace_id) event.trace_id = scope.txn.trace_id; // links this error to its request's trace
    const fp = ctx.fingerprint || scope.fingerprint;
    if (fp) event.fingerprint = Array.isArray(fp) ? fp.map(String) : [String(fp)];
    const request = ctx.request || scope.request || (this.platform.defaultRequest && this.platform.defaultRequest(o));
    if (request) event.request = request;
    if (scope.breadcrumbs.length) event.breadcrumbs = scope.breadcrumbs.slice();
    const meta = this.platform.defaultMetadata && this.platform.defaultMetadata();
    if (meta) event.metadata = { ...meta };
    if (event.exception && event.exception.stacktrace.length) {
      event.metadata = { ...(event.metadata || {}), traceback: event.exception.stacktrace.map((f) => `at ${f.function ? f.function + ' ' : ''}(${f.filename}${f.lineno ? ':' + f.lineno : ''}${f.colno ? ':' + f.colno : ''})`).join('\n') };
    }
    return event;
  }

  describeError(err) {
    const chain = [];
    const one = (e) => {
      if (isErrorLike(e)) {
        const stack = parseStack(e.stack, { strip: this.platform.stripPath });
        return { type: clip(e.name || (e.constructor && e.constructor.name) || 'Error', 100), value: clip(String(e.message ?? ''), 1000), stacktrace: stack };
      }
      // thrown strings, objects, null: keep what we can, and say it wasn't an Error
      return { type: 'NonErrorException', value: clip(stringify(e), 1000), stacktrace: [] };
    };
    const main = one(err);
    let cause = err && err.cause;
    for (let i = 0; cause && i < 5; i++) {
      const c = one(cause);
      chain.push({ type: c.type, value: c.value, stacktrace: c.stacktrace.slice(0, 20) });
      cause = cause.cause;
    }
    if (!main.stacktrace.length && this.platform.syntheticStack) main.stacktrace = this.platform.syntheticStack();
    return { exception: main, chain };
  }

  /** Apply filters, hooks and limits, then queue for delivery. Returns the event (or null when dropped). */
  enqueueEvent(event, original) {
    if (this.disabled) return null;
    const o = this.options;
    const text = `${event.exception ? event.exception.type + ': ' + event.exception.value : event.description}`;
    if (matches(o.ignoreErrors, text)) { this.log('ignored by ignoreErrors:', text); return null; }
    if (o.sampleRate < 1 && Math.random() >= o.sampleRate) return null;
    if (this.pausedUntil > Date.now() && this.quotaPaused) { this.dropped++; return null; }
    const now = Date.now();
    this.recent = this.recent.filter((t) => now - t < 60_000);
    if (this.recent.length >= o.maxEventsPerMinute) { this.dropped++; this.warnOnce('rate', `Insight: more than ${o.maxEventsPerMinute} events/minute, dropping the extras (maxEventsPerMinute).`); return null; }
    if (typeof o.beforeSend === 'function') {
      let r;
      try { r = o.beforeSend(event, { originalException: original }); } catch (e) { this.log('beforeSend threw', e); r = event; }
      if (r && typeof r.then === 'function') {
        // async hook: deliver when it resolves
        r.then((res) => res && this.push(res)).catch(() => {});
        return event;
      }
      if (!r) return null;
      event = r;
    }
    this.recent.push(now);
    if (this.replay) this.replay.noteEvent(event); // links the error to the visitor's recording (and uploads the buffered seconds before it)
    if (this.session) this.session.note(event); // release health: only events that are really sent move a session from ok to errored/crashed
    this.push(event);
    return event;
  }

  push(event) {
    this.queue.push({ event, attempts: 0 });
    if (this.queue.length > MAX_QUEUE) { this.queue.shift(); this.dropped++; }
    this.schedule(this.options.batchDelayMs);
  }

  // ---- delivery -----------------------------------------------------------------------------
  schedule(ms) {
    if (this.timer || this.disabled) return;
    const wait = Math.max(ms, this.pausedUntil - Date.now(), 0);
    this.timer = setTimeout(() => { this.timer = null; this.pump(); }, wait);
    // Background delivery must not keep a process alive, but while someone is awaiting flush() the retry/next-batch timers must,
    // or a short script could exit in the middle of a retry and lose events.
    if (!this.waiters.length && this.timer && typeof this.timer.unref === 'function') this.timer.unref();
  }

  async pump(opts = {}) {
    if (this.disabled || !this.queue.length || this.inflight) { this.notify(); return; }
    if (this.pausedUntil > Date.now()) { this.schedule(this.pausedUntil - Date.now()); return; }
    const batch = this.queue.splice(0, opts.keepalive ? 5 : Math.min(this.maxBatch || MAX_BATCH, MAX_BATCH));
    this.inflight++;
    let delay = 0;
    try {
      delay = await this.deliver(batch, opts);
    } catch (e) {
      this.log('deliver crashed', e);
    } finally {
      this.inflight--;
    }
    if (this.queue.length) this.schedule(delay);
    this.notify();
  }

  /** Sends one batch; returns how long to wait before the next one. */
  async deliver(batch, opts) {
    let res;
    try {
      res = await (this.options.fetch || this.platform.fetch)(this.endpoints.events, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': this.options.apiKey },
        body: JSON.stringify(batch.map((b) => b.event)),
        keepalive: !!opts.keepalive,
      });
    } catch (e) {
      return this.retry(batch, `network error: ${e && e.message}`);
    }
    const status = res.status;
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    if (status >= 200 && status < 300) { this.sent += batch.length; return 0; }
    const code = body && body.code;
    const retryAfter = Math.min(Number(res.headers && res.headers.get && res.headers.get('retry-after')) || 0, MAX_PAUSE_S);
    if (status === 401 || status === 403) {
      this.disabled = true;
      this.queue = [];
      this.warnOnce('auth', `Insight: the server rejected the API key (HTTP ${status}${body && body.error ? ': ' + body.error : ''}). Events are disabled for this session. Check apiKey and that the key has write permission.`);
      return 0;
    }
    if (status === 429 && code === 'quota_exceeded') {
      this.quotaPaused = true;
      this.pausedUntil = Date.now() + Math.max(retryAfter, 60) * 1000;
      this.dropped += batch.length + this.queue.length;
      this.queue = [];
      this.warnOnce('quota', `Insight: your monthly event quota is used up. Pausing sending for ${Math.round(Math.max(retryAfter, 60) / 60)} min; events in the meantime are dropped. Upgrade your plan to continue.`);
      return 0;
    }
    if (status === 429) {
      this.quotaPaused = false;
      this.pausedUntil = Date.now() + Math.max(retryAfter, 1) * 1000;
      this.log(`rate limited, retrying in ${retryAfter}s`);
      for (const b of batch) b.attempts++;
      this.queue.unshift(...batch.filter((b) => b.attempts < MAX_ATTEMPTS));
      return retryAfter * 1000;
    }
    if (status === 413) {
      if (batch.length > 1) { // too big as one request: retry in smaller pieces
        this.queue.unshift(...batch);
        this.maxBatch = Math.max(1, Math.floor(batch.length / 2));
        return 0;
      }
      this.dropped++;
      this.log('event too large, dropped');
      return 0;
    }
    if (status >= 500) return this.retry(batch, `server error ${status}`);
    this.dropped += batch.length; // 400 and anything else unexpected: the server will not accept it
    this.log(`rejected (HTTP ${status})`, body);
    return 0;
  }

  retry(batch, why) {
    const next = [];
    for (const b of batch) { b.attempts++; if (b.attempts < MAX_ATTEMPTS) next.push(b); else this.dropped++; }
    this.queue.unshift(...next);
    const attempts = Math.max(...batch.map((b) => b.attempts));
    const delay = Math.min(60_000, 1000 * 2 ** (attempts - 1)) * (0.5 + Math.random() / 2) * (this.retryDelayScale ?? 1);
    this.log(`${why}; retrying ${next.length} event(s) in ${Math.round(delay)}ms`);
    return delay;
  }

  notify() {
    if (this.queue.length || this.inflight) return;
    const w = this.waiters.splice(0);
    for (const r of w) r(true);
  }

  /** Resolves true when everything queued has been delivered (or dropped), false on timeout. */
  flush(timeout = 2000, opts = {}) {
    const events = this.flushEvents(timeout, opts);
    if (!this.metrics) return events;
    // performance summaries go out with the errors; a slow metrics endpoint never delays past `timeout`
    const m = Promise.race([this.metrics.flush().catch(() => false), new Promise((r) => { const t = setTimeout(() => r(false), timeout); if (t.unref) t.unref(); })]);
    return Promise.all([events, m]).then(([ok]) => ok);
  }

  flushEvents(timeout, opts) {
    if (this.disabled || (!this.queue.length && !this.inflight)) return Promise.resolve(true);
    return new Promise((resolve) => {
      const t = setTimeout(() => { this.waiters = this.waiters.filter((w) => w !== done); resolve(false); }, timeout); // not unref'd: the caller asked to wait
      const done = (v) => { clearTimeout(t); resolve(v); };
      this.waiters.push(done);
      if (this.timer) { clearTimeout(this.timer); this.timer = null; }
      this.pump(opts);
    });
  }

  close(timeout) {
    if (this.metrics) this.metrics.close();
    return this.flush(timeout).then((ok) => { this.disabled = true; return ok; });
  }
}

export { safeUrl };
