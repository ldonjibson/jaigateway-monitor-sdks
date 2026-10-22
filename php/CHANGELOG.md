# Changelog

## 1.5.0

- Cache spans (op `cache`, Laravel). Redis `CommandExecuted` events become timed spans described by the upper-case command name only (parameters are never read); the service provider calls `Redis::enableEvents()` (guarded, no-op without Redis). Cache store events (`CacheHit`, `CacheMissed`, `KeyWritten`, `KeyForgotten`) become zero-duration markers `GET hit`, `GET miss`, `PUT`, `FORGET` with `cache.hit` on gets, never the key.
- Markers are always counted in the summaries but only the first 20 per description and request join the span list (`Client::MAX_CACHE_MARKERS`); a marker is skipped when a cache-like Redis command was recorded in the same request within 50 ms (no double counting for Redis stores).
- New option `performance_cache_spans` (default true; Laravel env `INSIGHT_PERFORMANCE_CACHE_SPANS`), `Client::isCacheSpansEnabled()`, `Client::recordCacheMarker()`; `recordSpan()` takes an optional `$listed` flag (false = count only).

## 1.4.0

- Distributed tracing, server side (protocol "Distributed tracing and browser performance", phase 4). Requests read the W3C `traceparent` header (plain PHP via `$_SERVER`, Laravel via the request) and continue that trace: same `trace_id`, a new 16-hex `span_id` per request, `parent_span_id` from the header. Malformed headers (wrong version, zero ids, bad hex) are ignored. Error events carry the adopted `trace_id`.
- Samples now carry `kind: "server"`, `op` (`http.server` or `queue.task`), `span_id` and, when continued, `parent_span_id`. In the shared window they are stored in a compact form (`si`, `pi`, `o`) and expanded when sent.
- Sampled traces (flags `01`) are always kept as samples: up to 20 forced samples per window (`s.f`) in addition to slowest 3 / failed 5 / 1 random. A window can now send up to 30 samples; since the server reads 10 per body, the extra ones go in samples-only bodies. Payload shrinking is now levelled (`Metrics::samplesAt()`): spans of ordinary samples, then spans of all, then ordinary samples, and last the forced ones; HTTP 413 drops ordinary samples first.
- Opt-in outgoing propagation: option `trace_propagation_targets` (hosts, `*.wildcards`, `host:port`, `#regex#`; Laravel env `INSIGHT_TRACE_PROPAGATION_TARGETS`). `Insight\Tracing\GuzzleMiddleware` adds `traceparent` (unless the app set one) and the `http.client` span uses the id placed in the header. Laravel HTTP client: a global request middleware via `Http::globalRequestMiddleware()` (Laravel 10.14+) when targets are configured. Never added to JaiGateway Monitor's own host.
- New API: `Insight::continueTrace($traceparent, $op = 'queue.task')`, `Insight::traceparent()`, `Client::parseTraceparent()`, `Client::outgoingTrace()`, `Client::matchesTarget()`; `Insight::beginTrace()` takes an optional traceparent; `recordSpan()` takes an optional span id.

## 1.3.0

- Spans and traces (protocol "Spans and traces", performance phase 3). Every request or job gets a `trace_id`; error events captured meanwhile carry it as a top-level `trace_id`, and the request's sample uses the same id.
- `Insight::startSpan()` (returns `Insight\Span`, `finish($ok, $data)`), `Insight::span($op, $description, $fn)`, `Insight::recordSpan()`, `Insight::beginTrace()`, `Insight::traceId()`. At most 200 spans are collected per request (more are only counted).
- Span summaries per (transaction, op, description) are aggregated in the shared window like transactions and sent as `spans` (same histogram table, at most 300 per payload, extra folded into `<other>`). Samples carry up to 100 spans (slowest kept, chronological). Payloads are kept under 1 MB (sample spans are dropped first).
- Laravel: automatic spans for database queries, outbound HTTP client calls and queue jobs (config `performance_spans`, env `INSIGHT_PERFORMANCE_SPANS`, default on).
- `Insight\Tracing\Sql::normalize()` (port of the server's normaliser), `Insight\Tracing\TracedPdo` and `Insight\Tracing\GuzzleMiddleware::create()` for non-Laravel apps.
- `Metrics::ingest()` takes an optional span list; window records and queued windows gained an `x` key. New option `performance_spans`.

## 1.2.0

- Backend performance monitoring (protocol "Performance metrics v1", `POST /api/ingest/metrics/`): per-route request counts, errors, min/max/sum and a latency histogram, about one POST per minute per app.
- PHP-FPM friendly: requests write into a shared one-minute window (APCu, else a flock-guarded file in the temp dir, else process memory); the request that notices the window has elapsed claims it atomically and sends it after the response. No per-request network call.
- `Insight::recordTransaction()`, `startTransaction()`, `finishTransaction()`, `setTransactionName()`; `registerErrorHandlers()` records the plain-PHP request at shutdown when `performance` is on.
- Laravel: automatic global terminable `PerformanceMiddleware` (Laravel 10/11/12), route-template names, queue job timing (`TASK <job>`, op `queue.task`), config `performance`, `performance_ignore`, `performance_window_seconds`, env `INSIGHT_PERFORMANCE`, `INSIGHT_PERFORMANCE_IGNORE`.
- Quota (`429 quota_exceeded`, `quota: performance`) pauses metrics only; error reporting is unaffected.
- New options `performance`, `performance_ignore`, `performance_window_seconds`; `State` gained `mutate()` and the `file` mode.

## 1.0.0

- Initial release: `Insight\Insight` facade, `Insight\Client`, pluggable `Insight\Transport` (curl, streams fallback).
- Structured exceptions (most recent frame first, `in_app`), previous-exception chain in `extra.exception_chain`.
- End-of-request batching with `fastcgi_finish_request()`, 401/403/413/429/5xx handling, shared pause state, rate guard.
- Plain PHP error, exception and fatal handlers.
- Laravel 10/11/12 integration: service provider, config, log and SQL breadcrumbs, `insight:test`, queue/Octane flushing.
