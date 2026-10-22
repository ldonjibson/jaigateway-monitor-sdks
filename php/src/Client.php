<?php

namespace Insight;

use Throwable;

class Client
{
    public const VERSION = '1.5.0';
    public const SDK_NAME = 'insight.php';

    private const JSON_FLAGS = JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR;

    private const BLOCKED_HEADERS = ['cookie', 'set-cookie', 'authorization', 'proxy-authorization', 'x-api-key', 'x-csrf-token', 'x-xsrf-token'];

    private array $options;
    private Transport $transport;
    private State $state;
    private array $queue = [];
    private array $breadcrumbs = [];
    private ?array $user = null;
    private array $tags = [];
    private array $extra = [];
    private bool $disabled = false;
    private bool $shutdownRegistered = false;
    private int $requestCount = 0;
    private ?int $lastStatus = null;
    private array $logged = [];
    private array $pendingMetrics = [];
    private array $ignore = [];
    /** @var array<int, string> compiled trace_propagation_targets (regular expressions) */
    private array $targets = [];
    /** @var array|null current transaction: id, start, op, list (compact spans, max 200), agg (summaries), n */
    private ?array $trace = null;
    private bool $traceConsumed = false;
    private array $orphans = [];
    private int $orphanCalls = 0;
    private bool $orphanShutdown = false;
    /** @var callable|null returns the current unix time as float; tests only */
    private $clock = null;

    /** Paths never reported as performance transactions (prefix match on a path boundary). */
    public const MAX_TRACE_SPANS = 200;
    /** Zero-duration cache markers kept in the span list per request and description; the rest is only counted. */
    public const MAX_CACHE_MARKERS = 20;
    private const SPAN_DATA_KEYS = ['db.system', 'db.name', 'http.method', 'http.status_code', 'host', 'cache.hit'];

    public const DEFAULT_PERFORMANCE_IGNORE = [
        '/health', '/healthz', '/ping', '/status', '/metrics', '/favicon.ico', '/robots.txt',
        '/ready', '/live', '/up', '/storage/', '/build/', '/assets/',
    ];

    public function __construct(array $options = [], ?Transport $transport = null, ?State $state = null)
    {
        $defaults = [
            'api_key' => '',
            'host' => '',
            'endpoint' => '',
            'release' => null,
            'environment' => null,
            'server_name' => null,
            'sample_rate' => 1.0,
            'before_send' => null,
            'send_default_pii' => false,
            'max_breadcrumbs' => 100,
            'timeout' => 3,
            'connect_timeout' => 2,
            'debug' => false,
            'enabled' => true,
            'register_shutdown' => true,
            'rate_limit_per_minute' => 60,
            'max_events_per_request' => 20,
            'retry_delay_ms' => 200,
            'state_mode' => 'auto',
            'state_dir' => null,
            'performance' => false,
            'performance_ignore' => null,
            'performance_window_seconds' => 60,
            'performance_spans' => true,
            'performance_cache_spans' => true,
            'trace_propagation_targets' => [],
        ];
        $clean = [];
        foreach ($options as $key => $value) {
            if ($value !== null) {
                $clean[$key] = $value;
            }
        }
        $o = array_merge($defaults, $clean);
        $o['api_key'] = trim((string) $o['api_key']);
        $source = trim((string) $o['endpoint']) !== '' ? (string) $o['endpoint'] : (string) $o['host'];
        $o['url'] = self::buildUrl($source);
        if ($o['server_name'] === null) {
            $name = function_exists('gethostname') ? gethostname() : false;
            $o['server_name'] = is_string($name) ? $name : null;
        }
        $o['metrics_url'] = $o['url'] === '' ? '' : (preg_replace('#/events/$#', '/metrics/', $o['url']) ?? '');
        $o['performance'] = self::truthy($o['performance']);
        $o['performance_spans'] = self::truthy($o['performance_spans']);
        $o['performance_cache_spans'] = self::truthy($o['performance_cache_spans']);
        $o['performance_window_seconds'] = max(1, min(3600, (int) $o['performance_window_seconds']));
        $ignore = $o['performance_ignore'];
        if (is_string($ignore)) {
            $ignore = explode(',', $ignore);
        }
        $this->ignore = [];
        foreach (is_array($ignore) ? $ignore : self::DEFAULT_PERFORMANCE_IGNORE as $rule) {
            $rule = trim((string) $rule);
            if ($rule !== '') {
                $this->ignore[] = $rule;
            }
        }
        $targets = $o['trace_propagation_targets'];
        if (is_string($targets)) {
            $targets = explode(',', $targets);
        }
        $this->targets = [];
        foreach (is_array($targets) ? $targets : [] as $rule) {
            $re = self::compileTarget(is_scalar($rule) ? (string) $rule : '');
            if ($re !== null) {
                $this->targets[] = $re;
            }
        }
        $this->options = $o;
        $this->transport = $transport !== null ? $transport : new CurlTransport();
        $this->state = $state !== null ? $state : new State($o['api_key'], (string) $o['state_mode'], $o['state_dir']);
    }

    private static function truthy($value): bool
    {
        if (is_string($value)) {
            return in_array(strtolower(trim($value)), ['1', 'true', 'yes', 'on'], true);
        }

        return (bool) $value;
    }

    /** Turns a host (or full endpoint) into the ingest URL, tolerating trailing slashes and a trailing /api. */
    public static function buildUrl(string $host): string
    {
        $h = rtrim(trim($host), '/');
        if ($h === '') {
            return '';
        }
        if (preg_match('#/api/ingest/events$#', $h) === 1) {
            return $h . '/';
        }
        if (substr($h, -4) === '/api') {
            return $h . '/ingest/events/';
        }

        return $h . '/api/ingest/events/';
    }

    public function getUrl(): string
    {
        return (string) $this->options['url'];
    }

    public function isEnabled(): bool
    {
        return (bool) $this->options['enabled']
            && $this->options['api_key'] !== ''
            && $this->options['url'] !== ''
            && !$this->disabled;
    }

    public function isDisabled(): bool
    {
        return $this->disabled;
    }

    public function getOption(string $name)
    {
        return isset($this->options[$name]) ? $this->options[$name] : null;
    }

    public function lastStatus(): ?int
    {
        return $this->lastStatus;
    }

    public function pendingCount(): int
    {
        return count($this->queue);
    }

    // ---------------------------------------------------------------- scope

    public function setUser(?array $user): void
    {
        $this->user = $user;
    }

    public function setTag(string $key, $value): void
    {
        $this->tags[$key] = $value;
    }

    public function setTags(array $tags): void
    {
        foreach ($tags as $key => $value) {
            $this->tags[(string) $key] = $value;
        }
    }

    public function setExtra(string $key, $value): void
    {
        $this->extra[$key] = $value;
    }

    public function addBreadcrumb(string $message, string $category = 'custom', string $level = 'info', array $data = []): void
    {
        try {
            $max = (int) $this->options['max_breadcrumbs'];
            if ($max <= 0) {
                return;
            }
            $crumb = [
                'timestamp' => self::now(),
                'category' => $category,
                'level' => $level,
                'message' => self::truncate($message, 500),
            ];
            if ($data) {
                $crumb['data'] = $data;
            }
            $this->breadcrumbs[] = $crumb;
            if (count($this->breadcrumbs) > $max) {
                $this->breadcrumbs = array_slice($this->breadcrumbs, -$max);
            }
        } catch (Throwable $t) {
            // never throw into the app
        }
    }

    /** Clears user, tags, extra and breadcrumbs (call between jobs / Octane requests). */
    public function resetScope(): void
    {
        $this->user = null;
        $this->tags = [];
        $this->extra = [];
        $this->breadcrumbs = [];
    }

    // -------------------------------------------------------------- capture

    /**
     * Context keys: tags, extra, user, level, fingerprint (list of strings),
     * request (array), frames (array), force (bool, skips sampling and rate guard).
     */
    public function captureException(Throwable $e, array $context = []): bool
    {
        try {
            if (!$this->isEnabled()) {
                return false;
            }
            $level = isset($context['level']) ? (string) $context['level'] : 'error';
            $type = self::cleanName(get_class($e));
            $message = $e->getMessage();
            $event = $this->base('exception', $level, self::truncate($type . ': ' . $message, 255), $context);
            $frames = (isset($context['frames']) && is_array($context['frames'])) ? $context['frames'] : self::frames($e);
            $event['exception'] = ['type' => $type, 'value' => $message, 'stacktrace' => $frames];
            $event['metadata'] = [
                'traceback' => self::truncate(
                    $type . ': ' . $message . ' in ' . $e->getFile() . ':' . $e->getLine() . "\nStack trace:\n" . $e->getTraceAsString(),
                    20000
                ),
            ];
            $chain = [];
            $previous = $e->getPrevious();
            while ($previous !== null && count($chain) < 10) {
                $chain[] = [
                    'type' => self::cleanName(get_class($previous)),
                    'value' => $previous->getMessage(),
                    'file' => $previous->getFile(),
                    'line' => $previous->getLine(),
                ];
                $previous = $previous->getPrevious();
            }
            if ($chain) {
                $event['extra']['exception_chain'] = $chain;
            }

            return $this->enqueue($event, !empty($context['force']));
        } catch (Throwable $t) {
            $this->log('capture failed: ' . $t->getMessage());

            return false;
        }
    }

    public function captureMessage(string $message, string $level = 'info', array $context = []): bool
    {
        try {
            if (!$this->isEnabled()) {
                return false;
            }
            $event = $this->base('message', $level, self::truncate($message, 255), $context);

            return $this->enqueue($event, !empty($context['force']));
        } catch (Throwable $t) {
            $this->log('capture failed: ' . $t->getMessage());

            return false;
        }
    }

    private function base(string $action, string $level, string $description, array $context): array
    {
        $o = $this->options;
        $tags = array_merge(
            ['php_version' => PHP_VERSION, 'os' => PHP_OS_FAMILY, 'sapi' => PHP_SAPI],
            $this->tags,
            (isset($context['tags']) && is_array($context['tags'])) ? $context['tags'] : []
        );
        $cleanTags = [];
        foreach ($tags as $key => $value) {
            if (count($cleanTags) >= 50) {
                break;
            }
            if (is_scalar($value)) {
                $cleanTags[(string) $key] = self::truncate((string) $value, 200);
            }
        }
        $event = [
            'event_type' => 'error',
            'action' => $action,
            'status' => 'error',
            'description' => $description,
            'level' => $level,
            'timestamp' => self::now(),
            'platform' => 'php',
            'sdk' => ['name' => self::SDK_NAME, 'version' => self::VERSION],
            'tags' => $cleanTags,
            'breadcrumbs' => $this->breadcrumbs,
        ];
        $traceId = $this->traceId();
        if ($traceId !== null) {
            $event['trace_id'] = $traceId;
        }
        if ($o['release'] !== null) {
            $event['release'] = (string) $o['release'];
        }
        if ($o['environment'] !== null) {
            $event['environment'] = (string) $o['environment'];
        }
        if ($o['server_name'] !== null) {
            $event['server_name'] = (string) $o['server_name'];
        }
        $extra = array_merge($this->extra, (isset($context['extra']) && is_array($context['extra'])) ? $context['extra'] : []);
        if ($extra) {
            $event['extra'] = $extra;
        }
        $user = (isset($context['user']) && is_array($context['user'])) ? $context['user'] : $this->user;
        if ($o['send_default_pii'] && $user === null && isset($_SERVER['REMOTE_ADDR']) && is_string($_SERVER['REMOTE_ADDR'])) {
            $user = ['ip_address' => $_SERVER['REMOTE_ADDR']];
        }
        if ($user !== null) {
            $event['user'] = $user;
        }
        $request = $this->request($context);
        if ($request !== null) {
            $event['request'] = $request;
        }
        if (isset($context['fingerprint']) && is_array($context['fingerprint'])) {
            $event['fingerprint'] = array_values(array_map('strval', $context['fingerprint']));
        }

        return $event;
    }

    private function request(array $context): ?array
    {
        if (isset($context['request']) && is_array($context['request'])) {
            return $context['request'];
        }
        if (PHP_SAPI === 'cli' || PHP_SAPI === 'phpdbg' || empty($_SERVER['REQUEST_METHOD'])) {
            return null;
        }
        $s = $_SERVER;
        $https = !empty($s['HTTPS']) && $s['HTTPS'] !== 'off';
        $host = isset($s['HTTP_HOST']) ? (string) $s['HTTP_HOST'] : (isset($s['SERVER_NAME']) ? (string) $s['SERVER_NAME'] : '');
        $uri = isset($s['REQUEST_URI']) ? (string) $s['REQUEST_URI'] : '';
        $path = explode('?', $uri, 2)[0];
        $headers = [];
        foreach ($s as $key => $value) {
            if (is_string($key) && strpos($key, 'HTTP_') === 0 && is_string($value)) {
                $headers[strtolower(str_replace('_', '-', substr($key, 5)))] = $value;
            }
        }
        if (isset($s['CONTENT_TYPE']) && is_string($s['CONTENT_TYPE'])) {
            $headers['content-type'] = $s['CONTENT_TYPE'];
        }

        return [
            'url' => ($https ? 'https' : 'http') . '://' . $host . $path,
            'method' => (string) $s['REQUEST_METHOD'],
            'query' => isset($s['QUERY_STRING']) ? (string) $s['QUERY_STRING'] : '',
            'headers' => self::scrubHeaders($headers),
        ];
    }

    /** Lower-cases names, joins array values and removes cookie / authorization / x-api-key style headers. */
    public static function scrubHeaders(array $headers): array
    {
        $out = [];
        foreach ($headers as $name => $value) {
            $lower = strtolower((string) $name);
            if (in_array($lower, self::BLOCKED_HEADERS, true)) {
                continue;
            }
            if (is_array($value)) {
                $value = implode(', ', array_map('strval', $value));
            }
            if (is_scalar($value)) {
                $out[$lower] = (string) $value;
            }
        }

        return $out;
    }

    // ------------------------------------------------------------ stacktrace

    /** Most recent frame first. */
    public static function frames(Throwable $e): array
    {
        $frames = [];
        $file = $e->getFile();
        $line = $e->getLine();
        foreach ($e->getTrace() as $call) {
            $function = isset($call['function']) ? (string) $call['function'] : '';
            if (isset($call['class'])) {
                $function = self::cleanName((string) $call['class']) . '::' . $function;
            }
            $frames[] = self::makeFrame($file, $line, $function);
            $file = isset($call['file']) ? (string) $call['file'] : null;
            $line = isset($call['line']) ? (int) $call['line'] : 0;
        }
        $frames[] = self::makeFrame($file, $line, '{main}');

        return array_slice($frames, 0, 100);
    }

    public static function makeFrame(?string $file, int $line, string $function): array
    {
        $name = ($file === null || $file === '') ? '[internal]' : $file;

        return [
            'filename' => $name,
            'function' => $function,
            'lineno' => $line,
            'in_app' => self::isInApp($name),
        ];
    }

    public static function isInApp(string $file): bool
    {
        if ($file === '[internal]') {
            return false;
        }

        return strpos(str_replace('\\', '/', $file), '/vendor/') === false;
    }

    private static function cleanName(string $name): string
    {
        $pos = strpos($name, "\0");

        return $pos === false ? $name : substr($name, 0, $pos);
    }

    public static function truncate(string $text, int $max): string
    {
        if (strlen($text) <= $max) {
            return $text;
        }
        if (function_exists('mb_substr')) {
            return mb_substr($text, 0, $max, 'UTF-8');
        }

        return substr($text, 0, $max);
    }

    private static function now(): string
    {
        $t = microtime(true);
        $s = (int) floor($t);
        $ms = (int) (($t - $s) * 1000);

        return gmdate('Y-m-d\TH:i:s', $s) . '.' . sprintf('%03d', $ms) . 'Z';
    }

    // ---------------------------------------------------------------- queue

    private function enqueue(array $event, bool $force): bool
    {
        if ($this->state->pausedUntil() > time()) {
            $this->log('paused (quota or rate limit), event dropped');

            return false;
        }
        $rate = (float) $this->options['sample_rate'];
        if (!$force && $rate < 1.0 && (mt_rand() / mt_getrandmax()) >= $rate) {
            return false;
        }
        $callback = $this->options['before_send'];
        if ($callback !== null && is_callable($callback)) {
            try {
                $result = call_user_func($callback, $event);
            } catch (Throwable $t) {
                $this->log('before_send threw: ' . $t->getMessage());
                $result = $event;
            }
            if ($result === null) {
                return false;
            }
            if (is_array($result)) {
                $event = $result;
            }
        }
        if (isset($event['extra']) && is_array($event['extra'])) {
            $encoded = json_encode($event['extra'], self::JSON_FLAGS);
            if (!is_string($encoded) || strlen($encoded) > 16000) {
                $event['extra'] = ['_truncated' => true];
            }
        }
        if (!$force) {
            if ($this->requestCount >= (int) $this->options['max_events_per_request']) {
                $this->log('per-request event cap reached, event dropped');

                return false;
            }
            if ($this->state->isPersistent() && !$this->state->allow((int) $this->options['rate_limit_per_minute'])) {
                $this->log('rate guard: too many events this minute, event dropped');

                return false;
            }
        }
        $this->queue[] = $event;
        $this->requestCount++;
        if ($this->options['register_shutdown'] && !$this->shutdownRegistered) {
            $this->shutdownRegistered = true;
            register_shutdown_function([$this, 'finishAndFlush']);
        }

        return true;
    }

    /** Shutdown hook: release the user's response first, then send. */
    public function finishAndFlush(): void
    {
        try {
            $this->foldOrphans();
            if (!$this->queue && !$this->pendingMetrics) {
                return;
            }
            if (PHP_SAPI !== 'cli' && function_exists('fastcgi_finish_request')) {
                @fastcgi_finish_request();
            }
            $this->flush();
        } catch (Throwable $t) {
            // never throw into the app
        }
    }

    /**
     * Sends everything queued in one POST (and any claimed performance window, see flushMetrics()).
     * Returns the last HTTP status of the events POST, or null when nothing was sent.
     */
    public function flush(): ?int
    {
        try {
            $status = null;
            if ($this->queue) {
                $events = $this->queue;
                $this->queue = [];
                $this->requestCount = 0;
                $status = $this->post($events, 0);
            }
            $this->flushMetrics();

            return $status;
        } catch (Throwable $t) {
            $this->log('flush failed: ' . $t->getMessage());

            return null;
        }
    }

    private function post(array $events, int $depth): ?int
    {
        if ($this->disabled || $this->state->pausedUntil() > time()) {
            return null;
        }
        $body = json_encode($events, self::JSON_FLAGS);
        if (!is_string($body)) {
            $this->log('could not encode events, dropped');

            return null;
        }
        if (strlen($body) > 900000) {
            return $this->splitOrDrop($events, $depth, 'payload over 900 KB');
        }
        $status = null;
        for ($attempt = 0; $attempt < 2; $attempt++) {
            $response = $this->send($body);
            $status = $response['status'];
            $this->lastStatus = $status;
            if ($status >= 200 && $status < 300) {
                return $status;
            }
            if ($status === 401 || $status === 403) {
                $this->disabled = true;
                $this->logOnce('auth', 'HTTP ' . $status . ': invalid key or no write permission, SDK disabled for this process');

                return $status;
            }
            if ($status === 413) {
                return $this->splitOrDrop($events, $depth, 'HTTP 413', 413);
            }
            if ($status === 429) {
                $code = '';
                $json = json_decode($response['body'], true);
                if (is_array($json) && isset($json['code'])) {
                    $code = (string) $json['code'];
                }
                $wait = $response['retry_after'] === null ? 60 : (int) $response['retry_after'];
                $wait = min(3600, max(1, $wait));
                $this->state->pause(time() + $wait);
                $this->logOnce('429' . $code, 'HTTP 429 ' . $code . ': pausing for ' . $wait . 's, events dropped');

                return $status;
            }
            if ($status === 0 || $status >= 500) {
                if ($attempt === 0) {
                    $delay = (int) $this->options['retry_delay_ms'];
                    if ($delay > 0) {
                        usleep($delay * 1000);
                    }
                    continue;
                }
                $this->log('send failed (status ' . $status . '), events dropped');

                return $status;
            }
            $this->log('HTTP ' . $status . ', events dropped');

            return $status;
        }

        return $status;
    }

    private function splitOrDrop(array $events, int $depth, string $why, ?int $status = null): ?int
    {
        if (count($events) > 1 && $depth < 6) {
            $half = intdiv(count($events), 2);
            $first = $this->post(array_slice($events, 0, $half), $depth + 1);
            $second = $this->post(array_slice($events, $half), $depth + 1);

            return $second !== null ? $second : $first;
        }
        $this->log($why . ': event dropped');

        return $status;
    }

    private function send(string $body, ?string $url = null): array
    {
        $headers = [
            'Content-Type: application/json',
            'Accept: application/json',
            'X-API-Key: ' . $this->options['api_key'],
            'User-Agent: ' . self::SDK_NAME . '/' . self::VERSION,
        ];
        try {
            $r = $this->transport->send(
                $url !== null ? $url : (string) $this->options['url'],
                $headers,
                $body,
                (int) $this->options['connect_timeout'],
                (int) $this->options['timeout']
            );

            return [
                'status' => isset($r['status']) ? (int) $r['status'] : 0,
                'body' => isset($r['body']) ? (string) $r['body'] : '',
                'retry_after' => isset($r['retry_after']) ? (int) $r['retry_after'] : null,
            ];
        } catch (Throwable $t) {
            return ['status' => 0, 'body' => '', 'retry_after' => null];
        }
    }

    // ---------------------------------------------------------- performance

    public function isPerformanceEnabled(): bool
    {
        return (bool) $this->options['performance'] && $this->isEnabled();
    }

    /** @internal tests: inject the clock (callable returning unix time as float) */
    public function setClock(?callable $clock): void
    {
        $this->clock = $clock;
    }

    private function nowFloat(): float
    {
        return $this->clock !== null ? (float) call_user_func($this->clock) : microtime(true);
    }

    /** True when a path ("/health") or transaction name ("GET /health") matches the ignore list. */
    public function shouldIgnore(string $value): bool
    {
        $path = $value;
        if (preg_match('/^[A-Z]+ (\/.*)$/', $value, $m) === 1) {
            $path = $m[1];
        }
        $path = explode('?', $path, 2)[0];
        foreach ($this->ignore as $rule) {
            if ($rule[0] === '#' || $rule[0] === '~') { // regular expression with its delimiters
                if (@preg_match($rule, $path) === 1) {
                    return true;
                }
                continue;
            }
            $prefix = rtrim($rule, '/');
            if ($path === $rule || $path === $prefix || ($prefix !== '' && strpos($path, $prefix . '/') === 0)) {
                return true;
            }
        }

        return false;
    }

    /**
     * Records one finished transaction into the shared one-minute window (APCu, file or memory,
     * see State::mutate()). When this call notices the stored window has elapsed it claims it, and
     * the claimed window is sent by flushMetrics() (at shutdown after the response, or explicitly).
     * No network call happens here. Never throws.
     */
    public function recordTransaction(string $name, float $durationMs, ?int $statusCode = null, bool $error = false, string $op = 'http.server'): bool
    {
        $trace = null;
        try {
            if ($this->trace !== null && $this->trace['op'] === $op) {
                $trace = $this->trace; // this transaction ends the trace, whatever happens next
                $this->trace = null;
                $this->traceConsumed = true;
            }
            if (!$this->isPerformanceEnabled()) {
                return false;
            }
            if ($op === 'http.server' && $this->shouldIgnore($name)) {
                return false;
            }
            $name = trim($name);
            if ($name === '') {
                return false;
            }
            $ended = $this->nowFloat();
            $tx = [
                'name' => self::truncate($name, 300),
                'op' => self::truncate($op, 64),
                'ms' => $durationMs,
                'status' => $statusCode,
                'error' => $error,
                'ended' => $ended,
            ];
            $spans = $this->takeOrphans();
            if ($trace !== null) {
                $tx['trace_id'] = $trace['id'];
                $tx['span_id'] = $trace['span_id'];
                if ($trace['parent'] !== null) {
                    $tx['parent_span_id'] = $trace['parent'];
                }
                if ($trace['sampled']) {
                    $tx['sampled'] = true;
                }
                foreach ($trace['agg'] as $e) {
                    $spans[] = [$tx['name'], $e[1], $e[2], [$e[3], $e[4], $e[5], $e[6], $e[7], $e[8]]];
                }
                if ($trace['list']) {
                    $tx['sp'] = Metrics::pickSampleSpans($trace['list']);
                }
            }

            return $this->ingest($tx, $spans, $ended);
        } catch (Throwable $t) {
            $this->log('recordTransaction failed: ' . $t->getMessage());

            return false;
        }
    }

    /** @param array<int, array> $spans [tx, op, desc, stat] */
    private function ingest(?array $tx, array $spans, float $ended): bool
    {
        $now = (int) floor($ended);
        $window = (int) $this->options['performance_window_seconds'];
        $claimed = $this->state->mutate(function (array &$data) use ($tx, $now, $window, $spans) {
            return Metrics::ingest($data, $tx, $now, $window, $spans);
        });
        if (is_array($claimed) && $claimed) {
            $this->pendingMetrics = array_merge($this->pendingMetrics, $claimed);
            $this->registerShutdown();
        }

        return true;
    }

    private function registerShutdown(): void
    {
        if ($this->options['register_shutdown'] && !$this->shutdownRegistered) {
            $this->shutdownRegistered = true;
            register_shutdown_function([$this, 'finishAndFlush']);
        }
    }

    // ---------------------------------------------------------------- spans

    /** @internal current unix time (the injected test clock when set) */
    public function clockNow(): float
    {
        return $this->nowFloat();
    }

    public function isSpansEnabled(): bool
    {
        return (bool) $this->options['performance_spans'] && $this->isPerformanceEnabled();
    }

    /** Cache spans need spans on and the `performance_cache_spans` option (default true). */
    public function isCacheSpansEnabled(): bool
    {
        return (bool) $this->options['performance_cache_spans'] && $this->isSpansEnabled();
    }

    /**
     * Zero-duration cache marker ("GET hit", "GET miss", "PUT", "FORGET"; never a key). Every marker
     * is counted in the summaries, but only the first MAX_CACHE_MARKERS per description and request
     * enter the span list, so a request with thousands of cache calls cannot use up the 200-span budget.
     */
    public function recordCacheMarker(string $description, ?bool $hit = null): bool
    {
        try {
            if (!$this->isCacheSpansEnabled()) {
                return false;
            }
            $listed = true;
            if ($this->activeTrace() !== null) {
                $seen = isset($this->trace['mk'][$description]) ? (int) $this->trace['mk'][$description] : 0;
                $this->trace['mk'][$description] = $seen + 1;
                $listed = $seen < self::MAX_CACHE_MARKERS;
            }

            return $this->recordSpan('cache', $description, 0.0, true, $hit === null ? [] : ['cache.hit' => $hit], null, null, $listed);
        } catch (Throwable $t) {
            return false;
        }
    }

    /**
     * Starts the trace of the current transaction ($op is the transaction op, "http.server" or
     * "queue.task"). Returns false when spans are off or a request trace is already running
     * (a sync queue job inside a request keeps feeding the request's trace).
     */
    public function beginTrace(?float $start = null, string $op = 'http.server', ?string $traceparent = null): bool
    {
        try {
            if (!$this->isPerformanceEnabled()) {
                return false;
            }
            if ($this->trace !== null && $op !== 'http.server' && $this->trace['op'] === 'http.server') {
                return false;
            }
            if ($traceparent === null && $op === 'http.server' && isset($_SERVER['HTTP_TRACEPARENT']) && is_string($_SERVER['HTTP_TRACEPARENT'])) {
                $traceparent = $_SERVER['HTTP_TRACEPARENT']; // plain PHP / FPM; Laravel passes the request header explicitly
            }
            $ctx = $traceparent !== null && $traceparent !== '' ? self::parseTraceparent($traceparent) : null;
            $this->trace = [
                'id' => $ctx !== null ? $ctx['trace_id'] : bin2hex(random_bytes(16)),
                'span_id' => self::newSpanId(),
                'parent' => $ctx !== null ? $ctx['parent_id'] : null,
                'sampled' => $ctx !== null && $ctx['sampled'],
                'start' => $start !== null ? $start : $this->nowFloat(),
                'op' => $op,
                'list' => [],
                'agg' => [],
                'n' => 0,
                'mk' => [],
            ];
            $this->traceConsumed = false;

            return true;
        } catch (Throwable $t) {
            return false;
        }
    }

    public function endTrace(): void
    {
        $this->trace = null;
        $this->traceConsumed = false;
    }

    /**
     * Parses a W3C traceparent header ("00-<32 hex>-<16 hex>-<2 hex>", case-insensitive, version 00 only,
     * ids not all zeros). Returns null for anything else; never throws.
     *
     * @return array{trace_id: string, parent_id: string, sampled: bool}|null
     */
    public static function parseTraceparent(string $header): ?array
    {
        if (strlen($header) > 256) {
            return null;
        }
        if (preg_match('/^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/', strtolower(trim($header)), $m) !== 1) {
            return null;
        }
        if ($m[1] === str_repeat('0', 32) || $m[2] === str_repeat('0', 16)) {
            return null;
        }

        return ['trace_id' => $m[1], 'parent_id' => $m[2], 'sampled' => (hexdec($m[3]) & 1) === 1];
    }

    /** A new random 16 hex span id (never all zeros). */
    public static function newSpanId(): string
    {
        $id = bin2hex(random_bytes(8));

        return $id === '0000000000000000' ? '0000000000000001' : $id;
    }

    /**
     * Continues the trace of an incoming traceparent for work that is not an HTTP request (queue job,
     * CLI command). Adopts the header's trace id, parent span and sampled flag: in the trace already
     * running (a Laravel job has one), else by starting a new one of op $op. False when the header is
     * invalid or performance monitoring is off. Never throws.
     */
    public function continueTrace(string $traceparent, string $op = 'queue.task'): bool
    {
        try {
            $ctx = self::parseTraceparent($traceparent);
            if ($ctx === null || !$this->isPerformanceEnabled()) {
                return false;
            }
            if ($this->trace === null) {
                return $this->beginTrace(null, $op, $traceparent);
            }
            $this->trace['id'] = $ctx['trace_id'];
            $this->trace['parent'] = $ctx['parent_id'];
            $this->trace['sampled'] = $ctx['sampled'];

            return true;
        } catch (Throwable $t) {
            return false;
        }
    }

    /**
     * The traceparent value of the current transaction ("00-<trace>-<span of the request root>-<01|00>")
     * for apps that call other services with a client Insight does not instrument. Null without a trace.
     */
    public function traceparent(): ?string
    {
        try {
            $t = $this->activeTrace();

            return $t !== null ? '00-' . $t['id'] . '-' . $t['span_id'] . '-' . ($t['sampled'] ? '01' : '00') : null;
        } catch (Throwable $e) {
            return null;
        }
    }

    /**
     * For instrumentations that add the header themselves: when $hostPort ("api.example.com" or
     * "api.example.com:8443") matches trace_propagation_targets, spans are on and a trace is active,
     * returns ['header' => the traceparent value, 'span_id' => the id the http.client span for this
     * call must be recorded with]; else null. Never matches Insight's own host.
     *
     * @return array{header: string, span_id: string}|null
     */
    public function outgoingTrace(string $hostPort): ?array
    {
        try {
            if (!$this->targets || !$this->isSpansEnabled()) {
                return null;
            }
            $hostPort = strtolower(trim($hostPort));
            if ($hostPort === '' || $hostPort === Tracing\Sql::host((string) $this->options['url']) || !$this->matchesTarget($hostPort)) {
                return null;
            }
            $t = $this->activeTrace();
            if ($t === null || count($t['list']) >= self::MAX_TRACE_SPANS) {
                return null; // no trace, or the span would not be kept and the id would dangle
            }
            $id = self::newSpanId();

            return ['header' => '00-' . $t['id'] . '-' . $id . '-' . ($t['sampled'] ? '01' : '00'), 'span_id' => $id];
        } catch (Throwable $e) {
            return null;
        }
    }

    /** True when "host[:port]" matches one of the trace_propagation_targets. */
    public function matchesTarget(string $hostPort): bool
    {
        $hostPort = strtolower($hostPort);
        foreach ($this->targets as $re) {
            if (@preg_match($re, $hostPort) === 1) {
                return true;
            }
        }

        return false;
    }

    /** "api.example.com", "*.example.com", "localhost:8000" (no port in the rule: any port) or "#regex#" -> regular expression. */
    private static function compileTarget(string $rule): ?string
    {
        $rule = trim($rule);
        if ($rule === '') {
            return null;
        }
        if ($rule[0] === '#') {
            return @preg_match($rule, '') !== false ? $rule : null;
        }
        $rule = strtolower($rule);
        $q = str_replace('\\*', '[^:/]+', preg_quote($rule, '#'));

        return '#^' . $q . (strpos($rule, ':') === false ? '(?::[0-9]+)?' : '') . '$#';
    }

    /** 32 hex id of the transaction in progress, or null. Error events carry it as trace_id. */
    public function traceId(): ?string
    {
        try {
            $t = $this->activeTrace();

            return $t !== null ? $t['id'] : null;
        } catch (Throwable $e) {
            return null;
        }
    }

    private function activeTrace(): ?array
    {
        if ($this->trace === null && !$this->traceConsumed && PHP_SAPI !== 'cli' && PHP_SAPI !== 'phpdbg'
            && isset($_SERVER['REQUEST_TIME_FLOAT']) && $this->isPerformanceEnabled()) {
            $this->beginTrace((float) $_SERVER['REQUEST_TIME_FLOAT']); // plain FPM request that never called startTransaction()
        }

        return $this->trace;
    }

    /**
     * Records one finished span. Inside a transaction it joins the trace (at most 200 are kept, all
     * are counted); outside one it is aggregated under transaction "". SQL is normalised and HTTP
     * reduced to "METHOD host" here, whatever the caller passed. Never throws.
     */
    public function recordSpan(string $op, string $description, float $durationMs, bool $ok = true, array $data = [], ?float $startedAt = null, ?string $spanId = null, bool $listed = true): bool
    {
        try {
            if (!$this->isSpansEnabled()) {
                return false;
            }
            $op = self::truncate(trim($op), 40);
            if ($op === '') {
                return false;
            }
            $desc = self::spanDescription($op, $description);
            if ($desc === '') {
                $desc = $op; // the server rejects empty descriptions
            }
            $ms = round(max(0.0, $durationMs), 3);
            $trace = $this->activeTrace();
            if ($trace === null) {
                Metrics::foldSpan($this->orphans, '', $op, $desc, $ms, $ok);
                $this->orphanCalls++;
                if ($this->orphanCalls >= 100) {
                    $this->foldOrphans();
                } else {
                    $this->registerShutdown();
                }

                return true;
            }
            Metrics::foldSpan($this->trace['agg'], '', $op, $desc, $ms, $ok);
            $this->trace['n']++;
            if ($listed && count($this->trace['list']) < self::MAX_TRACE_SPANS) {
                $started = $startedAt !== null ? $startedAt : $this->nowFloat() - $ms / 1000;
                $span = [
                    ($spanId !== null && preg_match('/^[0-9a-f]{16}$/', $spanId) === 1) ? $spanId : self::newSpanId(),
                    $op,
                    $desc,
                    round(max(0.0, ($started - $trace['start']) * 1000), 3),
                    $ms,
                    $ok ? 1 : 0,
                ];
                $clean = self::spanData($data);
                if ($clean) {
                    $span[] = $clean;
                }
                $this->trace['list'][] = $span;
            }

            return true;
        } catch (Throwable $t) {
            $this->log('recordSpan failed: ' . $t->getMessage());

            return false;
        }
    }

    public static function spanDescription(string $op, string $description): string
    {
        if (strncmp($op, 'db', 2) === 0) {
            return Tracing\Sql::normalize($description);
        }
        if (strncmp($op, 'http', 4) === 0) {
            return Tracing\Sql::describeHttp($description);
        }
        $d = preg_replace('/\s+/', ' ', $description);

        return self::truncate(trim(is_string($d) ? $d : ''), 300);
    }

    private static function spanData(array $data): array
    {
        $out = [];
        foreach (self::SPAN_DATA_KEYS as $k) {
            if (!isset($data[$k])) {
                continue;
            }
            $v = $data[$k];
            if (is_bool($v) || is_int($v) || is_float($v)) {
                $out[$k] = $v;
            } elseif (is_string($v)) {
                $out[$k] = $k === 'host' ? Tracing\Sql::host($v) : self::truncate($v, 200);
            }
        }

        return $out;
    }

    private function takeOrphans(): array
    {
        $out = [];
        foreach ($this->orphans as $e) {
            $out[] = [$e[0], $e[1], $e[2], [$e[3], $e[4], $e[5], $e[6], $e[7], $e[8]]];
        }
        $this->orphans = [];
        $this->orphanCalls = 0;

        return $out;
    }

    /** Moves spans recorded outside any transaction into the shared window. */
    private function foldOrphans(): void
    {
        if (!$this->orphans || !$this->isPerformanceEnabled()) {
            $this->orphans = [];
            $this->orphanCalls = 0;

            return;
        }
        $this->ingest(null, $this->takeOrphans(), $this->nowFloat());
    }

    public function pendingMetricWindows(): int
    {
        return count($this->pendingMetrics);
    }

    /**
     * Sends the performance windows this process has claimed, one POST per window (normally zero
     * or one per minute). Returns the last HTTP status or null when nothing was sent. Never throws.
     */
    public function flushMetrics(): ?int
    {
        try {
            $this->foldOrphans();
            if (!$this->pendingMetrics) {
                return null;
            }
            $windows = $this->pendingMetrics;
            $this->pendingMetrics = [];
            if (!$this->isPerformanceEnabled()) {
                return null;
            }
            $seconds = (int) $this->options['performance_window_seconds'];
            $meta = [
                'release' => $this->options['release'],
                'environment' => $this->options['environment'],
                'server_name' => $this->options['server_name'],
            ];
            $status = null;
            foreach ($windows as $i => $window) {
                $rest = array_slice($windows, $i);
                $level = 0; // Metrics::samplesAt(): 0 everything .. 3 forced samples only, 4 no samples
                for ($attempt = 0; $attempt < 5; $attempt++) {
                    $overflow = [];
                    $body = $this->windowBody($window, $meta, $seconds, $level, $overflow);
                    while (is_string($body) && strlen($body) > Metrics::MAX_PAYLOAD_BYTES && $level < 4) {
                        $level++;
                        $body = $this->windowBody($window, $meta, $seconds, $level, $overflow);
                    }
                    if (!is_string($body)) {
                        $this->log('could not encode performance window, dropped');
                        break;
                    }
                    $response = $this->send($body, (string) $this->options['metrics_url']);
                    $status = $response['status'];
                    if ($status >= 200 && $status < 300) {
                        $this->sendOverflowSamples($window, $meta, $seconds, $overflow);
                        break;
                    }
                    if ($status === 413 && $level < 4) {
                        $level = $level < 3 ? 3 : 4; // drop the ordinary samples first, then the forced ones; keep the counts
                        continue;
                    }
                    if ($status === 401 || $status === 403) {
                        $this->state->mutate(function (array &$data) {
                            Metrics::disable($data);
                        });
                        $this->logOnce('mauth', 'metrics: HTTP ' . $status . ' invalid key or no write permission, performance monitoring disabled');

                        return $status;
                    }
                    if ($status === 429) {
                        $json = json_decode($response['body'], true);
                        $code = (is_array($json) && isset($json['code'])) ? (string) $json['code'] : '';
                        $quota = (is_array($json) && isset($json['quota'])) ? (string) $json['quota'] : '';
                        $wait = min(3600, max(1, $response['retry_after'] === null ? 60 : (int) $response['retry_after']));
                        $until = (int) floor($this->nowFloat()) + $wait;
                        if ($code === 'quota_exceeded' || $quota === 'performance') {
                            $this->state->mutate(function (array &$data) use ($until) {
                                Metrics::pause($data, $until);
                            });
                            $this->logOnce('mquota', 'metrics: tracked-request quota used up, performance data paused for ' . $wait . 's (error reporting is not affected)');
                        } else {
                            $this->state->mutate(function (array &$data) use ($rest, $until) {
                                Metrics::requeue($data, $rest, $until);
                            });
                            $this->logOnce('mrate', 'metrics: HTTP 429 rate_limited, retrying after ' . $wait . 's');
                        }

                        return $status;
                    }
                    if ($status === 0 || $status >= 500) {
                        $until = (int) floor($this->nowFloat()) + 30;
                        $this->state->mutate(function (array &$data) use ($rest, $until) {
                            Metrics::requeue($data, $rest, $until);
                        });
                        $this->log('metrics: send failed (status ' . $status . '), window kept for a later request');

                        return $status;
                    }
                    $this->log('metrics: HTTP ' . $status . ', window dropped'); // 400 and other 4xx
                    break;
                }
            }

            return $status;
        } catch (Throwable $t) {
            $this->log('flushMetrics failed: ' . $t->getMessage());

            return null;
        }
    }

    /** Window body at shrink level $level; samples beyond the server's per-body limit go to $overflow. */
    private function windowBody(array $window, array $meta, int $seconds, int $level, array &$overflow)
    {
        $p = Metrics::payload($window, $meta, $seconds, true, true, $level);
        $overflow = [];
        if (isset($p['samples']) && count($p['samples']) > Metrics::SAMPLES_PER_BODY) {
            $overflow = array_slice($p['samples'], Metrics::SAMPLES_PER_BODY);
            $p['samples'] = array_slice($p['samples'], 0, Metrics::SAMPLES_PER_BODY);
        }

        return json_encode($p, self::JSON_FLAGS);
    }

    /** The server keeps 10 samples per body: forced samples beyond that travel in extra, samples-only bodies (best effort). */
    private function sendOverflowSamples(array $window, array $meta, int $seconds, array $samples): void
    {
        try {
            foreach (array_chunk($samples, Metrics::SAMPLES_PER_BODY) as $chunk) {
                $p = Metrics::payload(['w' => $window['w'], 't' => []], $meta, $seconds, false);
                $p['samples'] = $chunk;
                $body = json_encode($p, self::JSON_FLAGS);
                if (!is_string($body)) {
                    continue;
                }
                if (strlen($body) > Metrics::MAX_PAYLOAD_BYTES) {
                    foreach ($chunk as $i => $smp) {
                        unset($chunk[$i]['spans']);
                    }
                    $p['samples'] = array_values($chunk);
                    $body = json_encode($p, self::JSON_FLAGS);
                    if (!is_string($body) || strlen($body) > Metrics::MAX_PAYLOAD_BYTES) {
                        continue;
                    }
                }
                $this->send($body, (string) $this->options['metrics_url']);
            }
        } catch (Throwable $t) {
            $this->log('metrics: extra samples not sent: ' . $t->getMessage());
        }
    }

    private function log(string $message): void
    {
        if ($this->options['debug']) {
            error_log('[insight] ' . $message);
        }
    }

    private function logOnce(string $key, string $message): void
    {
        if (isset($this->logged[$key])) {
            return;
        }
        $this->logged[$key] = true;
        $this->log($message);
    }
}
