# insight/sdk (PHP and Laravel)

Error reporting for PHP 8.0+ and Laravel 10, 11 and 12. No third-party runtime dependencies (uses the curl extension, with a streams fallback). Speaks the JaiGateway Monitor ingest protocol v1 (`POST {host}/api/ingest/events/`).

## Install

```bash
composer require insight/sdk
```

## Plain PHP in 5 lines

```php
require 'vendor/autoload.php';

\Insight\Insight::init(['api_key' => 'YOUR_WRITE_KEY', 'host' => 'https://monitor.jaigateway.com', 'release' => 'shop@1.4.2']);
\Insight\Insight::registerErrorHandlers();   // uncaught exceptions, fatals, warnings as breadcrumbs

try { risky(); } catch (Throwable $e) { \Insight\Insight::captureException($e); }
```

Events are queued and sent once, in one POST, at the end of the request (after `fastcgi_finish_request()` when available, so the user's response is never delayed).

## Laravel

The package is auto-discovered. Add to `.env`:

```
INSIGHT_API_KEY=your-write-key
INSIGHT_HOST=https://monitor.jaigateway.com
INSIGHT_RELEASE=shop@1.4.2
```

Then hook the exception handler.

**Laravel 11 / 12**, `bootstrap/app.php`:

```php
->withExceptions(function (Illuminate\Foundation\Configuration\Exceptions $exceptions) {
    \Insight\Laravel\Integration::handles($exceptions);
})
```

**Laravel 10**, `app/Exceptions/Handler.php`:

```php
public function register(): void
{
    $this->reportable(function (Throwable $e) {
        \Insight\Laravel\Integration::report($e);
    });
}
```

Verify the setup: `php artisan insight:test` sends a test exception and prints the HTTP status. Publish the config with `php artisan vendor:publish --tag=insight-config`.

Laravel's `$dontReport` / `dontReport()` is respected. Log messages and SQL queries (truncated, no bindings) become breadcrumbs. The authenticated user is attached only when `INSIGHT_SEND_PII=true`.

Anywhere in your code (no facade needed): `\Insight\Insight::captureException($e)`, `\Insight\Insight::captureMessage('x', 'warning')`, `\Insight\Insight::setTag('feature', 'checkout')`.

## API

| Call | Notes |
|---|---|
| `Insight::init(array $options): Client` | Creates the global client. |
| `Insight::captureException(Throwable, array $context = [])` | Context: `tags`, `extra`, `user`, `level`, `fingerprint`, `request`. Returns whether it was queued. |
| `Insight::captureMessage(string, string $level = 'info')` | |
| `Insight::setUser(?array)`, `setTag`, `setTags`, `setExtra` | Scope data attached to later events. |
| `Insight::addBreadcrumb(string $message, string $category = 'custom', string $level = 'info', array $data = [])` | |
| `Insight::flush(): ?int` | Sends now, returns the HTTP status or null. |
| `Insight::registerErrorHandlers()` | Chains to existing handlers; records the request transaction at shutdown when `performance` is on. |
| `Insight::recordTransaction(string $name, float $durationMs, ?int $status = null, bool $error = false, string $op = 'http.server')` | See Performance monitoring. |
| `Insight::startTransaction()`, `setTransactionName(string)`, `finishTransaction(?string $name = null, ?int $status = null)` | Plain PHP request timing. |

The same methods exist on `Insight\Client` (`new Client($options, ?Transport $transport)`).

## Options

| Option | Default | Laravel env |
|---|---|---|
| `api_key` | none (SDK disabled without it) | `INSIGHT_API_KEY` |
| `host` (or `endpoint`) | none. `/api/ingest/events/` is appended; trailing slashes and a host already ending in `/api` are fine | `INSIGHT_HOST` |
| `release`, `environment`, `server_name` | `null`, `null`, hostname | `INSIGHT_RELEASE`, `INSIGHT_ENVIRONMENT` |
| `sample_rate` | `1.0` | `INSIGHT_SAMPLE_RATE` |
| `before_send` | `null`. `callable(array $event): array\|null`, return null to drop | |
| `send_default_pii` | `false` | `INSIGHT_SEND_PII` |
| `max_breadcrumbs` | `100` | |
| `connect_timeout` / `timeout` | `2` / `3` seconds | |
| `debug` | `false`. Logs problems with `error_log` | `INSIGHT_DEBUG` |
| `rate_limit_per_minute` | `60` per key, shared via APCu or a temp file | |
| `max_events_per_request` | `20` (also the batch size) | |
| `performance` | `false` (plain PHP), `true` under Laravel | `INSIGHT_PERFORMANCE` |
| `performance_ignore` | health checks, static paths (see below) | `INSIGHT_PERFORMANCE_IGNORE` |
| `performance_window_seconds` | `60` | |
| `performance_spans` | `true` | Collect spans (queries, HTTP calls, custom). Laravel env `INSIGHT_PERFORMANCE_SPANS`. |
| `performance_cache_spans` | `true` | Cache spans (Redis commands, hit/miss markers). Needs `performance_spans`. Laravel env `INSIGHT_PERFORMANCE_CACHE_SPANS`. |
| `trace_propagation_targets` | `[]` (never add `traceparent` to outgoing calls) | `INSIGHT_TRACE_PROPAGATION_TARGETS` (comma list). See "Distributed tracing". |

## How failures and quota are handled

The SDK never throws into your app and never retries more than once (PHP must not block).

| Response | Behaviour |
|---|---|
| 201 | done |
| 400 | dropped |
| 401, 403 | invalid key or no write permission: SDK disabled for the rest of the process, logged once when `debug` is on |
| 413 | batch is split in halves and resent; a single oversized event is dropped |
| 429 `rate_limited` / `quota_exceeded` | events dropped; sending is paused until `Retry-After` (default 60 s, capped at 1 h). The pause is stored in APCu or a small file in the temp dir keyed by a hash of the API key, so other requests skip sending too |
| 5xx, network error | one quick retry, then dropped |

Use `php artisan insight:test` or `Insight::flush()` (returns the status) to see the outcome. Set `debug` to get `error_log` lines.

## Performance monitoring

Reports how long each route takes: request count, errors, min/avg/max and p50/p95/p99 per transaction, plus a few slow or failed samples. It has its own monthly quota ("tracked requests"), separate from error events.

**What is measured.** One transaction per HTTP request, named `METHOD route-template` (`GET /items/{id}`, `GET /` for the root), never the raw URL. A request that matched no route is `GET <unmatched>`. Queue jobs are `TASK App\Jobs\SendMail` (op `queue.task`). Duration is from `LARAVEL_START` (when set; not under Octane) to the moment the response is ready.

**Errors vs 4xx.** A transaction counts as an error when the response status is 5xx, or an exception was reported (Laravel: through `Integration`) or uncaught (plain PHP). 4xx responses (404, 422, 401 ...) are NOT errors. Queue: every attempt is one transaction; an attempt that threw is an error even when the job will be retried (attempt-level error rate); `JobFailed` only records when that attempt did not already record the exception.

**Turn it on / off.**
- Laravel: on by default. `INSIGHT_PERFORMANCE=false` (or `'performance' => false` in `config/insight.php`) turns it off and the middleware is not installed. `PerformanceMiddleware` is pushed on the global stack automatically (`Kernel::pushMiddleware`, Laravel 10/11/12), no change to `bootstrap/app.php` or `Kernel.php`.
- Plain PHP: off unless `Insight::init(['performance' => true, ...])`. With `Insight::registerErrorHandlers()` the request is recorded at shutdown (status from `http_response_code()`); or call `Insight::startTransaction()` early and `Insight::finishTransaction('GET /items/{id}', 200)` yourself. Name the route with `Insight::setTransactionName('/items/{id}')` (the method is added); without a name it is `GET <unmatched>`.
- Anything else: `Insight::recordTransaction('GET /x', $durationMs, 200, false)`.

**Ignored paths.** Default: `/health`, `/healthz`, `/ping`, `/status`, `/metrics`, `/favicon.ico`, `/robots.txt`, `/ready`, `/live`, `/up`, `/storage/`, `/build/`, `/assets/` (a prefix matches on a path boundary: `/up` ignores `/up/x` but not `/users`). Override with `performance_ignore` (array or comma list, `INSIGHT_PERFORMANCE_IGNORE=/internal/,#^/v\d+/ping$#`); entries starting with `#` or `~` are regular expressions. A custom list replaces the defaults.

**How the once-a-minute window works in PHP-FPM.** Every request is a fresh process with no background thread, so nothing can be aggregated in memory. Each request instead adds its duration to a small shared record (APCu when enabled, else a JSON file `insight_<hash>_m.json` in `sys_get_temp_dir()` guarded by `flock`, else process memory for CLI/Octane/queue workers) bucketed by minute. The first request that sees the stored minute has elapsed closes it, hands it to itself atomically (under the lock, so exactly one request per window) and POSTs it once after the response (`fastcgi_finish_request()`). That is about one HTTP call per minute per app regardless of traffic, and the cost per request is one lock, one small read and one write (no network). Consequences: the last minute of traffic is sent when the next request arrives after it; a low-traffic app reports with that delay; state is shared per API key and per server (not across machines: each server sends its own summary).

**Failures and quota.** Never throws, never blocks the response.

| Response | Behaviour |
|---|---|
| 201 | done |
| 400 | window dropped |
| 401, 403 | metrics switched off (stored in the shared state, other workers skip too), logged once with `debug` on; error reporting is unaffected |
| 413 | resent without samples (counts kept) |
| 429 `rate_limited` | window kept, retried by a later request after `Retry-After` |
| 429 `quota_exceeded` (`quota: "performance"`) | metrics paused until `Retry-After` (capped at 1 h) for all workers, data dropped meanwhile. **Error reporting continues normally** |
| 5xx, network error | window kept, retried by a later request after 30 s; at most 5 windows are buffered (oldest dropped) |

Option `performance_window_seconds` (default 60) changes the window length. Without APCu or a writable temp dir (state mode `none`) windows live in process memory only.

## Spans and traces (where the time goes)

With `performance` on, the SDK also records **spans**: timed steps inside a request or job. JaiGateway Monitor shows the slowest queries and external services, a "where the time goes" breakdown per endpoint, and a trace view that links a failed request to its errors.

What is collected: database queries (op `db.query`), outbound HTTP calls (`http.client`), your own spans, and per request a random `trace_id`. Error events captured during the request carry the same `trace_id`.

Privacy guarantees:
- SQL is normalised **in the SDK** before it leaves your process: string and numeric literals become `?`, `IN (...)` and `VALUES (...)` lists collapse, comments are removed, whitespace collapsed, 300 characters at most. Only the SQL text is read, never bound values (Laravel `$event->bindings` is not touched).
- HTTP calls are described as `METHOD host` only: no path, query string or credentials.
- Span data is limited to `db.system`, `db.name`, `http.method`, `http.status_code`, `host`, `cache.hit`. Descriptions of your own spans are sent as you write them (whitespace collapsed, 300 chars), so do not put secrets in them.
- Collection is bounded: 200 spans per request, 300 summaries per payload, 100 spans per sample, payloads under 1 MB.

Laravel: queries, HTTP client calls (`Http::` facade) and queue jobs are instrumented automatically. Config `performance_spans` / env `INSIGHT_PERFORMANCE_SPANS=false` turns it off (it only runs when `performance` is on).

### Cache spans

Laravel (on by default with spans; `performance_cache_spans` / `INSIGHT_PERFORMANCE_CACHE_SPANS=false` turns them off):

- **Redis commands** (op `cache`): one timed span per command, described by the upper-case command name only (`GET`, `SET`, `HGETALL`). Keys, values and other parameters are never read. Laravel dispatches `Illuminate\Redis\Events\CommandExecuted` only when Redis events are enabled, so the service provider calls `Redis::enableEvents()` when cache spans are on (a no-op without Redis). Without the provider (or with `performance_cache_spans` off) enable it yourself with `Redis::enableEvents()` or the `redis` events setting. Note that every Redis command is recorded, also queue or session traffic.
- **Cache store markers** (`CacheHit`, `CacheMissed`, `KeyWritten`, `KeyForgotten`): Laravel gives no duration for these, so they are zero-duration spans described `GET hit`, `GET miss`, `PUT`, `FORGET` (never the key), with `cache.hit` true/false on gets. They give hit and miss counts per endpoint. Every marker is counted in the summaries, but only the first 20 per description and request enter the span list of a sample, so they cannot use up the 200-span budget.
- **No double counting**: when the cache store is Redis both events fire. A store marker is skipped if a cache-like Redis command (`GET`, `SET`, `SETEX`, `DEL`, ...) was recorded in the same request within the previous 50 ms, so the timed Redis span stands for the operation. Limitation: a non-Redis store used within 50 ms after an unrelated Redis command loses that marker, and hit/miss data (`cache.hit`) is then only available for non-Redis stores.

Custom spans:

```php
$span = Insight::startSpan('payment.capture', 'stripe capture');
try { /* work */ $span->finish(true); } catch (Throwable $e) { $span->finish(false); throw $e; }

$rows = Insight::span('report.build', 'monthly report', fn () => build());   // rethrows, marks errors
Insight::recordSpan('cache', 'get', 0.4, true, ['cache.hit' => true]);       // for instrumentations
```

Spans outside a request (CLI scripts) are aggregated under an empty transaction name and appear in summaries only.

Plain PHP / other frameworks:

```php
$pdo = new \Insight\Tracing\TracedPdo($dsn, $user, $pass, $options);   // query(), exec(), prepare()->execute() become spans

$stack = \GuzzleHttp\HandlerStack::create();
$stack->push(\Insight\Tracing\GuzzleMiddleware::create(), 'insight');   // Guzzle is not required by this package
```

Call `Insight::startTransaction()` at the start of the request so spans get offsets from the request start and errors get a `trace_id`.

## Distributed tracing

JaiGateway Monitor uses the W3C `traceparent` header (`00-<32 hex trace id>-<16 hex parent span id>-<01|00>`), so a browser page, this PHP app and the services it calls can show up as one waterfall in the dashboard. Needs `performance` on (the default in Laravel).

**Incoming (automatic).** Every request reads `traceparent` (plain PHP: `$_SERVER['HTTP_TRACEPARENT']`; Laravel: the request header, also under Octane). A valid header (version `00`, ids not all zeros, any letter case) makes the request join that trace: its `trace_id` is the header's, it gets its own new 16-hex `span_id` and remembers `parent_span_id`. Error events captured during the request carry the same `trace_id`. A missing or malformed header just starts a new trace. Samples are sent with `kind: "server"`, `op: "http.server"` (or `"queue.task"`), `span_id` and `parent_span_id`.

If the header's flags say **sampled** (`01`), the request is always kept as a sample, however fast and successful, so both halves of the trace exist. Up to 20 such forced samples are kept per one-minute window, in addition to the usual slowest 3, failed 5 and 1 random one. (The server reads 10 samples per request body, so the SDK sends forced samples beyond that in extra, samples-only bodies.) When a payload is too big or the server answers `413`, ordinary samples go first, then span lists, and forced samples are the last to be dropped.

For work that is not an HTTP request (a queue job whose payload carries the parent's header, a CLI command):

```php
\Insight\Insight::continueTrace($payload['traceparent']);   // false for an invalid header, never throws
```

**Outgoing (opt-in).** Nothing is added unless you list the hosts that may receive the header:

```php
Insight::init([..., 'trace_propagation_targets' => ['api.example.com', '*.internal.example.com', 'localhost:8000', '#^pay[0-9]+\.example\.net$#']]);
```

Laravel: `config/insight.php` `trace_propagation_targets`, or `INSIGHT_TRACE_PROPAGATION_TARGETS=api.example.com,*.example.com` (plain host entries only in the env list; commas cannot appear in a regex). Entries are matched against `host[:port]`, case-insensitively: a host without port matches any port, `*` matches one or more characters of a host name, `#...#` is a regular expression. A matching call, made while a trace is active, gets `traceparent: 00-<trace id>-<span id of the http.client span recorded for that call>-<01|00>` (the flag repeats the request's sampled bit), so the dashboard can join the two halves. An existing `traceparent` set by your code is never overwritten. The header is never added to JaiGateway Monitor's own host.

- Guzzle: `\Insight\Tracing\GuzzleMiddleware::create()` does it (see "Spans and traces").
- Laravel HTTP client (`Http::get()` ...): the service provider registers a global request middleware with `Http::globalRequestMiddleware()`, which exists from Laravel 10.14 on (all of 11 and 12). On an older Laravel 10 it silently does nothing: push `GuzzleMiddleware::create()` yourself with `Http::withMiddleware(...)`.
- Anything else (curl, vendor SDKs): `\Insight\Insight::traceparent()` returns the current header value (`00-<trace id>-<this request's span id>-<flags>`, or `null` without a trace); send it yourself.

Browsers sending cross-origin requests to your PHP app need your CORS config to allow the `traceparent` request header.

## Privacy

With `send_default_pii` off (default) no user, e-mail or IP is attached automatically. Request headers are sent without `cookie`, `authorization`, `x-api-key` (and CSRF tokens). The server additionally scrubs secrets. Use `before_send` to remove anything else. Note that PHP stack traces in the `metadata.traceback` string may contain argument values if `zend.exception_ignore_args` is off; keep it `On` in production (the default in php.ini-production).

## Queue workers and Octane

Request-end hooks do not run in long-lived processes. The Laravel provider flushes and clears the scope on queue `Looping`, `JobProcessed`, `JobFailed`, `JobExceptionOccurred`, `WorkerStopping` and Octane `RequestTerminated`, `TaskTerminated`, `TickTerminated`. For your own daemons call `Insight::flush()` after each unit of work. Events beyond 20 per flush cycle are dropped.

## Other frameworks (Slim, Symfony, ...)

Call `Insight::init()` and `registerErrorHandlers()` in your front controller. In a PSR-15-free middleware:

```php
try { return $next($request); }
catch (Throwable $e) { \Insight\Insight::captureException($e, ['request' => [
    'url' => (string) $request->getUri(), 'method' => $request->getMethod(),
]]); throw $e; }
```

## Tests

```bash
composer install && vendor/bin/phpunit
```

Tests use a fake `Insight\Transport`; no network is needed. Implement `Transport` to use Guzzle or another client.

## Cron jobs
```php
\Insight\Insight::monitor('https://insight.yourcompany.com/api/ping/<token>/', function () { runJob(); });
\Insight\Insight::checkIn($url, 'log', 'processed 120 rows');   // 'success' | 'start' | 'fail' | 'log'
```
In Laravel's scheduler you can also use the built-in `->pingBefore($url.'start/')->thenPing($url)->pingOnFailure($url.'fail/')`. Nothing here throws.
