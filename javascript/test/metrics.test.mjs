import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Client } from '../src/core.js';
import { Metrics, bucketIndex, BOUNDS_MS } from '../src/metrics.js';
import { nodePlatform } from './helpers.mjs';

const resp = (status, body = {}, headers = {}) => ({ status, headers: { get: (k) => headers[k.toLowerCase()] ?? null }, json: async () => body });
function rig(script = [], opts = {}) {
  const calls = [];
  const warns = [];
  const fetch = async (url, init) => {
    calls.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    const r = script.length > 1 ? script.shift() : script[0] ?? resp(201);
    if (r instanceof Error) throw r;
    return r;
  };
  const platform = { ...nodePlatform, serverName: () => 'host-1', rawConsole: { warn: (m) => warns.push(m), log() {} } };
  const client = new Client({ apiKey: 'ak_test', host: 'https://insight.test', release: 'api@1', environment: 'production', fetch, performanceFlushIntervalMs: 3_600_000, ...opts }, platform);
  client.metrics = new Metrics(client, platform);
  return { client, m: client.metrics, calls, warns };
}

test('bucket table matches the protocol (edges)', () => {
  assert.equal(BOUNDS_MS.length, 27);
  assert.deepEqual([0.5, 1, 1.01, 300, 60000, 60001].map(bucketIndex), [0, 0, 1, 13, 26, 27]);
});

test('aggregates per transaction and sends one summary in the documented shape', async () => {
  const { m, calls } = rig();
  for (const d of [10, 20, 30]) m.record('GET /items/{id}', d, { status: 200 });
  m.record('GET /items/{id}', 900, { status: 500, error: true });
  m.record('GET /items/{id}', 40, { status: 404 }); // 4xx is not an error
  m.record('POST /orders', 5, { status: 201 });
  assert.equal(await m.flush(), true);
  assert.equal(calls.length, 1);
  const c = calls[0];
  assert.equal(c.url, 'https://insight.test/api/ingest/metrics/');
  assert.equal(c.headers['X-API-Key'], 'ak_test');
  const b = c.body;
  assert.deepEqual([b.platform, b.release, b.environment, b.server_name, b.sdk.name], ['node', 'api@1', 'production', 'host-1', 'insight.node']);
  assert.ok(b.interval_start && b.interval_seconds >= 1);
  const t = b.transactions.find((x) => x.name === 'GET /items/{id}');
  assert.deepEqual([t.count, t.errors, t.sum_ms, t.min_ms, t.max_ms, t.op], [5, 1, 1000, 10, 900, 'http.server']);
  assert.equal(Object.values(t.buckets).reduce((a, n) => a + n, 0), 5); // histogram adds up to the count
  assert.equal(t.buckets[bucketIndex(900)], 1);
  assert.equal(b.transactions.length, 2);
  assert.ok(b.samples.length >= 1 && b.samples.length <= 10);
  assert.match(b.samples[0].trace_id, /^[0-9a-f]{32}$/);
  assert.equal(await m.flush(), true);
  assert.equal(calls.length, 1); // nothing new: nothing sent
});

test('keeps at most 10 samples: the slowest, the failures, and a random one', async () => {
  const { m, calls } = rig();
  for (let i = 0; i < 200; i++) m.record('GET /a', i === 77 ? 5000 : 10 + (i % 7), { status: i % 50 === 0 ? 500 : 200, error: i % 50 === 0 });
  await m.flush();
  const s = calls[0].body.samples;
  assert.ok(s.length <= 10);
  assert.ok(s.some((x) => x.duration_ms === 5000), 'the slowest request is kept');
  assert.ok(s.some((x) => !x.ok), 'a failed request is kept');
});

test('ignores health checks and static files by default, and anything the app lists', () => {
  const { m } = rig([], { performanceIgnore: ['/internal/', /^\/v1\/ping/] });
  for (const p of ['/health', '/healthz?x=1', '/ping', '/favicon.ico', '/static/app.js', '/_next/static/chunk.js', '/internal/jobs', '/v1/ping/3']) assert.equal(m.isIgnored(p), true, p);
  for (const p of ['/', '/items/1', '/healthy-living']) assert.equal(m.isIgnored(p), false, p);
});

test('401/403 turn metrics off (only metrics) and say so once', async () => {
  const { m, calls, warns } = rig([resp(401, { error: 'Invalid or missing API key' })]);
  m.record('GET /a', 5);
  assert.equal(await m.flush(), false);
  assert.equal(m.disabled, true);
  m.record('GET /a', 5);
  await m.flush();
  assert.equal(calls.length, 1);
  assert.equal(warns.filter((w) => /rejected the API key for performance/.test(w)).length, 1);
});

test('quota_exceeded pauses metrics only, drops meanwhile, warns once; errors keep flowing', async () => {
  const { client, m, calls, warns } = rig([resp(429, { code: 'quota_exceeded', quota: 'performance' }, { 'retry-after': '1800' })]);
  m.record('GET /a', 5);
  await m.flush();
  assert.ok(m.pausedUntil - Date.now() > 1_700_000 && m.pausedUntil - Date.now() <= 3_600_000);
  m.record('GET /a', 5); // dropped without any network call
  assert.equal(m.tx.size, 0);
  assert.equal(warns.filter((w) => /tracked-request quota/.test(w)).length, 1);
  assert.equal(client.disabled, false, 'the error client is untouched');
  const before = calls.length;
  assert.ok(client.captureException(new Error('still reported')));
  assert.equal(calls.length, before); // (the error goes to the events endpoint via client.fetch queue, not through metrics)
});

test('rate_limited waits for Retry-After and keeps the window', async () => {
  const { m, calls } = rig([resp(429, { code: 'rate_limited' }, { 'retry-after': '1' }), resp(201)]);
  m.record('GET /a', 5);
  assert.equal(await m.flush(), false);
  assert.equal(m.pending.length, 1);
  await new Promise((r) => setTimeout(r, 1100));
  assert.equal(await m.flush(), true);
  assert.equal(calls.length, 2);
  assert.equal(m.pending.length, 0);
});

test('a failed send keeps the window and merges it into the next flush (at most 5 windows)', async () => {
  const { m, calls } = rig([new Error('ECONNRESET'), resp(503), resp(503), resp(503), resp(503), resp(503), resp(503), resp(201)]);
  for (let i = 0; i < 8; i++) { m.record(`GET /w${i}`, 5); await m.flush(); }
  assert.ok(m.pending.length <= 5);
  const sentOk = await m.flush();
  assert.equal(sentOk, true);
  assert.equal(m.pending.length, 0);
  assert.ok(calls.at(-1).body.transactions.length >= 1);
});

test('413 drops the samples and keeps the counts', async () => {
  const { m, calls } = rig([resp(413, { code: 'payload_too_large' }), resp(201)]);
  m.record('GET /a', 5);
  await m.flush();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].body.samples, []);
  assert.equal(calls[1].body.transactions[0].count, 1);
});

test('performance: false records nothing and sends nothing', async () => {
  const { m, calls } = rig([], { performance: false });
  m.record('GET /a', 5);
  assert.equal(await m.flush(), true);
  assert.equal(calls.length, 0);
});

test('client.flush() sends the summary too, and close() stops the timer', async () => {
  const { client, m, calls } = rig();
  m.record('GET /a', 5);
  assert.equal(await client.flush(1000), true);
  assert.equal(calls.filter((c) => c.url.endsWith('/ingest/metrics/')).length, 1);
  await client.close(500);
});

test('express: names by route template, never by raw URL; flags failures; skips ignored paths', async () => {
  const sdk = await import('../src/node.js');
  const calls = [];
  const fetch = async (url, init) => { calls.push({ url, body: JSON.parse(init.body) }); return resp(201); };
  sdk.init({ apiKey: 'ak_t', host: 'https://insight.test', fetch, integrations: { globalHandlers: false }, performanceFlushIntervalMs: 3_600_000 });
  const run = (req, status, extra) => new Promise((resolve) => {
    const res = new EventEmitter();
    res.statusCode = status;
    sdk.requestHandler()({ method: 'GET', url: req.url, originalUrl: req.url, headers: { host: 'x' }, socket: {}, baseUrl: '', ...req, ...extra }, res, () => { setTimeout(() => { res.emit('finish'); res.emit('close'); resolve(); }, 3); });
  });
  await run({ url: '/items/42?x=1', route: { path: '/items/:id' } }, 200);
  await run({ url: '/items/7', route: { path: '/items/:id' } }, 200);
  await run({ url: '/api/orders', baseUrl: '/api', route: { path: '/orders' }, method: 'POST' }, 201);
  await run({ url: '/wp-login.php' }, 404);            // no route matched: one shared name, not one per scanned path
  await run({ url: '/random-bot-path-9' }, 404);
  await run({ url: '/boom', route: { path: '/boom' } }, 500);
  await run({ url: '/odd', route: { path: '/odd' } }, 200, { __insightError: true }); // the error handler saw it
  await run({ url: '/health' }, 200);
  await run({ url: '/static/app.js' }, 200);
  await sdk.flush(1000);
  const b = calls.filter((c) => c.url.endsWith('/metrics/')).flatMap((c) => c.body.transactions);
  const by = Object.fromEntries(b.map((t) => [t.name, t]));
  assert.deepEqual(Object.keys(by).sort(), ['GET /boom', 'GET /items/{id}', 'GET /odd', 'GET <unmatched>', 'POST /api/orders']);
  assert.equal(by['GET /items/{id}'].count, 2);
  assert.equal(by['GET <unmatched>'].count, 2);
  assert.equal(by['GET <unmatched>'].errors, 0, '404 is not an error');
  assert.equal(by['GET /boom'].errors, 1);
  assert.equal(by['GET /odd'].errors, 1);
});

test('transaction() times async work, records failures, and rethrows', async () => {
  const sdk = await import('../src/node.js');
  const calls = [];
  const fetch = async (url, init) => { calls.push({ url, body: JSON.parse(init.body) }); return resp(201); };
  sdk.init({ apiKey: 'ak_t', host: 'https://insight.test', fetch, integrations: { globalHandlers: false }, performanceFlushIntervalMs: 3_600_000 });
  assert.equal(await sdk.transaction('TASK send-emails', async () => { await new Promise((r) => setTimeout(r, 12)); return 7; }, { op: 'queue.task' }), 7);
  await assert.rejects(sdk.transaction('TASK send-emails', async () => { throw new Error('smtp down'); }), /smtp down/);
  sdk.recordTransaction('TASK nightly', 250, { status: null });
  await sdk.flush(1000);
  const t = calls.filter((c) => c.url.endsWith('/metrics/')).flatMap((c) => c.body.transactions);
  const mail = t.find((x) => x.name === 'TASK send-emails');
  assert.deepEqual([mail.count, mail.errors, mail.op], [2, 1, 'queue.task']);
  assert.ok(mail.max_ms >= 10);
  assert.equal(t.find((x) => x.name === 'TASK nightly').count, 1);
});
