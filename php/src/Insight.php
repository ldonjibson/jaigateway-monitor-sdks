<?php

namespace Insight;

use Throwable;

/**
 * Static facade over a single Client.
 */
final class Insight
{
    private static ?Client $client = null;
    private static bool $handlersRegistered = false;
    private static $previousExceptionHandler = null;
    private static $previousErrorHandler = null;
    private static ?float $txStart = null;
    private static ?string $txName = null;
    private static bool $txRecorded = false;
    private static bool $requestFailed = false;

    public static function init(array $options = [], ?Transport $transport = null): Client
    {
        self::$client = new Client($options, $transport);

        return self::$client;
    }

    public static function setClient(?Client $client): void
    {
        self::$client = $client;
    }

    public static function getClient(): ?Client
    {
        return self::$client;
    }

    public static function captureException(Throwable $e, array $context = []): bool
    {
        return self::$client !== null ? self::$client->captureException($e, $context) : false;
    }

    public static function captureMessage(string $message, string $level = 'info', array $context = []): bool
    {
        return self::$client !== null ? self::$client->captureMessage($message, $level, $context) : false;
    }

    public static function setUser(?array $user): void
    {
        if (self::$client !== null) {
            self::$client->setUser($user);
        }
    }

    public static function setTag(string $key, $value): void
    {
        if (self::$client !== null) {
            self::$client->setTag($key, $value);
        }
    }

    public static function setTags(array $tags): void
    {
        if (self::$client !== null) {
            self::$client->setTags($tags);
        }
    }

    public static function setExtra(string $key, $value): void
    {
        if (self::$client !== null) {
            self::$client->setExtra($key, $value);
        }
    }

    public static function addBreadcrumb(string $message, string $category = 'custom', string $level = 'info', array $data = []): void
    {
        if (self::$client !== null) {
            self::$client->addBreadcrumb($message, $category, $level, $data);
        }
    }

    /** Report a cron job run: $status is 'success' (default), 'start', 'fail' or 'log'. Never throws. */
    public static function checkIn(string $target, string $status = 'success', ?string $message = null): bool
    {
        return Heartbeat::checkIn($target, $status, $message);
    }

    /** Run $fn as a monitored cron job (start ping, then success, or fail with the error and rethrow). */
    public static function monitor(string $target, callable $fn)
    {
        return Heartbeat::monitor($target, $fn);
    }

    // ------------------------------------------------------- performance

    /**
     * Records one finished unit of work (an HTTP request, a job) into the shared one-minute
     * window. No network call; the window is sent about once a minute by whichever request
     * notices it has elapsed. See README "Performance monitoring".
     */
    public static function recordTransaction(string $name, float $durationMs, ?int $statusCode = null, bool $error = false, string $op = 'http.server'): bool
    {
        if ($op === 'http.server') {
            self::$txRecorded = true; // the shutdown hook must not count the same request again
        }

        return self::$client !== null ? self::$client->recordTransaction($name, $durationMs, $statusCode, $error, $op) : false;
    }

    /** Plain PHP: marks the start of the request (REQUEST_TIME_FLOAT when available). */
    public static function startTransaction(): void
    {
        self::$txStart = isset($_SERVER['REQUEST_TIME_FLOAT']) ? (float) $_SERVER['REQUEST_TIME_FLOAT'] : microtime(true);
        self::$txRecorded = false;
        self::$requestFailed = false;
        if (self::$client !== null) {
            self::$client->beginTrace(self::$txStart);
        }
    }

    /** Names the current request after its route template, e.g. "GET /items/{id}" or just "/items/{id}". */
    public static function setTransactionName(string $name): void
    {
        self::$txName = $name;
    }

    /** Plain PHP: records the request now (instead of at shutdown). $name defaults to the one from setTransactionName(). */
    public static function finishTransaction(?string $name = null, ?int $statusCode = null): bool
    {
        return self::recordRequest($name, $statusCode, false);
    }

    /** Marks the current request as failed (Laravel integration calls this for reported exceptions). */
    public static function markRequestFailed(): void
    {
        self::$requestFailed = true;
    }

    public static function requestFailed(): bool
    {
        return self::$requestFailed;
    }

    /** Clears per-request performance state (long-running workers, tests). */
    public static function resetTransaction(): void
    {
        self::$txStart = null;
        self::$txName = null;
        self::$txRecorded = false;
        self::$requestFailed = false;
        if (self::$client !== null) {
            self::$client->endTrace();
        }
    }

    /** Starts the trace (trace_id, span collection) of the current request; $start is a unix time. Laravel middleware calls this. */
    public static function beginTrace(?float $start = null, string $op = 'http.server', ?string $traceparent = null): bool
    {
        return self::$client !== null ? self::$client->beginTrace($start, $op, $traceparent) : false;
    }

    /**
     * Continues the trace of an incoming W3C traceparent ("00-<trace id>-<parent span id>-<flags>") for work
     * that is not an HTTP request: a queue job, a CLI command. HTTP requests do this automatically.
     * Returns false for an invalid header or when performance monitoring is off. Never throws.
     */
    public static function continueTrace(string $traceparent, string $op = 'queue.task'): bool
    {
        try {
            return self::$client !== null ? self::$client->continueTrace($traceparent, $op) : false;
        } catch (Throwable $e) {
            return false;
        }
    }

    /**
     * The traceparent header value for the current trace ("00-<trace id>-<span id>-<01|00>"), for apps that
     * call other services with a client Insight does not instrument (curl, other SDKs). Null without a trace.
     */
    public static function traceparent(): ?string
    {
        try {
            return self::$client !== null ? self::$client->traceparent() : null;
        } catch (Throwable $e) {
            return null;
        }
    }

    /** The trace id of the request in progress (also sent on error events), or null. */
    public static function traceId(): ?string
    {
        return self::$client !== null ? self::$client->traceId() : null;
    }

    /** Starts a span; call ->finish($ok, $data) on it. Always returns an object, never throws. */
    public static function startSpan(string $op, string $description, array $data = []): Span
    {
        try {
            return new Span(self::$client, $op, $description, null, $data);
        } catch (Throwable $e) {
            return new Span(null, $op, $description);
        }
    }

    /** Times $fn as a span (error status when it throws; the exception is rethrown) and returns its result. */
    public static function span(string $op, string $description, callable $fn)
    {
        $span = self::startSpan($op, $description);
        try {
            $result = $fn();
        } catch (Throwable $e) {
            $span->finish(false);
            throw $e;
        }
        $span->finish(true);

        return $result;
    }

    /** For instrumentations: records a span that already finished. $startedAt is a unix time (default now - duration). */
    public static function recordSpan(string $op, string $description, float $durationMs, bool $ok = true, array $data = [], ?float $startedAt = null, ?string $spanId = null): bool
    {
        return self::$client !== null ? self::$client->recordSpan($op, $description, $durationMs, $ok, $data, $startedAt, $spanId) : false;
    }

    private static function recordRequest(?string $name, ?int $status, bool $failed): bool
    {
        try {
            $client = self::$client;
            if ($client === null || !$client->isPerformanceEnabled() || self::$txRecorded) {
                return false;
            }
            $start = self::$txStart !== null ? self::$txStart : (isset($_SERVER['REQUEST_TIME_FLOAT']) ? (float) $_SERVER['REQUEST_TIME_FLOAT'] : null);
            if ($start === null) {
                return false;
            }
            $method = isset($_SERVER['REQUEST_METHOD']) && is_string($_SERVER['REQUEST_METHOD']) ? strtoupper($_SERVER['REQUEST_METHOD']) : 'GET';
            $uri = isset($_SERVER['REQUEST_URI']) && is_string($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '';
            if ($uri !== '' && $client->shouldIgnore(explode('?', $uri, 2)[0])) {
                return false;
            }
            $name = $name !== null && $name !== '' ? $name : self::$txName;
            if ($name === null || $name === '') {
                $name = $method . ' <unmatched>';
            } elseif ($name[0] === '/') {
                $name = $method . ' ' . $name;
            }
            if ($status === null) {
                $code = http_response_code();
                $status = is_int($code) ? $code : null;
            }
            $ms = (microtime(true) - $start) * 1000;

            return self::recordTransaction($name, $ms, $status, $failed || self::$requestFailed || ($status !== null && $status >= 500));
        } catch (Throwable $t) {
            return false;
        }
    }

    public static function flush(): ?int
    {
        return self::$client !== null ? self::$client->flush() : null;
    }

    /**
     * Installs the exception handler, an error handler (warnings and notices
     * become breadcrumbs) and a shutdown hook for fatal errors. Existing
     * handlers are chained, never replaced.
     */
    public static function registerErrorHandlers(): void
    {
        if (self::$handlersRegistered) {
            return;
        }
        self::$handlersRegistered = true;
        self::$previousExceptionHandler = set_exception_handler([self::class, 'handleException']);
        self::$previousErrorHandler = set_error_handler([self::class, 'handleError']);
        register_shutdown_function([self::class, 'handleShutdown']);
    }

    public static function handleException(Throwable $e): void
    {
        self::$requestFailed = true;
        try {
            if (self::$client !== null) {
                self::$client->captureException($e, ['level' => 'fatal']);
            }
        } catch (Throwable $t) {
            // never throw into the app
        }
        $previous = self::$previousExceptionHandler;
        if ($previous !== null && is_callable($previous)) {
            call_user_func($previous, $e);

            return;
        }
        // No earlier handler: report it the way PHP would (fatal error, exit status 255).
        // Do not rethrow from here: since PHP 8.3 a throw inside the handler re-enters
        // the handler (infinite recursion), even after restore_exception_handler().
        restore_exception_handler();
        trigger_error('Uncaught ' . (string) $e . "\n  thrown in " . $e->getFile() . ' on line ' . $e->getLine(), E_USER_ERROR);
    }

    public static function handleError(int $errno, string $errstr, string $errfile = '', int $errline = 0): bool
    {
        try {
            if (self::$client !== null && (error_reporting() & $errno)) {
                self::$client->addBreadcrumb($errstr . ' in ' . $errfile . ':' . $errline, 'error', self::levelFor($errno));
            }
        } catch (Throwable $t) {
            // never throw into the app
        }
        $previous = self::$previousErrorHandler;
        if ($previous !== null && is_callable($previous)) {
            return call_user_func($previous, $errno, $errstr, $errfile, $errline) !== false;
        }

        return false; // fall through to PHP's standard handling
    }

    public static function handleShutdown(): void
    {
        try {
            $client = self::$client;
            if ($client === null) {
                return;
            }
            $error = error_get_last();
            $fatal = [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR, E_USER_ERROR, E_RECOVERABLE_ERROR];
            if ($error !== null && in_array($error['type'], $fatal, true)) {
                self::$requestFailed = true;
            }
            if (PHP_SAPI !== 'cli' && PHP_SAPI !== 'phpdbg') {
                self::recordRequest(null, null, self::$requestFailed);
            }
            // "Uncaught ..." fatals were already captured by handleException.
            if ($error !== null && in_array($error['type'], $fatal, true) && strpos($error['message'], 'Uncaught ') !== 0) {
                $exception = new \ErrorException($error['message'], 0, $error['type'], $error['file'], $error['line']);
                $client->captureException($exception, [
                    'level' => 'fatal',
                    'frames' => [Client::makeFrame($error['file'], (int) $error['line'], '{main}')],
                ]);
            }
            $client->finishAndFlush();
        } catch (Throwable $t) {
            // never throw into the app
        }
    }

    private static function levelFor(int $errno): string
    {
        if (in_array($errno, [E_NOTICE, E_USER_NOTICE], true)) {
            return 'info';
        }
        if (in_array($errno, [E_DEPRECATED, E_USER_DEPRECATED], true)) {
            return 'info';
        }

        return 'warning';
    }
}
