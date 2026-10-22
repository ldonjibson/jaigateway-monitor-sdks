<?php

namespace Insight\Tests;

use Insight\Client;
use Insight\Insight;
use Insight\Laravel\PerformanceMiddleware;
use Insight\Metrics;
use Insight\State;
use PHPUnit\Framework\TestCase;

class PerformanceTest extends TestCase
{
    private string $dir = '';
    private float $t = 1000000.0; // multiple of 60: start of a window
    /** @var array<int, string> */
    private array $server = [];

    protected function setUp(): void
    {
        $this->dir = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'insight-perf-' . bin2hex(random_bytes(4));
        mkdir($this->dir);
        $this->t = 1000020.0; // 1000020 = 16667 * 60: window aligned
        Insight::setClient(null);
        Insight::resetTransaction();
        $this->server = [];
        foreach (['REQUEST_METHOD', 'REQUEST_URI', 'REQUEST_TIME_FLOAT'] as $k) {
            if (isset($_SERVER[$k])) {
                $this->server[$k] = $_SERVER[$k];
            }
        }
    }

    protected function tearDown(): void
    {
        foreach (glob($this->dir . '/*') ?: [] as $f) {
            @unlink($f);
        }
        @rmdir($this->dir);
        Insight::setClient(null);
        Insight::resetTransaction();
        foreach (['REQUEST_METHOD', 'REQUEST_URI', 'REQUEST_TIME_FLOAT'] as $k) {
            unset($_SERVER[$k]);
        }
        foreach ($this->server as $k => $v) {
            $_SERVER[$k] = $v;
        }
    }

    /** @return array{0: Client, 1: FakeTransport} */
    private function make(array $options = [], array $responses = [], string $mode = 'file'): array
    {
        $transport = new FakeTransport($responses);
        $client = new Client(array_merge([
            'api_key' => 'key-123',
            'host' => 'https://insight.test',
            'register_shutdown' => false,
            'state_mode' => $mode,
            'state_dir' => $this->dir,
            'retry_delay_ms' => 0,
            'server_name' => 'web-1',
            'release' => 'app@1.0.0',
            'environment' => 'testing',
            'performance' => true,
        ], $options), $transport);
        $client->setClock(function () {
            return $this->t;
        });

        return [$client, $transport];
    }

    private function closeWindow(Client $c): ?int
    {
        $this->t += 61; // past the 60 s window
        $c->recordTransaction('GET /tick', 1.0);

        return $c->flushMetrics();
    }

    // ------------------------------------------------------------ histogram

    public function testBucketTableAndEdges(): void
    {
        $this->assertSame([1, 2, 3, 5, 8, 12, 18, 25, 40, 60, 90, 130, 200, 300, 450, 700, 1000, 1500, 2200, 3300, 5000, 7500, 11000, 17000, 25000, 40000, 60000], Metrics::BOUNDS_MS);
        $this->assertSame(0, Metrics::bucketIndex(0));
        $this->assertSame(0, Metrics::bucketIndex(1));
        $this->assertSame(1, Metrics::bucketIndex(1.01));
        $this->assertSame(1, Metrics::bucketIndex(2));
        $this->assertSame(3, Metrics::bucketIndex(5));
        $this->assertSame(4, Metrics::bucketIndex(6));
        $this->assertSame(26, Metrics::bucketIndex(60000));
        $this->assertSame(27, Metrics::bucketIndex(60001));
        $this->assertSame(27, Metrics::bucketIndex(10 ** 9));
    }

    // ----------------------------------------------------------- aggregation

    public function testAggregationAndPayloadShape(): void
    {
        [$c, $tr] = $this->make();
        $this->assertTrue($c->recordTransaction('GET /items/{id}', 1.0, 200));
        $c->recordTransaction('GET /items/{id}', 1.01, 200);
        $c->recordTransaction('GET /items/{id}', 60001, 500, true);
        $c->recordTransaction('POST /orders', 40, 201);
        $this->assertSame(0, $c->pendingMetricWindows());
        $this->closeWindow($c);
        $this->assertCount(1, $tr->calls);
        $call = $tr->calls[0];
        $this->assertSame('https://insight.test/api/ingest/metrics/', $call['url']);
        $this->assertContains('X-API-Key: key-123', $call['headers']);
        $this->assertContains('Content-Type: application/json', $call['headers']);
        $p = $call['events'];
        $this->assertSame('insight.php', $p['sdk']['name']);
        $this->assertSame(Client::VERSION, $p['sdk']['version']);
        $this->assertSame('php', $p['platform']);
        $this->assertSame('app@1.0.0', $p['release']);
        $this->assertSame('testing', $p['environment']);
        $this->assertSame('web-1', $p['server_name']);
        $this->assertSame(gmdate('Y-m-d\TH:i:s\Z', 1000020), $p['interval_start']);
        $this->assertSame(60, $p['interval_seconds']);
        $byName = [];
        foreach ($p['transactions'] as $t) {
            $byName[$t['name']] = $t;
        }
        $this->assertCount(2, $byName);
        $items = $byName['GET /items/{id}'];
        $this->assertSame('http.server', $items['op']);
        $this->assertSame(3, $items['count']);
        $this->assertSame(1, $items['errors']);
        $this->assertEquals(60003.01, $items['sum_ms']);
        $this->assertEquals(1, $items['min_ms']);
        $this->assertEquals(60001, $items['max_ms']);
        $this->assertSame(['0' => 1, '1' => 1, '27' => 1], $items['buckets']);
        $this->assertSame([8 => 1], $byName['POST /orders']['buckets']);
        $this->assertSame(1, $byName['POST /orders']['count']);
        $this->assertSame(0, $byName['POST /orders']['errors']);
    }

    public function testBucketsWithOnlyIndexZeroStayAJsonObject(): void
    {
        [$c, $tr] = $this->make();
        $c->recordTransaction('GET /fast', 0.4, 200);
        $this->closeWindow($c);
        $this->assertStringContainsString('"buckets":{"0":1}', $tr->calls[0]['body']);
    }

    public function testSamplesAreCappedAndShaped(): void
    {
        [$c, $tr] = $this->make();
        for ($i = 1; $i <= 60; $i++) {
            $failed = $i % 3 === 0;
            $c->recordTransaction('GET /items/{id}', (float) $i, $failed ? 500 : 200, $failed);
        }
        $this->closeWindow($c);
        $samples = $tr->calls[0]['events']['samples'];
        $this->assertGreaterThan(0, count($samples));
        $this->assertTrue(count($samples) <= 10);
        $ids = array_column($samples, 'trace_id');
        $this->assertSame(count($ids), count(array_unique($ids)));
        $durations = array_column($samples, 'duration_ms');
        foreach ([60, 59, 58] as $slowest) { // slowest 3 are kept
            $this->assertTrue(in_array((float) $slowest, array_map('floatval', $durations), true));
        }
        $failedKept = 0;
        foreach ($samples as $s) {
            $this->assertMatchesRegularExpression('/^[0-9a-f]{32}$/', $s['trace_id']);
            $this->assertMatchesRegularExpression('/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/', $s['started_at']);
            $this->assertSame('GET /items/{id}', $s['name']);
            $this->assertSame($s['status_code'] >= 500, $s['ok'] === false);
            if ($s['ok'] === false) {
                $failedKept++;
            }
        }
        $this->assertTrue($failedKept >= 1 && $failedKept <= 5 + 1);
    }

    public function testFailedSamplesLimitedToFive(): void
    {
        $data = [];
        for ($i = 1; $i <= 30; $i++) {
            Metrics::ingest($data, ['name' => 'GET /x', 'op' => 'http.server', 'ms' => (float) $i, 'status' => 500, 'error' => true, 'ended' => 1000020.5], 1000020, 60);
        }
        $this->assertCount(5, $data['s']['fail']);
        $this->assertCount(3, $data['s']['slow']);
        $this->assertTrue(count(Metrics::samples($data['s'])) <= 10);
        $this->assertSame(30, $data['t']['GET /x']['c']);
    }

    public function testNamesAreCappedIntoOther(): void
    {
        $data = [];
        for ($i = 0; $i < Metrics::MAX_NAMES + 20; $i++) {
            Metrics::ingest($data, ['name' => 'GET /n' . $i, 'op' => 'http.server', 'ms' => 1.0, 'status' => 200, 'error' => false, 'ended' => 1000020.0], 1000020, 60);
        }
        $this->assertCount(Metrics::MAX_NAMES + 1, $data['t']);
        $this->assertSame(20, $data['t'][Metrics::OTHER]['c']);
    }

    // ------------------------------------------------------------ the window

    public function testNothingIsSentWhileTheWindowIsOpen(): void
    {
        [$c, $tr] = $this->make();
        for ($i = 0; $i < 20; $i++) {
            $this->t += 2;
            $c->recordTransaction('GET /a', 5.0, 200);
        }
        $this->assertSame(0, $c->pendingMetricWindows());
        $this->assertNull($c->flushMetrics());
        $this->assertCount(0, $tr->calls);
    }

    public function testTwoWorkersClaimTheWindowExactlyOnce(): void
    {
        [$w1, $tr1] = $this->make();
        [$w2, $tr2] = $this->make();
        for ($i = 0; $i < 10; $i++) {
            $w1->recordTransaction('GET /a', 5.0, 200);
            $w2->recordTransaction('GET /a', 7.0, 200);
        }
        $this->t += 61;
        $w1->recordTransaction('GET /a', 5.0, 200); // notices the elapsed window and claims it
        $w2->recordTransaction('GET /a', 5.0, 200); // sees an empty claim queue
        $this->assertSame(1, $w1->pendingMetricWindows());
        $this->assertSame(0, $w2->pendingMetricWindows());
        $w1->flushMetrics();
        $w2->flushMetrics();
        $this->assertCount(1, $tr1->calls);
        $this->assertCount(0, $tr2->calls);
        $tx = $tr1->calls[0]['events']['transactions'];
        $this->assertSame(20, $tx[0]['count']); // both workers' requests are in the one summary
        // the new window holds the two later requests and is sent once it closes
        $this->t += 61;
        $w2->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(1, $w2->pendingMetricWindows());
        $w2->flushMetrics();
        $this->assertSame(2, $tr2->calls[0]['events']['transactions'][0]['count']);
    }

    public function testMemoryModeWorksAndIsProcessLocal(): void
    {
        [$c, $tr] = $this->make([], [], 'memory');
        $c->recordTransaction('GET /m', 5.0, 200);
        $this->closeWindow($c);
        $this->assertCount(1, $tr->calls);
        $this->assertSame([], glob($this->dir . '/*') ?: []);
    }

    public function testStaleWindowsOlderThanADayAreDropped(): void
    {
        [$c, $tr] = $this->make();
        $c->recordTransaction('GET /old', 5.0, 200);
        $this->t += 90000;
        $c->recordTransaction('GET /new', 5.0, 200);
        $this->assertSame(0, $c->pendingMetricWindows());
        $this->assertCount(0, $tr->calls);
    }

    // ------------------------------------------------------------- responses

    public function test201IsDone(): void
    {
        [$c, $tr] = $this->make();
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(201, $this->closeWindow($c));
        $this->t += 61;
        $c->recordTransaction('GET /a', 5.0, 200);
        $c->flushMetrics();
        $this->assertCount(2, $tr->calls); // only the tick window, nothing re-sent
        $this->assertSame(1, $tr->calls[1]['events']['transactions'][0]['count']);
    }

    public function test400IsDropped(): void
    {
        [$c, $tr] = $this->make([], [['status' => 400, 'body' => '{"error":"bad"}']]);
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(400, $this->closeWindow($c));
        $this->t += 61;
        $c->recordTransaction('GET /a', 5.0, 200);
        $c->flushMetrics();
        $this->assertCount(2, $tr->calls);
        $names = array_column($tr->calls[1]['events']['transactions'], 'name');
        $this->assertSame(['GET /tick'], $names); // 'GET /a' of the rejected window is not retried
    }

    public function test401DisablesMetricsOnlyAndErrorsKeepFlowing(): void
    {
        [$c, $tr] = $this->make([], [['status' => 401]]);
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(401, $this->closeWindow($c));
        $this->t += 200;
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertNull($c->flushMetrics());
        $this->assertCount(1, $tr->calls); // metrics never sent again
        // another worker sharing the state also stays quiet
        [$other, $tr2] = $this->make();
        $other->recordTransaction('GET /a', 5.0, 200);
        $this->t += 200;
        $other->recordTransaction('GET /a', 5.0, 200);
        $other->flushMetrics();
        $this->assertCount(0, $tr2->calls);
        // error reporting is independent
        $this->assertTrue($c->captureMessage('still works'));
        $this->assertSame(201, $c->flush());
        $this->assertStringContainsString('/api/ingest/events/', $tr->calls[1]['url']);
    }

    public function test413DropsSamplesKeepsCounts(): void
    {
        [$c, $tr] = $this->make([], [['status' => 413]]);
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(201, $this->closeWindow($c));
        $this->assertCount(2, $tr->calls);
        $this->assertArrayHasKey('samples', $tr->calls[0]['events']);
        $this->assertArrayNotHasKey('samples', $tr->calls[1]['events']);
        $this->assertSame($tr->calls[0]['events']['transactions'], $tr->calls[1]['events']['transactions']);
    }

    public function test429RateLimitedKeepsTheWindowAndRetriesAfterRetryAfter(): void
    {
        [$c, $tr] = $this->make([], [['status' => 429, 'body' => '{"code":"rate_limited"}', 'retry_after' => 20]]);
        for ($i = 0; $i < 4; $i++) {
            $c->recordTransaction('GET /a', 5.0, 200);
        }
        $this->assertSame(429, $this->closeWindow($c));
        $this->assertCount(1, $tr->calls);
        $this->t += 10; // still inside Retry-After
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(0, $c->pendingMetricWindows());
        $this->t += 15; // past it
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(1, $c->pendingMetricWindows());
        $this->assertSame(201, $c->flushMetrics());
        $retry = $tr->calls[1]['events'];
        $this->assertSame(gmdate('Y-m-d\TH:i:s\Z', 1000020), $retry['interval_start']); // the kept window
        $a = null;
        foreach ($retry['transactions'] as $t) {
            if ($t['name'] === 'GET /a') {
                $a = $t;
            }
        }
        $this->assertSame(4, $a['count']);
    }

    public function test429QuotaPausesMetricsOnlyUntilRetryAfter(): void
    {
        [$c, $tr] = $this->make([], [['status' => 429, 'body' => '{"code":"quota_exceeded","quota":"performance"}', 'retry_after' => 7200]]);
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(429, $this->closeWindow($c));
        $start = $this->t;
        // error reporting continues
        $this->assertTrue($c->captureMessage('hello'));
        $this->assertSame(201, $c->flush());
        // metrics paused: dropped for every worker, no HTTP
        $this->t = $start + 600;
        [$other, $tr2] = $this->make();
        $other->recordTransaction('GET /a', 5.0, 200);
        $this->t += 61;
        $other->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(0, $other->pendingMetricWindows());
        $this->assertNull($other->flushMetrics());
        $this->assertCount(0, $tr2->calls);
        // the 7200 s Retry-After is capped at one hour
        $this->t = $start + 3599;
        $other->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(0, $other->pendingMetricWindows());
        $this->t = $start + 3601;
        $other->recordTransaction('GET /a', 5.0, 200);
        $this->t += 61;
        $other->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(1, $other->pendingMetricWindows());
        $this->assertSame(201, $other->flushMetrics());
    }

    public function test5xxAndNetworkErrorKeepTheWindowForALaterRequest(): void
    {
        [$c, $tr] = $this->make([], [['status' => 503], ['status' => 0]]);
        $c->recordTransaction('GET /a', 5.0, 200);
        $this->assertSame(503, $this->closeWindow($c));
        $this->t += 10; // retry backoff is 30 s
        $c->recordTransaction('GET /b', 5.0, 200);
        $this->assertSame(0, $c->pendingMetricWindows());
        $this->t += 25;
        $c->recordTransaction('GET /b', 5.0, 200);
        $this->assertSame(1, $c->pendingMetricWindows());
        $this->assertSame(0, $c->flushMetrics()); // network error: kept again
        $this->t += 31;
        $c->recordTransaction('GET /b', 5.0, 200);
        $this->assertSame(201, $c->flushMetrics());
        $this->assertSame(gmdate('Y-m-d\TH:i:s\Z', 1000020), $tr->calls[2]['events']['interval_start']);
    }

    public function testAtMostFiveWindowsAreBuffered(): void
    {
        $failing = array_fill(0, 40, ['status' => 503]);
        [$c, $tr] = $this->make(['performance_window_seconds' => 1], $failing);
        $this->t = 2000000.0;
        $c->recordTransaction('GET /w0', 1.0, 200);
        for ($i = 1; $i <= 8; $i++) { // 8 closed windows, all within the 30 s backoff after the first failure
            $this->t += 1;
            $c->recordTransaction('GET /w' . $i, 1.0, 200);
            $c->flushMetrics();
        }
        $failedCalls = count($tr->calls);
        $this->assertSame(1, $failedCalls); // one attempt, then backoff
        // a recovered server receives only the newest five windows
        [$r, $ok] = $this->make(['performance_window_seconds' => 1]);
        $this->t += 40;
        $r->recordTransaction('GET /late', 1.0, 200);
        $this->assertSame(5, $r->pendingMetricWindows());
        $r->flushMetrics();
        $this->assertCount(5, $ok->calls);
        $first = $ok->calls[0]['events']['transactions'][0]['name'];
        $this->assertTrue($first !== 'GET /w0' && $first !== 'GET /w1');
    }

    // ----------------------------------------------------------------- misc

    public function testPerformanceOffRecordsNothing(): void
    {
        [$c, $tr] = $this->make(['performance' => false]);
        $this->assertFalse($c->recordTransaction('GET /a', 5.0, 200));
        $this->assertSame([], glob($this->dir . '/*_m.json') ?: []);
        $this->assertFalse($c->isPerformanceEnabled());
        [$d] = $this->make(['api_key' => '']);
        $this->assertFalse($d->recordTransaction('GET /a', 5.0, 200));
    }

    public function testPerformanceDefaultsToOffForPlainPhp(): void
    {
        $c = new Client(['api_key' => 'k', 'host' => 'https://insight.test'], new FakeTransport());
        $this->assertFalse($c->isPerformanceEnabled());
        $this->assertSame(60, $c->getOption('performance_window_seconds'));
    }

    public function testIgnoreList(): void
    {
        [$c] = $this->make();
        foreach (['/health', '/healthz', '/ping', '/status', '/metrics', '/favicon.ico', '/robots.txt', '/ready', '/live', '/up', '/storage/a.png', '/build/app.js', '/assets/x.css', 'GET /health', 'GET /up?x=1', '/health/deep'] as $v) {
            $this->assertTrue($c->shouldIgnore($v), $v);
        }
        foreach (['/users', '/upload', '/statusboard', '/items/{id}', 'GET /', 'GET <unmatched>', 'TASK App\\Jobs\\Send'] as $v) {
            $this->assertFalse($c->shouldIgnore($v), $v);
        }
        $this->assertFalse($c->recordTransaction('GET /health', 3.0, 200));
        $this->assertTrue($c->recordTransaction('GET /healthy-food', 3.0, 200));
        [$custom] = $this->make(['performance_ignore' => ['/internal/', '#^/v\d+/ping$#']]);
        $this->assertTrue($custom->shouldIgnore('/internal/x'));
        $this->assertTrue($custom->shouldIgnore('/v2/ping'));
        $this->assertFalse($custom->shouldIgnore('/health')); // custom list replaces the defaults
        [$csv] = $this->make(['performance_ignore' => '/a, /b']);
        $this->assertTrue($csv->shouldIgnore('/b/c'));
    }

    // ---------------------------------------------------------- plain PHP API

    public function testPlainPhpUnmatchedAndNamedTransactions(): void
    {
        [$c, $tr] = $this->make();
        Insight::setClient($c);
        $_SERVER['REQUEST_METHOD'] = 'GET';
        $_SERVER['REQUEST_URI'] = '/items/42?x=1';
        $_SERVER['REQUEST_TIME_FLOAT'] = microtime(true) - 0.25;
        Insight::startTransaction();
        $this->assertTrue(Insight::finishTransaction(null, 200));
        $_SERVER['REQUEST_METHOD'] = 'POST';
        $_SERVER['REQUEST_URI'] = '/orders/9';
        Insight::startTransaction();
        Insight::setTransactionName('/orders/{id}');
        $this->assertTrue(Insight::finishTransaction(null, 500));
        $this->closeWindow($c);
        $byName = [];
        foreach ($tr->calls[0]['events']['transactions'] as $t) {
            $byName[$t['name']] = $t;
        }
        $this->assertArrayHasKey('GET <unmatched>', $byName); // never the raw path
        $this->assertArrayHasKey('POST /orders/{id}', $byName);
        $this->assertSame(1, $byName['POST /orders/{id}']['errors']);
        $this->assertSame(0, $byName['GET <unmatched>']['errors']);
    }

    public function testPlainPhpIgnoresHealthPathAndDoesNotDoubleCount(): void
    {
        [$c, $tr] = $this->make();
        Insight::setClient($c);
        $_SERVER['REQUEST_METHOD'] = 'GET';
        $_SERVER['REQUEST_URI'] = '/health';
        Insight::startTransaction();
        $this->assertFalse(Insight::finishTransaction(null, 200));
        $_SERVER['REQUEST_URI'] = '/x';
        Insight::startTransaction();
        $this->assertTrue(Insight::finishTransaction('GET /x', 200));
        $this->assertFalse(Insight::finishTransaction('GET /x', 200)); // already recorded
        $this->closeWindow($c);
        $counts = array_column($tr->calls[0]['events']['transactions'], 'count', 'name');
        $this->assertSame(1, $counts['GET /x']);
    }

    public function testRecordTransactionFacadeWithoutClientIsSafe(): void
    {
        $this->assertFalse(Insight::recordTransaction('TASK x', 5.0, null, false, 'queue.task'));
    }

    public function testQueueTaskOp(): void
    {
        [$c, $tr] = $this->make();
        $c->recordTransaction('TASK App\\Jobs\\SendMail', 12.0, null, true, 'queue.task');
        $this->closeWindow($c);
        $t = $tr->calls[0]['events']['transactions'][0];
        $this->assertSame('queue.task', $t['op']);
        $this->assertSame('TASK App\\Jobs\\SendMail', $t['name']);
        $this->assertSame(1, $t['errors']);
    }

    // ---------------------------------------------------------------- Laravel

    public function testRouteTemplateNames(): void
    {
        $this->assertSame('GET /items/{id}', PerformanceMiddleware::transactionName(new FakeRequest('GET', '/items/42', new FakeRoute('items/{id}'))));
        $this->assertSame('GET /items/{id}', PerformanceMiddleware::transactionName(new FakeRequest('GET', '/items/42', new FakeRoute('/items/{id}'))));
        $this->assertSame('GET /', PerformanceMiddleware::transactionName(new FakeRequest('GET', '/', new FakeRoute('/'))));
        $this->assertSame('POST /api/orders', PerformanceMiddleware::transactionName(new FakeRequest('post', '/api/orders', new FakeRoute('api/orders'))));
        $this->assertSame('GET <unmatched>', PerformanceMiddleware::transactionName(new FakeRequest('GET', '/wp-login.php', null)));
        $fallback = new FakeRoute('{fallbackPlaceholder}');
        $fallback->isFallback = true;
        $this->assertSame('GET <unmatched>', PerformanceMiddleware::transactionName(new FakeRequest('GET', '/nope/123', $fallback)));
    }

    public function testMiddlewareRecordsStatusAndErrors(): void
    {
        [$c, $tr] = $this->make();
        Insight::setClient($c);
        $mw = new PerformanceMiddleware();
        $run = function (FakeRequest $req, int $status, bool $reported = false) use ($mw) {
            $response = $mw->handle($req, function () use ($status, $reported) {
                if ($reported) {
                    Insight::markRequestFailed();
                }

                return new FakeResponse($status);
            });
            $mw2 = new PerformanceMiddleware(); // Laravel builds a fresh instance for terminate()
            $mw2->terminate($req, $response);
        };
        $run(new FakeRequest('GET', '/items/1', new FakeRoute('items/{id}')), 200);
        $run(new FakeRequest('GET', '/items/2', new FakeRoute('items/{id}')), 404);   // 4xx is not an error
        $run(new FakeRequest('GET', '/items/3', new FakeRoute('items/{id}')), 500);
        $run(new FakeRequest('GET', '/items/4', new FakeRoute('items/{id}')), 200, true); // exception reported
        $run(new FakeRequest('GET', '/health', new FakeRoute('health')), 200);            // ignored
        $run(new FakeRequest('GET', '/scanner.php', null), 404);
        $this->closeWindow($c);
        $byName = [];
        foreach ($tr->calls[0]['events']['transactions'] as $t) {
            $byName[$t['name']] = $t;
        }
        $this->assertSame(4, $byName['GET /items/{id}']['count']);
        $this->assertSame(2, $byName['GET /items/{id}']['errors']);
        $this->assertSame(1, $byName['GET <unmatched>']['count']);
        $this->assertSame(0, $byName['GET <unmatched>']['errors']);
        $this->assertArrayNotHasKey('GET /health', $byName);
    }

    public function testMiddlewareSendsTheClaimedWindowAtTerminate(): void
    {
        [$c, $tr] = $this->make();
        Insight::setClient($c);
        $mw = new PerformanceMiddleware();
        $ok = function () {
            return new FakeResponse(200);
        };
        $req = new FakeRequest('GET', '/a', new FakeRoute('a'));
        $mw->terminate($req, $mw->handle($req, $ok));
        $this->assertCount(0, $tr->calls);
        $this->t += 61;
        $req2 = new FakeRequest('GET', '/a', new FakeRoute('a'));
        $mw->terminate($req2, $mw->handle($req2, $ok));
        $this->assertCount(1, $tr->calls); // sent from terminate(), i.e. after the response
        $this->assertSame(1, $tr->calls[0]['events']['transactions'][0]['count']);
    }

    public function testMiddlewareRethrowsExceptions(): void
    {
        [$c] = $this->make();
        Insight::setClient($c);
        $mw = new PerformanceMiddleware();
        $this->expectException(\RuntimeException::class);
        $mw->handle(new FakeRequest('GET', '/a', new FakeRoute('a')), function () {
            throw new \RuntimeException('boom');
        });
    }
}

class FakeBag
{
    public array $items = [];

    public function set($k, $v): void
    {
        $this->items[$k] = $v;
    }

    public function get($k)
    {
        return $this->items[$k] ?? null;
    }
}

class FakeRoute
{
    public bool $isFallback = false;
    private string $uri;

    public function __construct(string $uri)
    {
        $this->uri = $uri;
    }

    public function uri(): string
    {
        return $this->uri;
    }
}

class FakeRequest
{
    public FakeBag $attributes;
    private string $method;
    private string $path;
    private ?FakeRoute $route;

    public function __construct(string $method, string $path, ?FakeRoute $route)
    {
        $this->attributes = new FakeBag();
        $this->method = $method;
        $this->path = $path;
        $this->route = $route;
    }

    public function getMethod(): string
    {
        return $this->method;
    }

    public function getPathInfo(): string
    {
        return $this->path;
    }

    public function route(): ?FakeRoute
    {
        return $this->route;
    }
}

class FakeResponse
{
    private int $status;

    public function __construct(int $status)
    {
        $this->status = $status;
    }

    public function getStatusCode(): int
    {
        return $this->status;
    }
}
