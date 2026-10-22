/**
 * Browser tracing and page performance (opt-in; a separate file so the core bundle stays small).
 * Records page loads / navigations with Core Web Vitals and the fetch/XHR calls they make, adds a `traceparent` header to
 * same-origin requests and to hosts the app lists, and gives errors the trace id of the page they happened on.
 * Everything is sampled per visitor session. See sdks/PROTOCOL.md "Distributed tracing and browser performance".
 */

import { bucketIndex } from './buckets.js';

const hex = (bytes) => {
  const a = new Uint8Array(bytes);
  (self.crypto || window.crypto).getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
};

/** `/orders/8231/items/3f2a…` → `/orders/{id}/items/{id}`. Replace this with a router's real pattern via routeName or setRoute. */
export function normalizePath(pathname) {
  const parts = String(pathname || '/').split('?')[0].split('#')[0].split('/').slice(0, 9);
  return parts.map((seg) => (/^\d+$/.test(seg) || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(seg) || /^[0-9a-f]{8,}$/i.test(seg) || /^[A-Za-z0-9_-]{20,}$/.test(seg) ? '{id}' : seg)).join('/') || '/';
}

/** CLS as the largest "session window" of shifts (gap < 1 s, window <= 5 s), ignoring shifts right after input. */
export function clsFromEntries(entries) {
  let max = 0, cur = 0, first = 0, last = 0;
  for (const e of entries) {
    if (e.hadRecentInput) continue;
    if (cur && e.startTime - last < 1000 && e.startTime - first < 5000) cur += e.value;
    else { cur = e.value; first = e.startTime; }
    last = e.startTime;
    if (cur > max) max = cur;
  }
  return max;
}

/** INP: the slowest interaction (for few interactions), or roughly the 98th percentile for many. */
export function inpFromDurations(durations) {
  if (!durations.length) return null;
  const s = durations.slice().sort((a, b) => b - a);
  return s[Math.min(Math.floor(s.length / 50), s.length - 1)];
}

const deviceOf = (ua) => (/iPad|Tablet/i.test(ua) ? 'tablet' : /Mobi|Android|iPhone/i.test(ua) ? 'mobile' : 'desktop');
const browserOf = (ua) => {
  let m;
  if ((m = /(Edg|EdgA|EdgiOS)\/(\d+)/.exec(ua))) return `Edge ${m[2]}`;
  if ((m = /(Firefox|FxiOS)\/(\d+)/.exec(ua))) return `Firefox ${m[2]}`;
  if ((m = /(Chrome|CriOS)\/(\d+)/.exec(ua))) return `Chrome ${m[2]}`;
  if ((m = /Version\/(\d+).*Safari/.exec(ua))) return `Safari ${m[1]}`;
  return 'Other';
};

function targetMatches(targets, host) {
  const h = String(host || '').toLowerCase();
  return (targets || []).some((p) => {
    if (p instanceof RegExp) return p.test(h);
    const s = String(p).toLowerCase();
    return s.startsWith('*.') ? h === s.slice(2) || h.split(':')[0].endsWith(s.slice(1)) : h === s || h.split(':')[0] === s;
  });
}

export function enableTracing(sdk, opts = {}) {
  const client = sdk.getClient && sdk.getClient();
  if (!client || client.disabled || !client.endpoints || typeof window === 'undefined' || client.__tracing) return null;
  const cfg = typeof opts === 'object' && opts ? opts : {};
  const sampleRate = Math.min(1, Math.max(0, cfg.sampleRate ?? 0.1));
  const wantVitals = cfg.vitals !== false;
  const ua = navigator.userAgent;
  const metricsUrl = client.endpoints.metrics;
  const own = (url) => { try { return new URL(url, location.href).href.startsWith(new URL(metricsUrl).origin + '/api/'); } catch { return false; } };
  let disabled = false;
  const warned = new Set();
  const warn = (k, m) => { if (!warned.has(k)) { warned.add(k); try { console.warn(m); } catch { /* ignore */ } } };

  // one sampling decision per browser session, so a visitor's navigations are all in or all out
  let sampled;
  try {
    const k = 'insight:tracing:v1';
    const v = window.sessionStorage.getItem(k);
    if (v === '1' || v === '0') sampled = v === '1';
    else { sampled = Math.random() < sampleRate; window.sessionStorage.setItem(k, sampled ? '1' : '0'); }
  } catch { sampled = Math.random() < sampleRate; }

  let route = client.__route ? normalizePath(String(client.__route)) : null; // set by setRoute() (router integrations), possibly before this module loaded
  const routeNow = () => {
    try { if (route) return route; if (typeof cfg.routeName === 'function') { const r = cfg.routeName(location); if (r) return String(r).slice(0, 300); } } catch { /* fall back */ }
    return normalizePath(location.pathname);
  };

  let page = null;
  const startPage = (op) => {
    const p = {
      op, trace_id: hex(16), span_id: hex(8), name: routeNow(), t0: performance.now(), startedAt: Date.now(), spans: [],
      lcp: null, fcp: null, ttfb: null, cls: null, inp: null, shifts: [], interactions: [], sent: false,
    };
    page = p;
    client.globalScope.txn = { trace_id: p.trace_id, span_id: p.span_id, sampled }; // errors captured on this page carry the trace id
    return p;
  };

  // --- fetch / XHR: spans, plus traceparent to same-origin and listed hosts -----------------------------------------------------
  const propagateTo = (url) => {
    try {
      const u = new URL(url, location.href);
      return u.origin === location.origin || targetMatches(cfg.tracePropagationTargets, u.host);
    } catch { return false; }
  };
  const span = (method, url) => {
    const p = page;
    if (!p || p.spans.length >= 100) return null;
    let host = '';
    try { host = new URL(url, location.href).host; } catch { return null; }
    const s = { span_id: hex(8), op: 'http.client', description: `${method} ${host}`, start_ms: Math.round((performance.now() - p.t0) * 10) / 10, duration_ms: 0, status: 'ok', data: { 'http.method': method, host }, _t: performance.now() };
    p.spans.push(s);
    return { s, header: `00-${p.trace_id}-${s.span_id}-${sampled ? '01' : '00'}` };
  };
  const done = (sp, status, failed) => {
    if (!sp) return;
    sp.s.duration_ms = Math.round((performance.now() - sp.s._t) * 10) / 10;
    sp.s.status = failed || status >= 500 || status === 0 ? 'error' : 'ok';
    if (status) sp.s.data['http.status_code'] = status;
    delete sp.s._t;
  };

  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function tracedFetch(input, init) {
      let sp = null;
      try {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : (input && input.url) || '';
        if (!disabled && url && !own(url)) {
          sp = span(String((init && init.method) || (input && input.method) || 'GET').toUpperCase(), url);
          if (sp && propagateTo(url)) {
            const headers = new Headers((init && init.headers) || (input && input.headers) || undefined);
            if (!headers.has('traceparent')) { headers.set('traceparent', sp.header); init = { ...(init || {}), headers }; }
          }
        }
      } catch { /* never break the app's request */ }
      const p = origFetch.call(this, input, init);
      if (sp) p.then((r) => done(sp, r.status, false), () => done(sp, 0, true));
      return p;
    };
  }
  const X = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  if (X) {
    const open = X.open, send = X.send;
    X.open = function (method, url, ...rest) { try { this.__itrace = { method: String(method).toUpperCase(), url: String(url) }; } catch { /* ignore */ } return open.call(this, method, url, ...rest); };
    X.send = function (...args) {
      try {
        const i = this.__itrace;
        if (i && !disabled && !own(i.url)) {
          const sp = span(i.method, i.url);
          if (sp) {
            if (propagateTo(i.url)) { try { this.setRequestHeader('traceparent', sp.header); } catch { /* headers not allowed here */ } }
            this.addEventListener('loadend', () => done(sp, this.status, this.status === 0));
          }
        }
      } catch { /* ignore */ }
      return send.apply(this, args);
    };
  }

  // --- web vitals --------------------------------------------------------------------------------------------------------------
  const observers = [];
  const observe = (type, cb, extra) => {
    try {
      const o = new PerformanceObserver((list) => { try { cb(list.getEntries()); } catch { /* ignore */ } });
      o.observe({ type, buffered: true, ...extra });
      observers.push(o);
    } catch { /* not supported in this browser */ }
  };
  if (wantVitals) {
    observe('paint', (es) => { for (const e of es) if (e.name === 'first-contentful-paint' && page && page.op === 'pageload') page.fcp = e.startTime; });
    observe('largest-contentful-paint', (es) => { if (page && page.op === 'pageload' && es.length) page.lcp = es[es.length - 1].startTime; });
    observe('layout-shift', (es) => { if (page && page.op === 'pageload') page.shifts.push(...es); });
    observe('event', (es) => { if (page) for (const e of es) if (e.interactionId > 0) page.interactions.push(e.duration); }, { durationThreshold: 40 });
    try { const nav = performance.getEntriesByType('navigation')[0]; if (nav && nav.responseStart > 0) startPage('pageload').ttfb = nav.responseStart; } catch { /* ignore */ }
  }
  if (!page) startPage('pageload');

  // --- sending ---------------------------------------------------------------------------------------------------------------------
  const vitalsOf = (p) => {
    const out = {};
    const add = (k, v) => { if (v !== null && v !== undefined && isFinite(v) && v >= 0) out[k] = { count: 1, buckets: { [bucketIndex(v)]: 1 } }; };
    add('lcp', p.lcp); add('fcp', p.fcp); add('ttfb', p.ttfb); add('inp', inpFromDurations(p.interactions));
    if (p.op === 'pageload') add('cls', Math.round(clsFromEntries(p.shifts) * 1000));
    return out;
  };

  const finish = () => {
    const p = page;
    if (!p || p.sent) return;
    p.sent = true;
    if (!sampled || disabled || client.disabled) return;
    const o = client.options;
    const duration = Math.round(Math.max(p.lcp || 0, performance.now() - p.t0) * 10) / 10;
    const spans = p.spans.map(({ _t, ...s }) => s);
    const body = {
      sdk: { name: 'insight.browser', version: client.sdkVersion }, platform: 'javascript',
      ...(o.release ? { release: String(o.release) } : {}), ...(o.environment ? { environment: String(o.environment) } : {}),
      interval_start: new Date(p.startedAt).toISOString(), interval_seconds: Math.max(1, Math.round(duration / 1000)),
      pages: [{ name: p.name, count: 1, sample_rate: sampleRate || 1, browser: browserOf(ua), device: deviceOf(ua), vitals: vitalsOf(p) }],
      samples: [{ kind: 'browser', op: p.op, trace_id: p.trace_id, span_id: p.span_id, name: p.name, duration_ms: duration, ok: !spans.some((s) => s.status === 'error'), started_at: new Date(p.startedAt).toISOString(), spans }],
    };
    try {
      const f = client.options.fetch || origFetch.bind(window);
      Promise.resolve(f(metricsUrl, { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json', 'X-API-Key': o.apiKey }, body: JSON.stringify(body) }))
        .then((res) => {
          if (res.status === 401 || res.status === 403) { disabled = true; warn('auth', `Insight: the server rejected the API key for browser performance data (HTTP ${res.status}). Tracing is off for this page session.`); }
          else if (res.status === 429) { disabled = true; warn('quota', 'Insight: your monthly tracked-page-load quota is used up (or rate limited). Browser performance data is paused for this page session; error reporting is not affected.'); }
        }, () => {});
    } catch { /* never break the page */ }
  };

  // report when the page is hidden/closed; a router navigation ends one page record and starts the next
  const onHide = () => { if (document.visibilityState === 'hidden') finish(); };
  document.addEventListener('visibilitychange', onHide);
  window.addEventListener('pagehide', finish);
  let lastPath = location.pathname + location.search;
  const onNav = () => {
    const now = location.pathname + location.search;
    if (now === lastPath) return;
    lastPath = now;
    finish();
    route = null; // the new page gets its own name: the router helper calls setRoute() again right after it renders
    startPage('navigation');
  };
  for (const m of ['pushState', 'replaceState']) { const orig = history[m]; history[m] = function (...a) { const r = orig.apply(this, a); try { onNav(); } catch { /* ignore */ } return r; }; }
  window.addEventListener('popstate', onNav);

  const api = {
    /** Name the current page by its route pattern (`/orders/[id]`); router integrations call this. */
    // whatever a router helper passes, ids that slipped through are still replaced, so a raw URL can never become a page name
    setRoute(name) { route = name ? normalizePath(String(name).slice(0, 300)) : null; if (page) page.name = routeNow(); },
    /** Stop recording and sending for this page session. */
    disable() { disabled = true; observers.forEach((o) => { try { o.disconnect(); } catch { /* ignore */ } }); },
    get traceId() { return page && page.trace_id; },
    sampled,
  };
  client.__tracing = api;
  return api;
}
