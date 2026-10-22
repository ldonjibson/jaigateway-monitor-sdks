<?php

namespace Insight;

/**
 * Performance metric aggregation. Everything here is a pure function over a plain array
 * (the "shared window") so it can live in APCu, a lock-guarded file or process memory
 * (see State::mutate()).
 *
 * Shared window layout (compact keys, it is rewritten on every request):
 *   w     int    start of the current window (unix seconds, aligned to the window length)
 *   n     int    requests seen in the current window (for the random sample)
 *   t     array  name => [op, c (count), e (errors), sum, min, max, b (bucket index => count)]
 *   s     array  samples: slow (top 3), fail (up to 5), rand (one or null), f (forced by a sampled traceparent, up to 20; key absent when none).
 *                A stored sample is compact: trace_id, name, duration_ms, status_code, ok, started_at, sp, si (span id),
 *                pi (parent span id, only when continued), o (op, only when not "http.server"); kind/op/span_id/parent_span_id are expanded by samples().
 *   x     array  span summaries: key => [tx, op, desc, c, e, sum, min, max, b] (at most MAX_SPAN_KEYS)
 *   q     list   closed windows waiting to be sent: [w, t, s, x] (at most 5)
 *   retry int    do not try to send q before this time
 *   pause int    quota pause: metrics are dropped until this time
 *   off   bool   key rejected (401/403): metrics stay off
 */
final class Metrics
{
    public const BOUNDS_MS = [1, 2, 3, 5, 8, 12, 18, 25, 40, 60, 90, 130, 200, 300, 450, 700, 1000, 1500, 2200, 3300, 5000, 7500, 11000, 17000, 25000, 40000, 60000];

    public const MAX_QUEUED_WINDOWS = 5;
    public const MAX_NAMES = 300;
    public const OTHER = '<other>';
    public const SLOW_SAMPLES = 3;
    public const FAILED_SAMPLES = 5;
    public const MAX_SAMPLES = 10;
    /** Samples kept because the incoming traceparent had the sampled flag (in addition to the 10 above). */
    public const FORCED_SAMPLES = 20;
    /** Spans stored with a forced sample (keeps the shared window record small). */
    public const FORCED_SAMPLE_SPANS = 50;
    /** The server reads at most this many samples per request body; the SDK sends the rest in extra bodies. */
    public const SAMPLES_PER_BODY = 10;
    /** Span summaries per window/payload; new descriptions beyond SOFT go to "<other>". */
    public const MAX_SPAN_KEYS = 300;
    public const SOFT_SPAN_KEYS = 280;
    public const MAX_SAMPLE_SPANS = 100;
    public const MAX_PAYLOAD_BYTES = 1000000;

    /** Index of the first bound >= $ms; durations above 60000 go to index 27 (count(BOUNDS_MS)). */
    public static function bucketIndex(float $ms): int
    {
        foreach (self::BOUNDS_MS as $i => $bound) {
            if ($ms <= $bound) {
                return $i;
            }
        }

        return count(self::BOUNDS_MS);
    }

    public static function iso(float $t): string
    {
        $s = (int) floor($t);
        $ms = (int) (($t - $s) * 1000);

        return gmdate('Y-m-d\TH:i:s', $s) . '.' . sprintf('%03d', $ms) . 'Z';
    }

    public static function emptySamples(): array
    {
        return ['slow' => [], 'fail' => [], 'rand' => null];
    }

    /**
     * Adds one transaction to the shared window, rolls the window over when it has elapsed and
     * returns the closed windows this caller must now send (null when there is nothing to send).
     * Because the caller holds the storage lock, exactly one request gets a given window.
     *
     * $tx: name, op, ms, status (?int), error (bool), ended (float unix time); optional trace_id,
     * span_id, parent_span_id, sampled (bool: keep as a forced sample) and sp (compact sample spans). $tx may be null to add only span summaries ($spans: list of
     * [tx, op, desc, stat] where stat = [c, e, sum, min, max, b]).
     *
     * @return array<int, array>|null
     */
    public static function ingest(array &$data, ?array $tx, int $now, int $window, array $spans = []): ?array
    {
        if (!empty($data['off'])) {
            return null;
        }
        if (isset($data['pause']) && (int) $data['pause'] > $now) {
            return null;
        }
        $window = max(1, $window);
        $ws = intdiv($now, $window) * $window;
        if (!isset($data['w']) || !isset($data['t']) || !is_array($data['t'])) {
            self::reset($data, $ws);
        } elseif ((int) $data['w'] < $ws) {
            if ($data['t'] || !empty($data['x'])) {
                $q = (isset($data['q']) && is_array($data['q'])) ? $data['q'] : [];
                $q[] = ['w' => (int) $data['w'], 't' => $data['t'], 's' => isset($data['s']) ? $data['s'] : self::emptySamples(), 'x' => isset($data['x']) && is_array($data['x']) ? $data['x'] : []];
                $data['q'] = self::capQueue($q);
            }
            self::reset($data, $ws);
        }
        if ($tx !== null) {
            self::add($data, $tx);
        }
        if ($spans) {
            if (!isset($data['x']) || !is_array($data['x'])) {
                $data['x'] = [];
            }
            foreach ($spans as $sp) {
                self::addSpanSummary($data['x'], (string) $sp[0], (string) $sp[1], (string) $sp[2], $sp[3]);
            }
        }

        $claimed = null;
        if (!empty($data['q']) && (int) (isset($data['retry']) ? $data['retry'] : 0) <= $now) {
            $fresh = [];
            foreach ($data['q'] as $w) {
                if (is_array($w) && isset($w['w']) && (int) $w['w'] > $now - 86400) {
                    $fresh[] = $w;
                }
            }
            $data['q'] = [];
            unset($data['retry']);
            $claimed = $fresh ?: null;
        }

        return $claimed;
    }

    private static function reset(array &$data, int $ws): void
    {
        $data['w'] = $ws;
        $data['n'] = 0;
        $data['t'] = [];
        $data['s'] = self::emptySamples();
        $data['x'] = [];
    }

    /** Folds one duration into a (op|description) summary map (per request, tx = ''). */
    public static function foldSpan(array &$map, string $tx, string $op, string $desc, float $ms, bool $ok): void
    {
        $ms = round(max(0.0, $ms), 3);
        self::addSpanSummary($map, $tx, $op, $desc, [1, $ok ? 0 : 1, $ms, $ms, $ms, [self::bucketIndex($ms) => 1]]);
    }

    /**
     * Merges a summary stat [c, e, sum, min, max, b] into $map under (tx, op, desc). A new key beyond
     * SOFT_SPAN_KEYS is folded into (tx, op, "<other>"); beyond MAX_SPAN_KEYS it is dropped.
     */
    public static function addSpanSummary(array &$map, string $tx, string $op, string $desc, array $stat): void
    {
        $key = 'k' . substr(md5($tx . "\0" . $op . "\0" . $desc), 0, 16);
        if (!isset($map[$key])) {
            if (count($map) >= self::SOFT_SPAN_KEYS) {
                $desc = self::OTHER;
                $key = 'k' . substr(md5($tx . "\0" . $op . "\0" . $desc), 0, 16);
                if (!isset($map[$key]) && count($map) >= self::MAX_SPAN_KEYS) {
                    return;
                }
            }
        }
        if (!isset($map[$key])) {
            $map[$key] = [$tx, $op, $desc, 0, 0, 0.0, $stat[3], $stat[4], []];
        }
        $e = &$map[$key];
        $e[3] += (int) $stat[0];
        $e[4] += (int) $stat[1];
        $e[5] = round($e[5] + (float) $stat[2], 3);
        if ($stat[3] < $e[6]) {
            $e[6] = $stat[3];
        }
        if ($stat[4] > $e[7]) {
            $e[7] = $stat[4];
        }
        foreach ($stat[5] as $i => $n) {
            $e[8][$i] = (isset($e[8][$i]) ? $e[8][$i] : 0) + (int) $n;
        }
        unset($e);
    }

    private static function capQueue(array $q): array
    {
        if (count($q) > self::MAX_QUEUED_WINDOWS) {
            usort($q, function ($a, $b) {
                return $a['w'] <=> $b['w'];
            });
            $q = array_slice($q, -self::MAX_QUEUED_WINDOWS);
        }

        return array_values($q);
    }

    private static function add(array &$data, array $tx): void
    {
        $name = (string) $tx['name'];
        $ms = round(max(0.0, (float) $tx['ms']), 3);
        if (!isset($data['t'][$name]) && count($data['t']) >= self::MAX_NAMES) {
            $name = self::OTHER;
        }
        if (!isset($data['t'][$name])) {
            $data['t'][$name] = ['op' => (string) $tx['op'], 'c' => 0, 'e' => 0, 'sum' => 0.0, 'min' => $ms, 'max' => $ms, 'b' => []];
        }
        $t = &$data['t'][$name];
        $t['c']++;
        if (!empty($tx['error'])) {
            $t['e']++;
        }
        $t['sum'] = round($t['sum'] + $ms, 3);
        if ($ms < $t['min']) {
            $t['min'] = $ms;
        }
        if ($ms > $t['max']) {
            $t['max'] = $ms;
        }
        $i = self::bucketIndex($ms);
        $t['b'][$i] = (isset($t['b'][$i]) ? $t['b'][$i] : 0) + 1;
        unset($t);

        // Samples: slowest 3, up to 5 failed (the slowest ones), 1 uniformly random.
        $s = isset($data['s']) && is_array($data['s']) ? $data['s'] : self::emptySamples();
        $s += self::emptySamples();
        $data['n'] = (isset($data['n']) ? (int) $data['n'] : 0) + 1;
        $sample = null;
        $make = function () use (&$sample, $tx, $name, $ms) {
            if ($sample === null) {
                $sample = [
                    'trace_id' => isset($tx['trace_id']) && is_string($tx['trace_id']) && $tx['trace_id'] !== '' ? $tx['trace_id'] : bin2hex(random_bytes(16)),
                    'name' => $name,
                    'duration_ms' => $ms,
                    'status_code' => $tx['status'],
                    'ok' => empty($tx['error']),
                    'started_at' => self::iso((float) $tx['ended'] - $ms / 1000),
                ];
                $sample['si'] = isset($tx['span_id']) && is_string($tx['span_id']) && $tx['span_id'] !== '' ? $tx['span_id'] : bin2hex(random_bytes(8));
                if (isset($tx['parent_span_id']) && is_string($tx['parent_span_id']) && $tx['parent_span_id'] !== '') {
                    $sample['pi'] = $tx['parent_span_id'];
                }
                if (isset($tx['op']) && $tx['op'] !== 'http.server') {
                    $sample['o'] = (string) $tx['op'];
                }
                if (!empty($tx['sp']) && is_array($tx['sp'])) {
                    $sample['sp'] = $tx['sp']; // compact spans, expanded by samples()
                }
            }

            return $sample;
        };
        self::keepSlowest($s['slow'], self::SLOW_SAMPLES, $ms, $make);
        if (!empty($tx['error'])) {
            self::keepSlowest($s['fail'], self::FAILED_SAMPLES, $ms, $make);
        }
        if (mt_rand(1, $data['n']) === 1) {
            $s['rand'] = $make();
        }
        // A request that continued a sampled trace is always kept (first FORCED_SAMPLES of the window).
        if (!empty($tx['sampled'])) {
            $f = isset($s['f']) && is_array($s['f']) ? $s['f'] : [];
            if (count($f) < self::FORCED_SAMPLES) {
                $copy = $make();
                if (isset($copy['sp']) && is_array($copy['sp']) && count($copy['sp']) > self::FORCED_SAMPLE_SPANS) {
                    $copy['sp'] = self::pickSampleSpans($copy['sp'], self::FORCED_SAMPLE_SPANS);
                }
                $f[] = $copy;
                $s['f'] = $f;
            }
        }
        $data['s'] = $s;
    }

    private static function keepSlowest(array &$list, int $cap, float $ms, callable $make): void
    {
        if (count($list) < $cap) {
            $list[] = $make();

            return;
        }
        $minAt = 0;
        foreach ($list as $i => $item) {
            if ($item['duration_ms'] < $list[$minAt]['duration_ms']) {
                $minAt = $i;
            }
        }
        if ($ms > $list[$minAt]['duration_ms']) {
            $list[$minAt] = $make();
        }
    }

    /** Puts windows that could not be delivered back (newest 5 kept) and delays the next attempt. */
    public static function requeue(array &$data, array $windows, int $retryAt): void
    {
        $q = isset($data['q']) && is_array($data['q']) ? $data['q'] : [];
        $data['q'] = self::capQueue(array_merge($windows, $q));
        $data['retry'] = max((int) (isset($data['retry']) ? $data['retry'] : 0), $retryAt);
    }

    /** Quota pause: drop everything buffered and ignore metrics until $until. */
    public static function pause(array &$data, int $until): void
    {
        $data = ['pause' => $until];
    }

    /** Bad key: metrics stay off (until the state is cleared). */
    public static function disable(array &$data): void
    {
        $data = ['off' => 1];
    }

    /**
     * Builds the protocol body for one closed window.
     *
     * @param array{release?:?string, environment?:?string, server_name?:?string} $meta
     */
    public static function payload(array $window, array $meta, int $seconds, bool $withSamples = true, bool $withSampleSpans = true, ?int $level = null): array
    {
        if ($level === null) {
            $level = !$withSamples ? 4 : ($withSampleSpans ? 0 : 2);
        }
        $transactions = [];
        foreach ($window['t'] as $name => $t) {
            $b = $t['b'];
            ksort($b);
            $transactions[] = [
                'name' => (string) $name,
                'op' => $t['op'],
                'count' => (int) $t['c'],
                'errors' => (int) $t['e'],
                'sum_ms' => $t['sum'],
                'min_ms' => $t['min'],
                'max_ms' => $t['max'],
                'buckets' => (object) $b, // keys 0.. as an object, never a JSON list
            ];
        }
        $body = [
            'sdk' => ['name' => Client::SDK_NAME, 'version' => Client::VERSION],
            'platform' => 'php',
        ];
        foreach (['release', 'environment', 'server_name'] as $key) {
            if (isset($meta[$key]) && $meta[$key] !== null && $meta[$key] !== '') {
                $body[$key] = (string) $meta[$key];
            }
        }
        $body['interval_start'] = gmdate('Y-m-d\TH:i:s\Z', (int) $window['w']);
        $body['interval_seconds'] = $seconds;
        $body['transactions'] = $transactions;
        $spans = self::spanSummaries(isset($window['x']) && is_array($window['x']) ? $window['x'] : []);
        if ($spans) {
            $body['spans'] = $spans;
        }
        if ($level < 4) {
            $samples = self::samplesAt(isset($window['s']) && is_array($window['s']) ? $window['s'] : [], $level);
            if ($samples) {
                $body['samples'] = $samples;
            }
        }

        return $body;
    }

    /** Distinct samples: forced ones (up to 20) plus at most 10 of slowest 3, failed up to 5, the random one. */
    public static function samples(array $s, bool $withSpans = true): array
    {
        return self::samplesAt($s, $withSpans ? 0 : 2);
    }

    /**
     * Shrink levels used when a payload is too big (forced samples are the last to go):
     * 0 everything, 1 spans only on forced samples, 2 no spans, 3 forced samples only (no spans), 4 no samples.
     * Forced samples come first so that they survive any cut by the server.
     */
    public static function samplesAt(array $s, int $level = 0): array
    {
        if ($level >= 4) {
            return [];
        }
        $out = [];
        $seen = [];
        $forced = isset($s['f']) && is_array($s['f']) ? $s['f'] : [];
        $rest = array_merge(
            isset($s['slow']) ? (array) $s['slow'] : [],
            isset($s['fail']) ? (array) $s['fail'] : [],
            (isset($s['rand']) && is_array($s['rand'])) ? [$s['rand']] : []
        );
        $others = 0;
        foreach (array_merge($forced, $rest) as $i => $sample) {
            $isForced = $i < count($forced);
            if (!is_array($sample) || !isset($sample['trace_id'])) {
                continue;
            }
            if (!$isForced && ($level >= 3 || $others >= self::MAX_SAMPLES)) {
                continue;
            }
            $key = $sample['trace_id'] . '/' . (isset($sample['si']) ? $sample['si'] : '');
            if (isset($seen[$key])) {
                continue;
            }
            $seen[$key] = true;
            if (!$isForced) {
                $others++;
            }
            $out[] = self::expandSample($sample, $level === 0 || ($level === 1 && $isForced));
            if (count($out) >= self::MAX_SAMPLES + self::FORCED_SAMPLES) {
                break;
            }
        }

        return $out;
    }

    private static function expandSample(array $sample, bool $withSpans): array
    {
        if ($sample['status_code'] === null) {
            unset($sample['status_code']);
        }
        $compact = isset($sample['sp']) && is_array($sample['sp']) ? $sample['sp'] : [];
        $si = isset($sample['si']) ? (string) $sample['si'] : null;
        $pi = isset($sample['pi']) ? (string) $sample['pi'] : null;
        $op = isset($sample['o']) ? (string) $sample['o'] : 'http.server';
        unset($sample['sp'], $sample['si'], $sample['pi'], $sample['o']);
        $sample = ['kind' => 'server', 'op' => $op] + $sample;
        if ($si !== null) {
            $sample['span_id'] = $si;
        }
        if ($pi !== null) {
            $sample['parent_span_id'] = $pi;
        }
        if ($withSpans && $compact) {
            $sample['spans'] = self::expandSpans($compact);
        }

        return $sample;
    }

    /** @return array<int, array> protocol span summaries, at most MAX_SPAN_KEYS */
    public static function spanSummaries(array $x): array
    {
        $out = [];
        foreach ($x as $e) {
            if (!is_array($e) || count($e) < 9) {
                continue;
            }
            $b = (array) $e[8];
            ksort($b);
            $out[] = [
                'transaction' => (string) $e[0],
                'op' => (string) $e[1],
                'description' => (string) $e[2],
                'count' => (int) $e[3],
                'errors' => (int) $e[4],
                'sum_ms' => $e[5],
                'min_ms' => $e[6],
                'max_ms' => $e[7],
                'buckets' => (object) $b,
            ];
            if (count($out) >= self::MAX_SPAN_KEYS) {
                break;
            }
        }

        return $out;
    }

    /** Compact span [id, op, desc, start_ms, ms, ok(0|1), data] -> protocol span. */
    public static function expandSpans(array $compact): array
    {
        $out = [];
        foreach ($compact as $c) {
            if (!is_array($c) || count($c) < 6) {
                continue;
            }
            $span = [
                'span_id' => (string) $c[0],
                'parent_id' => null,
                'op' => (string) $c[1],
                'description' => (string) $c[2],
                'start_ms' => $c[3],
                'duration_ms' => $c[4],
                'status' => $c[5] ? 'ok' : 'error',
            ];
            if (!empty($c[6]) && is_array($c[6])) {
                $span['data'] = $c[6];
            }
            $out[] = $span;
        }

        return $out;
    }

    /** At most $max (default MAX_SAMPLE_SPANS) of $spans, the slowest ones, kept in chronological order. */
    public static function pickSampleSpans(array $spans, int $max = self::MAX_SAMPLE_SPANS): array
    {
        if (count($spans) > $max) {
            $idx = array_keys($spans);
            usort($idx, function ($a, $b) use ($spans) {
                return ($spans[$b][4] <=> $spans[$a][4]) ?: ($a <=> $b);
            });
            $keep = array_slice($idx, 0, $max);
            $picked = [];
            foreach ($keep as $i) {
                $picked[] = $spans[$i];
            }
            $spans = $picked;
        }

        $spans = array_values($spans);
        usort($spans, function ($a, $b) {
            return $a[3] <=> $b[3]; // chronological (usort is stable since PHP 8.0)
        });

        return $spans;
    }
}
