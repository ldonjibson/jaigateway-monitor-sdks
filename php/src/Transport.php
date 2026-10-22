<?php

namespace Insight;

/**
 * Sends one HTTP POST. Implementations must never throw for HTTP errors; they
 * return status 0 for network failures.
 */
interface Transport
{
    /**
     * @param string   $url
     * @param string[] $headers        Raw header lines, e.g. "X-API-Key: abc".
     * @param string   $body           JSON body.
     * @param int      $connectTimeout Seconds.
     * @param int      $timeout        Seconds (total).
     *
     * @return array{status:int, body?:string, retry_after?:int|null}
     */
    public function send(string $url, array $headers, string $body, int $connectTimeout, int $timeout): array;
}
