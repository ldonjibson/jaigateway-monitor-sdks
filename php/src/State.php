<?php

namespace Insight;

/**
 * Tiny cross-request state: "paused until" timestamp and a per-minute event
 * counter, keyed by a hash of the api key. Backends: APCu, a small file in the
 * temp dir, or plain memory. Modes: auto, memory (process local but treated as
 * persistent, used by tests), file (force the temp-dir file backend), none (memory, not persistent).
 *
 * mutate() is the atomic read-modify-write used by the performance metrics: the whole
 * one-minute window is shared between PHP-FPM workers through it.
 */
final class State
{
    private string $key;
    private bool $apcu = false;
    private ?string $file = null;
    private bool $persistent = false;
    private array $mem = [];
    private ?string $metricsFile = null;
    private array $metricsMem = [];

    public function __construct(string $apiKey, string $mode = 'auto', ?string $dir = null)
    {
        $this->key = 'insight_' . substr(sha1($apiKey), 0, 16);
        if ($mode === 'memory') {
            $this->persistent = true;

            return;
        }
        if ($mode !== 'auto' && $mode !== 'file') {
            return;
        }
        if ($mode === 'auto' && function_exists('apcu_fetch') && function_exists('apcu_enabled') && apcu_enabled()) {
            $this->apcu = true;
            $this->persistent = true;

            return;
        }
        $base = $dir !== null ? $dir : sys_get_temp_dir();
        if (is_string($base) && $base !== '' && is_dir($base) && is_writable($base)) {
            $this->file = rtrim($base, '/\\') . DIRECTORY_SEPARATOR . $this->key . '.json';
            $this->metricsFile = rtrim($base, '/\\') . DIRECTORY_SEPARATOR . $this->key . '_m.json';
            $this->persistent = true;
        }
    }

    public function isPersistent(): bool
    {
        return $this->persistent;
    }

    public function pausedUntil(): int
    {
        $data = $this->read();

        return isset($data['paused']) ? (int) $data['paused'] : 0;
    }

    public function pause(int $until): void
    {
        $data = $this->read();
        $data['paused'] = $until;
        $this->write($data);
    }

    /** Counts one event in the current minute; false when the limit is reached. */
    public function allow(int $limitPerMinute): bool
    {
        $data = $this->read();
        $window = intdiv(time(), 60);
        if (!isset($data['w']) || (int) $data['w'] !== $window) {
            $data['w'] = $window;
            $data['c'] = 0;
        }
        $count = isset($data['c']) ? (int) $data['c'] : 0;
        if ($count >= $limitPerMinute) {
            return false;
        }
        $data['c'] = $count + 1;
        $this->write($data);

        return true;
    }

    /**
     * Runs $fn(array &$data) with exclusive access to the shared metrics window and stores the
     * (modified) data afterwards. Returns what $fn returned, or null when the lock could not be
     * obtained within a few milliseconds or storage failed (callers then just skip: never block).
     * Backends: APCu (apcu_add spin lock: apcu_cas only supports integers), a small file guarded
     * by flock(), or process memory.
     *
     * @return mixed
     */
    public function mutate(callable $fn)
    {
        try {
            if ($this->apcu) {
                return $this->mutateApcu($fn);
            }
            if ($this->metricsFile !== null) {
                return $this->mutateFile($fn);
            }
            $data = $this->metricsMem;
            $result = $fn($data);
            $this->metricsMem = $data;

            return $result;
        } catch (\Throwable $e) {
            return null;
        }
    }

    private function mutateApcu(callable $fn)
    {
        $lock = $this->key . '_ml';
        $got = false;
        for ($i = 0; $i < 25; $i++) {
            if (apcu_add($lock, 1, 3)) { // the 3 s ttl frees the lock if a worker dies holding it
                $got = true;
                break;
            }
            usleep(1000);
        }
        if (!$got) {
            return null;
        }
        try {
            $ok = false;
            $data = apcu_fetch($this->key . '_m', $ok);
            if (!$ok || !is_array($data)) {
                $data = [];
            }
            $result = $fn($data);
            apcu_store($this->key . '_m', $data, 86400);

            return $result;
        } finally {
            apcu_delete($lock);
        }
    }

    private function mutateFile(callable $fn)
    {
        $fh = @fopen((string) $this->metricsFile, 'c+');
        if ($fh === false) {
            return null;
        }
        try {
            $got = false;
            for ($i = 0; $i < 25; $i++) {
                if (flock($fh, LOCK_EX | LOCK_NB)) {
                    $got = true;
                    break;
                }
                usleep(2000);
            }
            if (!$got) {
                return null;
            }
            $raw = stream_get_contents($fh);
            $data = (is_string($raw) && $raw !== '') ? json_decode($raw, true) : null;
            if (!is_array($data)) {
                $data = [];
            }
            $result = $fn($data);
            $json = json_encode($data);
            if (is_string($json)) {
                ftruncate($fh, 0);
                rewind($fh);
                fwrite($fh, $json);
                fflush($fh);
            }
            flock($fh, LOCK_UN);

            return $result;
        } finally {
            fclose($fh);
        }
    }

    private function read(): array
    {
        if ($this->apcu) {
            $ok = false;
            $value = apcu_fetch($this->key, $ok);

            return ($ok && is_array($value)) ? $value : [];
        }
        if ($this->file !== null) {
            if (!is_file($this->file)) {
                return [];
            }
            $raw = @file_get_contents($this->file);
            $decoded = is_string($raw) ? json_decode($raw, true) : null;

            return is_array($decoded) ? $decoded : [];
        }

        return $this->mem;
    }

    private function write(array $data): void
    {
        if ($this->apcu) {
            apcu_store($this->key, $data, 3700);

            return;
        }
        if ($this->file !== null) {
            @file_put_contents($this->file, json_encode($data), LOCK_EX);

            return;
        }
        $this->mem = $data;
    }
}
