import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import Insight, { init, captureException, withScope, setTag, flush, wrap, requestHandler, errorHandler, captureRequestError, getClient } from '../src/node.js';
import { fakeFetch, sleep } from './helpers.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const f = fakeFetch();
init({ apiKey: 'ak_test', host: 'https://insight.test', fetch: f, environment: 'test', release: 'r1', integrations: { globalHandlers: false } });
const sent = async () => { await flush(1000); return f.calls.flatMap((c) => c.body); };
const last = async () => (await sent()).at(-1);

test('node: reports with server_name, release, cwd-relative paths and node metadata', async () => {
  captureException(new Error('node boom'));
  const e = await last();
  assert.equal(e.platform, 'node');
  assert.equal(e.release, 'r1');
  assert.ok(e.server_name);
  assert.match(e.metadata.browser, /^Node /);
  assert.ok(e.exception.stacktrace[0].filename.includes('node.test.mjs'));
  assert.ok(!e.exception.stacktrace[0].filename.startsWith('file://'));
});

test('node: withScope follows async calls and never leaks into other work', async () => {
  const run = (name, ms) => withScope(async (scope) => {
    scope.setTag('who', name);
    await sleep(ms);
    captureException(new Error('in ' + name));
  });
  await Promise.all([run('a', 30), run('b', 10)]);
  captureException(new Error('outside'));
  const all = await sent();
  const by = (m) => all.find((e) => e.description.endsWith(m));
  assert.equal(by('in a').tags.who, 'a');
  assert.equal(by('in b').tags.who, 'b');
  assert.equal(by('outside').tags?.who, undefined);
});

test('node: wrap reports and rethrows, sync and async', async () => {
  const sync = wrap(() => { throw new Error('wrapped sync'); });
  assert.throws(() => sync(), /wrapped sync/);
  const asyncFn = wrap(async () => { throw new Error('wrapped async'); });
  await assert.rejects(asyncFn(), /wrapped async/);
  assert.equal(wrap((x) => x * 2)(21), 42);
  const d = (await sent()).map((e) => e.description);
  assert.ok(d.some((x) => x.includes('wrapped sync')) && d.some((x) => x.includes('wrapped async')));
});

test('node: express — request context per request, 5xx reported once, 4xx ignored, cookies never sent', async () => {
  const app = (handlers) => (req, res) => {
    let i = 0;
    const next = (err) => {
      const h = handlers[i++];
      if (!h) return res.end('done');
      if (err) return h.length === 4 ? h(err, req, res, next) : next(err);
      if (h.length === 4) return next();
      try { h(req, res, next); } catch (e) { next(e); }
    };
    next();
  };
  const server = http.createServer(app([
    requestHandler(),
    (req, res, next) => { setTag('route', 'checkout'); if (req.url.startsWith('/bad')) { const e = new Error('client mistake'); e.status = 404; throw e; } throw new Error('server broke'); },
    errorHandler(),
    (err, req, res, next) => { res.statusCode = err.status || 500; res.end('x'); },
  ]));
  await new Promise((r) => server.listen(0, r));
  const port = server.address().port;
  await fetch(`http://localhost:${port}/pay?token=abc&x=1`, { headers: { cookie: 'session=SECRET', authorization: 'Bearer SECRET', 'x-test': 'ok' } });
  await fetch(`http://localhost:${port}/bad`);
  server.close();
  const all = (await sent()).filter((e) => /server broke|client mistake/.test(e.description));
  assert.equal(all.length, 1);
  const e = all[0];
  assert.equal(e.tags.route, 'checkout');
  assert.match(e.request.url, /\/pay\?token=%5BFiltered%5D&x=1|\/pay\?token=\[Filtered\]&x=1/);
  assert.equal(e.request.method, 'GET');
  assert.equal(e.request.headers['x-test'], 'ok');
  assert.ok(!JSON.stringify(e).includes('SECRET'));
  assert.ok(e.breadcrumbs.some((b) => b.message === 'GET /pay'));
});

test('node: Next.js onRequestError', async () => {
  captureRequestError(new Error('next page failed'), { path: '/products/1', method: 'GET', headers: { cookie: 'a=b', 'user-agent': 'x' } }, { routePath: '/products/[id]', routeType: 'render', routerKind: 'App Router' });
  const e = await last();
  assert.equal(e.tags.route, '/products/[id]');
  assert.equal(e.request.headers.cookie, undefined);
  assert.equal(e.request.headers['user-agent'], 'x');
});

test('node: an uncaught exception is reported (and flushed) before the process exits', async () => {
  const got = [];
  const server = http.createServer((req, res) => {
    let b = '';
    req.on('data', (c) => (b += c));
    req.on('end', () => { got.push(...JSON.parse(b)); res.writeHead(201, { 'content-type': 'application/json' }).end('{}'); });
  });
  await new Promise((r) => server.listen(0, r));
  const code = `import Insight from ${JSON.stringify(path.join(here, '../src/node.js'))};
    Insight.init({ apiKey: 'ak', host: 'http://localhost:${server.address().port}' });
    console.log('about to crash');
    setTimeout(() => { throw new Error('fatal crash'); }, 10);`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', code], { stdio: 'pipe' });
  let stderr = '';
  child.stderr.on('data', (d) => (stderr += d));
  const exit = await new Promise((r) => child.on('exit', r));
  server.close();
  assert.equal(exit, 1, 'the process still exits non-zero like Node would');
  assert.match(stderr, /fatal crash/);
  assert.equal(got.length, 1);
  assert.equal(got[0].level, 'fatal');
  assert.equal(got[0].description, 'Error: fatal crash');
  assert.ok(got[0].breadcrumbs.some((b) => b.message === 'about to crash'), 'console breadcrumb captured');
});

test('node: an unhandled rejection is reported without killing the process', async () => {
  const got = [];
  const server = http.createServer((req, res) => {
    let b = '';
    req.on('data', (c) => (b += c));
    req.on('end', () => { got.push(...JSON.parse(b)); res.writeHead(201, { 'content-type': 'application/json' }).end('{}'); });
  });
  await new Promise((r) => server.listen(0, r));
  const code = `import Insight from ${JSON.stringify(path.join(here, '../src/node.js'))};
    Insight.init({ apiKey: 'ak', host: 'http://localhost:${server.address().port}' });
    Promise.reject(new Error('nobody caught me'));
    Promise.reject('a string reason');
    setTimeout(async () => { await Insight.flush(); console.log('still alive'); }, 200);`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', code], { stdio: 'pipe' });
  let out = '';
  child.stdout.on('data', (d) => (out += d));
  const exit = await new Promise((r) => child.on('exit', r));
  server.close();
  assert.equal(exit, 0);
  assert.match(out, /still alive/);
  assert.deepEqual(got.map((e) => e.description).sort(), ['Error: nobody caught me', 'UnhandledRejection: Non-Error promise rejection captured with value: a string reason'].sort());
});

test('cli: send-test-event and sourcemaps upload talk to the right URLs', async () => {
  const reqs = [];
  const server = http.createServer((req, res) => {
    let b = '';
    req.on('data', (c) => (b += c));
    req.on('end', () => { reqs.push({ url: req.url, key: req.headers['x-api-key'], body: JSON.parse(b) }); res.writeHead(201, { 'content-type': 'application/json' }).end('{"success":true}'); });
  });
  await new Promise((r) => server.listen(0, r));
  const host = `http://localhost:${server.address().port}`;
  const run = (...a) => new Promise((resolve) => { const c = spawn(process.execPath, [path.join(here, '../bin/insight-sdk.js'), ...a], { stdio: 'pipe' }); let o = ''; c.stdout.on('data', (d) => (o += d)); c.stderr.on('data', (d) => (o += d)); c.on('exit', (code) => resolve({ code, o })); });
  const t = await run('send-test-event', '--host', host, '--api-key', 'ak_cli');
  assert.equal(t.code, 0);
  assert.equal(reqs[0].url, '/api/ingest/events/');
  assert.equal(reqs[0].key, 'ak_cli');
  const fs = await import('node:fs');
  const os = await import('node:os');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'maps-'));
  fs.mkdirSync(path.join(dir, 'static'));
  fs.writeFileSync(path.join(dir, 'static', 'app.js.map'), '{"version":3,"mappings":""}');
  const u = await run('sourcemaps', 'upload', dir, '--release', 'web@9', '--host', host + '/api', '--api-key', 'ak_cli');
  assert.equal(u.code, 0, u.o);
  assert.equal(reqs[1].url, '/api/ingest/sourcemaps/');
  assert.equal(reqs[1].body.release, 'web@9');
  assert.equal(reqs[1].body.files[0].name, 'static/app.js.map');
  const none = await run('sourcemaps', 'upload', os.tmpdir() + '/definitely-empty-' + Date.now(), '--release', 'x', '--host', host, '--api-key', 'k');
  assert.notEqual(none.code, 0);
  server.close();
});
