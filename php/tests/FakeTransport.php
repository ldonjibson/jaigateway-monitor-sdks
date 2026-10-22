<?php

namespace Insight\Tests;

use Insight\Transport;

class FakeTransport implements Transport
{
    /** @var array<int, array> */
    public array $calls = [];

    private array $responses;

    /** @param array<int, array> $responses Each: ['status' => int, 'body' => string, 'retry_after' => int|null]. Default 201. */
    public function __construct(array $responses = [])
    {
        $this->responses = $responses;
    }

    public function send(string $url, array $headers, string $body, int $connectTimeout, int $timeout): array
    {
        $this->calls[] = [
            'url' => $url,
            'headers' => $headers,
            'body' => $body,
            'events' => json_decode($body, true),
            'connect_timeout' => $connectTimeout,
            'timeout' => $timeout,
        ];
        $response = array_shift($this->responses);
        if ($response === null) {
            $response = [];
        }

        return $response + ['status' => 201, 'body' => '', 'retry_after' => null];
    }
}
