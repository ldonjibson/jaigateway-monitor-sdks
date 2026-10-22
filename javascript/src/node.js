import { AsyncLocalStorage } from 'node:async_hooks';
import os from 'node:os';
import path from 'node:path';
import { makeSdk } from './api.js';
import { installNodeIntegrations, requestData } from './integrations-node.js';
import { parseStack } from './stack.js';
import { Metrics } from './metrics.js';
import { installSpanInstrumentation } from './instrument-node.js';
import { newTxn, openSpan, formatTraceparent } from './spans.js';

const als = new AsyncLocalStorage();
const cwd = process.cwd();
const rawConsole = { log: console.log.bind(console), warn: console.warn.bind(console), error: console.error.bind(console) };

const platform = {
  name: 'node',
  rawConsole,
  fetch: (url, init) => globalThis.fetch(url, init),
  getStore: () => als.getStore(),
  runInScope: (scope, fn) => als.run(scope, fn),
  serverName: () => os.hostname(),
  // file:///srv/app/a.js -> a.js (relative to where the app runs) so reports don't leak the server's directory layout
  stripPath(f) {
    let p = f.startsWith('file://') ? decodeURIComponent(f.slice(7)) : f;
    if (path.isAbsolute(p) && p.startsWith(cwd + path.sep)) p = path.relative(cwd, p);
    return p;
  },
  defaultMetadata: () => ({ browser: `Node ${process.versions.node}`, os: process.platform }),
  onInit(client) {
    client.metrics = new Metrics(client, platform);
    if (!platform.spansInstalled) { platform.spansInstalled = true; try { installSpanInstrumentation(sdk); } catch { /* optional */ } }
  },
  syntheticStack: () => parseStack(new Error().stack, { skip: 4, strip: platform.stripPath }),
};

const sdk = makeSdk(platform, installNodeIntegrations);
const api = sdk.api;

const envRelease = () => process.env.INSIGHT_RELEASE || process.env.VERCEL_GIT_COMMIT_SHA || process.env.GITHUB_SHA || process.env.HEROKU_SLUG_COMMIT || process.env.RENDER_GIT_COMMIT || undefined;
export const init = (o = {}) => api.init({
  apiKey: process.env.INSIGHT_API_KEY,
  host: process.env.INSIGHT_HOST,
  release: envRelease(),
  environment: process.env.INSIGHT_ENVIRONMENT || process.env.NODE_ENV,
  ...o,
});

/** "GET /items/:id": the matched route template, never the raw URL (404s and scanners would otherwise create endless names). */
function transactionName(req) {
  const r = req.route && req.route.path;
  if (typeof r === 'string') return `${req.method} ${(req.baseUrl || '') + (r === '/' && req.baseUrl ? '' : r) || '/'}`.replace(/:(\w+)/g, '{$1}');
  return `${req.method} <unmatched>`;
}

/** Time any async unit of work (a job, a serverless handler, a Next.js route handler) as a transaction. */
export async function transaction(name, fn, opts = {}) {
  const c = sdk.getClient();
  if (!c) return fn();
  const started = Date.now(), t0 = process.hrtime.bigint();
  const scope = c.scope.clone();
  scope.txn = newTxn(opts.traceparent); // work inside the callback records spans and errors against this transaction (opts.traceparent joins a trace)
  let error = false;
  try {
    return await als.run(scope, fn);
  } catch (e) {
    error = true;
    throw e;
  } finally {
    if (c.metrics) c.metrics.record(name, Number(process.hrtime.bigint() - t0) / 1e6, { error, op: opts.op || 'custom', startedAt: started, txn: scope.txn });
  }
}

/** The traceparent header value for the current request (to pass on with a client we do not instrument), or undefined. */
export function getTraceparent() {
  const c = sdk.getClient();
  const txn = c && c.scope && c.scope.txn;
  return txn ? formatTraceparent(txn.trace_id, txn.span_id, txn.sampled) : undefined;
}

/** Time a step inside a request or transaction (a query through an ORM we do not instrument, a cache call, a template render). */
export function startSpan(op, description, data) {
  return openSpan(() => { const c = sdk.getClient(); return c && c.scope && c.scope.txn; }, String(op).slice(0, 40), String(description).slice(0, 300), data);
}
export async function span(op, description, fn, data) {
  const s = startSpan(op, description, data);
  try {
    const r = await fn();
    s.finish(true);
    return r;
  } catch (e) {
    s.finish(false);
    throw e;
  }
}

/** Record a transaction you timed yourself. */
export function recordTransaction(name, durationMs, opts = {}) {
  const c = sdk.getClient();
  if (c && c.metrics) c.metrics.record(name, durationMs, opts);
}

/** Express / Connect: first middleware. Gives every request its own scope (user, tags, breadcrumbs). */
export function requestHandler() {
  return function insightRequestHandler(req, res, next) {
    const c = sdk.getClient();
    if (!c) return next();
    const scope = c.scope.clone();
    scope.request = requestData(req);
    scope.txn = newTxn(req.headers && req.headers.traceparent); // continues the caller's trace when it sent a traceparent
    const started = Date.now(), t0 = process.hrtime.bigint();
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      const m = c.metrics;
      if (!m || !m.enabled || m.isIgnored(req.originalUrl || req.url)) return;
      const ms = Number(process.hrtime.bigint() - t0) / 1e6;
      const status = res.statusCode;
      m.record(transactionName(req), ms, { status, error: status >= 500 || !!req.__insightError, startedAt: started, txn: scope.txn });
    };
    if (res && typeof res.on === 'function') { res.on('finish', finish); res.on('close', finish); }
    als.run(scope, () => {
      c.addBreadcrumb({ category: 'http', message: `${req.method} ${String(req.originalUrl || req.url).split('?')[0]}` });
      next();
    });
  };
}

/** Express / Connect: after your routes. Reports errors that would be a 5xx and passes them on. */
export function errorHandler(opts = {}) {
  const shouldCapture = opts.shouldHandleError || ((err) => { const s = Number(err && (err.status || err.statusCode)); return !s || s >= 500; });
  return function insightErrorHandler(err, req, res, next) {
    const c = sdk.getClient();
    req.__insightError = true; // the request will end as a failure for the performance numbers, even if the app sends a custom status
    if (c && shouldCapture(err)) c.captureException(err, { tags: { mechanism: 'express' }, request: requestData(req) });
    next(err);
  };
}

/** Next.js 15: `export const onRequestError = Insight.captureRequestError` in instrumentation.ts */
export function captureRequestError(err, request = {}, context = {}) {
  const c = sdk.getClient();
  if (!c) return;
  c.withScope((scope) => {
    scope.setTags({ mechanism: 'next.onRequestError', ...(context.routePath ? { route: context.routePath } : {}), ...(context.routeType ? { route_type: context.routeType } : {}), ...(context.routerKind ? { router: context.routerKind } : {}) });
    c.captureException(err, { request: request.path ? { url: safeRequestPath(request.path), method: request.method, headers: Object.fromEntries(Object.entries(request.headers || {}).filter(([k]) => !/^(cookie|authorization|set-cookie|x-api-key)$/i.test(k))) } : undefined });
  });
}
const safeRequestPath = (p) => String(p).split('#')[0].slice(0, 500);

/** Browser-only (names pages for browser tracing); present here so code shared with the browser imports cleanly. */
export const setRoute = () => {};

export const {
  captureException, captureMessage, addBreadcrumb, setUser, setTag, setTags, setExtra, setFingerprint,
  withScope, configureScope, flush, close, isEnabled, getClient, wrap, checkIn, monitor,
} = api;
export { SDK_VERSION } from './core.js';
export default { ...api, init, requestHandler, errorHandler, captureRequestError, transaction, recordTransaction, startSpan, span, getTraceparent };
