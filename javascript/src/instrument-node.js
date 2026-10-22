/**
 * Automatic spans for Node: outbound HTTP (http, https, fetch) and the pg / mysql2 database drivers.
 * Everything is wrapped so an instrumentation bug can never break the app; only SQL text (never parameters) and METHOD host
 * (never path, query or credentials) are recorded.
 */

import http from 'node:http';
import https from 'node:https';
import { createRequire } from 'node:module';
import path from 'node:path';
import { openSpan, normalizeSql, httpDescription, matchesTarget, formatTraceparent } from './spans.js';

const patched = new Set();

export function installSpanInstrumentation(sdk) {
  const client = () => sdk.getClient();
  const enabled = (name) => { const c = client(); return !!c && !c.disabled && c.options.performance !== false && c.options.performanceSpans !== false && c.options.integrations?.[name] !== false; };
  const getTxn = () => { const c = client(); return c && c.scope && c.scope.txn; };
  const isOwnTraffic = (host, p) => {
    const c = client();
    if (!c || !c.endpoints) return false;
    try { return new URL(c.endpoints.events).host === host && /^\/api\/(ingest|ping)\//.test(p || ''); } catch { return false; }
  };
  const safe = (fn) => { try { return fn(); } catch { return undefined; } };
  /** traceparent for a call to `host`, only when the app listed it in tracePropagationTargets (cross-origin headers need the target's consent). */
  const propagate = (span, host) => {
    const c = client();
    if (!c || !span || !span.id || !matchesTarget(c.options.tracePropagationTargets, host)) return null;
    return formatTraceparent(span.trace_id, span.id, span.sampled);
  };

  // --- http / https ---------------------------------------------------------------------------------------------
  for (const [name, mod] of [['http', http], ['https', https]]) {
    if (patched.has(name)) continue;
    patched.add(name);
    const orig = mod.request;
    const wrap = (origFn) => function patchedRequest(...args) {
      const req = origFn.apply(this, args);
      safe(() => {
        if (!enabled('http') || !getTxn()) return;
        const o = typeof args[0] === 'string' || args[0] instanceof URL ? { ...(typeof args[1] === 'object' ? args[1] : {}), ...urlParts(args[0]) } : args[0] || {};
        const host = (o.host || o.hostname || 'localhost') + (o.port && !String(o.host || '').includes(':') ? `:${o.port}` : '');
        if (isOwnTraffic(host, o.path)) return;
        const method = (o.method || 'GET').toUpperCase();
        const span = openSpan(getTxn, 'http.client', httpDescription(method, host), { 'http.method': method, host });
        const tp = propagate(span, host);
        if (tp && !req.getHeader('traceparent') && !req.headersSent) req.setHeader('traceparent', tp);
        let status = null;
        req.once('response', (res) => {
          status = res.statusCode;
          res.once('end', () => span.finish(status < 500, { 'http.status_code': status }));
          res.once('close', () => span.finish(status < 500, { 'http.status_code': status }));
        });
        req.once('error', () => span.finish(false));
        req.once('timeout', () => span.finish(false));
      });
      return req;
    };
    mod.request = wrap(orig);
    mod.get = function patchedGet(...args) { const r = mod.request(...args); r.end(); return r; }; // http.get calls its own internal request, so route it through ours
  }

  // --- fetch (undici) -------------------------------------------------------------------------------------------
  if (typeof globalThis.fetch === 'function' && !patched.has('fetch-spans')) {
    patched.add('fetch-spans');
    const prev = globalThis.fetch;
    globalThis.fetch = function spanFetch(input, init) {
      let span = null;
      safe(() => {
        if (!enabled('fetch') || !getTxn()) return;
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : (input && input.url) || '';
        const u = new URL(url);
        if (isOwnTraffic(u.host, u.pathname)) return;
        const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        span = openSpan(getTxn, 'http.client', httpDescription(method, u.host), { 'http.method': method, host: u.host });
        const tp = propagate(span, u.host);
        if (tp) {
          const headers = new Headers((init && init.headers) || (input && input.headers) || undefined);
          if (!headers.has('traceparent')) { headers.set('traceparent', tp); init = { ...(init || {}), headers }; }
        }
      });
      const p = prev.call(this, input, init);
      if (span && p && typeof p.then === 'function') p.then((r) => span.finish(r.status < 500, { 'http.status_code': r.status }), () => span.finish(false));
      return p;
    };
  }

  // --- pg / mysql2: resolved from the APP's node_modules so we patch the very module the app uses ----------------------
  let req;
  try { req = createRequire(path.join(process.cwd(), 'insight-resolver.js')); } catch { req = null; }
  const tryRequire = (id) => { try { return req ? req(id) : null; } catch { return null; } };

  const pg = !patched.has('pg') && tryRequire('pg');
  if (pg && pg.Client && pg.Client.prototype.query) {
    patched.add('pg');
    const orig = pg.Client.prototype.query;
    pg.Client.prototype.query = function patchedQuery(...args) {
      let span = null;
      safe(() => {
        if (!enabled('pg') || !getTxn()) return;
        const first = args[0];
        const text = typeof first === 'string' ? first : first && typeof first.text === 'string' ? first.text : null;
        if (text === null) return;
        span = openSpan(getTxn, 'db.query', normalizeSql(text), { 'db.system': 'postgresql', ...(this.database ? { 'db.name': String(this.database) } : {}) });
        const cbIndex = args.findIndex((a, i) => i > 0 && typeof a === 'function');
        if (cbIndex > 0) { const cb = args[cbIndex]; args[cbIndex] = function (err, ...rest) { span.finish(!err); return cb.call(this, err, ...rest); }; }
        else if (first && typeof first === 'object' && typeof first.callback === 'function') { const cb = first.callback; first.callback = function (err, ...rest) { span.finish(!err); return cb.call(this, err, ...rest); }; }
      });
      const r = orig.apply(this, args);
      if (span) {
        if (r && typeof r.then === 'function') r.then(() => span.finish(true), () => span.finish(false));
        else if (r && typeof r.once === 'function') { r.once('end', () => span.finish(true)); r.once('error', () => span.finish(false)); }
      }
      return r;
    };
  }

  const mysql = !patched.has('mysql2') && (tryRequire('mysql2/lib/connection.js') || null);
  if (mysql && mysql.prototype && mysql.prototype.query) {
    patched.add('mysql2');
    for (const method of ['query', 'execute']) {
      const orig = mysql.prototype[method];
      if (typeof orig !== 'function') continue;
      mysql.prototype[method] = function patchedMysql(...args) {
        let span = null;
        safe(() => {
          if (!enabled('mysql2') || !getTxn()) return;
          const first = args[0];
          const text = typeof first === 'string' ? first : first && typeof first.sql === 'string' ? first.sql : null;
          if (text === null) return;
          span = openSpan(getTxn, 'db.query', normalizeSql(text), { 'db.system': 'mysql' });
          const i = args.findIndex((a) => typeof a === 'function');
          if (i >= 0) { const cb = args[i]; args[i] = function (err, ...rest) { span.finish(!err); return cb.call(this, err, ...rest); }; }
        });
        const r = orig.apply(this, args);
        if (span && r && typeof r.once === 'function' && !args.some((a) => typeof a === 'function')) { r.once('end', () => span.finish(true)); r.once('error', () => span.finish(false)); }
        return r;
      };
    }
  }
}

function urlParts(u) {
  const x = new URL(String(u));
  return { host: x.hostname, port: x.port || undefined, path: x.pathname + x.search, protocol: x.protocol };
}
