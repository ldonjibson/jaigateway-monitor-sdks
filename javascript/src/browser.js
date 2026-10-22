import { makeSdk } from './api.js';
import { installBrowserIntegrations } from './integrations-browser.js';
import { safeUrl, scrubHeaders } from './scope.js';
import { parseUA } from './ua.js';
import { startSessions } from './sessions.js';

const rawConsole = { log: console.log.bind(console), warn: console.warn.bind(console) };
const origFetch = typeof fetch === 'function' ? fetch.bind(globalThis) : undefined; // captured BEFORE we patch it for breadcrumbs

const platform = {
  name: 'browser',
  rawConsole,
  fetch: (url, init) => (origFetch ? origFetch(url, init) : Promise.reject(new Error('fetch is not available'))),
  defaultRequest() {
    if (typeof location === 'undefined') return null;
    const headers = { 'User-Agent': navigator.userAgent };
    if (document.referrer) headers.Referer = safeUrl(document.referrer);
    return { url: safeUrl(location.href), headers: scrubHeaders(headers) };
  },
  defaultMetadata() {
    const ua = parseUA(typeof navigator !== 'undefined' ? navigator.userAgent : '');
    return { browser: ua.browser, os: ua.os, path: typeof location !== 'undefined' ? location.pathname : undefined };
  },
};

const sdk = makeSdk(platform, installBrowserIntegrations);

// Optional tracing module: a separate file. CDN users load insight.tracing.min.js (global InsightTracing); npm users get it through the
// package's ./tracing export. Nothing here runs unless init({ tracing: ... }) is used.
const startTracing = async (api, cfg) => {
  try {
    let mod = typeof window !== 'undefined' ? window.InsightTracing : null;
    if (!mod && typeof __INSIGHT_IIFE__ === 'undefined') mod = await import('insight-sdk/tracing');
    if (mod) mod.enableTracing(api, cfg);
  } catch { /* tracing is optional: a failure to load must not affect error reporting */ }
};
const startReplay = async (api, cfg) => {
  try {
    let mod = typeof window !== 'undefined' ? window.InsightReplay : null;
    if (!mod && typeof __INSIGHT_IIFE__ === 'undefined') mod = await import('insight-sdk/replay');
    if (mod) mod.enableReplay(api, cfg);
  } catch { /* replay is optional: a failure to load must not affect error reporting */ }
};
const baseInit = sdk.init;
sdk.init = (options = {}) => {
  const api = baseInit(options);
  if (pendingRoute && sdk.getClient()) { sdk.getClient().__route = pendingRoute; pendingRoute = null; }
  if (options.sessions && typeof window !== 'undefined') { try { startSessions(sdk.getClient(), window); } catch { /* optional */ } }
  if (options.replay) startReplay(sdk, options.replay === true ? {} : options.replay);
  if (options.tracing) startTracing(sdk, options.tracing === true ? {} : options.tracing);
  return api;
};
sdk.api.init = (o) => sdk.init(o);
/** Name the current page by your router's pattern ("/orders/{id}"). Router helpers call this; it only has an effect when tracing is on. */
let pendingRoute = null;
export const setRoute = (name) => {
  const c = sdk.getClient();
  if (!c) { pendingRoute = name || null; return; } // React runs a child's effect before its parent's: a helper can call this before init()
  c.__route = name || null; // remembered if tracing finishes loading later
  if (c.__tracing) c.__tracing.setRoute(name);
};

sdk.api.setRoute = setRoute;
/** Replay with `manualStart` (consent): start / stop recording from your own consent code. */
sdk.api.startReplay = () => { const c = sdk.getClient(); if (c && c.replay) c.replay.start(); };
sdk.api.stopReplay = () => { const c = sdk.getClient(); if (c && c.replay) c.replay.stop(); }; // also on Insight.* for CDN users

export const {
  init, captureException, captureMessage, addBreadcrumb, setUser, setTag, setTags, setExtra, setFingerprint,
  withScope, configureScope, flush, close, isEnabled, getClient, wrap, checkIn, monitor,
} = sdk.api;
export { SDK_VERSION } from './core.js';
export default sdk.api;
