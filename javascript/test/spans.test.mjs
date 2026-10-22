import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';

// a project directory with fake `pg` and `mysql2` packages, so we can test the driver instrumentation without real databases
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'spans-'));
fs.mkdirSync(path.join(dir, 'node_modules/pg'), { recursive: true });
fs.writeFileSync(path.join(dir, 'node_modules/pg/index.js'), `
class Client { constructor() { this.database = 'shop'; }
  query(text, values, cb) { const sql = typeof text === 'string' ? text : text.text; const fail = /FAIL/.test(sql);
    if (typeof values === 'function') { cb = values; values = undefined; }
    const run = () => new Promise((res, rej) => setTimeout(() => (fail ? rej(new Error('boom')) : res({ rows: [] })), 15));
    if (cb) { run().then((r) => cb(null, r), (e) => cb(e)); return undefined; }
    return run(); } }
module.exports = { Client };`);
fs.mkdirSync(path.join(dir, 'node_modules/mysql2/lib'), { recursive: true });
fs.writeFileSync(path.join(dir, 'node_modules/mysql2/lib/connection.js'), `
class Connection { query(sql, cb) { setTimeout(() => cb(null, []), 10); } execute(sql, vals, cb) { setTimeout(() => cb(null, []), 10); } }
module.exports = Connection;`);
process.chdir(dir);
const appRequire = createRequire(path.join(dir, 'app.js')); // the app's own module resolution, like a real project

const sdk = await import('../src/node.js');
const { normalizeSql, httpDescription, newTxn, addSpan, summarise, sampleSpans } = await import('../src/spans.js');
const metricsPosts = [];
const eventPosts = [];
const fakeFetch = async (url, init) => {
  const body = JSON.parse(init.body);
  (url.endsWith('/metrics/') ? metricsPosts : eventPosts).push(...(Array.isArray(body) ? body : [body]));
  return { status: 201, headers: { get: () => null }, json: async () => ({}) };
};
before(() => sdk.init({ apiKey: 'ak_t', host: 'http://insight.invalid', fetch: fakeFetch, integrations: { globalHandlers: false }, performanceFlushIntervalMs: 3_600_000, release: 'r1', environment: 'test' }));

const request = (handlerFn, url = '/x') => new Promise((resolve, reject) => {
  const res = new EventEmitter(); res.statusCode = 200;
  sdk.requestHandler()({ method: 'GET', url, originalUrl: url, headers: { host: 'x' }, socket: {}, baseUrl: '', route: { path: url } }, res, () => {
    Promise.resolve().then(handlerFn).then(() => { res.emit('finish'); resolve(); }, (e) => { res.statusCode = 500; res.emit('finish'); resolve(e); });
  });
});

test('SQL normaliser removes every literal (same cases as the server)', () => {
  assert.equal(normalizeSql("SELECT * FROM users WHERE id = 42 AND name = 'bob' AND note = 'it''s'"), 'SELECT * FROM users WHERE id = ? AND name = ? AND note = ?');
  assert.equal(normalizeSql("SELECT 1 FROM t WHERE x IN (1, 2, 3) AND y IN ('a','b')"), 'SELECT ? FROM t WHERE x IN (?) AND y IN (?)');
  assert.equal(normalizeSql("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y'), (3, 'z')"), 'INSERT INTO t (a, b) VALUES (?)');
  assert.equal(normalizeSql('SELECT /* trace=abc */ a -- password=hunter2\n FROM t WHERE price > 9.99e2'), 'SELECT a FROM t WHERE price > ?');
  assert.equal(normalizeSql('SELECT "user_id", t1.col2 FROM "Table2" WHERE id = $1'), 'SELECT "user_id", t1.col2 FROM "Table2" WHERE id = $1');
  assert.ok(normalizeSql('x'.repeat(500)).length <= 300);
  assert.equal(httpDescription('post', 'https://u:secret@api.stripe.com/v1/charges?key=sk_live'), 'POST api.stripe.com');
  assert.equal(httpDescription('GET', 'localhost:8080'), 'GET localhost:8080');
});

test('spans: collection cap keeps counting, summaries fold into one entry per (txn, op, description), samples keep the slowest 100 in order', () => {
  const txn = newTxn();
  for (let i = 0; i < 250; i++) addSpan(txn, { op: 'db.query', description: i < 200 ? `q${i}` : 'overflow-q', startMs: i, durationMs: i === 7 ? 500 : 2, ok: i !== 3 });
  assert.equal(txn.spans.length, 200);
  assert.equal(txn.dropped, 50);
  const into = new Map();
  summarise('GET /a', txn, into);
  const all = [...into.values()];
  assert.equal(all.length, 201);
  const over = all.find((s) => s.description === 'overflow-q');
  assert.equal(over.count, 50);
  assert.equal(all.find((s) => s.description === 'q3').errors, 1);
  assert.equal(Object.values(all.find((s) => s.description === 'q7').buckets).reduce((a, b) => a + b, 0), 1);
  const picked = sampleSpans(txn);
  assert.equal(picked.length, 100);
  assert.ok(picked.some((s) => s.description === 'q7'));
  assert.deepEqual(picked.map((s) => s.start_ms), [...picked.map((s) => s.start_ms)].sort((a, b) => a - b));
  const capped = new Map();
  for (let i = 0; i < 400; i++) { const t = newTxn(); addSpan(t, { op: 'db.query', description: `u${i}`, startMs: 0, durationMs: 1 }); summarise('GET /b', t, capped, 300); }
  assert.ok(capped.size <= 301);
  assert.ok(capped.has('GET /b\u0000db.query\u0000<other>'));
});

test('a request records pg and mysql spans, outbound http spans, and one trace id shared by the sample and an error', async () => {
  const server = http.createServer((req, res) => { res.statusCode = req.url === '/bad' ? 502 : 200; res.end('ok'); });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const { Client } = appRequire('pg');
  const my = appRequire('mysql2/lib/connection.js');
  metricsPosts.length = 0; eventPosts.length = 0;

  const call = (p) => new Promise((resolve) => http.get({ host: '127.0.0.1', port, path: p }, (res) => { res.resume(); res.on('end', resolve); }));
  await request(async () => {
    const c = new Client();
    await c.query("SELECT * FROM items WHERE id = 17 AND token = 'secret-token'", [1]);
    await new Promise((r) => c.query({ text: 'SELECT 2 FROM t' }, r));
    await new Promise((r) => new my().query('SELECT * FROM orders WHERE total > 99.5', r));
    await call('/ok?apikey=hush');
    await call('/bad');
    await fetch(`http://127.0.0.1:${port}/ok?x=1`);
    await assert.rejects(c.query('SELECT FAIL FROM nope WHERE a = 1'));
    sdk.captureException(new Error('failed inside the request'));
    throw new Error('boom');
  }, '/items');
  server.close();
  await sdk.flush(2000);

  const err = eventPosts.find((e) => e.description.includes('failed inside'));
  assert.match(err.trace_id, /^[0-9a-f]{32}$/);
  const w = metricsPosts.find((x) => x.transactions);
  assert.equal(w.samples[0].trace_id, err.trace_id, 'the error links to the request sample');
  const spans = w.samples[0].spans;
  const desc = spans.map((s) => `${s.op} ${s.description}`);
  assert.ok(desc.includes('db.query SELECT * FROM items WHERE id = ? AND token = ?'));
  assert.ok(desc.includes('db.query SELECT ? FROM t'));
  assert.ok(desc.includes('db.query SELECT * FROM orders WHERE total > ?'));
  assert.equal(desc.filter((d) => d === `http.client GET 127.0.0.1:${port}`).length, 3); // http.get x2 and fetch
  const bad = spans.filter((s) => s.op === 'http.client' && s.data['http.status_code'] === 502);
  assert.equal(bad.length, 1);
  assert.equal(bad[0].status, 'error');
  assert.equal(spans.find((s) => s.description.includes('FAIL')).status, 'error');
  assert.ok(spans.every((s) => s.duration_ms >= 0 && s.start_ms >= 0));
  assert.ok(spans.find((s) => s.description.includes('items')).duration_ms >= 10);
  assert.equal(spans.find((s) => s.op === 'db.query').data['db.system'], 'postgresql');
  // privacy: nothing but normalised SQL and METHOD host anywhere in the payload
  const wire = JSON.stringify(w);
  for (const leak of ['secret-token', 'apikey=hush', 'x=1', '/ok', '/bad', '17', '99.5']) assert.ok(!wire.includes(`"${leak}"`) && !/description":"[^"]*(secret-token|apikey|\/ok|\/bad|99\.5)/.test(wire), `leaked ${leak}`);
  const sums = w.spans.filter((s) => s.transaction === 'GET /items');
  assert.ok(sums.length >= 5);
  assert.equal(sums.find((s) => s.description === 'SELECT * FROM items WHERE id = ? AND token = ?').count, 1);
  assert.equal(sums.find((s) => s.description === `GET 127.0.0.1:${port}`).count, 3);
  assert.equal(sums.find((s) => s.description === `GET 127.0.0.1:${port}`).errors, 1);
});

test('SDK traffic to Insight itself is never a span, and spans outside a request cost nothing', async () => {
  metricsPosts.length = 0;
  const s = sdk.startSpan('custom', 'no transaction here');
  s.finish();
  const out = await sdk.span('cache', 'get', async () => 5);
  assert.equal(out, 5);
  await sdk.flush(1000);
  assert.equal(metricsPosts.length, 0);
});

test('transaction() gives jobs their own trace: spans and errors inside link together', async () => {
  metricsPosts.length = 0; eventPosts.length = 0;
  await sdk.transaction('TASK nightly', async () => {
    await sdk.span('db.query', 'SELECT nightly FROM jobs', async () => { await new Promise((r) => setTimeout(r, 12)); }, { 'db.system': 'postgresql' });
    sdk.captureException(new Error('job step failed'));
  }, { op: 'queue.task' });
  await sdk.flush(1000);
  const w = metricsPosts.find((x) => x.transactions);
  const err = eventPosts.find((e) => e.description.includes('job step failed'));
  assert.equal(w.samples[0].trace_id, err.trace_id);
  assert.equal(w.samples[0].spans[0].description, 'SELECT nightly FROM jobs');
  assert.ok(w.samples[0].spans[0].duration_ms >= 10);
  assert.equal(w.spans[0].transaction, 'TASK nightly');
});

test('performanceSpans: false turns span collection off (the transaction and its trace id remain)', async () => {
  sdk.init({ apiKey: 'ak_t', host: 'http://insight.invalid', fetch: fakeFetch, integrations: { globalHandlers: false }, performanceSpans: false, performanceFlushIntervalMs: 3_600_000 });
  metricsPosts.length = 0;
  const { Client } = appRequire('pg');
  await request(async () => { await new Client().query('SELECT 1'); });
  await sdk.flush(1000);
  const w = metricsPosts.find((x) => x.transactions);
  assert.equal(w.spans.length, 0);
  assert.ok(w.samples[0].trace_id);
});

test('traceparent: a request continues the caller trace, is kept as a sample when sampled, and passes the header on only to listed hosts', async () => {
  const { parseTraceparent, formatTraceparent, matchesTarget } = await import('../src/spans.js');
  assert.deepEqual(parseTraceparent('00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'), { trace_id: '4bf92f3577b34da6a3ce929d0e0e4736', parent_span_id: '00f067aa0ba902b7', sampled: true });
  assert.equal(parseTraceparent('00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-00').sampled, false); // case-insensitive
  for (const bad of ['', 'garbage', '01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01', '00-' + '0'.repeat(32) + '-00f067aa0ba902b7-01', '00-4bf92f3577b34da6a3ce929d0e0e4736-' + '0'.repeat(16) + '-01', '00-short-00f067aa0ba902b7-01', undefined, 42]) assert.equal(parseTraceparent(bad), null, String(bad));
  assert.equal(formatTraceparent('a'.repeat(32), 'b'.repeat(16), true), `00-${'a'.repeat(32)}-${'b'.repeat(16)}-01`);
  assert.equal(matchesTarget(['api.example.com', '*.svc.test', 'localhost:8000', /^10\./], 'api.example.com'), true);
  assert.equal(matchesTarget(['*.svc.test'], 'a.svc.test:8080'), true);
  assert.equal(matchesTarget(['localhost:8000'], 'localhost:9000'), false);
  assert.equal(matchesTarget(['api.example.com'], 'evil-api.example.com'), false);
  assert.equal(matchesTarget([], 'x.com'), false);

  const seen = [];
  const server = http.createServer((req, res) => { seen.push({ path: req.url, tp: req.headers.traceparent }); res.end('ok'); });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  sdk.init({ apiKey: 'ak_t', host: 'http://insight.invalid', fetch: fakeFetch, integrations: { globalHandlers: false }, performanceFlushIntervalMs: 3_600_000, tracePropagationTargets: [`127.0.0.1:${port}`] });
  metricsPosts.length = 0;
  const get = (p, headers) => new Promise((resolve) => http.get({ host: '127.0.0.1', port, path: p, headers }, (res) => { res.resume(); res.on('end', resolve); }));
  const incoming = `00-${'c'.repeat(32)}-${'d'.repeat(16)}-01`;
  await new Promise((resolve) => {
    const res = new EventEmitter(); res.statusCode = 200;
    sdk.requestHandler()({ method: 'GET', url: '/fast', originalUrl: '/fast', headers: { host: 'x', traceparent: incoming }, socket: {}, baseUrl: '', route: { path: '/fast' } }, res, async () => {
      await get('/via-http');
      await get('/with-own', { traceparent: 'app-own-value' });
      await fetch(`http://127.0.0.1:${port}/via-fetch`);
      res.emit('finish'); resolve();
    });
  });
  // a different host is never given the header
  const other = http.createServer((req, res) => { seen.push({ path: 'OTHER' + req.url, tp: req.headers.traceparent }); res.end('ok'); });
  await new Promise((r) => other.listen(0, 'localhost', r));
  await new Promise((resolve) => {
    const res = new EventEmitter(); res.statusCode = 200;
    sdk.requestHandler()({ method: 'GET', url: '/b', originalUrl: '/b', headers: { host: 'x' }, socket: {}, baseUrl: '', route: { path: '/b' } }, res, async () => {
      await new Promise((r) => http.get({ host: 'localhost', port: other.address().port, path: '/x' }, (rs) => { rs.resume(); rs.on('end', r); }));
      res.emit('finish'); resolve();
    });
  });
  server.close(); other.close();
  await sdk.flush(1000);

  const w = metricsPosts.find((x) => x.samples && x.samples.some((s) => s.name === 'GET /fast'));
  const s = w.samples.find((x) => x.name === 'GET /fast');
  assert.equal(s.kind, 'server');
  assert.equal(s.trace_id, 'c'.repeat(32), 'adopted the caller trace');
  assert.equal(s.parent_span_id, 'd'.repeat(16));
  assert.match(s.span_id, /^[0-9a-f]{16}$/);
  assert.equal(s.ok, true); // a fast, successful request: kept only because the caller's trace is sampled
  const httpSpans = s.spans.filter((x) => x.op === 'http.client');
  assert.equal(httpSpans.length, 3);
  const viaHttp = seen.find((x) => x.path === '/via-http'), viaFetch = seen.find((x) => x.path === '/via-fetch'), own = seen.find((x) => x.path === '/with-own');
  for (const call of [viaHttp, viaFetch]) {
    const m = /^00-([0-9a-f]{32})-([0-9a-f]{16})-(01|00)$/.exec(call.tp);
    assert.ok(m, `bad header ${call.tp}`);
    assert.equal(m[1], 'c'.repeat(32));
    assert.equal(m[3], '01');
    assert.ok(httpSpans.some((sp) => sp.span_id === m[2]), 'the header carries the id of the http.client span recorded for that call');
  }
  assert.equal(own.tp, 'app-own-value', 'a header the app set itself is never overwritten');
  assert.equal(seen.find((x) => x.path === 'OTHER/x').tp, undefined, 'unlisted hosts never get the header');
  // the second request (no incoming header) starts its own trace, not sampled
  const s2 = metricsPosts.flatMap((x) => x.samples || []).find((x) => x.name === 'GET /b');
  assert.ok(!s2 || (s2.trace_id !== 'c'.repeat(32) && !s2.parent_span_id));
  assert.equal(w.samples.filter((x) => x.name === 'GET /b').length <= 1, true);
});
