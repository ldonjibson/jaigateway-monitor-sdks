import { Client } from '../src/core.js';

/** A scripted fetch: each call takes the next response ({status, body, headers}) or throws if it's an Error. */
export function fakeFetch(script = []) {
  const calls = [];
  const fn = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, headers: init.headers, body, keepalive: init.keepalive });
    const r = script.length ? (script.length > 1 ? script.shift() : script[0]) : { status: 201 };
    if (r instanceof Error) throw r;
    return { status: r.status, headers: { get: (k) => (r.headers || {})[k.toLowerCase()] ?? null }, json: async () => r.body ?? {} };
  };
  fn.calls = calls;
  return fn;
}

export const nodePlatform = { name: 'node', rawConsole: { log() {}, warn() {}, error() {} }, fetch: () => Promise.reject(new Error('no fetch')), defaultMetadata: () => ({ browser: 'Node test', os: 'test' }) };

export function client(options = {}, fetch = fakeFetch(), platform = nodePlatform) {
  const c = new Client({ apiKey: 'ak_test', host: 'https://insight.test', batchDelayMs: 1, fetch, ...options }, platform);
  return { c, fetch };
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const boom = (msg = 'boom') => { try { null.x; } catch (e) { e.message = msg; return e; } };
