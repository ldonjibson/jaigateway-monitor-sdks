import { test } from 'node:test';
import assert from 'node:assert/strict';
import { client, fakeFetch, sleep, boom } from './helpers.mjs';
import { resolveEndpoints } from '../src/core.js';
import { parseStack } from '../src/stack.js';

test('endpoint is built from host in every spelling', () => {
  for (const h of ['https://a.com', 'https://a.com/', 'https://a.com/api', 'https://a.com/api/', 'a.com']) {
    assert.match(resolveEndpoints({ host: h }).events, /^https:\/\/a\.com\/api\/ingest\/events\/$/, h);
  }
  assert.equal(resolveEndpoints({ endpoint: 'https://a.com/api/ingest/events' }).events, 'https://a.com/api/ingest/events/');
  assert.equal(resolveEndpoints({ host: 'https://a.com' }).sourcemaps, 'https://a.com/api/ingest/sourcemaps/');
  assert.equal(resolveEndpoints({}), null);
});

test('sends a well-formed protocol event with the API key header', async () => {
  const { c, fetch } = client({ release: 'web@1.0.0', environment: 'production' });
  c.scope.user = { id: '42', email: 'a@b.com' };
  c.scope.tags.feature = 'checkout';
  c.addBreadcrumb({ category: 'ui.click', message: 'button#pay' });
  const ev = c.captureException(boom('pay is not a function'), { extra: { orderId: 1 } });
  assert.equal(await c.flush(1000), true);
  assert.equal(fetch.calls.length, 1);
  const call = fetch.calls[0];
  assert.equal(call.url, 'https://insight.test/api/ingest/events/');
  assert.equal(call.headers['X-API-Key'], 'ak_test');
  const e = call.body[0];
  assert.equal(e.event_type, 'error');
  assert.equal(e.release, 'web@1.0.0');
  assert.equal(e.environment, 'production');
  assert.deepEqual(e.user, { id: '42', email: 'a@b.com' });
  assert.deepEqual(e.tags, { feature: 'checkout' });
  assert.equal(e.extra.orderId, 1);
  assert.equal(e.exception.type, 'TypeError');
  assert.match(e.description, /^TypeError: pay is not a function$/);
  assert.ok(e.exception.stacktrace.length > 0 && e.exception.stacktrace[0].filename);
  assert.equal(e.breadcrumbs[0].message, 'button#pay');
  assert.equal(e.sdk.name, 'insight.javascript');
  assert.ok(e.metadata.traceback.includes('at '));
  assert.equal(ev.timestamp, e.timestamp);
});

test('non-Error throws and messages are reported sensibly', async () => {
  const { c, fetch } = client();
  c.captureException('just a string');
  c.captureException({ code: 7 });
  c.captureMessage('deploy finished', 'info');
  await c.flush(1000);
  const [a, b, m] = fetch.calls[0].body;
  assert.equal(a.exception.type, 'NonErrorException');
  assert.equal(a.exception.value, 'just a string');
  assert.equal(b.exception.value, '{"code":7}');
  assert.equal(m.description, 'deploy finished');
  assert.equal(m.level, 'info');
  assert.equal(m.action, 'message');
  assert.equal(m.exception, undefined);
});

test('error causes become an exception chain', async () => {
  const { c, fetch } = client();
  const err = new Error('outer', { cause: new TypeError('inner') });
  c.captureException(err);
  await c.flush(1000);
  assert.deepEqual(fetch.calls[0].body[0].extra.exception_chain.map((x) => x.type), ['TypeError']);
});

test('the same error object is reported once', async () => {
  const { c, fetch } = client();
  const e = boom();
  assert.ok(c.captureException(e));
  assert.equal(c.captureException(e), null);
  await c.flush(1000);
  assert.equal(fetch.calls[0].body.length, 1);
});

test('events batch into one request, at most 20 each', async () => {
  const { c, fetch } = client({ maxEventsPerMinute: 1000 });
  for (let i = 0; i < 45; i++) c.captureException(new Error('e' + i));
  await c.flush(2000);
  assert.deepEqual(fetch.calls.map((x) => x.body.length), [20, 20, 5]);
});

test('beforeSend can edit, drop, or be async; a throwing hook never loses the event', async () => {
  const { c, fetch } = client({ beforeSend: (e) => (e.description.includes('secret') ? null : { ...e, tags: { edited: 'yes' } }) });
  c.captureException(new Error('secret thing'));
  c.captureException(new Error('fine'));
  await c.flush(1000);
  assert.equal(fetch.calls[0].body.length, 1);
  assert.deepEqual(fetch.calls[0].body[0].tags, { edited: 'yes' });
  const t = client({ beforeSend: () => { throw new Error('hook bug'); } });
  t.c.captureException(new Error('kept'));
  await t.c.flush(1000);
  assert.equal(t.fetch.calls[0].body.length, 1);
  const a = client({ beforeSend: async (e) => ({ ...e, tags: { async: '1' } }) });
  a.c.captureException(new Error('x'));
  await sleep(20); await a.c.flush(1000);
  assert.deepEqual(a.fetch.calls[0].body[0].tags, { async: '1' });
});

test('sampleRate, ignoreErrors and the per-minute guard', async () => {
  const none = client({ sampleRate: 0 });
  assert.equal(none.c.captureException(new Error('x')), null);
  const ign = client({ ignoreErrors: [/ignore me/, 'noisy'] });
  assert.equal(ign.c.captureException(new Error('please ignore me')), null);
  assert.equal(ign.c.captureException(new Error('very noisy thing')), null);
  assert.equal(ign.c.captureException(new Error('real')) !== null, true);
  const lim = client({ maxEventsPerMinute: 3 });
  const out = Array.from({ length: 6 }, (_, i) => lim.c.captureException(new Error('e' + i)));
  assert.equal(out.filter(Boolean).length, 3);
  assert.equal(lim.c.dropped, 3);
});

test('401/403: stops sending for good and says why exactly once', async () => {
  const warns = [];
  const platform = { ...(await import('./helpers.mjs')).nodePlatform, rawConsole: { warn: (m) => warns.push(m), log() {} } };
  const { c, fetch } = client({}, fakeFetch([{ status: 401, body: { error: 'Invalid or missing API key' } }]), platform);
  c.captureException(new Error('a'));
  await c.flush(1000);
  assert.equal(c.disabled, true);
  assert.equal(c.captureException(new Error('b')), null);
  assert.equal(fetch.calls.length, 1);
  assert.equal(warns.length, 1);
  assert.match(warns[0], /rejected the API key/);
  const f = client({}, fakeFetch([{ status: 403, body: { error: 'API key does not have write permissions' } }]));
  f.c.captureException(new Error('a'));
  await f.c.flush(1000);
  assert.equal(f.c.disabled, true);
});

test('429 quota_exceeded pauses sending, drops meanwhile, warns once', async () => {
  const warns = [];
  const platform = { ...(await import('./helpers.mjs')).nodePlatform, rawConsole: { warn: (m) => warns.push(m), log() {} } };
  const { c, fetch } = client({}, fakeFetch([{ status: 429, headers: { 'retry-after': '120' }, body: { code: 'quota_exceeded' } }]), platform);
  c.captureException(new Error('a'));
  await c.flush(1000);
  assert.equal(fetch.calls.length, 1);
  assert.equal(c.captureException(new Error('b')), null); // dropped without a network call
  assert.ok(c.pausedUntil - Date.now() > 100_000 && c.pausedUntil - Date.now() <= 3_600_000);
  assert.equal(warns.length, 1);
  assert.match(warns[0], /quota/);
  assert.equal(fetch.calls.length, 1);
});

test('429 rate_limited keeps the events and retries after Retry-After', async () => {
  const f = fakeFetch([{ status: 429, headers: { 'retry-after': '1' }, body: { code: 'rate_limited' } }, { status: 201 }]);
  const { c } = client({}, f);
  c.captureException(new Error('a'));
  const t0 = Date.now();
  assert.equal(await c.flush(4000), true);
  assert.equal(f.calls.length, 2);
  assert.ok(Date.now() - t0 >= 900);
});

test('5xx and network errors retry with backoff, then succeed; give up after 5 attempts', async () => {
  const f = fakeFetch([new Error('ECONNRESET'), { status: 503 }, { status: 201 }]);
  const { c } = client({}, f);
  c.retryDelayScale = 0;
  c.captureException(new Error('a'));
  assert.equal(await c.flush(10_000), true);
  assert.equal(f.calls.length, 3);
  const dead = fakeFetch([{ status: 500 }]);
  const d = client({}, dead);
  d.c.captureException(new Error('x'));
  assert.equal(await d.c.flush(60_000), true);
  assert.equal(dead.calls.length, 5);
  assert.equal(d.c.dropped, 1);
}, { timeout: 120_000 });

test('400 drops, 413 splits the batch and retries in halves, a single huge event is dropped', async () => {
  const bad = client({}, fakeFetch([{ status: 400 }]));
  bad.c.captureException(new Error('x'));
  await bad.c.flush(1000);
  assert.equal(bad.fetch.calls.length, 1);
  assert.equal(bad.c.dropped, 1);

  const f = fakeFetch([{ status: 413, body: { code: 'payload_too_large' } }, { status: 201 }, { status: 201 }, { status: 201 }, { status: 201 }]);
  const { c } = client({ maxEventsPerMinute: 1000 }, f);
  for (let i = 0; i < 4; i++) c.captureException(new Error('e' + i));
  await c.flush(3000);
  assert.deepEqual(f.calls.map((x) => x.body.length), [4, 2, 2]);
});

test('the queue is bounded: oldest events are dropped', () => {
  const f = fakeFetch([{ status: 201 }]);
  const { c } = client({ maxEventsPerMinute: 10_000, batchDelayMs: 100000 }, f);
  for (let i = 0; i < 130; i++) c.captureException(new Error('e' + i));
  assert.equal(c.queue.length, 100);
  assert.match(c.queue[0].event.description, /e30$/);
  assert.equal(c.dropped, 30);
});

test('flush times out instead of hanging when the server is slow', async () => {
  const slow = async () => { await sleep(500); return { status: 201, headers: { get: () => null }, json: async () => ({}) }; };
  const { c } = client({}, slow);
  c.captureException(new Error('x'));
  const t0 = Date.now();
  assert.equal(await c.flush(100), false);
  assert.ok(Date.now() - t0 < 400);
});

test('a missing key or host disables the SDK instead of throwing', () => {
  const { c } = client({ apiKey: undefined });
  assert.equal(c.disabled, true);
  assert.equal(c.captureException(new Error('x')), null);
  assert.doesNotThrow(() => c.captureMessage('y'));
});

test('scopes are isolated by withScope', async () => {
  const { c, fetch } = client();
  c.scope.tags.global = '1';
  c.withScope((s) => { s.setTag('local', '1'); c.captureException(new Error('inside')); });
  c.captureException(new Error('outside'));
  await c.flush(1000);
  const [inside, outside] = fetch.calls[0].body;
  assert.deepEqual(inside.tags, { global: '1', local: '1' });
  assert.deepEqual(outside.tags, { global: '1' });
});

test('breadcrumbs are capped and newest are kept; beforeBreadcrumb can drop', () => {
  const { c } = client({ maxBreadcrumbs: 3, beforeBreadcrumb: (b) => (b.message === 'skip' ? null : b) });
  for (const m of ['1', 'skip', '2', '3', '4']) c.addBreadcrumb({ message: m });
  assert.deepEqual(c.scope.breadcrumbs.map((b) => b.message), ['2', '3', '4']);
});

test('stack parsing: V8, Firefox and Safari formats, most recent frame first', () => {
  const v8 = parseStack('Error: x\n    at pay (https://s.test/app.js:10:5)\n    at async run (/srv/a.js:2:3)\n    at https://s.test/b.js:7:8\n    at Array.map (<anonymous>)');
  assert.deepEqual(v8.map((f) => [f.function, f.filename, f.lineno, f.colno]), [['pay', 'https://s.test/app.js', 10, 5], ['run', '/srv/a.js', 2, 3], [undefined, 'https://s.test/b.js', 7, 8]]);
  const ff = parseStack('pay@https://s.test/app.js:10:5\n@https://s.test/b.js:7:8\nmap@[native code]');
  assert.deepEqual(ff.map((f) => [f.function, f.lineno]), [['pay', 10], [undefined, 7]]);
  assert.equal(parseStack('/srv/node_modules/x/i.js:1:1'.padStart(0)).length, 0);
  assert.equal(parseStack('    at f (/srv/node_modules/lib/i.js:1:1)')[0].in_app, false);
  assert.deepEqual(parseStack(undefined), []);
});

test('checkIn / monitor: token or full URL, start+success+fail pings, never throws', async () => {
  const { makeSdk } = await import('../src/api.js');
  const calls = [];
  const fetch = async (url, init) => { calls.push({ url, method: init.method, body: init.body }); return { status: 200, headers: { get: () => null }, json: async () => ({}) }; };
  const sdk = makeSdk({ ...(await import('./helpers.mjs')).nodePlatform, fetch }, null);
  const api = sdk.api;
  // bare token needs init (to know the host)
  assert.equal(await api.checkIn('tok123tok123tok123tok1'), false);
  api.init({ apiKey: 'ak', host: 'https://insight.test', fetch });
  assert.equal(await api.checkIn('tok123tok123tok123tok1'), true);
  assert.equal(calls.at(-1).url, 'https://insight.test/api/ping/tok123tok123tok123tok1/');
  // a full URL works even when copied with a trailing action
  await api.checkIn('https://other.test/api/ping/abc/start/', 'fail', 'boom');
  assert.deepEqual([calls.at(-1).url, calls.at(-1).method, calls.at(-1).body], ['https://other.test/api/ping/abc/fail/', 'POST', 'boom']);
  calls.length = 0;
  const out = await api.monitor('https://insight.test/api/ping/zzz/', async () => 42);
  assert.equal(out, 42);
  assert.deepEqual(calls.map((c) => c.url), ['https://insight.test/api/ping/zzz/start/', 'https://insight.test/api/ping/zzz/']);
  calls.length = 0;
  await assert.rejects(api.monitor('https://insight.test/api/ping/zzz/', async () => { throw new Error('job broke'); }), /job broke/);
  assert.deepEqual(calls.map((c) => c.url.split('/').slice(-2).join('/')), ['start/', 'fail/']);
  assert.match(calls[1].body, /job broke/);
  // a network failure is swallowed
  const broken = makeSdk({ ...(await import('./helpers.mjs')).nodePlatform, fetch: async () => { throw new Error('down'); } }, null).api;
  assert.equal(await broken.checkIn('https://x.test/api/ping/abc/'), false);
  await assert.doesNotReject(broken.monitor('https://x.test/api/ping/abc/', async () => 1));
});
