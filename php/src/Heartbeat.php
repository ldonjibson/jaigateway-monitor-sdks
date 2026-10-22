<?php

namespace Insight;

use Throwable;

/**
 * Cron job (heartbeat) monitoring helpers. Nothing here throws: a monitoring hiccup must not break the job.
 *
 *   \Insight\Insight::monitor('https://insight.example.com/api/ping/<token>/', function () { runJob(); });
 *   \Insight\Insight::checkIn($url, 'start');   // 'success' (default), 'start', 'fail', 'log'
 */
class Heartbeat
{
    /** @var callable|null replaces the HTTP call, for tests: fn(string $method, string $url, ?string $body): int */
    public static $sender = null;

    /** Token or full ping URL -> full ping URL (a bare token needs Insight::init() so the host is known). */
    public static function base(string $target): ?string
    {
        $t = trim($target);
        if ($t === '') {
            return null;
        }
        if (preg_match('#^https?://#i', $t) === 1) {
            $t = preg_replace('#/(start|fail|log|\d{1,3})?/*$#i', '', $t);

            return rtrim((string) $t, '/') . '/';
        }
        $client = Insight::getClient();
        $url = $client !== null ? $client->getUrl() : '';
        if ($url === '') {
            return null;
        }
        $root = preg_replace('#/ingest/events/?$#', '', $url);

        return rtrim((string) $root, '/') . '/ping/' . rawurlencode($t) . '/';
    }

    public static function checkIn(string $target, string $status = 'success', ?string $message = null): bool
    {
        try {
            $base = self::base($target);
            if ($base === null) {
                return false;
            }
            $suffix = in_array($status, ['start', 'fail', 'log'], true) ? $status . '/' : ''; // the slash matters: without it the server redirects
            $body = $message !== null && $message !== '' ? substr($message, 0, 10000) : null;
            $method = $body !== null ? 'POST' : 'GET';
            $code = self::$sender !== null ? (int) call_user_func(self::$sender, $method, $base . $suffix, $body) : self::send($method, $base . $suffix, $body);

            return $code >= 200 && $code < 300;
        } catch (Throwable $e) {
            return false;
        }
    }

    /** Runs $fn as a monitored job: start ping, then success; if it throws, a fail ping with the error, then rethrow. */
    public static function monitor(string $target, callable $fn)
    {
        self::checkIn($target, 'start');
        try {
            $result = $fn();
        } catch (Throwable $e) {
            self::checkIn($target, 'fail', get_class($e) . ': ' . $e->getMessage() . "\n" . $e->getTraceAsString());
            throw $e;
        }
        self::checkIn($target, 'success');

        return $result;
    }

    private static function send(string $method, string $url, ?string $body): int
    {
        if (function_exists('curl_init')) {
            $ch = curl_init($url);
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_TIMEOUT => 5,
                CURLOPT_CONNECTTIMEOUT => 3,
                CURLOPT_USERAGENT => 'insight-php-heartbeat',
                CURLOPT_CUSTOMREQUEST => $method,
            ]);
            if ($body !== null) {
                curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
            }
            curl_exec($ch);
            $code = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
            $ch = null;

            return $code;
        }
        $ctx = stream_context_create(['http' => ['method' => $method, 'timeout' => 5, 'ignore_errors' => true, 'content' => $body ?? '', 'header' => "User-Agent: insight-php-heartbeat\r\n"]]);
        @file_get_contents($url, false, $ctx);
        $headers = isset($http_response_header) ? $http_response_header : [];

        return isset($headers[0]) && preg_match('#\s(\d{3})\s#', $headers[0], $m) === 1 ? (int) $m[1] : 0;
    }
}
