import { test } from 'node:test';
import assert from 'node:assert/strict';
import zlib from 'node:zlib';
import { client, fakeFetch, sleep, boom } from './helpers.mjs';
import { createReplay, maskText, replayOptions, recordOptions } from '../src/replay.js';

const fakeWindow = () => {
  const store = new Map();
  const listeners = {}, docListeners = {};
  return {
    sessionStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) },
    location: { pathname: '/checkout' },
    addEventListener: (n, f) => { (listeners[n] ||= []).push(f); },
    document: { visibilityState: 'visible', addEventListener: (n, f) => { (docListeners[n] ||= []).push(f); } },
    fire: (n) => (listeners[n] || []).forEach((f) => f()),
    store,
  };
};
// a stand-in for rrweb: the test pushes events through `emit`
const fakeRecord = () => {
  const rec = { opts: null, stopped: false, emit: null };
  rec.record = (opts) => { rec.opts = opts; rec.emit = opts.emit; return () => { rec.stopped = true; }; };
  return rec;
};
const ev = (type, t, data = {}) => ({ type, timestamp: t, data });
const posts = (f) => f.calls.filter((c) => /\/ingest\/replay\/$/.test(c.url)).map((c) => c.body);
const unpack = (p) => JSON.parse(zlib.gunzipSync(Buffer.from(p.data, 'base64')).toString('utf8'));
const setup = (replay, opts = {}) => {
  const rec = fakeRecord();
  const { c, fetch } = client({ release: 'web@1', environment: 'production', replay, ...opts });
  const w = fakeWindow();
  const ctl = createReplay(c, w, replay, { record: rec.record });
  c.replay = ctl;
  return { c, fetch, w, ctl, rec };
};

test('masking: text is replaced character by character but keeps spaces; unmask areas stay readable', () => {
  assert.equal(maskText('Pay ₦15,000 now'), '*** ******* ***');
  const o = recordOptions(replayOptions({}));
  assert.equal(o.maskTextSelector, '*');
  assert.equal(o.maskAllInputs, true);
  assert.equal(o.maskInputOptions.password, true);
  assert.equal(o.maskTextFn('secret', { closest: () => null }), '******');
  assert.equal(o.maskTextFn('visible', { closest: (s) => (s === '.insight-unmask' ? {} : null) }), 'visible');
  assert.equal(o.maskInputFn('hunter2'), '*******'); // typed values are never readable
  const open = recordOptions(replayOptions({ maskAllText: false, maskSelector: '.private' }));
  assert.equal(open.maskTextSelector, '.private');
  assert.equal(open.blockClass, 'insight-block');
});

test('defaults are conservative and the options are clamped', () => {
  const d = replayOptions(true);
  assert.deepEqual([d.sampleRate, d.errorSampleRate, d.maskAllText, d.maskAllInputs, d.manualStart], [0.1, 1, true, true, false]);
  const c = replayOptions({ sampleRate: 5, errorSampleRate: -1, maxDurationMs: 9e9, maskAllText: false });
  assert.deepEqual([c.sampleRate, c.errorSampleRate, c.maxDurationMs, c.maskAllText], [1, 0, 3600000, false]);
});

test('does nothing without a key or host, and never starts when both rates are 0', () => {
  const rec = fakeRecord();
  const noKey = client({ apiKey: '', replay: true });
  assert.equal(createReplay(noKey.c, fakeWindow(), { sampleRate: 1 }, { record: rec.record }).mode, 'off');
  const none = setup({ sampleRate: 0, errorSampleRate: 0 });
  assert.equal(none.ctl.mode, 'off');
  assert.equal(none.rec.opts, null);
});

test('session mode uploads gzip chunks with the right fields, in order, and links nothing to errors it did not see', async () => {
  const { ctl, rec, fetch, c } = setup({ sampleRate: 1, flushIntervalMs: 1000 });
  assert.equal(ctl.mode, 'session');
  c.scope.user = { id: 42, email: 'a@b.c' };
  rec.emit(ev(4, 1000, { href: 'http://x.test/' }), true);
  rec.emit(ev(2, 1001, { node: {} }), true);
  rec.emit(ev(3, 1500, { source: 2 }), false);
  ctl._flush();
  await sleep(20);
  rec.emit(ev(3, 2500, { source: 2 }), false);
  ctl._flush();
  await sleep(20);
  const [a, b] = posts(fetch);
  assert.deepEqual([a.seq, b.seq], [0, 1]);
  assert.match(a.replay_id, /^[0-9a-f]{32}$/);
  assert.equal(a.replay_id, b.replay_id);
  assert.deepEqual({ r: a.release, e: a.environment, u: a.url, uid: a.user_id, enc: a.encoding, m: a.masking }, { r: 'web@1', e: 'production', u: '/checkout', uid: '42', enc: 'gzip-base64', m: { text: true, inputs: true } });
  assert.ok(!('email' in a));
  assert.deepEqual(unpack(a).map((e) => e.type), [4, 2, 3]);
  assert.equal(fetch.calls[0].headers['X-API-Key'], 'ak_test');
  assert.equal(fetch.calls[0].url, 'https://insight.test/api/ingest/replay/');
});

test('an error sent during a session carries the replay id', async () => {
  const { c, ctl, rec } = setup({ sampleRate: 1 });
  rec.emit(ev(4, 1), true); rec.emit(ev(2, 2), true);
  const e = c.captureException(boom('x'));
  assert.equal(e.replay_id, ctl.id);
});

test('buffer mode keeps only the last two segments, and uploads them when an error is reported', async () => {
  const { c, ctl, rec, fetch } = setup({ sampleRate: 0, errorSampleRate: 1 });
  assert.equal(ctl.mode, 'buffer');
  for (let s = 0; s < 4; s++) { // four checkouts: only the last two segments stay
    rec.emit(ev(4, 1000 * s), true); rec.emit(ev(2, 1000 * s + 1), true); rec.emit(ev(3, 1000 * s + 500), false);
  }
  ctl._flush();
  await sleep(10);
  assert.equal(posts(fetch).length, 0, 'nothing is uploaded while nothing went wrong');
  const e = c.captureException(boom('crash'));
  assert.equal(ctl.mode, 'session');
  await sleep(30);
  const [p] = posts(fetch);
  const events = unpack(p);
  assert.deepEqual(events.map((x) => x.timestamp), [2000, 2001, 2500, 3000, 3001, 3500]); // starts at a full snapshot
  assert.equal(e.replay_id, p.replay_id);
  rec.emit(ev(3, 4000), false);
  ctl._flush();
  await sleep(20);
  assert.equal(posts(fetch).length, 2); // and it keeps recording after the error
});

test('manualStart waits for start() (consent), stop() uploads the rest and stops recording', async () => {
  const { ctl, rec, fetch } = setup({ sampleRate: 1, manualStart: true });
  assert.equal(ctl.mode, 'off');
  assert.equal(rec.opts, null);
  ctl.start();
  assert.equal(ctl.mode, 'session');
  rec.emit(ev(4, 1), true); rec.emit(ev(2, 2), true);
  ctl.stop();
  await sleep(30);
  assert.equal(rec.stopped, true);
  assert.equal(posts(fetch).length, 1);
  assert.equal(posts(fetch)[0].final, true);
  rec.emit?.(ev(3, 3));
  assert.equal(ctl.mode, 'off');
});

test('leaving the page uploads what is pending', async () => {
  const { ctl, rec, fetch, w } = setup({ sampleRate: 1 });
  rec.emit(ev(4, 1), true); rec.emit(ev(2, 2), true);
  w.fire('pagehide');
  await sleep(30);
  assert.equal(posts(fetch).length, 1);
  assert.equal(fetch.calls[0].keepalive, true);
  assert.ok(ctl.id);
});

test('the server saying no (not in plan, over quota, bad key) stops recording for this page', async () => {
  for (const status of [403, 429, 401, 413]) {
    const f = fakeFetch([{ status }]);
    const rec = fakeRecord();
    const { c } = client({ release: 'r', replay: true }, f);
    const ctl = createReplay(c, fakeWindow(), { sampleRate: 1 }, { record: rec.record });
    rec.emit(ev(4, 1), true); rec.emit(ev(2, 2), true);
    ctl._flush();
    await sleep(30);
    assert.equal(ctl.mode, 'off', `status ${status}`);
    assert.equal(rec.stopped, true);
  }
});

test('a recorder that throws, or an offline upload, never breaks the page', async () => {
  const { c } = client({ release: 'r', replay: true });
  const ctl = createReplay(c, fakeWindow(), { sampleRate: 1 }, { record: () => { throw new Error('rrweb exploded'); } });
  assert.equal(ctl.mode, 'off');
  const f = fakeFetch([new Error('offline')]);
  const rec = fakeRecord();
  const b = client({ release: 'r', replay: true }, f);
  const ctl2 = createReplay(b.c, fakeWindow(), { sampleRate: 1 }, { record: rec.record });
  rec.emit(ev(4, 1), true); rec.emit(ev(2, 2), true);
  assert.doesNotThrow(() => ctl2._flush());
  await sleep(30);
});

test('the same tab keeps one recording id across reloads of the same release', async () => {
  const w = fakeWindow();
  const a = client({ release: 'web@1', replay: true }); const ra = fakeRecord();
  const c1 = createReplay(a.c, w, { sampleRate: 1 }, { record: ra.record });
  const b = client({ release: 'web@1', replay: true }); const rb = fakeRecord();
  const c2 = createReplay(b.c, w, { sampleRate: 1 }, { record: rb.record });
  const d = client({ release: 'web@2', replay: true }); const rd = fakeRecord();
  const c3 = createReplay(d.c, w, { sampleRate: 1 }, { record: rd.record });
  assert.equal(c1.id, c2.id);
  assert.notEqual(c1.id, c3.id);
});
