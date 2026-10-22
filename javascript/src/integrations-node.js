import { safeUrl, scrubHeaders } from './scope.js';

const LEVEL = { log: 'info', info: 'info', warn: 'warning', error: 'error', debug: 'debug' };
const fmt = (a) => { if (typeof a === 'string') return a; if (a instanceof Error) return `${a.name}: ${a.message}`; try { return JSON.stringify(a); } catch { return String(a); } };

export function installNodeIntegrations(sdk, platform) {
  const on = (name) => { const c = sdk.getClient(); return !!c && !c.disabled && c.options.integrations?.[name] !== false; };

  // --- process-level errors -----------------------------------------------------------------
  process.on('uncaughtException', (err, origin) => {
    const c = sdk.getClient();
    if (!on('globalHandlers')) { // not ours to handle: behave like Node does without a listener
      if (process.listenerCount('uncaughtException') <= 1) { platform.rawConsole.error(err); process.exit(1); }
      return;
    }
    c.captureException(err, { level: 'fatal', tags: { mechanism: origin || 'uncaughtException' } });
    c.flush(c.options.shutdownTimeout ?? 2000).finally(() => {
      // Node would have exited; we only keep running if the app has its own handler
      if (c.options.exitOnUncaughtException !== false && process.listenerCount('uncaughtException') <= 1) {
        platform.rawConsole.error(err);
        process.exit(1);
      }
    });
  });
  process.on('unhandledRejection', (reason) => {
    if (!on('globalHandlers')) return;
    let err = reason;
    if (!(reason instanceof Error)) { err = new Error(`Non-Error promise rejection captured with value: ${fmt(reason)}`.slice(0, 500)); err.name = 'UnhandledRejection'; }
    sdk.getClient().captureException(err, { tags: { mechanism: 'unhandledRejection' } });
    if (process.listenerCount('unhandledRejection') <= 1) platform.rawConsole.error('Unhandled promise rejection:', reason);
  });
  process.on('beforeExit', () => { const c = sdk.getClient(); if (c && !c.disabled) c.flush(c.options.shutdownTimeout ?? 2000); });

  // --- breadcrumbs --------------------------------------------------------------------------
  let inConsole = false;
  for (const m of Object.keys(LEVEL)) {
    const orig = console[m];
    console[m] = function (...args) {
      if (!inConsole && on('console')) {
        inConsole = true;
        try { sdk.getClient().addBreadcrumb({ category: 'console', level: LEVEL[m], message: args.map(fmt).join(' ') }); } catch { /* ignore */ }
        inConsole = false;
      }
      return orig.apply(this, args);
    };
  }
  const origFetch = globalThis.fetch && globalThis.fetch.bind(globalThis);
  if (origFetch) {
    globalThis.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : (input && input.url) || String(input);
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      const c = sdk.getClient();
      if (!on('fetch') || url === (c.endpoints && c.endpoints.events)) return origFetch(input, init);
      const start = Date.now();
      const done = (status) => c.addBreadcrumb({ category: 'http', level: status === 0 || status >= 500 ? 'error' : status >= 400 ? 'warning' : 'info', message: `${method} ${safeUrl(url)} [${status || 'failed'}]`, data: { method, url: safeUrl(url), status_code: status, duration_ms: Date.now() - start } });
      return origFetch(input, init).then((r) => { done(r.status); return r; }, (e) => { done(0); throw e; });
    };
  }
}

/** The request as we report it: no cookies / auth headers, query secrets filtered. */
export function requestData(req) {
  const host = (req.headers && req.headers.host) || '';
  const proto = req.protocol || (req.socket && req.socket.encrypted ? 'https' : 'http');
  const full = req.originalUrl || req.url || '/';
  const url = host ? safeUrl(`${proto}://${host}${full}`) : safeUrl(full);
  const q = full.includes('?') ? safeUrl(full).split('?')[1] : undefined;
  return { url, method: req.method, ...(q ? { query: q } : {}), headers: scrubHeaders(req.headers) };
}
