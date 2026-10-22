<?php

namespace Insight\Laravel;

use Insight\Insight;
use Insight\Tracing\Sql;
use Throwable;

/**
 * Turns Laravel events into spans. Every handler is wrapped in try/catch and does nothing when
 * performance or spans are off. Privacy: only the SQL string of a query is read (never bindings),
 * and an outbound HTTP call is reduced to "METHOD host" (never path, query or credentials).
 */
final class SpanListeners
{
    private const MAX_PENDING = 100;

    /** @var array<int, array{0: float, 1: string, 2: string}> spl_object_id(request) => [started, method, host] */
    private array $pending = [];

    /** Redis commands that a cache store issues; one of these just before a store event means "same operation". */
    private const CACHE_COMMANDS = [
        'GET', 'MGET', 'SET', 'SETEX', 'PSETEX', 'SETNX', 'MSET', 'DEL', 'UNLINK', 'EXISTS',
        'INCR', 'INCRBY', 'DECR', 'DECRBY', 'INCRBYFLOAT', 'EVAL', 'EVALSHA', 'EXPIRE', 'PEXPIRE',
    ];
    /** Connection handshake commands: not cache work. */
    private const SKIP_COMMANDS = ['AUTH', 'HELLO', 'SELECT', 'CLIENT'];
    /** A store event within this many seconds after a cache-like Redis command is treated as the same operation. */
    private const REDIS_DEDUPE_SECONDS = 0.05;

    private float $lastRedisAt = 0.0;
    private ?string $lastRedisTrace = null;

    public function register($events): void
    {
        $events->listen('Illuminate\Database\Events\QueryExecuted', [$this, 'onQuery']);
        $client = Insight::getClient();
        if ($client !== null && $client->getOption('performance_cache_spans')) {
            $events->listen('Illuminate\Redis\Events\CommandExecuted', [$this, 'onRedisCommand']);
            $events->listen('Illuminate\Cache\Events\CacheHit', [$this, 'onCacheHit']);
            $events->listen('Illuminate\Cache\Events\CacheMissed', [$this, 'onCacheMissed']);
            $events->listen('Illuminate\Cache\Events\KeyWritten', [$this, 'onCacheWritten']);
            $events->listen('Illuminate\Cache\Events\KeyForgotten', [$this, 'onCacheForgotten']);
        }
        $events->listen('Illuminate\Http\Client\Events\RequestSending', [$this, 'onHttpSending']);
        $events->listen('Illuminate\Http\Client\Events\ResponseReceived', [$this, 'onHttpResponse']);
        $events->listen('Illuminate\Http\Client\Events\ConnectionFailed', [$this, 'onHttpFailed']);
    }

    /** QueryExecuted: ->sql, ->time (ms), ->connection. ->bindings is never read. */
    public function onQuery($event): void
    {
        try {
            $client = Insight::getClient();
            if ($client === null || !$client->isSpansEnabled()) {
                return;
            }
            $sql = isset($event->sql) ? (string) $event->sql : '';
            if ($sql === '') {
                return;
            }
            $ms = isset($event->time) ? max(0.0, (float) $event->time) : 0.0;
            $data = [];
            $conn = isset($event->connection) ? $event->connection : null;
            if (is_object($conn)) {
                if (method_exists($conn, 'getDriverName')) {
                    $data['db.system'] = (string) $conn->getDriverName();
                }
                if (method_exists($conn, 'getDatabaseName')) {
                    $name = $conn->getDatabaseName();
                    if (is_string($name) && $name !== '') {
                        $data['db.name'] = $name;
                    }
                }
            }
            $client->recordSpan('db.query', $sql, $ms, true, $data, $client->clockNow() - $ms / 1000);
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    /**
     * Redis CommandExecuted: ->command (string), ->time (ms). ONLY the upper-cased command name is
     * used: ->parameters (keys, values, passwords) are never read.
     */
    public function onRedisCommand($event): void
    {
        try {
            $client = Insight::getClient();
            if ($client === null || !$client->isCacheSpansEnabled() || !isset($event->command) || !is_string($event->command)) {
                return;
            }
            $parts = preg_split('/\s+/', trim($event->command), 2);
            $name = strtoupper(is_array($parts) ? (string) $parts[0] : '');
            if ($name === '' || preg_match('/^[A-Z][A-Z0-9_.]{0,30}$/', $name) !== 1 || in_array($name, self::SKIP_COMMANDS, true)) {
                return;
            }
            $ms = isset($event->time) ? max(0.0, (float) $event->time) : 0.0;
            $now = $client->clockNow();
            if (in_array($name, self::CACHE_COMMANDS, true)) {
                $this->lastRedisAt = $now;
                $this->lastRedisTrace = $client->traceId();
            }
            $client->recordSpan('cache', $name, $ms, true, [], $now - $ms / 1000);
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    public function onCacheHit($event): void
    {
        $this->marker('GET hit', true);
    }

    public function onCacheMissed($event): void
    {
        $this->marker('GET miss', false);
    }

    public function onCacheWritten($event): void
    {
        $this->marker('PUT', null);
    }

    public function onCacheForgotten($event): void
    {
        $this->marker('FORGET', null);
    }

    /**
     * Zero-duration store marker. Skipped when the Redis listener recorded a cache-like command in
     * this request within the last 50 ms (the store is Redis, the timed command span already
     * represents the operation). The event object is never read: keys and values stay out.
     */
    private function marker(string $description, ?bool $hit): void
    {
        try {
            $client = Insight::getClient();
            if ($client === null || !$client->isCacheSpansEnabled()) {
                return;
            }
            if ($this->lastRedisAt > 0.0 && $this->lastRedisTrace === $client->traceId()
                && $client->clockNow() - $this->lastRedisAt <= self::REDIS_DEDUPE_SECONDS) {
                return;
            }
            $client->recordCacheMarker($description, $hit);
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    public function onHttpSending($event): void
    {
        try {
            $client = Insight::getClient();
            if ($client === null || !$client->isSpansEnabled() || !isset($event->request) || !is_object($event->request)) {
                return;
            }
            if (count($this->pending) >= self::MAX_PENDING) {
                $this->pending = [];
            }
            $r = $event->request;
            $this->pending[spl_object_id($r)] = [
                $client->clockNow(),
                strtoupper((string) $r->method()),
                Sql::host((string) $r->url()),
                self::spanIdFromHeader($r, $client),
            ];
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    public function onHttpResponse($event): void
    {
        try {
            $status = null;
            if (isset($event->response) && is_object($event->response) && method_exists($event->response, 'status')) {
                $status = (int) $event->response->status();
            }
            $this->finishHttp($event, $status !== null && $status < 500, $status);
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    public function onHttpFailed($event): void
    {
        try {
            $this->finishHttp($event, false, null);
        } catch (Throwable $e) {
            // never throw into the app
        }
    }

    private function finishHttp($event, bool $ok, ?int $status): void
    {
        $client = Insight::getClient();
        if ($client === null || !isset($event->request) || !is_object($event->request)) {
            return;
        }
        $id = spl_object_id($event->request);
        if (!isset($this->pending[$id])) {
            return;
        }
        [$started, $method, $host, $spanId] = $this->pending[$id];
        unset($this->pending[$id]);
        if (!$client->isSpansEnabled()) {
            return;
        }
        $data = ['http.method' => $method, 'host' => $host];
        if ($status !== null) {
            $data['http.status_code'] = $status;
        }
        $now = $client->clockNow();
        $client->recordSpan('http.client', $method . ' ' . $host, max(0.0, ($now - $started) * 1000), $ok, $data, $started, $spanId);
    }

    /**
     * When the global request middleware (see InsightServiceProvider) added a traceparent for this
     * call, the http.client span must carry the span id from that header. Only the id of the CURRENT
     * trace is accepted (an app-set header of another trace is not ours).
     */
    private static function spanIdFromHeader($request, $client): ?string
    {
        try {
            if (!method_exists($request, 'header')) {
                return null;
            }
            $v = $request->header('traceparent');
            if (is_array($v)) {
                $v = reset($v);
            }
            $ctx = is_string($v) ? \Insight\Client::parseTraceparent($v) : null;
            if ($ctx !== null && $ctx['trace_id'] === $client->traceId()) {
                return $ctx['parent_id'];
            }
        } catch (Throwable $e) {
            // no id: the span gets a random one
        }

        return null;
    }
}
