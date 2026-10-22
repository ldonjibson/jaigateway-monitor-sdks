# Changelog

## 1.6.0

- Session replay (opt-in, browser): `init({ replay: { sampleRate, errorSampleRate } })` records visitor sessions with rrweb in a separate module (`insight-sdk/replay`, or `insight.replay.min.js`, about 25 KB gzipped, never in the core bundle). All text and inputs are masked by default; `.insight-block`, `.insight-unmask`, `blockSelector`, `manualStart` (consent) and `Insight.startReplay()/stopReplay()`. Errors carry the `replay_id` of the recording they happened in; with `errorSampleRate` the seconds before an error are kept in memory and uploaded only when one happens.

## 1.5.0

- Release health (opt-in, browser): `init({ release, sessions: true })` reports one session per tab (ok → errored → crashed) so the dashboard shows crash-free sessions and users per release. `sessionSampleRate` limits volume. Nothing is sent without `release`.

## 1.4.0
- Router helpers so browser tracing names pages by the router's real route pattern: `insight-sdk/next` (App Router), `insight-sdk/next-pages` (Pages Router), `insight-sdk/react-router` (v6.4+ / v7+), `insight-sdk/vue` (Vue Router 3/4; also reports errors thrown in components). `Insight.setRoute(name)` for any other router (a route set before `init()` is remembered).
- Any name passed to `setRoute` still has ids stripped, so a raw URL can never become a page name.

## 1.3.0
- Distributed tracing and browser performance. Browser (opt-in `tracing` option, separate small file): page loads and navigations by route pattern, Core Web Vitals, fetch/XHR spans, `traceparent` to same-origin and `tracePropagationTargets`, per-session sampling. Node: continues an incoming `traceparent`, keeps requests of sampled traces, `tracePropagationTargets`, `getTraceparent()`.

## 1.2.0
- Spans and traces (Node): outbound `http`/`https`/`fetch`, `pg` and `mysql2` are recorded automatically inside requests; `span()` / `startSpan()` for anything else. Each request has a `trace_id` that errors captured in it carry.

## 1.1.0
- Backend performance monitoring (Node): `requestHandler()` times requests, `transaction()` / `recordTransaction()` for jobs and handlers; summaries sent about once a minute; own quota, errors unaffected when it is exhausted.
- `checkIn()` / `monitor()` helpers for cron job (heartbeat) monitoring.

## 1.0.0
- New SDK: browser + Node + React in one package, zero dependencies.
- Unhandled errors/rejections, breadcrumbs (console, clicks, navigation, fetch/XHR), user/tags/extra, release/environment.
- Queue with retry, `Retry-After`, quota and invalid-key handling, per-minute guard, `flush()`.
- Express/Connect middleware, Next.js `onRequestError`, serverless `wrap()`, React `ErrorBoundary`.
- `insight-sdk` CLI: `send-test-event`, `sourcemaps upload`.
- Supersedes the earlier `sendEvent`/`sendError` helpers.
