# Insight ingest protocol (v1)

Every SDK speaks this. The server is `POST {host}/api/ingest/events/`.

## Auth
Header `X-API-Key: <key>` (or `Authorization: ApiKey <key>`). The key needs `write` permission. Keys for browser SDKs are public by nature: create a dedicated write-only key per site.

## Request
`Content-Type: application/json`. Body is one event object or an array (max 500 events, max 1 MB per request; SDKs should send small batches (≤ 20)).

```jsonc
{
  "event_type": "error",              // always "error" for now (other types are accepted and ignored)
  "action": "exception",
  "status": "error",
  "description": "TypeError: x is not a function",   // the title shown in the dashboard (<= 255 chars)
  "level": "error",                   // fatal | error | warning | info | debug
  "timestamp": "2026-10-08T12:00:00.000Z",
  "platform": "javascript",           // javascript | node | python | php | dart | other
  "sdk": { "name": "insight.javascript", "version": "1.0.0" },
  "release": "web@1.4.2",             // enables source maps and "new in release"
  "environment": "production",
  "server_name": "api-1",
  "user": { "id": "42", "email": "a@b.com", "username": "ada", "ip_address": "1.2.3.4" },
  "tags": { "feature": "checkout" },  // short searchable strings (<= 50 tags, values <= 200 chars)
  "extra": { "orderId": 991 },        // anything JSON (<= 16 KB after serialisation)
  "request": { "url": "https://app/x", "method": "POST", "query": "a=1", "headers": { "user-agent": "…" } },
  "breadcrumbs": [ { "timestamp": "…", "category": "console|http|ui.click|navigation|log|query|custom", "level": "info", "message": "…", "data": {} } ],  // newest last, max 100
  "exception": {
    "type": "TypeError",
    "value": "x is not a function",
    "stacktrace": [                   // MOST RECENT CALL FIRST
      { "filename": "https://app/static/app.min.js", "function": "pay", "lineno": 1, "colno": 5821, "in_app": true }
    ]
  },
  "fingerprint": ["checkout", "timeout"],   // optional: force grouping
  "metadata": { "traceback": "raw text stack, if you have one" }   // legacy/free-form, merged with the fields above
}
```

Only `event_type`+`description` are required. Unknown fields are kept. Secrets (passwords, tokens, card numbers) are scrubbed server side, but SDKs should not send them (`send_default_pii` is off by default).

## Grouping
The server groups events by fingerprint: your `fingerprint` if given, else a hash of the exception type + the top in-app frames (file basename + function, no line numbers) or, without frames, the title and stack with numbers removed. Line-number changes between releases therefore don't split an issue.

## Responses
| Status | Body `code` | SDK behaviour |
|---|---|---|
| 201 | | done |
| 400 | | drop the event (malformed) |
| 401 | | invalid or expired key: stop sending, log once, don't retry |
| 403 | | key lacks write permission: same as 401 |
| 413 | `payload_too_large` / `batch_too_large` | drop; split batches |
| 429 | `rate_limited` | retry after `Retry-After` seconds |
| 429 | `quota_exceeded` | monthly plan quota used up: pause sending until `Retry-After` (capped at 1 h) and drop events meanwhile; log once |
| 5xx / network | | retry with exponential backoff (1s, 2s, 4s … max 60s, jitter), at most 5 attempts, bounded queue (drop oldest) |

An SDK must never throw into the host app and never block the host app's request path (send asynchronously; flush on shutdown with a timeout).

## Source maps
`POST {host}/api/ingest/sourcemaps/` with the same key. Body: `{"release": "web@1.4.2", "files": [{"name": "static/app.min.js.map", "content": "<the .map file as a string>"}]}` (max 25 MB per request). A frame from `https://app/static/app.min.js` in an event with `release: "web@1.4.2"` is resolved to original file, function, line and column. The `insight-sdk` CLI does this for you: `npx insight-sdk sourcemaps upload ./dist --release web@1.4.2`.

## Minimum SDK feature set
init(config) · captureException(err, ctx) · captureMessage(msg, level, ctx) · setUser · setTag(s) · setExtra · addBreadcrumb · release/environment · beforeSend hook (return null to drop) · sampleRate · automatic unhandled-error capture · bounded queue with retry and 401/429 handling · flush(timeout) · tests.

---

# Performance metrics (protocol v1)

Server SDKs (Node, Python, PHP) report how long each request took. This is separate from error events: its own endpoint, its own monthly quota ("tracked requests"), and an error in one never blocks the other.

`POST {host}/api/ingest/metrics/`, same `X-API-Key` (write permission). `Content-Type: application/json`, max 1 MB.

SDKs **aggregate** requests in memory (or, for PHP, in shared storage) per transaction name and send one small summary about every 60 seconds, plus a few individual samples. Counts are exact.

```jsonc
{
  "sdk": { "name": "insight.node", "version": "1.1.0" },
  "platform": "node",
  "release": "api@1.4.2", "environment": "production", "server_name": "api-1",
  "interval_start": "2026-10-12T10:00:00Z",        // start of the window these numbers cover
  "interval_seconds": 60,
  "transactions": [
    {
      "name": "GET /items/{id}",                   // METHOD + ROUTE TEMPLATE, never the raw path (see below)
      "op": "http.server",
      "count": 120,                                // requests in the window
      "errors": 3,                                 // 5xx responses or unhandled exceptions (4xx are NOT errors)
      "sum_ms": 15400, "min_ms": 3, "max_ms": 950,
      "buckets": { "8": 40, "10": 60, "13": 20 }   // histogram: bucket index -> count (sparse)
    }
  ],
  "samples": [                                     // optional, at most 30 per request: the usual 10 (slowest, failed, one random) plus up to 20 of sampled traces
    { "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "name": "GET /items/{id}", "duration_ms": 950, "status_code": 500, "ok": false, "started_at": "2026-10-12T10:00:31.120Z" }
  ]
}
```

## Histogram buckets (all SDKs use exactly this table)
`BOUNDS_MS = [1, 2, 3, 5, 8, 12, 18, 25, 40, 60, 90, 130, 200, 300, 450, 700, 1000, 1500, 2200, 3300, 5000, 7500, 11000, 17000, 25000, 40000, 60000]`
A duration `d` (ms) goes in bucket `i` = the first index with `d <= BOUNDS_MS[i]`; durations above 60000 go in index 27 (`BOUNDS_MS.length`). The server computes p50/p95/p99 from merged histograms, so these are approximations that are within a bucket's width.

## Naming transactions
Use `"{METHOD} {route template}"`: `GET /items/{id}`, `POST /orders`. **Never put ids or raw URLs in a name.** If your framework did not match a route (404, scanner traffic), name it `"{METHOD} <unmatched>"`. Names that would exceed 500 per environment are folded into `<other>` by the server. By default, do not report health checks and static files (`/health`, `/healthz`, `/ping`, `/status`, `/metrics`, `/favicon.ico`, `/robots.txt`), because they would use up the quota.

## Responses
| Status | Meaning | SDK behaviour |
|---|---|---|
| 201 | accepted | done |
| 400 | malformed | drop |
| 401 / 403 | bad key / no write permission | stop sending metrics, log once |
| 413 | too large | drop the samples, keep the counts |
| 429 `rate_limited` | too many requests | wait `Retry-After`, then retry |
| 429 `quota_exceeded` (field `quota: "performance"`) | the plan's tracked requests for the month are used up | pause METRICS only (not errors) until `Retry-After` (cap 1 h), drop meanwhile, log once |
| 5xx / network | transient | keep the window and retry on the next flush, at most 5 windows buffered |

Never throw into the app, never block a request: all sending happens off the request path.

---

# Spans and traces (protocol v1, performance phase 3)

A **span** is one timed step inside a request: a database query, a call to another service. Spans answer "where did the time go?". They travel in two forms, both on `POST /api/ingest/metrics/`.

## 1. Span summaries (always sent, cheap)
Add a `spans` array next to `transactions`. One entry per (transaction, op, description), aggregated over the window exactly like transactions (same fields and the same histogram table):

```jsonc
"spans": [
  { "transaction": "GET /items/{id}", "op": "db.query", "description": "SELECT * FROM items WHERE id = ?",
    "count": 120, "errors": 0, "sum_ms": 540, "min_ms": 1.2, "max_ms": 40, "buckets": { "4": 90, "7": 30 } },
  { "transaction": "GET /items/{id}", "op": "http.client", "description": "POST api.stripe.com",
    "count": 12, "errors": 1, "sum_ms": 3100, "min_ms": 120, "max_ms": 800, "buckets": { "16": 12 } }
]
```
At most 300 summaries per request. `transaction` may be empty for work outside a request. The server shows these as the slowest queries and external services, and as the "where the time goes" breakdown per endpoint.

## 2. Spans inside samples (for the trace view)
A sample may carry `spans` (at most 100, ordered by `start_ms`):

```jsonc
{ "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "name": "GET /items/{id}", "duration_ms": 950, "status_code": 500, "ok": false, "started_at": "...",
  "spans": [
    { "span_id": "00f067aa0ba902b7", "parent_id": null, "op": "db.query", "description": "SELECT * FROM items WHERE id = ?",
      "start_ms": 12.3, "duration_ms": 4.2, "status": "ok", "data": { "db.system": "postgresql" } },
    { "span_id": "00f067aa0ba902b8", "parent_id": null, "op": "http.client", "description": "POST api.stripe.com",
      "start_ms": 30.0, "duration_ms": 800, "status": "error", "data": { "http.method": "POST", "http.status_code": 502, "host": "api.stripe.com" } }
  ] }
```
`start_ms` is the offset from the start of the request. `op` is `db.query`, `http.client`, `cache`, or any short string (<= 40 chars). `status` is `ok` or `error`. Allowed `data` keys: `db.system`, `db.name`, `http.method`, `http.status_code`, `host`, `cache.hit` (values are clipped to 200 chars).

## Rules for descriptions (privacy and cardinality)
- **SQL must be normalised by the SDK: no literal values.** `WHERE id = 42 AND name = 'bob'` becomes `WHERE id = ? AND name = ?`; `IN (1, 2, 3)` becomes `IN (?)`; `VALUES (...), (...)` becomes `VALUES (?)`; comments removed; whitespace collapsed; at most 300 chars. Bind values are never sent. The server normalises again defensively.
- **HTTP client spans are `METHOD host`** (no path, no query, no credentials), for example `GET api.stripe.com`.
- A request can have at most 200 spans collected; beyond that the SDK counts but does not record them.

## Linking errors to requests
Every transaction has a `trace_id` (32 hex characters, generated when the request starts). While a request is in progress, any error event the SDK sends carries the same value in a top-level `trace_id` field, and that request's sample (if one is sent) uses it too. The dashboard then links an error to its request trace and a trace back to the errors it caused.

---

# Distributed tracing and browser performance (protocol v1, performance phase 4)

## Trace context: the `traceparent` header
Insight uses the W3C Trace Context header, so other tools understand it:

`traceparent: 00-<trace_id: 32 hex>-<parent_span_id: 16 hex>-<flags: 2 hex>`    (flags `01` = sampled, `00` = not sampled)

- **Server SDKs** read `traceparent` on every incoming request. If it is valid they **continue the trace**: the request's `trace_id` is the header's, the request gets a new `span_id` (16 hex), and its `parent_span_id` is the header's span. Without a valid header they start a new trace. Every error event captured in the request carries the `trace_id` (already specified above).
- If the flags say **sampled**, the server SDK must send that request as a sample (up to 20 forced samples per window) even if it was fast and succeeded, so both halves of the trace exist.
- **Outgoing calls.** An SDK adds `traceparent` to outgoing HTTP requests only when the target host matches the configured `tracePropagationTargets` (default: none). The header's parent span is the id of the `http.client` span recorded for that call, and its flags repeat the current request's sampled bit. Never add the header to Insight's own endpoints.
- Spans carry `span_id` (16 hex). A sample has `span_id` (its root) and, when it continued another trace, `parent_span_id`.

## Samples: `kind`, `span_id`, `parent_span_id`
```jsonc
{ "kind": "server" | "browser",           // default "server"
  "op": "http.server" | "pageload" | "navigation",
  "trace_id": "...", "span_id": "...", "parent_span_id": "..." /* optional */,
  "name": "GET /api/pay" | "/orders/{id}", "duration_ms": 420, "status_code": 200, "ok": true, "started_at": "...",
  "spans": [ { "span_id": "...", "op": "http.client", "description": "POST api.example.com", "start_ms": 120, "duration_ms": 300, "status": "ok", "data": { ... } } ] }
```
The dashboard joins a browser sample and the server samples of the same `trace_id` where `server.parent_span_id` equals a `span_id` of the browser sample's `http.client` spans, and shows one waterfall.

## Browser performance: `pages` and page quota
Browser SDKs (the optional tracing module) send, on page hide, to the same `POST /api/ingest/metrics/`:

```jsonc
{ "sdk": { "name": "insight.browser", "version": "1.3.0" }, "platform": "javascript", "release": "web@1.4.2", "environment": "production",
  "interval_start": "...", "interval_seconds": 60,
  "pages": [ {
      "name": "/orders/{id}",              // route TEMPLATE: ids replaced by {id}; never the raw URL
      "count": 1,                          // page loads (or navigations) this entry stands for
      "sample_rate": 0.1,                  // the visitor-sampling rate in force; the dashboard estimates real traffic as count / sample_rate
      "browser": "Chrome 126", "device": "desktop" | "mobile" | "tablet",
      "vitals": {                          // each: how many measurements, and a histogram over the SAME bucket table
        "lcp":  { "count": 1, "buckets": { "17": 1 } },    // ms
        "fcp":  { "count": 1, "buckets": { "15": 1 } },    // ms
        "ttfb": { "count": 1, "buckets": { "13": 1 } },    // ms
        "inp":  { "count": 1, "buckets": { "13": 1 } },    // ms
        "cls":  { "count": 1, "buckets": { "7": 1 } }      // layout shift x 1000 (0.1 -> 100)
      } } ],
  "samples": [ { "kind": "browser", ... } ] }
```
Browser data has its **own monthly quota ("tracked page loads")**, counted as the sum of `count`. When it is used up the server answers `429 quota_exceeded` with `"quota": "page_loads"`; the browser SDK stops sending for the rest of the page session. Server request metrics are unaffected, and so are errors.

## Rules for browsers
- Everything is **opt-in** (`tracing` option, a separate script/module) and **sampled per visitor**; the decision is made once per page load and also decides the sampled flag on propagated headers.
- Never send raw URLs, query strings, hashes, cookies, IPs, or page content. Page names are route templates.
- Propagate `traceparent` only to same-origin requests and to hosts in `tracePropagationTargets` (adding a header to a cross-origin request triggers a CORS preflight, so the target server must allow `traceparent` in `Access-Control-Allow-Headers`).

# Sessions (release health)

`POST /api/ingest/sessions/` (write key, same `X-API-Key` header, max 4 KB, rate-limited per key, not counted against the event quota). Browser SDK only, opt-in (`sessions: true`).

```json
{ "session_id": "32 hex chars", "release": "web@1.4.2", "environment": "production",
  "user_id": "42",                                  // optional; only an id, never an email; stored hashed
  "status": "ok" | "errored" | "crashed",
  "duration_ms": 183000, "ended": true }            // sent when the tab is hidden / closed
```
- One session per browser tab (sessionStorage), a new one when the release changes. `release` is required (400 otherwise).
- A session starts `ok` and only gets worse: `errored` when an error was reported at level error/fatal during it, `crashed` when an unhandled error was (`mechanism` tag `onerror`, `onunhandledrejection`, `react.ErrorBoundary`, `vue`). The server keeps the worst status it has seen, whatever the order of the reports.
- Crash-free sessions = 1 - crashed / sessions. Crash-free users counts a user as crashed if any of their sessions crashed (sessions without `user_id` only count for the session rate). Adoption = a release's share of sessions in the window. `sessionSampleRate` makes these ratios of the reported sessions.
- Sessions are kept 35 days.

# Session replay

`POST /api/ingest/replay/` (write key, same `X-API-Key`; max 2.3 MB per request; 600 requests per minute per key). Browser SDK only, opt-in (`replay` option, a separate module). Plan-gated: Free has none (`403 replay_not_in_plan`), Pro 500 and Enterprise 5,000 recorded sessions a month (`429 quota_exceeded` with `"quota": "replays"` for a NEW session; one that already started may finish). Recordings never count against the error-event quota.

```json
{ "replay_id": "32 hex chars", "seq": 0, "release": "web@1.4.2", "environment": "production",
  "url": "/checkout", "user_id": "42", "started_at": 1790000000000,
  "masking": { "text": true, "inputs": true },
  "encoding": "gzip-base64", "data": "<base64 of gzip(JSON array of rrweb events)>" }
```
`events: [...]` (plain array) is accepted instead of `encoding`/`data`. `seq` numbers the chunks of one session from 0; a repeated `seq` is ignored (retries are safe). Limits: a chunk unpacks to at most 6 MB and 5,000 events (each event needs an integer `type` and a numeric `timestamp`); a session is stored up to 15 MB / 3,000 chunks (`413 replay_too_large`). The first chunk must start with rrweb's Meta (type 4) and full snapshot (type 2) events or the recording cannot be played.

- **Modes (per tab):** `session` uploads continuously (`sampleRate`, default 0.1 of sessions); `buffer` keeps the last one or two checkout segments (~30-60 s) in memory and uploads them, then everything after, when an error is reported (`errorSampleRate`, default 1 of the rest). Nothing leaves the browser in buffer mode until an error happens.
- **Linking:** an error event sent while a recording exists carries `replay_id`; the dashboard shows "Watch replay" on that error.
- **Privacy (the SDK's job, the server cannot re-mask):** by default all text is replaced by asterisks (spaces kept), all input values are hidden, passwords are never recorded; class `insight-block` / `blockSelector` removes an element, `insight-unmask` (or `unmaskSelector`) keeps text readable, `insight-ignore` ignores an input's events. `manualStart: true` records nothing until `Insight.startReplay()` (call it after the visitor consents). Customers must tell visitors (privacy policy, consent banner where the law needs one: NDPR, GDPR).
- **Retention:** deleted with the plan's retention (at most 90 days) and always after 95 days; `DELETE /api/replays/{id}/` (admin) removes one now.
