/**
 * Release health (opt-in: init({ sessions: true })): one session per browser tab, reported as ok -> errored -> crashed.
 * A session is "errored" when any error/message at error level was reported during it, and "crashed" when an unhandled
 * error was (window.onerror, unhandledrejection, a React/Vue error boundary). It never gets better. The server turns
 * these into crash-free sessions / users per release. Needs `release` to be set; without it nothing is sent.
 */

const CRASH = new Set(['onerror', 'onunhandledrejection', 'react.ErrorBoundary', 'vue']);
const RANK = { ok: 0, errored: 1, crashed: 2 };
const KEY = 'insight:session:v1';

const rid = () => {
  try { const a = new Uint8Array(16); crypto.getRandomValues(a); return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join(''); } catch { return (Math.random().toString(16).slice(2) + Date.now().toString(16)).padEnd(32, '0').slice(0, 32); }
};

export function startSessions(client, w) {
  const o = client.options;
  if (!o.sessions || client.disabled || !o.release || !client.endpoints) return;
  const rate = o.sessionSampleRate === undefined ? 1 : Number(o.sessionSampleRate);
  const ss = (fn) => { try { return fn(w.sessionStorage); } catch { return undefined; } };
  let s = ss((st) => JSON.parse(st.getItem(KEY) || 'null'));
  if (!s || s.release !== String(o.release)) { // a new tab, or a new release in an old tab: a new session
    s = { id: rid(), release: String(o.release), status: 'ok', t0: Date.now(), sampled: rate >= 1 || Math.random() < rate };
    ss((st) => st.setItem(KEY, JSON.stringify(s)));
  }
  if (!s.sampled) return;
  const url = client.endpoints.sessions;
  const send = (extra, keepalive) => {
    const user = client.scope.user;
    const body = { session_id: s.id, release: s.release, status: s.status, ...extra };
    if (o.environment) body.environment = String(o.environment);
    if (user && (user.id || user.id === 0)) body.user_id = String(user.id);
    try {
      Promise.resolve((o.fetch || client.platform.fetch)(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': o.apiKey }, body: JSON.stringify(body), keepalive: !!keepalive })).catch(() => {});
    } catch { /* never affect the page */ }
  };
  client.session = {
    note(event) {
      const mech = event.tags && event.tags.mechanism;
      const next = CRASH.has(mech) ? 'crashed' : event.level === 'error' || event.level === 'fatal' ? 'errored' : 'ok';
      if (RANK[next] > RANK[s.status]) {
        s.status = next;
        ss((st) => st.setItem(KEY, JSON.stringify(s)));
        send({});
      }
    },
  };
  send({});
  let ended = false;
  const leave = () => { if (!ended) { ended = true; send({ ended: true, duration_ms: Date.now() - s.t0 }, true); } };
  w.addEventListener('pagehide', leave);
  w.document.addEventListener('visibilitychange', () => { if (w.document.visibilityState === 'hidden') leave(); else ended = false; });
}
