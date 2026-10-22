# insight-sdk (Python)

Error monitoring SDK for [JaiGateway Monitor](../PROTOCOL.md). Zero required dependencies, Python 3.8+.
First-class integrations for Django (+DRF), Flask, FastAPI, Starlette, Litestar, Sanic, aiohttp, Tornado, Quart, Pyramid,
Bottle, Falcon, Celery, RQ, Dramatiq and Huey.
Events are sent from a background thread: the SDK never blocks your request path and never raises into your app.

## Install

```bash
pip install insight-sdk                 # core
pip install "insight-sdk[fastapi]"      # extras (django, drf, flask, fastapi, litestar, sanic, ...) only pin the framework; the SDK needs nothing
```

## Quickstart

```python
import insight_sdk

insight_sdk.init(api_key="ak_...", host="https://monitor.jaigateway.com", release="web@1.4.2", environment="production")

try:
    risky()
except Exception:
    insight_sdk.capture_exception()      # or: insight_sdk.capture_message("hello", level="warning")
```

Unhandled exceptions (main thread and other threads) and `logging.error(...)` calls are captured automatically.
Verify your setup from a shell:

```bash
insight-sdk send-test-event --host https://monitor.jaigateway.com --api-key ak_...
```

## Frameworks

Every integration lives in its own module under `insight_sdk.integrations`, imports its framework lazily (importing
`insight_sdk` never imports a framework) and extras only pin the framework: `pip install "insight-sdk[fastapi]"`.
Each one, wherever the framework allows it:

- gives every request/job its own scope (concurrent requests never share tags, user or breadcrumbs; helper threads
  and background tasks of a request write into the same scope);
- attaches the request `url` (without query string), `method`, the query string with secret-looking values filtered
  (`token=[Filtered]`), headers without `Authorization`/`Cookie`/`Set-Cookie`/`X-API-Key`, never the body;
- tags `framework`, `framework_version` and `route` (the route **template**, e.g. `/items/{id}`, not `/items/42`) and
  adds an `http` breadcrumb for the request (jobs: a `custom` breadcrumb plus job tags);
- reports each exception once (the same exception object is deduped across every layer that sees it), ignores
  expected exceptions (404, redirects, `HTTPException` < 500, DRF 4xx) and re-raises / returns the framework's own
  500 response, so normal error handling is unchanged;
- never raises from SDK code.

### Automatic vs one line

`insight_sdk.init(...)` with the default `integrations=None` enables, for frameworks your app has **already imported**,
only integrations that need no app object (it never imports a framework itself) and logs one debug line
(`insight_sdk: auto-enabled integrations: logging, excepthook, flask`, visible with `debug=True`).

| Framework | Automatic after `init()` | The one line you add |
|---|---|---|
| logging, `sys.excepthook`, threads | yes (always) | - |
| Celery | yes, if `celery` is imported | - |
| RQ | yes, if `rq` is imported | - |
| Django (500 signal) | yes, if `django` is imported | `MIDDLEWARE = ["insight_sdk.integrations.django.InsightMiddleware", ...]` for scope/route/template errors |
| Django REST Framework | **no** (it is a setting) | `REST_FRAMEWORK = {"EXCEPTION_HANDLER": "insight_sdk.integrations.django.drf_exception_handler"}` |
| Flask (500 signal) | yes, if `flask` is imported | `init_app(app)` for scope, route tag, swallowed errors |
| FastAPI | no | `init_app(app)` |
| Starlette | no | `init_app(app)` |
| Litestar | no | `Litestar(..., plugins=[InsightPlugin()])` |
| Sanic | no | `init_app(app)` |
| aiohttp | no | `setup(app)` |
| Tornado | no | `setup()` (or `InsightMixin`) |
| Quart | no | `init_app(app)` |
| Pyramid | no | `config.include("insight_sdk.integrations.pyramid")` |
| Bottle | no | `app.install(InsightPlugin())` |
| Falcon | no | `init_app(app)` |
| Dramatiq | no | `broker.add_middleware(InsightMiddleware())` |
| Huey | no | `install(huey)` |
| Plain WSGI / ASGI | no | `InsightWSGIMiddleware(app)` / `app.add_middleware(InsightASGIMiddleware)` |

All snippets assume `insight_sdk.init(api_key="ak_...", host="https://monitor.jaigateway.com", release="web@1.4.2")` ran first.

### Web

**Django** (`insight-sdk[django]`) - middleware first in the list; errors in views, middleware and template rendering
are reported with the URL pattern as `route` (`/items/<int:pk>/`). `process_exception`, Django's
`got_request_exception` signal and the DRF handler see the same exception and are deduped. With `send_default_pii=True`
the authenticated `request.user` and IP are attached.

```python
MIDDLEWARE = ["insight_sdk.integrations.django.InsightMiddleware", ...]   # put it first
REST_FRAMEWORK = {"EXCEPTION_HANDLER": "insight_sdk.integrations.django.drf_exception_handler"}   # only with DRF
```

The DRF handler wraps DRF's default one: unexpected exceptions and 5xx `APIException`s are reported, 4xx
(`ValidationError`, `NotFound`, `PermissionDenied`...) are not, and DRF's response is returned unchanged. To keep your
own handler: `wrap_drf_exception_handler(my_handler)` from the same module.

**Flask** (`insight-sdk[flask]`)

```python
from insight_sdk.integrations.flask import init_app
init_app(app)          # scope per request, route tag "/items/<int:id>", also reports errors your @app.errorhandler swallows
```

**Pyramid** (`insight-sdk[pyramid]`)

```python
config.include("insight_sdk.integrations.pyramid")   # tween just below the exception-view tween
```

**Bottle** (`insight-sdk[bottle]`)

```python
from insight_sdk.integrations.bottle import InsightPlugin
app.install(InsightPlugin())
```

**Falcon** (`insight-sdk[falcon]`) - `falcon.App` and `falcon.asgi.App`

```python
from insight_sdk.integrations.falcon import init_app
init_app(app)          # middleware + wraps the Exception / HTTPError handlers (5xx reported, 4xx not)
```

**Plain WSGI**: `from insight_sdk.integrations.wsgi import InsightWSGIMiddleware; app = InsightWSGIMiddleware(app)`.

### Async web

**FastAPI** (`insight-sdk[fastapi]`)

```python
from insight_sdk.integrations.fastapi import init_app
init_app(app)          # middleware + HTTPException handler; route tag "/items/{item_id}"
```

Unhandled exceptions are reported once; `HTTPException` with status >= 500 is reported, 4xx and validation errors are
not. Dependencies (sync or async), endpoints and background tasks share the request scope, so
`insight_sdk.set_user(...)` in a dependency lands on the event.

**Starlette** (`insight-sdk[starlette]`)

```python
from insight_sdk.integrations.starlette import init_app
init_app(app)
```

**Litestar** (`insight-sdk[litestar]`)

```python
from insight_sdk.integrations.litestar import InsightPlugin
app = Litestar([...], plugins=[InsightPlugin()])   # middleware + after_exception hook; route from path_template
```

**Sanic** (`insight-sdk[sanic]`)

```python
from insight_sdk.integrations.sanic import init_app
init_app(app)          # request/response middleware + @app.exception(Exception) -> then Sanic's default error response
```

**aiohttp** (`insight-sdk[aiohttp]`)

```python
from insight_sdk.integrations.aiohttp import setup
setup(app)             # or web.Application(middlewares=[insight_middleware()]); sub-apps need their own setup()
```

**Tornado** (`insight-sdk[tornado]`)

```python
from insight_sdk.integrations.tornado import setup
setup()                # wraps RequestHandler.log_exception once; or: class Base(InsightMixin, RequestHandler)
```

**Quart** (`insight-sdk[quart]`)

```python
from insight_sdk.integrations.quart import init_app
init_app(app)
```

**Plain ASGI**: `app.add_middleware(InsightASGIMiddleware)` from `insight_sdk.integrations.asgi`.

### Background jobs

Every job runs in its own scope (arguments are never sent). Retried attempts are not reported; only the final failure
is, so three attempts that all fail are one event and an attempt that succeeds later is none.

| Library | Tags |
|---|---|
| Celery | `celery_task`, `celery_task_id`, `celery_queue` (when run by a worker), `celery_retries` |
| RQ | `rq_job_id`, `rq_func`, `rq_queue` |
| Dramatiq | `dramatiq_actor`, `dramatiq_message_id`, `dramatiq_queue`, `dramatiq_retries` |
| Huey | `huey_task`, `huey_task_id`, `huey_retries` |

**Celery** (`insight-sdk[celery]`) - automatic; just call `init()` in the worker process (for example in the module
that creates your `Celery` app).

**RQ** (`insight-sdk[rq]`)

```python
from insight_sdk.integrations.rq import install
install()              # automatic when rq is imported; patches the Worker class once (also works with forked work horses)
# alternatives: install(worker)  |  rq worker --exception-handler insight_sdk.integrations.rq.exception_handler
```

**Dramatiq** (`insight-sdk[dramatiq]`)

```python
from insight_sdk.integrations.dramatiq import InsightMiddleware
broker.add_middleware(InsightMiddleware())     # honours Retries and the actor's throws= option
```

**Huey** (`insight-sdk[huey]`)

```python
from insight_sdk.integrations.huey import install
install(huey)          # works for the consumer and for immediate=True
```

Not included: arq and APScheduler (no hook was trivial enough to ship; use `capture_exception` in a listener).

### Logging

```python
import logging
log = logging.getLogger("payments")
log.info("charging card")        # becomes a breadcrumb on later events
log.error("charge failed")       # becomes an event; log.exception(...) keeps the traceback
```

Records from the `insight_sdk` logger are ignored, so the SDK can never report itself. Breadcrumbs are only
recorded for levels your loggers actually emit (set the level of your logger or the root logger to INFO).

## Context

```python
insight_sdk.set_user({"id": "42", "email": "ada@example.com"})
insight_sdk.set_tag("feature", "checkout")
insight_sdk.set_extra("order_id", 991)
insight_sdk.add_breadcrumb("clicked pay", category="ui.click")

with insight_sdk.push_scope() as scope:      # changes are dropped when the block ends
    scope.set_tag("job", "nightly")
    insight_sdk.capture_exception(exc, fingerprint=["nightly", "timeout"])

insight_sdk.capture_exception(exc, tags={"a": "b"}, extra={"k": 1}, user={...}, level="fatal")
```

Scopes use `contextvars`: every thread and every asyncio task has its own scope. A new thread starts empty; a task
inherits its parent's data and changes stay private.

## Configuration

| Option | Default | Meaning |
|---|---|---|
| `api_key` | `$INSIGHT_API_KEY` | Write-permission API key (sent as `X-API-Key`) |
| `host` | `$INSIGHT_HOST` | App URL. `/api/ingest/events/` is appended; trailing slashes and a trailing `/api` are tolerated |
| `endpoint` | - | Full ingest URL, instead of `host` |
| `release`, `environment` | `$INSIGHT_RELEASE`, `$INSIGHT_ENVIRONMENT` | Attached to every event |
| `server_name` | hostname | |
| `sample_rate` | `1.0` | Fraction of events to keep |
| `max_breadcrumbs` | `100` | Per scope |
| `before_send` | - | `fn(event, hint) -> event or None` (None drops it; raising drops it too) |
| `send_default_pii` | `False` | Only then are the Django user and IP attached automatically |
| `in_app_include` / `in_app_exclude` | `()` | Module prefixes forced in/out of "in app" (default: not stdlib, not site-packages) |
| `integrations` | `None` | `None` = auto: logging, excepthook, plus Django/Flask/Celery/RQ when already imported (see "Automatic vs one line"); `[]` = none; or names (`logging`, `excepthook`, `django`, `flask`, `celery`, `rq`) |
| `max_queue_size` | `100` | Bounded queue; the oldest event is dropped when full |
| `max_events_per_minute` | `60` | Client-side guard so a crash loop cannot burn your quota (`0` = off) |
| `shutdown_timeout` | `2.0` | Seconds `flush()`/`close()`/exit wait for pending events |
| `debug` | `False` | Print SDK diagnostics to stderr |
| `batch_size`, `request_timeout`, `max_attempts`, `backoff_base` | `20`, `10`, `5`, `1.0` | Delivery tuning |
| `performance` | `True` | Time requests and tasks (see "Performance monitoring"); `False` turns it off |
| `performance_ignore` | health checks, `/static/*`, `/assets/*` | Paths that are not timed (replaces the default list) |
| `performance_flush_interval` | `60.0` | Seconds between performance summaries (minimum 5) |
| `performance_spans` | `True` | Spans: automatic DB / outbound HTTP / cache instrumentation and `start_span` (needs `performance`) |
| `performance_max_spans` | `200` | Spans collected per transaction (more are only counted) |
| `trace_propagation_targets` | `()` | Hosts that receive a `traceparent` header on outgoing HTTP calls (see "Distributed tracing"); empty = never |
API: `init`, `capture_exception`, `capture_message`, `set_user`, `set_tag(s)`, `set_extra`, `add_breadcrumb`,
`push_scope`, `configure_scope`, `flush(timeout)`, `close()`, `start_transaction(name, op)`, `start_span(op, description)`, `record_span(...)`.

## Performance monitoring

Since 1.2 the SDK also reports **how long each request or task took**, with no change to your code: every web and job
integration listed above already times what it wraps. Errors and timing are independent (own endpoint, own monthly quota
of "tracked requests"): an error in one never blocks the other.

**What is measured.** One *transaction* per request, from the first byte the integration sees until the response is
built, named `"{METHOD} {route template}"`: `GET /items/{item_id}`, `POST /orders/<int:pk>/`. The template is the same
one that becomes the `route` tag on error events. A request no route matched (404, scanner traffic) is named
`"GET <unmatched>"`, **never** the raw path, so random URLs cannot explode the number of names (the SDK also folds
anything beyond 499 names per window into `<other>`). Background jobs are one transaction per execution named
`"TASK {task name}"` (op `queue.task`): Celery tasks, RQ jobs (function name), Dramatiq actors, Huey tasks.

**What is sent.** Nothing per request. Counts, errors, sum/min/max and a fixed 28-bucket latency histogram are
aggregated in memory per name and one small summary is POSTed to `/api/ingest/metrics/` about every 60 seconds from a
background daemon thread, plus up to 10 individual samples per window (the slowest 3, up to 5 failed, 1 random) with a
trace id. Counts are exact; percentiles are computed by the server from the histograms. Recording takes microseconds,
never blocks and never raises; the thread starts lazily on the first request and restarts itself after `fork()`
(gunicorn `--preload`, Celery prefork). Pending data is flushed by `insight_sdk.flush()`, `close()` and at exit.

**What counts as an error.** A 5xx response, or an exception that nobody handled. 4xx responses (404, 401, 422, ...)
are **not** errors, they are counted and timed. When the framework reports the response status, that status decides
(an exception your own error handler turns into a 200/400 is not an error). For tasks the error is the *final*
failure, using the same retry logic as error reporting: an attempt that will be retried is a timing sample but not an
error. Exceptions a Dramatiq actor lists in `throws=` are not errors.

**Not timed by default.** `/health`, `/healthz`, `/ping`, `/status`, `/metrics`, `/favicon.ico`, `/robots.txt`,
`/ready`, `/live`, `/livez`, `/readyz` (matched on the raw path; a trailing slash is ignored) and everything under
`/static/` and `/assets/`, so they do not use up your quota. Change it with `performance_ignore`; entries are exact
paths, prefixes with a trailing `*`, or compiled regexes (a list **replaces** the defaults, extend
`insight_sdk.performance.DEFAULT_IGNORE` to keep them):

```python
import re
from insight_sdk.performance import DEFAULT_IGNORE

insight_sdk.init(api_key="ak_...", host="https://monitor.jaigateway.com", release="api@1.4.2",
                 performance_ignore=[*DEFAULT_IGNORE, "/internal/*", re.compile(r"^/v\d+/debug")])
```

**Layered middleware** (Django middleware inside your own WSGI middleware, an ASGI middleware around Starlette, ...) still
yield one transaction per request, named by the innermost layer that knows the route. A plain `InsightWSGIMiddleware`/
`InsightASGIMiddleware` without a framework behind it has no route template: it reports `GET <unmatched>`, or pass
`InsightWSGIMiddleware(app, route=lambda environ: "/items/{id}")`.

**Quota and failures.** `429 quota_exceeded` (the plan's tracked requests for the month are used up) pauses
**performance metrics only** until `Retry-After` (at most 1 hour), drops them meanwhile and logs one warning; **error
reporting keeps working**. `401`/`403` disable metrics for the process (one warning), `400` drops the window, `413`
drops the samples and keeps the counts, `429 rate_limited` waits `Retry-After` and retries the window, and a 5xx or
network error keeps the window and resends it with the next flush (at most 5 windows are buffered).

**Turn it off:** `insight_sdk.init(..., performance=False)`: recording is then a single attribute check.

**Custom transactions** (management commands, scripts, cron jobs, anything that is not a request):

```python
with insight_sdk.start_transaction("import_catalog", op="script"):
    import_catalog()

@insight_sdk.start_transaction("nightly_report", op="cron")     # also works on async functions
def nightly_report(): ...
```

An exception that propagates out counts as an error; `t.set_error()` marks one by hand. Use fixed names, never ids.
Short-lived processes should end with `insight_sdk.flush()` so the last window is sent (it also flushes events).

**Limits.**
- Streaming responses: Django, Flask, Quart, Sanic, aiohttp, Pyramid and Falcon time until the response *object* is
  built, so the time spent streaming a generator body is not included. The ASGI integrations (FastAPI, Starlette,
  Litestar, plain ASGI) and Tornado time until the handler/app finished sending; the WSGI middleware (and Bottle, which
  uses it) until the server closes the response iterable.
- Litestar answers an unmatched URL before any middleware runs, so those 404s are counted (as `<unmatched>`) with a
  ~0 ms duration.
- RQ forks a work horse per job; the horse sends its own summary when the job ends (one small POST per job).
  `SimpleWorker` aggregates normally.
- Server side, apdex counts a fast 5xx as "satisfied": look at the error rate next to it.

## Spans and traces

Since 1.3 each request or task also records the **steps inside it** ("where did the time go?"): database queries,
outbound HTTP calls and cache commands, plus your own spans. The dashboard shows the slowest queries and external services,
a "time breakdown" per endpoint, and a trace view that lists the spans and the errors raised in that request.

**Collected automatically** (a library is patched when it is imported, or already is; nothing is imported by the SDK):

| Library | Span |
| --- | --- |
| `urllib.request.urlopen`, `requests`, `urllib3`, `httpx` (sync and async), `aiohttp` client | op `http.client`, description `GET host` |
| Django ORM (every connection), SQLAlchemy (all engines; psycopg2/sqlite3 through them) | op `db.query`, normalised SQL, `db.system` |
| `redis-py` (sync and asyncio) | op `cache`, the command name only (`GET`) |

Celery, RQ, Dramatiq and Huey tasks get spans through their `TASK name` transaction. An HTTP span is an error on an exception
or a status >= 500 (4xx is ok).

**Privacy guarantees.** SQL is normalised in the SDK before it is stored: string and numeric literals become `?`, `IN (...)`
and `VALUES (...)` lists collapse, comments are removed, at most 300 characters, and bind parameters are never read. HTTP spans
are `METHOD host` only: no path, query string or credentials. Cache spans never contain keys or values. Span `data` is limited to
`db.system`, `db.name`, `http.method`, `http.status_code`, `host`, `cache.hit`. The server normalises again defensively.

**Traces.** Each transaction has a `trace_id`; its sample and every error captured while it is running carry the same id, so the
dashboard links an error to its request. Outside a transaction there is no trace id; spans there are only counted under the
transaction name `""`.

**Your own spans:**

```python
with insight_sdk.start_span("db.query", "SELECT * FROM items WHERE id = 7", {"db.system": "postgresql"}):
    rows = run_query()

@insight_sdk.start_span("task", "resize image")        # also async functions; `async with` works too
def resize(): ...
```

A propagating exception marks the span as an error. Only `db*` (SQL) and `http*` descriptions are normalised for you: never put
ids or personal data in other descriptions. For instrumentation of your own client use
`insight_sdk.record_span(op, description, start, duration_ms, ok, data)` (`start` is a `time.perf_counter()` value).

**Limits and options.** At most `performance_max_spans` (default 200) spans are collected per transaction (the rest is counted in
the summaries), 300 summaries per payload (extra ones fold into `<other>`), 100 spans per sample (the slowest, in time order).

**Turn it off:** `init(..., performance_spans=False)` (no patches, no summaries) or pick libraries with
`integrations=["requests", "sqlalchemy"]` (names: `urllib`, `requests`, `urllib3`, `httpx`, `aiohttp_client`, `django_orm`,
`sqlalchemy`, `redis`). Spans need `performance=True`.

**Limitations.** A span needs the request's context: work in a plain `threading.Thread` started by a view, or in a process pool,
is not attached to the request (it is summarised under `""`). HTTP spans end when the response headers arrive. Blocking Redis
commands (`BLPOP`, ...) are not recorded.

## Distributed tracing

JaiGateway Monitor uses the W3C Trace Context header, `traceparent: 00-<trace_id 32 hex>-<parent span id 16 hex>-<flags 2 hex>` (flags `01` = sampled).

**What is read (always on, nothing to configure).** Every web integration (Django, Flask, FastAPI/Starlette, Litestar, Sanic, aiohttp,
Tornado, Quart, Pyramid, Bottle, Falcon, plain WSGI/ASGI) reads the incoming `traceparent` header (header name case-insensitive; WSGI
`HTTP_TRACEPARENT`, ASGI headers). A valid header (version `00`, non-zero 32-hex trace id, non-zero 16-hex parent id, 2-hex flags;
upper-case hex and surrounding whitespace are tolerated) makes the request **continue that trace**: the request's `trace_id` is the
header's, it gets a new 16-hex `span_id`, its `parent_span_id` is the header's span, and every error event captured in the request
carries the adopted `trace_id`. A malformed header is ignored (a new trace starts; nothing ever raises). Samples now carry
`kind: "server"`, `op` (`http.server`, or `queue.task` for jobs), `span_id` and, when the trace was continued, `parent_span_id`.

**Forced samples.** If the incoming flags say *sampled*, the request is sent as a sample whatever its speed or status: up to 20 forced
samples per window, in addition to the usual slowest 3 / failed 5 / 1 random. Under payload pressure (1 MB cap) sample spans go
first, then the regular samples, and the forced samples are the last thing dropped.

**What is sent (opt-in).** Outgoing calls get a `traceparent` header only when the target matches `trace_propagation_targets` and a
transaction is active:

```python
insight_sdk.init(..., trace_propagation_targets=["api.example.com", "*.example.com", "localhost:8000", re.compile(r"^svc-\d+\.internal$")])
```

Strings are matched (case-insensitively, `*` wildcard) against the host (`api.example.com`) and against `host:port`
(`localhost:8000`); a target without a port matches any port. Compiled regexes are searched in both. The header is
`00-<trace_id>-<span_id of the http.client span recorded for that call>-<01 if the trace is sampled else 00>`, so the dashboard joins both
halves. Clients patched: `requests`, `httpx` (sync and async), `aiohttp` client, `urllib3`, `urllib.request`. An existing `traceparent`
header set by the app is never overwritten; JaiGateway Monitor's own endpoints and heartbeat pings never get one. The header needs the HTTP
instrumentation (`performance_spans=True`, the default, and the library listed in `integrations` or auto-detected).

**Browsers and CORS.** When a browser page calls your API from another origin, the browser SDK only adds `traceparent` to hosts in its
`tracePropagationTargets`, and such a request triggers a CORS preflight: the API must answer `Access-Control-Allow-Headers: traceparent`
(and the usual `Access-Control-Allow-Origin`). Same-origin calls need nothing.

**Your own HTTP clients and background work.**

```python
headers = {"traceparent": insight_sdk.get_traceparent()}      # None outside a transaction

with insight_sdk.start_span("http.client", "POST api.example.com") as span:
    client.post(url, headers={"traceparent": span.traceparent})  # ties the header to this span

with insight_sdk.continue_trace(traceparent_from_job_payload, name="rebuild_index", op="task"):
    ...                                                        # a transaction that joins the trace (name optional)
```

`get_traceparent()` uses the innermost `start_span` as the parent, else the request's root span. Jobs: Celery reads `traceparent` from
the message headers (`apply_async(headers={"traceparent": ...})`; best effort, depends on the Celery protocol), RQ from
`job.meta["traceparent"]`, Dramatiq from `message.options["traceparent"]`; Huey tasks have no side channel, so wrap the task body in
`continue_trace(...)`. Jobs are not given outgoing headers automatically at enqueue time.

**Limitations.** A redirect that leaves a target host keeps the header already added (requests/httpx copy request headers). A span whose
id was sent in a header is always collected (up to 50 beyond `performance_max_spans`); only 100 spans go in one sample, propagated ones first.

## Delivery, failures and quota

- Events go through a background worker in batches of up to 20. Network errors and 5xx are retried with exponential
  backoff and jitter (1 s, 2 s, 4 s ... max 60 s), at most 5 attempts, then dropped.
- `429 rate_limited`: retried after `Retry-After`. `429 quota_exceeded` (plan quota used up): the SDK **pauses** until
  `Retry-After` (at most 1 hour), drops events meanwhile and logs **one** warning on the `insight_sdk` logger.
- `401`/`403`: invalid key or no write permission - sending is disabled for the process and one warning is logged.
- `400`: the event is dropped. `413`: the batch is split; a single oversized event is dropped.
- Pending events are flushed at interpreter exit (up to `shutdown_timeout`). The worker restarts itself after `fork()`.
- The same exception object is only reported once even if both a logging handler and a middleware see it.

Warnings go to the standard `logging` system (`logging.getLogger("insight_sdk")`); Python prints WARNING and above to
stderr when logging is unconfigured.

## Privacy

Local variables are never read or sent. `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie` and `X-API-Key`
headers are removed; request bodies are never sent. User data and IP are only attached when you call `set_user`, or
when you enable `send_default_pii`. The server additionally scrubs secrets, but anything you put in `extra`, tags,
breadcrumbs or exception messages is sent as is, so use `before_send` to redact. The hostname is sent as `server_name`
(set `server_name=` to override).

## Development

```bash
pip install -e ".[test]" && pytest        # one test module per framework: tests/test_fw_*.py
```

`tests/test_live.py` runs against a real server and is skipped unless `test-results/sdk-server-3142.json` exists
(see `scripts/sdk-test-server.mjs` in the JaiGateway Monitor repo).

## Cron jobs
```python
@insight_sdk.monitor("https://insight.yourcompany.com/api/ping/<token>/")
def nightly_backup(): ...

with insight_sdk.monitor("<token>"):      # a bare token needs init() so the host is known
    run_job()
insight_sdk.check_in(url, "log", "processed 120 rows")   # 'success' | 'start' | 'fail' | 'log'
```
Start, success, or fail (with the traceback) is reported; the job's own exception is never swallowed, and nothing here raises.
