# Changelog

## 1.4.0

Distributed tracing, server side (protocol "Distributed tracing and browser performance", performance phase 4). Still dependency-free.

- Inbound: every web integration (all go through `integrations/_common.begin_request`, plus the plain WSGI middleware and Litestar's
  after-exception path) reads `traceparent` (header case-insensitive, WSGI `HTTP_TRACEPARENT`, ASGI headers). A valid header (version `00`,
  non-zero ids, 2-hex flags) is adopted: the transaction takes its `trace_id`, records `parent_span_id` and `sampled = flags & 1`; the request
  always gets a new 16-hex `span_id`. Malformed headers are ignored. Error events captured in the request carry the adopted id.
- Job integrations: Celery (`request.traceparent` / `request.headers`), RQ (`job.meta["traceparent"]`) and Dramatiq
  (`message.options["traceparent"]`) continue a trace when one is present; Huey has no such channel (use `continue_trace`).
- Samples carry `kind: "server"`, `op` (`http.server` / `queue.task`), `span_id` and `parent_span_id` (when continued). Sample spans have
  16-hex `span_id`s.
- Forced samples: requests whose trace is sampled (`flags & 1`) are always sent as samples, up to 20 per window, in addition to the usual
  slowest 3 / failed 5 / 1 random. Under the 1 MB payload limit they are the last to be dropped (order: regular sample spans, forced sample
  spans, regular samples, all samples, span summaries).
- Outbound (opt-in): new `init(trace_propagation_targets=[...])` (host strings with `*` wildcards, `host:port`, or compiled regexes). Matching
  calls made inside a transaction get `traceparent: 00-<trace_id>-<http.client span id>-<01|00>` from `requests`, `httpx` (sync + async), the
  `aiohttp` client, `urllib3` and `urllib.request`; the `http.client` span is recorded with the same `span_id`. An existing header is never
  overwritten, JaiGateway Monitor's own endpoints and heartbeat pings never get one.
- New public helpers `insight_sdk.get_traceparent()` and `insight_sdk.continue_trace(traceparent, name=None, op="custom")` (sync and async
  context manager); `start_span` objects expose `.span_id` and `.traceparent`; `record_span(..., span_id=None)`.
- Tests: new `tests/test_tracing.py` plus adoption tests in the Flask, FastAPI and Django suites.

## 1.3.0

Spans and traces (protocol "Spans and traces", performance phase 3). Still dependency-free; every library is imported lazily.

- `insight_sdk.start_span(op, description, data=None)` (context manager and decorator, sync and async) and the lower-level
  `insight_sdk.record_span(op, description, start, duration_ms, ok, data)`. Each transaction collects at most
  `performance_max_spans` (default 200) spans; more are only counted in the aggregates.
- Every transaction has a `trace_id` (32 hex). The same id is on its sample and, as a top-level `trace_id`, on every error event
  captured while it is active (`capture_exception`, `capture_message`, logging and excepthook paths). No trace id outside a transaction.
- The metrics payload carries `spans` summaries per (transaction, op, description) with the same histogram table (max 300, the rest
  folded into `<other>`); samples carry up to 100 spans (slowest kept, chronological) with `start_ms`, `span_id`, `status`, allowed `data` keys.
  Spans outside a transaction are summarised under transaction `""`. Oversized payloads drop sample spans first, then samples.
- Automatic instrumentation, patched when the library is imported (or already is): `urllib.request.urlopen`, `requests`,
  `urllib3`, `httpx` (sync + async), `aiohttp` client, Django ORM (`execute_wrapper` on every connection), SQLAlchemy (Engine
  events), `redis-py` (command name only). Database spans use a pure-Python port of the server's `normalizeSql` (no literals, no
  bind parameters); HTTP spans are `METHOD host` only. Celery/RQ/Dramatiq/Huey tasks get spans through their task transaction.
- New `init()` options `performance_spans=True` and `performance_max_spans=200`; `integrations=[...]` accepts
  `urllib`, `requests`, `urllib3`, `httpx`, `aiohttp_client`, `django_orm`, `sqlalchemy`, `redis`.
- `start_transaction` now makes its transaction the current one, so spans and errors inside the block attach to it.
- Patches are idempotent, removed by `close()`/re-`init()`, and wrapped so an SDK bug can never break the host app.
- Tests: new `tests/test_spans.py` (30).

## 1.2.0

Backend performance monitoring (protocol "Performance metrics", `POST /api/ingest/metrics/`). Still dependency-free.

- Every web integration (Django/DRF, Flask, FastAPI/Starlette, Litestar, Sanic, aiohttp, Tornado, Quart, Pyramid, Bottle,
  Falcon, plain WSGI/ASGI) times each request, and every job integration (Celery, RQ, Dramatiq, Huey) each task
  execution, with no change needed from the user. Transactions are named `METHOD route-template` (`GET <unmatched>` when no
  route matched, never the raw path) or `TASK name`; 5xx and unhandled exceptions are errors, 4xx are not.
- `insight_sdk.performance`: thread-safe in-memory aggregator (count, errors, sum/min/max, the protocol's 28-bucket
  histogram: `BOUNDS_MS`, `bucket_index()`), up to 10 samples per window, a lazy background daemon thread that sends
  one summary about every 60 s, fork-safe, flushed at exit. Response handling per protocol: 400 drop, 401/403 disable
  metrics only, 413 drop samples, 429 `rate_limited` retry after `Retry-After`, 429 `quota_exceeded` pause metrics only
  (error reporting is unaffected), 5xx/network keep up to 5 windows.
- New `init()` options: `performance=True`, `performance_ignore` (default: health checks, `/static/*`, `/assets/*`),
  `performance_flush_interval=60.0` (minimum 5).
- New public helper `insight_sdk.start_transaction(name, op="custom")` (context manager and decorator, sync and async);
  `insight_sdk.flush()`/`close()`/exit now also flush pending metrics.
- Layered middleware report one transaction per request (deduped through the request scope). The Bottle plugin now wraps
  the app's WSGI callable so unmatched 404s and the response status are seen; Litestar counts unmatched 404s from its
  `after_exception` hook (duration ~0); Sanic times requests that fail before request middleware runs.
- Internal: `transport.http_post()` shared by the event and metrics senders; `Scope.txn`; `Client.performance`.
- Tests: ~100 new (bucket table, aggregation, samples, every response row, fork safety, ignore list, layering, and a
  success + 404 + 500 transaction assertion in every framework test file).

## 1.1.0

Dedicated, first-class integrations for the popular Python frameworks. The base package is still dependency-free and
imports no framework (a subprocess test guards it).

- New integrations: FastAPI, Starlette, Litestar (`InsightPlugin`), Sanic, aiohttp, Tornado (`setup()` / `InsightMixin`),
  Quart, Pyramid (tween), Bottle (plugin), Falcon (WSGI + ASGI), RQ, Dramatiq (middleware), Huey.
- Django: `drf_exception_handler` / `wrap_drf_exception_handler` for Django REST Framework, `process_exception` and
  `process_view` in the middleware (template-render errors, `route` tag), deduped with the `got_request_exception` path.
- Flask: `route` tag, framework tags, `http` breadcrumb, and exceptions swallowed by your own `@app.errorhandler`
  are reported. Behaviour change: events now always carry a `route` tag (tests that used a custom `route` tag as an
  isolation probe need another tag name).
- Every web integration tags `framework`, `framework_version` and `route` (template) and adds an `http` breadcrumb;
  4xx / `HTTPException` < 500 / redirects are not reported; each exception is reported once.
- Celery: tags `celery_task`, `celery_task_id`, `celery_queue`, `celery_retries`; retried attempts are never reported
  (breadcrumb only), only the final failure.
- Automatic mode (`init(integrations=None)`): also enables RQ when imported; logs one debug line listing what was enabled
  (`insight_sdk.integrations.auto`). App-object integrations stay explicit; see the README table.
- Query strings are filtered (`token=`, `password=`, `api_key=`, ... become `[Filtered]`) and URLs never contain a query.
- Scopes: `push_scope(shared=True)` lets helper threads / child tasks of one request write to the request scope.
- Extras for every framework (`pip install "insight-sdk[fastapi]"`).

## 1.0.0

First release of the rewritten SDK (replaces `insight_client` / `sdk_ingest`).

- Sentry-style API: `init`, `capture_exception`, `capture_message`, scopes, breadcrumbs, `flush`, `close`.
- Background worker with bounded queue (drops oldest), batching (20), exponential backoff with jitter,
  `Retry-After` handling, quota pause, auth disable, 413 split, client-side rate guard.
- Structured exceptions (most recent frame first, `in_app`, chained exceptions), never sends local variables.
- Integrations: logging, `sys.excepthook`/`threading.excepthook`, Django, Flask, WSGI, ASGI, Celery.
- `insight-sdk send-test-event` CLI.
