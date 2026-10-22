<?php

namespace Insight\Laravel;

use Closure;
use Insight\Insight;
use Throwable;

/**
 * Terminable global middleware: records one performance transaction per HTTP request
 * ("GET /items/{id}"), measured from LARAVEL_START (or middleware entry) until the response is
 * ready. terminate() runs after the response was sent, so nothing here delays the user.
 * Installed automatically by InsightServiceProvider when `insight.performance` is on.
 *
 * Laravel creates a NEW middleware instance for terminate(), so state travels on the request.
 */
class PerformanceMiddleware
{
    private const START = '_insight_start';
    private const END = '_insight_end';

    public function handle($request, Closure $next)
    {
        try {
            $start = self::startTime();
            $request->attributes->set(self::START, $start);
            Insight::resetTransaction();
            Insight::beginTrace($start, 'http.server', self::incomingTraceparent($request)); // '' = no header (never fall back to $_SERVER: Octane)
        } catch (Throwable $e) {
            // never throw into the app
        }
        try {
            $response = $next($request);
        } catch (Throwable $e) {
            $this->stamp($request);
            throw $e;
        }
        $this->stamp($request);

        return $response;
    }

    public function terminate($request, $response): void
    {
        try {
            $client = Insight::getClient();
            if ($client === null || !$client->isPerformanceEnabled()) {
                return;
            }
            if ($client->shouldIgnore((string) $request->getPathInfo())) {
                return;
            }
            $start = $request->attributes->get(self::START);
            $end = $request->attributes->get(self::END);
            if (!is_float($start)) {
                return;
            }
            $end = is_float($end) ? $end : microtime(true);
            $status = (is_object($response) && method_exists($response, 'getStatusCode')) ? (int) $response->getStatusCode() : null;
            $error = ($status !== null && $status >= 500) || Insight::requestFailed();
            Insight::recordTransaction(self::transactionName($request), max(0.0, ($end - $start) * 1000), $status, $error);
            $client->flushMetrics();
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    /** The request's traceparent header, or '' (also for anything that is not a Symfony/Laravel request). */
    public static function incomingTraceparent($request): string
    {
        try {
            $h = (is_object($request) && isset($request->headers) && is_object($request->headers) && method_exists($request->headers, 'get'))
                ? $request->headers->get('traceparent') : null;

            return is_string($h) ? $h : '';
        } catch (Throwable $e) {
            return '';
        }
    }

    /** "GET /items/{id}" from the matched route's URI template; "GET <unmatched>" when no route matched (never the raw path). */
    public static function transactionName($request): string
    {
        $method = strtoupper((string) $request->getMethod());
        $route = method_exists($request, 'route') ? $request->route() : null;
        if (is_object($route) && method_exists($route, 'uri') && empty($route->isFallback)) {
            return $method . ' ' . self::normaliseUri((string) $route->uri());
        }

        return $method . ' <unmatched>';
    }

    public static function normaliseUri(string $uri): string
    {
        return '/' . ltrim($uri, '/');
    }

    private function stamp($request): void
    {
        try {
            $request->attributes->set(self::END, microtime(true));
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    /** LARAVEL_START includes framework boot; ignored under Octane where it is a stale process constant. */
    private static function startTime(): float
    {
        $now = microtime(true);
        if (defined('LARAVEL_START') && !isset($_SERVER['LARAVEL_OCTANE']) && !isset($_ENV['LARAVEL_OCTANE'])) {
            $start = (float) constant('LARAVEL_START');
            if ($start > 0 && $now - $start >= 0 && $now - $start < 30) {
                return $start;
            }
        }

        return $now;
    }
}
