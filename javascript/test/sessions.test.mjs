import { test } from 'node:test';
import assert from 'node:assert/strict';
import { client, fakeFetch, sleep, boom } from './helpers.mjs';
import { startSessions } from '../src/sessions.js';

const fakeWindow = () => {
  const store = new Map();
  const listeners = {};
  const docListeners = {};
  return {
    sessionStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) },
    addEventListener: (n, f) => { (listeners[n] ||= []).push(f); },
    document: { visibilityState: 'visible', addEventListener: (n, f) => { (docListeners[n] ||= []).push(f); } },
    fire: (n) => (listeners[n] || []).forEach((f) => f()),
    hide() { this.document.visibilityState = 'hidden'; (docListeners.visibilitychange || []).forEach((f) => f()); },
    store,
  };
};
// sessions go through the same fetch as events; keep only the session posts
const posts = (f) => f.calls.filter((c) => /\/ingest\/sessions\/$/.test(c.url)).map((c) => c.body);

test('does nothing unless sessions is on and a release is set', async () => {
  for (const opts of [{}, { sessions: true }, { release: 'r1' }]) {
    const { c, fetch } = client(opts);
    startSessions(c, fakeWindow());
    await sleep(5);
    assert.equal(posts(fetch).length, 0, JSON.stringify(opts));
    assert.equal(c.session, undefined);
  }
});

test('starts an ok session and reports it to the sessions endpoint with the API key', async () => {
  const { c, fetch } = client({ release: 'web@1', environment: 'production', sessions: true });
  startSessions(c, fakeWindow());
  await sleep(5);
  const [first] = posts(fetch);
  assert.match(first.session_id, /^[0-9a-f]{32}$/);
  assert.deepEqual({ ...first, session_id: 'x' }, { session_id: 'x', release: 'web@1', status: 'ok', environment: 'production' });
  assert.equal(fetch.calls[0].headers['X-API-Key'], 'ak_test');
  assert.equal(fetch.calls[0].url, 'https://insight.test/api/ingest/sessions/');
});

test('an error report makes the session errored; an unhandled error makes it crashed; it never gets better', async () => {
  const { c, fetch } = client({ release: 'r1', sessions: true });
  startSessions(c, fakeWindow());
  c.captureMessage('just info', 'info');
  c.captureException(boom('handled'));
  c.captureException(boom('crash'), { tags: { mechanism: 'onerror' } });
  c.captureException(boom('handled again'));
  await sleep(10);
  assert.deepEqual(posts(fetch).map((p) => p.status), ['ok', 'errored', 'crashed']); // info did nothing; the last error added nothing
});

test('events that are dropped (ignoreErrors, sampleRate 0) do not change the session', async () => {
  const { c, fetch } = client({ release: 'r1', sessions: true, ignoreErrors: [/ignore me/] });
  startSessions(c, fakeWindow());
  c.captureException(boom('ignore me'), { tags: { mechanism: 'onerror' } });
  await sleep(10);
  assert.deepEqual(posts(fetch).map((p) => p.status), ['ok']);
});

test('the same tab keeps one session across reloads; a new release starts a new one', async () => {
  const w = fakeWindow();
  const a = client({ release: 'r1', sessions: true }); startSessions(a.c, w);
  const b = client({ release: 'r1', sessions: true }); startSessions(b.c, w);
  const c2 = client({ release: 'r2', sessions: true }); startSessions(c2.c, w);
  await sleep(5);
  assert.equal(posts(a.fetch)[0].session_id, posts(b.fetch)[0].session_id);
  assert.notEqual(posts(c2.fetch)[0].session_id, posts(a.fetch)[0].session_id);
});

test('sends the user id only when one is set, and the duration when the page is hidden', async () => {
  const w = fakeWindow();
  const { c, fetch } = client({ release: 'r1', sessions: true });
  startSessions(c, w);
  c.scope.user = { id: 42, email: 'a@b.c' };
  await sleep(5);
  w.hide();
  await sleep(5);
  const last = posts(fetch).at(-1);
  assert.equal(last.ended, true);
  assert.equal(last.user_id, '42');
  assert.ok(!('email' in last));
  assert.ok(last.duration_ms >= 0);
  assert.equal(fetch.calls.at(-1).keepalive, true);
  w.fire('pagehide'); // already ended: no duplicate
  await sleep(5);
  assert.equal(posts(fetch).filter((p) => p.ended).length, 1);
});

test('sessionSampleRate 0 reports nothing', async () => {
  const { c, fetch } = client({ release: 'r1', sessions: true, sessionSampleRate: 0 });
  startSessions(c, fakeWindow());
  c.captureException(boom('x'), { tags: { mechanism: 'onerror' } });
  await sleep(10);
  assert.equal(posts(fetch).length, 0);
});

test('a failing sessions endpoint never breaks the page', async () => {
  const f = fakeFetch([new Error('offline')]);
  const { c } = client({ release: 'r1', sessions: true }, f);
  assert.doesNotThrow(() => startSessions(c, fakeWindow()));
  c.captureException(boom('x'), { tags: { mechanism: 'onerror' } });
  await sleep(10);
});
