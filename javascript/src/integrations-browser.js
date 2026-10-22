import { safeUrl } from './scope.js';

const LEVEL = { log: 'info', info: 'info', warn: 'warning', error: 'error', debug: 'debug' };
const fmt = (a) => { if (typeof a === 'string') return a; if (a instanceof Error) return `${a.name}: ${a.message}`; try { return JSON.stringify(a); } catch { return String(a); } };

function describe(el, pii) {
  if (!el || !el.tagName) return '';
  const tag = el.tagName.toLowerCase();
  const id = el.id ? `#${el.id}` : '';
  const cls = typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
  const tid = el.getAttribute && (el.getAttribute('data-testid') || el.getAttribute('data-insight'));
  const text = pii && el.textContent ? ` "${el.textContent.trim().slice(0, 40)}"` : '';
  return `${tag}${id}${cls}${tid ? `[${tid}]` : ''}${text}`;
}

/** Installed once per page. Everything reads the *current* client, so init() can be called again. */
export function installBrowserIntegrations(sdk, platform) {
  const on = (name) => { const c = sdk.getClient(); return !!c && !c.disabled && c.options.integrations?.[name] !== false; };
  const crumb = (name, b) => { if (on(name)) sdk.getClient().addBreadcrumb(b); };
  const w = window;

  // --- errors -------------------------------------------------------------------------------
  w.addEventListener('error', (ev) => {
    if (!on('globalHandlers')) return;
    const c = sdk.getClient();
    const t = ev.target;
    if (t && t !== w && t.tagName) { // <img>/<script> failed to load: not an exception, but useful context
      crumb('globalHandlers', { category: 'resource', level: 'warning', message: `Failed to load <${t.tagName.toLowerCase()}> ${safeUrl(t.src || t.href || '')}` });
      return;
    }
    let err = ev.error;
    if (!err) {
      const m = String(ev.message || 'Error').replace(/^Uncaught /, '');
      const at = ev.filename ? `${ev.filename}:${ev.lineno || 0}:${ev.colno || 0}` : '<unknown>';
      err = new Error(m.replace(/^\w*Error: /, ''));
      const type = /^(\w*Error): /.exec(m);
      if (type) err.name = type[1];
      err.stack = `${err.name}: ${err.message}\n    at ${at}`;
    }
    c.captureException(err, { tags: { mechanism: 'onerror' } });
  }, true);

  w.addEventListener('unhandledrejection', (ev) => {
    if (!on('globalHandlers')) return;
    let r = ev.reason;
    if (!(r instanceof Error)) {
      const e = new Error(`Non-Error promise rejection captured with value: ${fmt(r)}`.slice(0, 500));
      e.name = 'UnhandledRejection';
      r = e;
    }
    sdk.getClient().captureException(r, { tags: { mechanism: 'onunhandledrejection' } });
  });

  // --- breadcrumbs: console -----------------------------------------------------------------
  let inConsole = false;
  for (const m of Object.keys(LEVEL)) {
    const orig = console[m];
    if (typeof orig !== 'function') continue;
    console[m] = function (...args) {
      if (!inConsole && on('console')) {
        inConsole = true;
        try { sdk.getClient().addBreadcrumb({ category: 'console', level: LEVEL[m], message: args.map(fmt).join(' ') }); } catch { /* never break logging */ }
        inConsole = false;
      }
      return orig.apply(this, args);
    };
  }

  // --- breadcrumbs: fetch / XHR -------------------------------------------------------------
  const sdkUrl = () => sdk.getClient() && sdk.getClient().endpoints && sdk.getClient().endpoints.events;
  const origFetch = w.fetch && w.fetch.bind(w);
  if (origFetch) {
    w.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || String(input);
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (!on('fetch') || url === sdkUrl()) return origFetch(input, init);
      const start = Date.now();
      const done = (status) => crumb('fetch', { category: 'http', level: status === 0 || status >= 500 ? 'error' : status >= 400 ? 'warning' : 'info', message: `${method} ${safeUrl(url)} [${status || 'failed'}]`, data: { method, url: safeUrl(url), status_code: status, duration_ms: Date.now() - start } });
      return origFetch(input, init).then((res) => { done(res.status); return res; }, (err) => { done(0); throw err; });
    };
  }
  const XHR = w.XMLHttpRequest && w.XMLHttpRequest.prototype;
  if (XHR) {
    const open = XHR.open, send = XHR.send;
    XHR.open = function (method, url, ...rest) { this.__insight = { method: String(method).toUpperCase(), url: String(url) }; return open.call(this, method, url, ...rest); };
    XHR.send = function (...args) {
      const info = this.__insight;
      if (info && on('xhr') && info.url !== sdkUrl()) {
        const start = Date.now();
        this.addEventListener('loadend', () => crumb('xhr', { category: 'http', level: this.status === 0 || this.status >= 500 ? 'error' : this.status >= 400 ? 'warning' : 'info', message: `${info.method} ${safeUrl(info.url)} [${this.status || 'failed'}]`, data: { method: info.method, url: safeUrl(info.url), status_code: this.status, duration_ms: Date.now() - start } }));
      }
      return send.apply(this, args);
    };
  }

  // --- breadcrumbs: clicks and navigation ---------------------------------------------------
  w.document.addEventListener('click', (ev) => {
    if (!on('dom')) return;
    const c = sdk.getClient();
    const el = ev.target && ev.target.closest ? ev.target.closest('button,a,input,select,textarea,[role=button],[data-testid],[data-insight]') || ev.target : ev.target;
    c.addBreadcrumb({ category: 'ui.click', message: describe(el, c.options.sendDefaultPii) });
  }, true);

  let last = w.location.pathname + w.location.search;
  const nav = () => {
    const to = w.location.pathname + w.location.search;
    if (to !== last) { crumb('history', { category: 'navigation', message: `${safeUrl(last)} → ${safeUrl(to)}`, data: { from: safeUrl(last), to: safeUrl(to) } }); last = to; }
  };
  for (const m of ['pushState', 'replaceState']) {
    const orig = w.history[m];
    w.history[m] = function (...a) { const r = orig.apply(this, a); try { nav(); } catch { /* ignore */ } return r; };
  }
  w.addEventListener('popstate', nav);

  // --- delivery on the way out, and a tiny offline queue ------------------------------------
  const KEY = 'insight:queue:v1';
  const store = (fn) => { try { return fn(w.localStorage); } catch { return undefined; } };
  const leave = () => {
    const c = sdk.getClient();
    if (!c || c.disabled) return;
    c.flush(0, { keepalive: true });
    const rest = c.queue.slice(0, 20).map((q) => q.event);
    if (rest.length) store((s) => s.setItem(KEY, JSON.stringify({ at: Date.now(), key: c.options.apiKey.slice(0, 8), events: rest })));
  };
  w.addEventListener('pagehide', leave);
  w.document.addEventListener('visibilitychange', () => { if (w.document.visibilityState === 'hidden') leave(); });
  platform.onInit = (client) => {
    const saved = store((s) => { const v = s.getItem(KEY); s.removeItem(KEY); return v; });
    if (!saved) return;
    try {
      const { at, key, events } = JSON.parse(saved);
      if (key === client.options.apiKey.slice(0, 8) && Date.now() - at < 24 * 3600_000) for (const e of events) client.push(e);
    } catch { /* corrupt: ignore */ }
  };
}
