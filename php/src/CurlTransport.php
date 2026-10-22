<?php

namespace Insight;

use Throwable;

class CurlTransport implements Transport
{
    public function send(string $url, array $headers, string $body, int $connectTimeout, int $timeout): array
    {
        try {
            if (function_exists('curl_init')) {
                return $this->viaCurl($url, $headers, $body, $connectTimeout, $timeout);
            }

            return $this->viaStream($url, $headers, $body, $timeout);
        } catch (Throwable $e) {
            return ['status' => 0, 'body' => '', 'retry_after' => null];
        }
    }

    private function viaCurl(string $url, array $headers, string $body, int $connectTimeout, int $timeout): array
    {
        $retryAfter = null;
        $ch = curl_init($url);
        if ($ch === false) {
            return ['status' => 0, 'body' => '', 'retry_after' => null];
        }

        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => $connectTimeout,
            CURLOPT_TIMEOUT => $timeout,
            CURLOPT_NOSIGNAL => true,
            CURLOPT_HEADERFUNCTION => function ($handle, $line) use (&$retryAfter) {
                if (stripos($line, 'retry-after:') === 0) {
                    $retryAfter = CurlTransport::parseRetryAfter(substr($line, 12));
                }

                return strlen($line);
            },
        ]);

        $response = curl_exec($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        $ch = null; // frees the handle (curl_close is deprecated on PHP 8.5)

        return [
            'status' => $response === false ? 0 : $status,
            'body' => is_string($response) ? $response : '',
            'retry_after' => $retryAfter,
        ];
    }

    private function viaStream(string $url, array $headers, string $body, int $timeout): array
    {
        $context = stream_context_create([
            'http' => [
                'method' => 'POST',
                'header' => implode("\r\n", $headers),
                'content' => $body,
                'timeout' => (float) $timeout,
                'ignore_errors' => true,
            ],
        ]);
        $response = @file_get_contents($url, false, $context);
        $status = 0;
        $retryAfter = null;
        if (isset($http_response_header) && is_array($http_response_header)) {
            foreach ($http_response_header as $line) {
                if (preg_match('#^HTTP/\S+\s+(\d{3})#', $line, $m)) {
                    $status = (int) $m[1];
                } elseif (stripos($line, 'retry-after:') === 0) {
                    $retryAfter = self::parseRetryAfter(substr($line, 12));
                }
            }
        }

        return [
            'status' => $status,
            'body' => is_string($response) ? $response : '',
            'retry_after' => $retryAfter,
        ];
    }

    public static function parseRetryAfter(string $value): ?int
    {
        $value = trim($value);
        if ($value === '') {
            return null;
        }
        if (ctype_digit($value)) {
            return (int) $value;
        }
        // Only HTTP-dates (they contain a month/day name); strtotime("-3") would be read as a timezone offset.
        $time = preg_match('/[A-Za-z]/', $value) === 1 ? strtotime($value) : false;
        if ($time !== false) {
            return max(0, $time - time());
        }

        return null;
    }
}
