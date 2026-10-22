/**
 * Session replay (opt-in; a separate file so the core bundle stays small): records what a visitor did on the page (DOM changes,
 * clicks, scrolls, navigation) so a developer can watch it later, next to the error it led to. Not video: rrweb records the DOM.
 *
 * Privacy first: ALL text is masked and ALL inputs are hidden by default (`maskAllText`, `maskAllInputs`); password and card
 * fields are never recorded; `.insight-block` removes an element from the recording, `.insight-ignore` ignores an input's
 * events, `.insight-unmask` shows the text inside it. Nothing is recorded until init({ replay }) is used, and with
 * `replay: { manualStart: true }` not before your consent code calls Insight.startReplay().
 *
 * Modes (per tab): "session" records and uploads continuously (sampleRate of sessions); "buffer" keeps only the last ~30-60 s in memory and
 * uploads them, and everything after, when an error is reported (errorSampleRate of the rest). See sdks/PROTOCOL.md "Session replay".
 */

import { record as rrwebRecord } from '@rrweb/record';

const KEY = 'insight:replay:v1';
const IDLE_MS = 30 * 60_000;
const MAX_CHUNK_EVENTS = 3000;

const rid = () => {
  try { const a = new Uint8Array(16); crypto.getRandomValues(a); return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join(''); } catch { return (Math.random().toString(16).slice(2) + Date.now().toString(16)).padEnd(32, '0').slice(0, 32); }
};

/** Replace every visible character but keep spaces and the length, so the layout in the replay still looks right. */
export const maskText = (t) => String(t).replace(/[^\s]/g, '*');

/** Defaults and normalisation of init({ replay }). */
export function replayOptions(cfg = {}) {
  const c = cfg === true ? {} : cfg;
  const rate = (v, d) => (v === undefined || v === null || Number.isNaN(Number(v)) ? d : Math.min(1, Math.max(0, Number(v))));
  return {
    sampleRate: rate(c.sampleRate, 0.1),
    errorSampleRate: rate(c.errorSampleRate, 1),
    maskAllText: c.maskAllText !== false,
    maskAllInputs: c.maskAllInputs !== false,
    blockSelector: c.blockSelector ? String(c.blockSelector) : undefined,
    maskSelector: c.maskSelector ? String(c.maskSelector) : undefined, // extra elements to mask when maskAllText is false
    unmaskSelector: c.unmaskSelector ? String(c.unmaskSelector) : '.insight-unmask',
    maxDurationMs: Math.min(Number(c.maxDurationMs) || 30 * 60_000, 60 * 60_000),
    flushIntervalMs: Math.max(1000, Number(c.flushIntervalMs) || 5000),
    bufferMs: Math.max(5000, Number(c.bufferMs) || 30_000),
    manualStart: !!c.manualStart,
  };
}

/** Build the rrweb options that implement the masking rules. */
export function recordOptions(o) {
  const unmask = (el) => !!(el && el.closest && el.closest(o.unmaskSelector));
  return {
    blockClass: 'insight-block',
    blockSelector: o.blockSelector || null,
    ignoreClass: 'insight-ignore',
    maskTextClass: 'insight-mask',
    maskTextSelector: o.maskAllText ? '*' : o.maskSelector || null,
    maskTextFn: (t, el) => (unmask(el) && o.maskAllText ? t : maskText(t)),
    maskAllInputs: o.maskAllInputs,
    maskInputOptions: { password: true, ...(o.maskAllInputs ? {} : {}) },
    maskInputFn: (t) => maskText(t),
    slimDOMOptions: 'all',
    inlineStylesheet: true,
    recordCanvas: false,
    collectFonts: false,
    sampling: { scroll: 150, input: 'last', media: 800 },
    checkoutEveryNms: o.bufferMs,
  };
}

const gzipB64 = async (text) => {
  if (typeof CompressionStream === 'undefined') return null;
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream('gzip'));
  const buf = new Uint8Array(await new Response(stream).arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return btoa(bin);
};

/**
 * @param client  the Insight client (options, endpoints, scope, platform.fetch)
 * @param w       window
 * @param cfg     init({ replay }) value
 * @param deps    { record } for tests
 * @returns controller { start, stop, id, mode }
 */
export function createReplay(client, w, cfg, deps = {}) {
  const o = replayOptions(cfg);
  const record = deps.record || rrwebRecord;
  const url = client.endpoints && client.endpoints.replay;
  const ss = (fn) => { try { return fn(w.sessionStorage); } catch { return undefined; } };
  const ctl = { id: null, mode: 'off', stopped: false };
  if (client.disabled || !client.options.apiKey || !url) return ctl;

  let stopFn = null;
  let segments = []; // buffer mode: [[events...], ...] each segment starts at a full snapshot
  let pending = []; // session mode: waiting to upload
  let seq = 0;
  let timer = null;
  let t0 = 0;
  let sending = Promise.resolve();
  let quit = false;

  const post = async (events, final) => {
    if (!events.length || quit) return;
    const body = {
      replay_id: ctl.id, seq: seq++, release: client.options.release, environment: client.options.environment,
      url: (w.location && w.location.pathname) || '/', started_at: t0, sdk: { name: 'insight.replay', version: '1' },
      masking: { text: o.maskAllText, inputs: o.maskAllInputs },
    };
    const user = client.scope && client.scope.user;
    if (user && (user.id || user.id === 0)) body.user_id = String(user.id);
    const json = JSON.stringify(events);
    let gz = null;
    try { gz = await gzipB64(json); } catch { gz = null; }
    if (gz) { body.encoding = 'gzip-base64'; body.data = gz; } else body.events = events;
    if (final) body.final = true;
    try {
      const res = await (client.options.fetch || client.platform.fetch)(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': client.options.apiKey }, body: JSON.stringify(body), keepalive: JSON.stringify(body).length < 60_000 });
      if (res && (res.status === 401 || res.status === 403 || res.status === 429 || res.status === 413)) halt(res.status); // not allowed / over quota / too big: stop for this page session
    } catch { /* offline: the chunk is lost, the page is unaffected */ }
  };

  const flush = (final) => {
    if (ctl.mode !== 'session' || !pending.length) return sending;
    const events = pending.splice(0, MAX_CHUNK_EVENTS);
    sending = sending.then(() => post(events, final));
    if (pending.length) return flush(final);
    return sending;
  };

  const halt = (why, afterUpload) => {
    if (afterUpload) sending = sending.then(() => { quit = true; }); // the final chunk is queued: let it go out, then close
    else quit = true;
    ctl.mode = 'off';
    ctl.stopped = why || true;
    if (stopFn) { try { stopFn(); } catch { /* ignore */ } stopFn = null; }
    if (timer) clearInterval(timer);
    segments = []; pending = [];
  };

  const emit = (event, isCheckout) => {
    if (quit) return;
    if (!t0) t0 = event.timestamp || Date.now();
    if (ctl.mode === 'session') {
      pending.push(event);
      if (event.timestamp - t0 > o.maxDurationMs) { flush(true); halt('max-duration', true); }
      return;
    }
    // buffer mode: a new segment starts at every full snapshot (type 2) that follows a Meta event (type 4); keep the last two
    if (event.type === 4 && isCheckout !== false) segments.push([]);
    if (!segments.length) segments.push([]);
    segments[segments.length - 1].push(event);
    if (segments.length > 2) segments.shift();
  };

  const begin = (mode) => {
    ctl.mode = mode;
    const s = ss((st) => JSON.parse(st.getItem(KEY) || 'null'));
    if (s && s.id && Date.now() - s.at < IDLE_MS && s.release === String(client.options.release || '')) { ctl.id = s.id; seq = s.seq || 0; }
    else { ctl.id = rid(); seq = 0; }
    ss((st) => st.setItem(KEY, JSON.stringify({ id: ctl.id, at: Date.now(), seq, release: String(client.options.release || '') })));
    try {
      stopFn = record({ ...recordOptions(o), emit });
    } catch (e) { halt('record-failed'); return; }
    timer = setInterval(() => { ss((st) => st.setItem(KEY, JSON.stringify({ id: ctl.id, at: Date.now(), seq, release: String(client.options.release || '') }))); flush(false); }, o.flushIntervalMs);
    if (timer && timer.unref) timer.unref();
    const leave = () => { flush(true); };
    w.addEventListener('pagehide', leave);
    w.document && w.document.addEventListener('visibilitychange', () => { if (w.document.visibilityState === 'hidden') leave(); });
  };

  /** Called by the client for every error that is really sent: link it to the replay, and in buffer mode start uploading. */
  const noteEvent = (event) => {
    if (quit || !ctl.id) return;
    event.replay_id = ctl.id;
    if (ctl.mode === 'buffer') {
      ctl.mode = 'session';
      pending = segments.flat();
      segments = [];
      flush(false);
    }
  };

  ctl.start = () => {
    if (ctl.mode !== 'off' || quit) return ctl;
    const r = Math.random();
    if (r < o.sampleRate) begin('session');
    else if (r < o.sampleRate + (1 - o.sampleRate) * o.errorSampleRate) begin('buffer');
    return ctl;
  };
  ctl.stop = () => { flush(true); halt('stopped', true); };
  ctl.noteEvent = noteEvent;
  ctl._flush = () => flush(false);
  if (!o.manualStart) ctl.start();
  return ctl;
}

/** Used by browser.js / the CDN file after init({ replay }). */
export function enableReplay(api, cfg) {
  const client = api.getClient && api.getClient();
  if (!client || typeof window === 'undefined') return null;
  if (client.replay) return client.replay;
  const ctl = createReplay(client, window, cfg);
  client.replay = ctl;
  return ctl;
}
