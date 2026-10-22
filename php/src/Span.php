<?php

namespace Insight;

/**
 * One timed step. Create with Insight::startSpan(); call finish() when the work is done.
 * Calling finish() twice is harmless, and nothing here ever throws.
 */
final class Span
{
    private ?Client $client;
    private string $op;
    private string $description;
    private float $started;
    private array $data;
    private bool $done = false;

    public function __construct(?Client $client, string $op, string $description, ?float $started = null, array $data = [])
    {
        $this->client = $client;
        $this->data = $data;
        $this->op = $op;
        $this->description = $description;
        $this->started = $started !== null ? $started : ($client !== null ? $client->clockNow() : microtime(true));
    }

    public function finish(bool $ok = true, array $data = []): void
    {
        if ($this->done) {
            return;
        }
        $this->done = true;
        try {
            if ($this->client !== null) {
                $now = $this->client->clockNow();
                $this->client->recordSpan($this->op, $this->description, max(0.0, ($now - $this->started) * 1000), $ok, $data + $this->data, $this->started);
            }
        } catch (\Throwable $e) {
            // never throw into the app
        }
    }
}
