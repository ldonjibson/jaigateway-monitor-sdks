/**
 * Backend performance metrics (sdks/PROTOCOL.md "Performance metrics"). Node only: it is not imported by the browser
 * bundle. Requests are aggregated in memory per transaction name and sent as one small summary every minute.
 */

import crypto from 'node:crypto';
import { summarise, sampleSpans } from './spans.js';
import { BOUNDS_MS, bucketIndex } from './buckets.js';

export { BOUNDS_MS, bucketIndex };

const DEFAULT_IGNORE = ['/health', '/healthz', '/ping', '/status', '/metrics', '/favicon.ico', '/robots.txt', '/ready', '/live', '/livez', '/readyz', '/up'];
const DEFAULT_IGNORE_PREFIX = ['/static/', '/assets/', '/_next/static/', '/_next/image'];
const MAX_WINDOWS = 5;
const MAX_PAUSE_S = 3600;

const newTraceId = () => crypto.randomBytes(16).toString('hex');

export class Metrics {
  constructor(client, platform) {
    this.client = client;
    this.platform = platform;
    const o = client.options;
    this.enabled = o.performance !== false && !client.disabled;
    this.intervalMs = o.performanceFlushIntervalMs || 60_000;
    this.ignore = o.performanceIgnore || [];
    this.tx = new Map();
    this.samples = [];
    this.forced = []; // requests of sampled traces: up to 20 per window, always sent
    this.spanSummaries = new Map();
    this.windowStart = Date.now();
    this.pending = []; // windows that failed to send, merged into the next flush (max 5)
    this.disabled = false;
    this.pausedUntil = 0;
    this.inflight = null;
    this.warned = new Set();
    if (this.enabled) {
      this.timer = setInterval(() => { this.flush(); }, this.intervalMs);
      if (this.timer.unref) this.timer.unref();
    }
  }

  warnOnce(key, msg) {
    if (this.warned.has(key)) return;
    this.warned.add(key);
    try { (this.platform.rawConsole || console).warn(msg); } catch { /* ignore */ }
  }

  isIgnored(path) {
    const p = String(path || '').split('?')[0];
    if (DEFAULT_IGNORE.includes(p) || DEFAULT_IGNORE_PREFIX.some((x) => p.startsWith(x))) return true;
    return this.ignore.some((m) => (m instanceof RegExp ? m.test(p) : p === m || p.startsWith(String(m))));
  }

  /** Record one finished request/task. `error` = 5xx or an unhandled exception (4xx is not an error). */
  record(name, durationMs, { status = null, error = false, op = 'http.server', startedAt = Date.now() - durationMs, txn = null } = {}) {
    try {
      if (!this.enabled || this.disabled) return;
      if (this.pausedUntil > Date.now() && this.quotaPaused) return;
      const d = Math.max(0, Number(durationMs) || 0);
      let t = this.tx.get(name);
      if (!t) {
        if (this.tx.size >= 500) { name = '<other>'; t = this.tx.get(name); }
        if (!t) { t = { name, op, count: 0, errors: 0, sum_ms: 0, min_ms: Infinity, max_ms: 0, buckets: {} }; this.tx.set(name, t); }
      }
      t.count++;
      if (error) t.errors++;
      t.sum_ms += d;
      if (d < t.min_ms) t.min_ms = d;
      if (d > t.max_ms) t.max_ms = d;
      const b = bucketIndex(d);
      t.buckets[b] = (t.buckets[b] || 0) + 1;
      const sample = { kind: 'server', op, trace_id: txn ? txn.trace_id : newTraceId(), ...(txn ? { span_id: txn.span_id, ...(txn.parent_span_id ? { parent_span_id: txn.parent_span_id } : {}) } : {}), name, duration_ms: Math.round(d * 10) / 10, status_code: status, ok: !error, started_at: new Date(startedAt).toISOString() };
      if (txn && txn.sampled) Object.defineProperty(sample, '_forced', { value: true, enumerable: false }); // the caller's trace is sampled: keep this request whatever its speed
      if (txn && (txn.spans.length || txn.overflow.size)) {
        if (this.client.options.performanceSpans !== false) summarise(name, txn, this.spanSummaries);
        if (txn.spans.length) Object.defineProperty(sample, '_spans', { value: sampleSpans(txn), enumerable: false });
      }
      this.sample(sample);
    } catch { /* never break the app */ }
  }

  /** Keep up to 10 samples per window: the slowest 3, up to 5 failures, one random. */
  sample(s) {
    const a = this.samples;
    if (s._forced) {
      if (this.forced.length < 20) this.forced.push(s);
      return;
    }
    a.push(s);
    if (a.length <= 40) return;
    // trim: keep the slowest 3, the first 5 failures, and a random one; stay bounded between flushes
    const slow = [...a].sort((x, y) => y.duration_ms - x.duration_ms).slice(0, 3);
    const failed = a.filter((x) => !x.ok).slice(0, 5);
    const rnd = a[Math.floor(Math.random() * a.length)];
    this.samples = [...new Set([...slow, ...failed, rnd])];
  }

  chooseSamples() {
    const forced = this.forced.slice();
    return [...forced, ...this.chooseRegular().filter((s) => !forced.includes(s))];
  }

  chooseRegular() {
    const a = this.samples;
    if (a.length <= 10) return a.slice();
    const slow = [...a].sort((x, y) => y.duration_ms - x.duration_ms).slice(0, 3);
    const failed = a.filter((x) => !x.ok && !slow.includes(x)).slice(0, 5);
    const rest = a.filter((x) => !slow.includes(x) && !failed.includes(x));
    const rnd = rest.length ? [rest[Math.floor(Math.random() * rest.length)]] : [];
    return [...slow, ...failed, ...rnd].slice(0, 10);
  }

  takeWindow() {
    if (!this.tx.size && !this.spanSummaries.size) return null;
    const w = {
      interval_start: new Date(this.windowStart).toISOString(),
      interval_seconds: Math.max(1, Math.round((Date.now() - this.windowStart) / 1000)),
      transactions: [...this.tx.values()].map((t) => ({ ...t, min_ms: t.min_ms === Infinity ? 0 : Math.round(t.min_ms * 10) / 10, max_ms: Math.round(t.max_ms * 10) / 10, sum_ms: Math.round(t.sum_ms * 10) / 10 })),
      samples: this.chooseSamples().map((s) => (s._spans ? { ...s, spans: s._spans } : s)),
      spans: [...this.spanSummaries.values()].map((s) => ({ ...s, min_ms: s.min_ms === Infinity ? 0 : Math.round(s.min_ms * 10) / 10, max_ms: Math.round(s.max_ms * 10) / 10, sum_ms: Math.round(s.sum_ms * 10) / 10 })),
    };
    this.tx = new Map();
    this.spanSummaries = new Map();
    this.samples = [];
    this.forced = [];
    this.windowStart = Date.now();
    return w;
  }

  payload(w) {
    const o = this.client.options;
    return {
      sdk: { name: 'insight.node', version: this.client.sdkVersion },
      platform: 'node',
      ...(o.release ? { release: String(o.release) } : {}),
      ...(o.environment ? { environment: String(o.environment) } : {}),
      ...(this.platform.serverName && this.platform.serverName() ? { server_name: this.platform.serverName() } : {}),
      ...w,
    };
  }

  /** Send the current window (and any that failed earlier). Resolves when done; never throws. */
  flush() {
    if (!this.enabled || this.disabled || !this.client.endpoints) return Promise.resolve(true);
    if (this.inflight) return this.inflight;
    const w = this.takeWindow();
    if (w) this.pending.push(w);
    if (!this.pending.length) return Promise.resolve(true);
    if (this.pausedUntil > Date.now()) { this.pending = this.quotaPaused ? [] : this.pending; return Promise.resolve(false); }
    this.inflight = this.sendPending().finally(() => { this.inflight = null; });
    return this.inflight;
  }

  async sendPending() {
    while (this.pending.length) {
      const w = this.pending[0];
      let res;
      try {
        res = await (this.client.options.fetch || this.platform.fetch)(this.client.endpoints.metrics, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': this.client.options.apiKey },
          body: JSON.stringify(this.payload(w)),
        });
      } catch (e) {
        this.client.log('metrics: network error', e && e.message);
        this.trimPending();
        return false;
      }
      let body = null;
      try { body = await res.json(); } catch { /* not JSON */ }
      const status = res.status;
      const retryAfter = Math.min(Number(res.headers && res.headers.get && res.headers.get('retry-after')) || 0, MAX_PAUSE_S);
      if (status >= 200 && status < 300) { this.pending.shift(); continue; }
      if (status === 401 || status === 403) {
        this.disabled = true;
        this.pending = [];
        this.warnOnce('auth', `Insight: the server rejected the API key for performance metrics (HTTP ${status}). Metrics are disabled for this session; error reporting is not affected.`);
        return false;
      }
      if (status === 429 && body && body.code === 'quota_exceeded') {
        this.quotaPaused = true;
        this.pausedUntil = Date.now() + Math.max(retryAfter, 60) * 1000;
        this.pending = [];
        this.warnOnce('quota', `Insight: your monthly tracked-request quota for performance monitoring is used up. Pausing metrics for ${Math.round(Math.max(retryAfter, 60) / 60)} min; error reporting is not affected. Upgrade your plan to continue.`);
        return false;
      }
      if (status === 429) { this.quotaPaused = false; this.pausedUntil = Date.now() + Math.max(retryAfter, 1) * 1000; return false; }
      if (status === 413) { // too large: drop sample spans first, then the samples, never the counts
        if (w.samples.some((s) => s.spans)) w.samples = w.samples.map(({ spans, ...rest }) => rest);
        else if (w.samples.length) w.samples = [];
        else this.pending.shift();
        continue;
      }
      if (status >= 500) { this.trimPending(); return false; }
      this.pending.shift(); // 400 etc.: the server will not accept this window
    }
    return true;
  }

  trimPending() {
    if (this.pending.length > MAX_WINDOWS) this.pending.splice(0, this.pending.length - MAX_WINDOWS);
  }

  close() {
    if (this.timer) clearInterval(this.timer);
    return this.flush();
  }
}
