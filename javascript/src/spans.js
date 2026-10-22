/**
 * Spans (database queries, outbound HTTP, custom steps) inside a request. Node only; not imported by the browser bundle.
 * A transaction context lives on the async scope (`scope.txn`); instrumentation calls recordSpan(), which is a cheap no-op
 * outside a transaction. See sdks/PROTOCOL.md "Spans and traces".
 */

import crypto from 'node:crypto';
import { bucketIndex } from './buckets.js';

const MAX_SPANS = 200;
const hex = (n) => crypto.randomBytes(n).toString('hex');

const TP = /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;

/** Parse a W3C traceparent header; null when missing or malformed (never throws). */
export function parseTraceparent(h) {
  try {
    const m = TP.exec(String(Array.isArray(h) ? h[0] : h || '').trim().toLowerCase());
    if (!m || m[1] !== '00' || /^0+$/.test(m[2]) || /^0+$/.test(m[3])) return null;
    return { trace_id: m[2], parent_span_id: m[3], sampled: (parseInt(m[4], 16) & 1) === 1 };
  } catch { return null; }
}
export const formatTraceparent = (traceId, spanId, sampled) => `00-${traceId}-${spanId}-${sampled ? '01' : '00'}`;

/** A new transaction: continues the caller's trace when a valid traceparent is given, else starts one. */
export function newTxn(traceparent) {
  const inbound = parseTraceparent(traceparent);
  return {
    trace_id: inbound ? inbound.trace_id : hex(16), span_id: hex(8), parent_span_id: inbound ? inbound.parent_span_id : null, sampled: inbound ? inbound.sampled : false,
    t0: process.hrtime.bigint(), started_at: Date.now(), spans: [], overflow: new Map(), dropped: 0,
  };
}

/** Does `host` (host[:port]) match the configured propagation targets? Strings: exact, "*.x.com" wildcard; RegExps are tested. */
export function matchesTarget(targets, host) {
  const h = String(host || '').toLowerCase();
  return (targets || []).some((p) => {
    if (p instanceof RegExp) return p.test(h);
    const s = String(p).toLowerCase();
    return s.startsWith('*.') ? h === s.slice(2) || h.endsWith(s.slice(1)) || h.split(':')[0].endsWith(s.slice(1)) : h === s || h.split(':')[0] === s;
  });
}

/** SQL with every literal removed (same behaviour as the server's normalizeSql, which also re-checks). */
export function normalizeSql(sql) {
  let s = String(sql == null ? '' : sql);
  s = s.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/--[^\n]*/g, ' ');
  s = s.replace(/'(?:[^']|'')*'/g, '?').replace(/\bE'(?:[^'\\]|\\.)*'/g, '?');
  s = s.replace(/\b0x[0-9a-f]+\b/gi, '?').replace(/(?<![\w$.])-?\d+(?:\.\d+)?(?:e[+-]?\d+)?\b/gi, '?');
  s = s.replace(/\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)/gi, 'IN (?)');
  s = s.replace(/\bVALUES\s*\(\s*\?(?:\s*,\s*\?)*\s*\)(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))*/gi, 'VALUES (?)');
  s = s.replace(/\s+/g, ' ').trim();
  return s.length > 300 ? s.slice(0, 297) + '...' : s;
}

/** "GET host": no path, query or credentials. */
export function httpDescription(method, urlOrHost) {
  let host = String(urlOrHost || '');
  try { host = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(host) ? host : `http://${host}`).host; } catch { host = host.split(/[/?#]/)[0].replace(/^.*@/, ''); }
  return `${String(method || 'GET').toUpperCase()} ${host}`.slice(0, 300);
}

/** Record a finished span on `txn` (the current transaction). */
export function addSpan(txn, { op, description, startMs, durationMs, ok = true, data, spanId }) {
  if (!txn) return;
  try {
    const d = Math.max(0, Number(durationMs) || 0);
    if (txn.spans.length < MAX_SPANS) {
      txn.spans.push({ span_id: spanId || hex(8), parent_id: null, op, description, start_ms: Math.round(Math.max(0, startMs) * 10) / 10, duration_ms: Math.round(d * 10) / 10, status: ok ? 'ok' : 'error', ...(data ? { data } : {}) });
    } else {
      txn.dropped++;
      const k = `${op}\u0000${description}`;
      const a = txn.overflow.get(k) || { op, description, count: 0, errors: 0, sum_ms: 0, min_ms: Infinity, max_ms: 0 };
      a.count++; if (!ok) a.errors++; a.sum_ms += d; a.min_ms = Math.min(a.min_ms, d); a.max_ms = Math.max(a.max_ms, d);
      txn.overflow.set(k, a);
    }
  } catch { /* never break the app */ }
}

/** Open a span now; call finish() when the work ends. Returns a no-op outside a transaction. */
export function openSpan(getTxn, op, description, data) {
  const txn = getTxn();
  if (!txn) return { finish() {} };
  const t0 = process.hrtime.bigint();
  const id = hex(8); // the id a propagated traceparent carries, so the next service's request joins under this span
  let done = false;
  return {
    id,
    trace_id: txn.trace_id,
    sampled: !!txn.sampled,
    finish(ok = true, extra) {
      if (done) return;
      done = true;
      const end = process.hrtime.bigint();
      addSpan(txn, { op, description, startMs: Number(t0 - txn.t0) / 1e6, durationMs: Number(end - t0) / 1e6, ok, data: extra ? { ...(data || {}), ...extra } : data, spanId: id });
    },
  };
}

/** Fold a finished transaction's spans into per-(txn, op, description) summaries. */
export function summarise(txnName, txn, into, cap = 300) {
  const add = (op, description, count, errors, sum, min, max, hist) => {
    let key = `${txnName}\u0000${op}\u0000${description}`;
    let s = into.get(key);
    if (!s) {
      if (into.size >= cap) { description = '<other>'; key = `${txnName}\u0000${op}\u0000${description}`; s = into.get(key); }
      if (!s) { s = { transaction: txnName, op, description, count: 0, errors: 0, sum_ms: 0, min_ms: Infinity, max_ms: 0, buckets: {} }; into.set(key, s); }
    }
    s.count += count; s.errors += errors; s.sum_ms += sum; s.min_ms = Math.min(s.min_ms, min); s.max_ms = Math.max(s.max_ms, max);
    for (const [b, n] of Object.entries(hist)) s.buckets[b] = (s.buckets[b] || 0) + n;
  };
  for (const sp of txn.spans) add(sp.op, sp.description, 1, sp.status === 'error' ? 1 : 0, sp.duration_ms, sp.duration_ms, sp.duration_ms, { [bucketIndex(sp.duration_ms)]: 1 });
  for (const o of txn.overflow.values()) add(o.op, o.description, o.count, o.errors, o.sum_ms, o.min_ms === Infinity ? 0 : o.min_ms, o.max_ms, { [bucketIndex(o.sum_ms / o.count)]: o.count });
}

/** Up to 100 spans for a sample: the slowest, kept in chronological order. */
export function sampleSpans(txn) {
  const all = txn.spans;
  if (all.length <= 100) return all.slice();
  const keep = new Set([...all].sort((a, b) => b.duration_ms - a.duration_ms).slice(0, 100));
  return all.filter((s) => keep.has(s));
}
