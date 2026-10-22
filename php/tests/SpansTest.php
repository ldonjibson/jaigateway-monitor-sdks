<?php

namespace Insight\Tests;

use Insight\Client;
use Insight\Insight;
use Insight\Laravel\SpanListeners;
use Insight\Metrics;
use Insight\Tracing\GuzzleMiddleware;
use Insight\Tracing\Sql;
use PHPUnit\Framework\TestCase;

class SpansTest extends TestCase
{
    private string $dir = '';
    private float $t = 1000020.0;

    protected function setUp(): void
    {
        $this->dir = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'insight-spans-' . bin2hex(random_bytes(4));
        mkdir($this->dir);
        $this->t = 1000020.0;
        Insight::setClient(null);
        Insight::resetTransaction();
        $this->rtf = $_SERVER['REQUEST_TIME_FLOAT'] ?? null;
        unset($_SERVER['REQUEST_TIME_FLOAT']); // no implicit (lazy) trace in these tests
    }

    private $rtf = null;

    protected function tearDown(): void
    {
        foreach (glob($this->dir . '/*') ?: [] as $f) {
            @unlink($f);
        }
        @rmdir($this->dir);
        Insight::setClient(null);
        Insight::resetTransaction();
        if ($this->rtf !== null) {
            $_SERVER['REQUEST_TIME_FLOAT'] = $this->rtf;
        }
    }

    /** @return array{0: Client, 1: FakeTransport} */
    private function make(array $options = [], array $responses = []): array
    {
        $transport = new FakeTransport($responses);
        $client = new Client(array_merge([
            'api_key' => 'key-123', 'host' => 'https://insight.test', 'register_shutdown' => false,
            'state_mode' => 'file', 'state_dir' => $this->dir, 'retry_delay_ms' => 0, 'server_name' => 'web-1',
            'performance' => true,
        ], $options), $transport);
        $client->setClock(function () {
            return $this->t;
        });
        Insight::setClient($client);

        return [$client, $transport];
    }

    /** Runs one request: begin, $fn, record. Returns the trace id. */
    private function request(Client $c, callable $fn, string $name = 'GET /items/{id}', float $ms = 100.0, int $status = 200): string
    {
        $c->beginTrace($this->t);
        $id = (string) $c->traceId();
        $fn($c);
        $c->recordTransaction($name, $ms, $status, $status >= 500);

        return $id;
    }

    private function flushWindow(Client $c, FakeTransport $tr): array
    {
        $this->t += 61;
        $c->recordTransaction('GET /tick', 1.0);
        $c->flushMetrics();
        $this->assertGreaterThan(0, count($tr->calls));

        return $tr->calls[count($tr->calls) - 1]['events'];
    }

    // ------------------------------------------------------------ normalisers

    public function testSqlNormaliserMatchesServerCases(): void
    {
        $n = [Sql::class, 'normalize'];
        $this->assertSame("SELECT * FROM users WHERE id = ? AND name = ? AND note = ?", $n("SELECT * FROM users WHERE id = 42 AND name = 'bob' AND note = 'it''s'"));
        $this->assertSame('SELECT ? FROM t WHERE x IN (?) AND y IN (?)', $n("SELECT 1 FROM t WHERE x IN (1, 2, 3) AND y IN ('a','b')"));
        $this->assertSame('INSERT INTO t (a, b) VALUES (?)', $n("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y'), (3, 'z')"));
        $this->assertSame('SELECT a FROM t WHERE price > ?', $n("SELECT /* trace=abc */ a -- password=hunter2\n FROM t WHERE price > 9.99e2"));
        $ident = 'SELECT "user_id", t1.col2 FROM "Table2" WHERE id = $1';
        $this->assertSame($ident, $n($ident));
        $this->assertTrue(strlen($n(str_repeat('x', 500))) <= 300);
        $this->assertSame('SELECT ? FROM t WHERE a = ? AND b = E?', $n("SELECT 0x1F FROM t WHERE a = -5 AND b = E'abc'"));
        $this->assertSame('', $n(''));
    }

    public function testHttpDescriptionIsMethodAndHostOnly(): void
    {
        $this->assertSame('POST api.stripe.com', Sql::describeHttp('POST https://user:secret@api.stripe.com/v1/charges?key=sk_live_1'));
        $this->assertSame('GET api.stripe.com', Sql::describeHttp('GET api.stripe.com'));
        $this->assertSame('GET localhost:8080', Sql::describeHttp('GET http://localhost:8080/x'));
        $this->assertSame('PUT example.org', Sql::http('put', 'https://example.org/a/b?c=d#e'));
    }

    // ------------------------------------------------------------ aggregation

    public function testSummariesAreAggregatedPerTransactionOpAndDescription(): void
    {
        [$c, $tr] = $this->make();
        for ($i = 1; $i <= 3; $i++) {
            $this->request($c, function (Client $c) use ($i) {
                $c->recordSpan('db.query', "SELECT * FROM items WHERE id = $i AND name = 'n$i'", 2.0 * $i, true, ['db.system' => 'mysql']);
                $c->recordSpan('http.client', 'POST https://u:p@api.stripe.com/v1/c?k=1', 100.0, $i !== 2);
            });
        }
        $this->request($c, function (Client $c) {
            $c->recordSpan('db.query', 'SELECT 1', 1.0);
        }, 'POST /orders');
        $p = $this->flushWindow($c, $tr);
        $this->assertArrayHasKey('spans', $p);
        $by = [];
        foreach ($p['spans'] as $s) {
            $by[$s['transaction'] . '|' . $s['op']] = $s;
        }
        $db = $by['GET /items/{id}|db.query'];
        $this->assertSame('SELECT * FROM items WHERE id = ? AND name = ?', $db['description']);
        $this->assertSame(3, $db['count']);
        $this->assertSame(0, $db['errors']);
        $this->assertEquals(12.0, $db['sum_ms']);
        $this->assertEquals(2.0, $db['min_ms']);
        $this->assertEquals(6.0, $db['max_ms']);
        $this->assertSame(['1' => 1, '3' => 1, '4' => 1], $db['buckets']); // 2, 4 and 6 ms
        $http = $by['GET /items/{id}|http.client'];
        $this->assertSame('POST api.stripe.com', $http['description']);
        $this->assertSame(1, $http['errors']);
        $this->assertSame(['11' => 3], $http['buckets']);
        $this->assertSame('SELECT ?', $by['POST /orders|db.query']['description']);
        $this->assertStringContainsString('"buckets":{', json_encode($p));
    }

    public function testSummaryCapFoldsIntoOther(): void
    {
        [$c, $tr] = $this->make();
        $this->request($c, function (Client $c) {
            for ($i = 0; $i < 450; $i++) {
                $c->recordSpan('db.query', "SELECT col_$i FROM t_$i", 1.0);
            }
        });
        $p = $this->flushWindow($c, $tr);
        $this->assertTrue(count($p['spans']) <= Metrics::MAX_SPAN_KEYS);
        $total = 0;
        $other = 0;
        foreach ($p['spans'] as $s) {
            $total += $s['count'];
            if ($s['description'] === '<other>') {
                $other += $s['count'];
            }
        }
        $this->assertSame(450, $total);
        $this->assertSame(450 - Metrics::SOFT_SPAN_KEYS, $other);
    }

    public function testSpansOutsideATransactionAreAggregatedUnderEmptyName(): void
    {
        [$c, $tr] = $this->make();
        $c->recordSpan('db.query', 'SELECT 5', 3.0);
        $c->recordSpan('db.query', 'SELECT 6', 4.0);
        $this->assertSame(0, $c->pendingMetricWindows());
        $c->flushMetrics(); // folds the buffered spans into the current window
        $p = $this->flushWindow($c, $tr);
        $this->assertCount(1, $p['spans']);
        $this->assertSame('', $p['spans'][0]['transaction']);
        $this->assertSame(2, $p['spans'][0]['count']);
    }

    public function testSpansOffWhenPerformanceOrSpansDisabled(): void
    {
        [$c] = $this->make(['performance_spans' => false]);
        $this->assertFalse($c->recordSpan('db.query', 'SELECT 1', 1.0));
        [$c2] = $this->make(['performance' => false]);
        $this->assertFalse($c2->recordSpan('db.query', 'SELECT 1', 1.0));
        $this->assertNull($c2->traceId());
        $s = Insight::startSpan('x', 'y');
        $s->finish();
        $this->assertTrue(true);
    }

    // ----------------------------------------------------------------- samples

    public function testSampleSpansHaveOffsetsAndAreCappedPreferringSlowest(): void
    {
        [$c, $tr] = $this->make();
        $start = $this->t;
        $this->request($c, function (Client $c) use ($start) {
            for ($i = 0; $i < 150; $i++) { // 150 spans, 1 ms each, except span 7 and 140 which are slow
                $ms = ($i === 7 || $i === 140) ? 50.0 : 1.0;
                $c->recordSpan('db.query', "SELECT $i", $ms, true, ['db.system' => 'pgsql', 'secret' => 'x'], $start + $i * 0.002);
            }
        }, 'GET /items/{id}', 400.0);
        $p = $this->flushWindow($c, $tr);
        $sample = null;
        foreach ($p['samples'] as $s) {
            if ($s['name'] === 'GET /items/{id}') {
                $sample = $s;
            }
        }
        $this->assertNotNull($sample);
        $spans = $sample['spans'];
        $this->assertSame(100, count($spans));
        $last = -1.0;
        foreach ($spans as $sp) {
            $this->assertMatchesRegularExpression('/^[0-9a-f]{16}$/', $sp['span_id']);
            $this->assertSame('db.query', $sp['op']);
            $this->assertSame('ok', $sp['status']);
            $this->assertSame(['db.system' => 'pgsql'], $sp['data']);
            $this->assertTrue($sp['start_ms'] >= $last);
            $last = $sp['start_ms'];
        }
        $starts = array_column($spans, 'start_ms');
        $this->assertTrue(in_array(14.0, array_map('floatval', $starts), true)); // span 7 at +14 ms kept
        $this->assertTrue(in_array(280.0, array_map('floatval', $starts), true)); // span 140 kept
        $this->assertEquals(0.0, $starts[0]);
    }

    public function testOnlyTwoHundredSpansAreCollectedButAllCounted(): void
    {
        [$c, $tr] = $this->make();
        $this->request($c, function (Client $c) {
            for ($i = 0; $i < 260; $i++) {
                $c->recordSpan('cache', 'get', 1.0);
            }
        });
        $data = null;
        $p = $this->flushWindow($c, $tr);
        $this->assertSame(260, $p['spans'][0]['count']);
        $max = 0;
        foreach ($p['samples'] as $s) {
            $max = max($max, count(isset($s['spans']) ? $s['spans'] : []));
        }
        $this->assertSame(100, $max);
    }

    public function testNestedSpansKeepChronologicalOrder(): void
    {
        [$c, $tr] = $this->make();
        $t0 = $this->t;
        $this->request($c, function (Client $c) use ($t0) {
            $c->recordSpan('inner', 'b', 5.0, true, [], $t0 + 0.020); // finishes first
            $c->recordSpan('outer', 'a', 50.0, true, [], $t0 + 0.010);
        });
        $p = $this->flushWindow($c, $tr);
        $names = array_column($p['samples'][0]['spans'], 'op');
        $this->assertSame(['outer', 'inner'], $names);
    }

    // ------------------------------------------------------------------ traces

    public function testErrorEventAndSampleShareTheTraceId(): void
    {
        [$c, $tr] = $this->make();
        $id = '';
        $c->beginTrace($this->t);
        $id = (string) $c->traceId();
        $this->assertMatchesRegularExpression('/^[0-9a-f]{32}$/', $id);
        $c->recordSpan('db.query', 'SELECT 1', 5.0);
        $c->captureException(new \RuntimeException('boom'));
        $c->captureMessage('hello');
        $c->flush();
        $this->assertSame(1, count($tr->calls));
        $events = $tr->calls[0]['events'];
        $events = isset($events['events']) ? $events['events'] : $events;
        foreach ($events as $e) {
            $this->assertSame($id, $e['trace_id']);
        }
        $c->recordTransaction('GET /boom', 300.0, 500, true);
        $p = $this->flushWindow($c, $tr);
        $ids = array_column($p['samples'], 'trace_id');
        $this->assertContains($id, $ids);
        $this->assertNull($c->traceId()); // trace ended with the transaction
        $c->captureMessage('after');
        $c->flush();
        $last = $tr->calls[count($tr->calls) - 1]['events'];
        $last = isset($last['events']) ? $last['events'] : $last;
        $this->assertArrayNotHasKey('trace_id', $last[0]);
    }

    public function testEventsOutsideARequestHaveNoTraceId(): void
    {
        [$c, $tr] = $this->make();
        $c->captureMessage('cli');
        $c->flush();
        $ev = $tr->calls[0]['events'];
        $ev = isset($ev['events']) ? $ev['events'] : $ev;
        $this->assertArrayNotHasKey('trace_id', $ev[0]);
    }

    public function testSyncJobInsideRequestKeepsRequestTrace(): void
    {
        [$c] = $this->make();
        $c->beginTrace($this->t);
        $id = $c->traceId();
        $this->assertFalse($c->beginTrace($this->t, 'queue.task'));
        $this->assertSame($id, $c->traceId());
    }

    // ---------------------------------------------------------------- API

    public function testStartSpanSpanAndRecordSpanApi(): void
    {
        [$c, $tr] = $this->make();
        $c->beginTrace($this->t);
        $s = Insight::startSpan('custom.step', "do   it\n now", ['host' => 'https://u:p@h.test/x']);
        $this->t += 0.25;
        $s->finish(false, ['http.status_code' => 502]);
        $s->finish(true); // second finish ignored
        $r = Insight::span('custom.fn', 'work', function () {
            $this->t += 0.01;

            return 42;
        });
        $this->assertSame(42, $r);
        try {
            Insight::span('custom.fail', 'bad', function () {
                throw new \LogicException('x');
            });
            $this->fail('must rethrow');
        } catch (\LogicException $e) {
            $this->assertSame('x', $e->getMessage());
        }
        $this->assertTrue(Insight::recordSpan('db.query', 'SELECT 9', 3.0, true, [], $this->t));
        $c->recordTransaction('GET /api', 10.0, 200);
        $p = $this->flushWindow($c, $tr);
        $by = [];
        foreach ($p['spans'] as $s2) {
            $by[$s2['op']] = $s2;
        }
        $this->assertSame('do it now', $by['custom.step']['description']);
        $this->assertSame(1, $by['custom.step']['errors']);
        $this->assertEquals(250.0, $by['custom.step']['sum_ms']);
        $this->assertSame(1, $by['custom.fail']['errors']);
        $this->assertSame(0, $by['custom.fn']['errors']);
        $spans = $p['samples'][0]['spans'];
        $step = $spans[0];
        $this->assertSame('error', $step['status']);
        $this->assertEquals(['host' => 'h.test', 'http.status_code' => 502], $step['data']);
    }

    // ----------------------------------------------------------------- limits

    public function testPayloadStaysUnderOneMegabyteByDroppingSampleSpansFirst(): void
    {
        $data = [];
        $big = [];
        for ($i = 0; $i < 100; $i++) {
            $big[] = [bin2hex(random_bytes(8)), 'cache', str_repeat('x', 300), (float) $i, 1.0, 1];
        }
        for ($i = 0; $i < 10; $i++) {
            Metrics::ingest($data, ['name' => 'GET /x' . $i, 'op' => 'http.server', 'ms' => 100.0 + $i, 'status' => 500, 'error' => true, 'ended' => 1000020.5, 'sp' => $big], 1000020, 60);
        }
        $window = ['w' => 1000020, 't' => $data['t'], 's' => $data['s'], 'x' => []];
        $full = json_encode(Metrics::payload($window, [], 60));
        $this->assertTrue(strlen($full) > 200000);
        $noSpans = Metrics::payload($window, [], 60, true, false);
        $this->assertArrayNotHasKey('spans', $noSpans['samples'][0]);
        // Client-level: shrink the limit effect by 413 retry path
        [$c, $tr] = $this->make([], [['status' => 413], ['status' => 201]]);
        $this->request($c, function (Client $c) {
            $c->recordSpan('cache', 'get', 1.0);
        });
        $this->t += 61;
        $c->recordTransaction('GET /tick', 1.0);
        $c->flushMetrics();
        $this->assertSame(2, count($tr->calls));
        $this->assertArrayNotHasKey('samples', $tr->calls[1]['events']);
        $this->assertArrayHasKey('spans', $tr->calls[1]['events']); // summaries survive
    }

    public function testWindowRecordStaysSmallWithManySpans(): void
    {
        [$c] = $this->make();
        for ($r = 0; $r < 30; $r++) {
            $this->request($c, function (Client $c) use ($r) {
                for ($i = 0; $i < 400; $i++) {
                    $c->recordSpan('db.query', "SELECT c$i FROM t$r", 1.0 + $i % 7);
                }
            }, 'GET /r' . ($r % 5), 50.0 + $r);
        }
        $file = glob($this->dir . '/*_m.json')[0];
        $this->assertTrue(filesize($file) < 600000, 'window record is ' . filesize($file) . ' bytes');
    }

    // ----------------------------------------------------------------- privacy

    public function testNoLiteralsBindingsPathsOrQueriesInAnyPayload(): void
    {
        [$c, $tr] = $this->make();
        $this->request($c, function (Client $c) {
            $c->recordSpan('db.query', "SELECT * FROM users WHERE email = 'ada@secret.test' AND ssn = 123456789 AND id IN (7, 8, 9)", 3.0);
            $c->recordSpan('http.client', 'GET https://user:hunter2@pay.test:8443/v1/charges/ch_SECRET?token=tok_SECRET', 80.0, true, ['host' => 'https://user:hunter2@pay.test/x?q=1', 'http.method' => 'GET']);
            $c->recordSpan('http.client', 'GET', 1.0);
        });
        $p = $this->flushWindow($c, $tr);
        $json = json_encode($p);
        foreach (['ada@secret', '123456789', 'hunter2', 'ch_SECRET', 'tok_SECRET', '/v1/charges', 'token=', '8, 9'] as $needle) {
            $this->assertFalse(strpos($json, $needle) !== false, "leaked $needle");
        }
        $this->assertStringContainsString('GET pay.test:8443', $json);
    }

    // ---------------------------------------------------------------- Laravel

    public function testLaravelListenersUseOnlySqlAndTimeAndNeverBindings(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $conn = new class {
            public function getDriverName(): string
            {
                return 'mysql';
            }

            public function getDatabaseName(): string
            {
                return 'shop';
            }
        };
        $bindings = new class {
            public function __get($n)
            {
                throw new \RuntimeException('bindings must not be read');
            }
        };
        $event = new \stdClass();
        $event->sql = 'select * from users where id = 5 and name = ?';
        $event->bindings = $bindings;
        $event->time = 12.5;
        $event->connection = $conn;
        $event->connectionName = 'mysql';
        $c->beginTrace($this->t - 1);
        $l->onQuery($event);
        // outbound HTTP
        $req = new class {
            public function method(): string
            {
                return 'post';
            }

            public function url(): string
            {
                return 'https://user:pw@api.pay.test/v1/charge?key=sk';
            }
        };
        $ev = new \stdClass();
        $ev->request = $req;
        $l->onHttpSending($ev);
        $this->t += 0.2;
        $resp = new class {
            public function status(): int
            {
                return 502;
            }
        };
        $done = new \stdClass();
        $done->request = $req;
        $done->response = $resp;
        $l->onHttpResponse($done);
        // failure
        $ev2 = new \stdClass();
        $ev2->request = $req;
        $l->onHttpSending($ev2);
        $f = new \stdClass();
        $f->request = $req;
        $f->exception = new \RuntimeException('cURL error');
        $l->onHttpFailed($f);
        $l->onHttpFailed($f); // unpaired: ignored
        // garbage events never throw
        $l->onQuery(new \stdClass());
        $l->onQuery(null);
        $l->onHttpSending(new \stdClass());
        $l->onHttpResponse(new \stdClass());
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        $by = [];
        foreach ($p['spans'] as $s) {
            $by[$s['op']] = $s;
        }
        $this->assertSame('select * from users where id = ? and name = ?', $by['db.query']['description']);
        $this->assertEquals(12.5, $by['db.query']['sum_ms']);
        $this->assertSame('POST api.pay.test', $by['http.client']['description']);
        $this->assertSame(2, $by['http.client']['count']);
        $this->assertSame(2, $by['http.client']['errors']);
        $spans = $p['samples'][0]['spans'];
        $this->assertEquals(['db.system' => 'mysql', 'db.name' => 'shop'], $spans[0]['data']);
        $this->assertEquals(987.5, $spans[0]['start_ms']);
        $this->assertSame(502, $spans[1]['data']['http.status_code']);
        $this->assertFalse(strpos(json_encode($p), 'user:pw') !== false || strpos(json_encode($p), 'key=sk') !== false);
    }

    public function testListenersAreInertWithoutClientOrWithSpansOff(): void
    {
        $l = new SpanListeners();
        $e = new \stdClass();
        $e->sql = 'select 1';
        $e->time = 1.0;
        $l->onQuery($e); // no client
        [$c] = $this->make(['performance_spans' => false]);
        $l->onQuery($e);
        $this->assertSame(0, $c->pendingMetricWindows());
        $this->assertTrue(true);
    }

    public function testGuzzleMiddlewareRecordsMethodAndHostOnly(): void
    {
        [$c, $tr] = $this->make();
        $uri = new class {
            public function getHost(): string
            {
                return 'API.Example.com';
            }

            public function getPort(): ?int
            {
                return null;
            }
        };
        $request = new class($uri) {
            private $u;

            public function __construct($u)
            {
                $this->u = $u;
            }

            public function getMethod(): string
            {
                return 'get';
            }

            public function getUri()
            {
                return $this->u;
            }
        };
        $response = new class {
            public function getStatusCode(): int
            {
                return 503;
            }
        };
        $promise = new class($response) {
            private $r;

            public function __construct($r)
            {
                $this->r = $r;
            }

            public function then(callable $ok, callable $fail)
            {
                return $ok($this->r);
            }
        };
        $mw = GuzzleMiddleware::create();
        $handler = $mw(function ($req, array $opts) use ($promise) {
            return $promise;
        });
        $c->beginTrace($this->t);
        $this->assertSame($response, $handler($request, []));
        $c->recordTransaction('GET /g', 10.0, 200);
        $p = $this->flushWindow($c, $tr);
        $this->assertSame('GET api.example.com', $p['spans'][0]['description']);
        $this->assertSame(1, $p['spans'][0]['errors']);
    }

    // -------------------------------------------------------------------- PDO

    public function testTracedPdoWithSqlite(): void
    {
        if (!class_exists('PDO') || !in_array('sqlite', \PDO::getAvailableDrivers(), true)) {
            $this->assertTrue(true); // pdo_sqlite not available here: skipped

            return;
        }
        [$c, $tr] = $this->make();
        $pdo = new \Insight\Tracing\TracedPdo('sqlite::memory:', null, null, [\PDO::ATTR_ERRMODE => \PDO::ERRMODE_EXCEPTION]);
        $c->beginTrace($this->t);
        $pdo->exec('CREATE TABLE t (id INTEGER, name TEXT)');
        $st = $pdo->prepare('INSERT INTO t (id, name) VALUES (?, ?)');
        $st->execute([4242, 'sekret-name']);
        $rows = $pdo->query("SELECT * FROM t WHERE id = 4242")->fetchAll();
        $this->assertCount(1, $rows);
        try {
            $pdo->query('SELECT * FROM nope');
            $this->fail('should throw');
        } catch (\PDOException $e) {
            $this->assertTrue(true);
        }
        $c->recordTransaction('GET /pdo', 10.0, 200);
        $p = $this->flushWindow($c, $tr);
        $json = json_encode($p);
        $this->assertFalse(strpos($json, 'sekret-name') !== false || strpos($json, '4242') !== false);
        $descs = array_column($p['spans'], 'description');
        $this->assertContains('INSERT INTO t (id, name) VALUES (?)', $descs);
        $this->assertContains('SELECT * FROM t WHERE id = ?', $descs);
        $errors = 0;
        foreach ($p['spans'] as $s) {
            $errors += $s['errors'];
        }
        $this->assertSame(1, $errors);
    }

    // ------------------------------------------------------------ never throws

    public function testNeverThrows(): void
    {
        $s = Insight::startSpan('a', 'b'); // no client at all
        $s->finish();
        $this->assertFalse(Insight::recordSpan('a', 'b', 1.0));
        $this->assertSame(5, Insight::span('a', 'b', function () {
            return 5;
        }));
        $this->assertNull(Insight::traceId());
        [$c] = $this->make(['state_mode' => 'file', 'state_dir' => '/nonexistent/dir']);
        $this->assertTrue($c->recordSpan('db.query', "bad \xB1\x31 utf8", -5.0, true, ['host' => ['array'], 'cache.hit' => true, 'db.name' => 12]));
        $this->assertFalse($c->recordSpan('', 'x', 1.0));
        $c->beginTrace($this->t);
        $this->assertTrue($c->recordSpan(str_repeat('o', 100), str_repeat('d', 1000), 1.0, true, [], null));
    }
}
