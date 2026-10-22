<?php

namespace Insight\Tests;

use Insight\Client;
use Insight\Insight;
use Insight\Laravel\PerformanceMiddleware;
use Insight\Laravel\SpanListeners;
use Insight\Metrics;
use Insight\Tracing\GuzzleMiddleware;
use PHPUnit\Framework\TestCase;

/** Minimal PSR-7-like request: immutable withHeader/hasHeader/getHeaderLine. */
class TracingFakeRequest
{
    public array $headers = [];
    private string $url;
    private string $method;

    public function __construct(string $url, string $method = 'GET', array $headers = [])
    {
        $this->url = $url;
        $this->method = $method;
        $this->headers = $headers;
    }

    public function getMethod(): string
    {
        return $this->method;
    }

    public function getUri()
    {
        $p = parse_url($this->url);

        return new class($p) {
            private array $p;

            public function __construct(array $p)
            {
                $this->p = $p;
            }

            public function getHost(): string
            {
                return (string) ($this->p['host'] ?? '');
            }

            public function getPort(): ?int
            {
                return isset($this->p['port']) ? (int) $this->p['port'] : null;
            }
        };
    }

    public function hasHeader(string $n): bool
    {
        return isset($this->headers[strtolower($n)]);
    }

    public function getHeaderLine(string $n): string
    {
        return $this->headers[strtolower($n)] ?? '';
    }

    public function withHeader(string $n, string $v): self
    {
        $c = clone $this;
        $c->headers[strtolower($n)] = $v;

        return $c;
    }
}

class TracingTest extends TestCase
{
    private const TID = '4bf92f3577b34da6a3ce929d0e0e4736';
    private const PID = '00f067aa0ba902b7';

    private string $dir = '';
    private float $t = 1000020.0;
    private $rtf = null;

    protected function setUp(): void
    {
        $this->dir = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'insight-trc-' . bin2hex(random_bytes(4));
        mkdir($this->dir);
        $this->t = 1000020.0;
        Insight::setClient(null);
        Insight::resetTransaction();
        $this->rtf = $_SERVER['REQUEST_TIME_FLOAT'] ?? null;
        unset($_SERVER['REQUEST_TIME_FLOAT'], $_SERVER['HTTP_TRACEPARENT']);
    }

    protected function tearDown(): void
    {
        foreach (glob($this->dir . '/*') ?: [] as $f) {
            @unlink($f);
        }
        @rmdir($this->dir);
        Insight::setClient(null);
        Insight::resetTransaction();
        unset($_SERVER['HTTP_TRACEPARENT']);
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

    /** Closes the window and returns the metrics bodies sent (first = the window itself). */
    private function flushWindow(Client $c, FakeTransport $tr): array
    {
        $before = count($tr->calls);
        $this->t += 61;
        $c->recordTransaction('GET /tick', 1.0);
        $c->flushMetrics();
        $this->assertGreaterThan($before, count($tr->calls));
        $bodies = [];
        foreach (array_slice($tr->calls, $before) as $call) {
            $bodies[] = $call['events'];
        }

        return $bodies;
    }

    private function header(string $trace = self::TID, string $span = self::PID, string $flags = '01'): string
    {
        return '00-' . $trace . '-' . $span . '-' . $flags;
    }

    // ---------------------------------------------------------------- parsing

    public function testParseTraceparent(): void
    {
        $ok = Client::parseTraceparent($this->header());
        $this->assertSame(['trace_id' => self::TID, 'parent_id' => self::PID, 'sampled' => true], $ok);
        $this->assertSame(false, Client::parseTraceparent($this->header(self::TID, self::PID, '00'))['sampled']);
        $this->assertSame(true, Client::parseTraceparent($this->header(self::TID, self::PID, '03'))['sampled']);
        $this->assertSame(false, Client::parseTraceparent($this->header(self::TID, self::PID, '02'))['sampled']);
        $up = Client::parseTraceparent(strtoupper($this->header()));
        $this->assertSame(self::TID, $up['trace_id']); // ids are normalised to lower case
        $this->assertSame(self::PID, $up['parent_id']);
        $this->assertNotNull(Client::parseTraceparent('  ' . $this->header() . " \n"));

        $bad = [
            '', 'garbage', '00-', '00--' . self::PID . '-01',
            '01-' . self::TID . '-' . self::PID . '-01',                       // wrong version
            'ff-' . self::TID . '-' . self::PID . '-01',
            '00-' . str_repeat('0', 32) . '-' . self::PID . '-01',             // zero trace id
            '00-' . self::TID . '-' . str_repeat('0', 16) . '-01',             // zero parent id
            '00-' . substr(self::TID, 0, 31) . '-' . self::PID . '-01',        // short trace id
            '00-' . self::TID . 'a-' . self::PID . '-01',                      // long trace id
            '00-' . self::TID . '-' . substr(self::PID, 1) . '-01',            // short parent id
            '00-' . self::TID . '-' . self::PID . '-1',                        // short flags
            '00-' . self::TID . '-' . self::PID . '-zz',                       // non hex flags
            '00-' . substr(self::TID, 0, 31) . 'g-' . self::PID . '-01',       // non hex id
            $this->header() . '-extra',                                        // version 00 has exactly 4 fields
            str_repeat('a', 5000),
            "00-\0-\0-01",
        ];
        foreach ($bad as $h) {
            $this->assertNull(Client::parseTraceparent($h), 'should reject ' . json_encode($h));
        }
    }

    // --------------------------------------------------------------- adoption

    public function testInboundHeaderIsAdoptedAndSampleCarriesTheIds(): void
    {
        [$c, $tr] = $this->make();
        $this->assertTrue($c->beginTrace($this->t, 'http.server', $this->header()));
        $this->assertSame(self::TID, $c->traceId());
        $c->recordTransaction('GET /items/{id}', 100.0, 200);
        $samples = $this->flushWindow($c, $tr)[0]['samples'];
        $mine = null;
        foreach ($samples as $s) {
            if ($s['trace_id'] === self::TID) {
                $mine = $s;
            }
        }
        $this->assertNotNull($mine);
        $this->assertSame('server', $mine['kind']);
        $this->assertSame('http.server', $mine['op']);
        $this->assertSame(self::PID, $mine['parent_span_id']);
        $this->assertMatchesRegularExpression('/^[0-9a-f]{16}$/', $mine['span_id']);
        $this->assertNotSame(self::PID, $mine['span_id']);
    }

    public function testNoHeaderStartsANewTraceWithoutParent(): void
    {
        [$c, $tr] = $this->make();
        $c->beginTrace($this->t);
        $id = (string) $c->traceId();
        $this->assertMatchesRegularExpression('/^[0-9a-f]{32}$/', $id);
        $c->recordTransaction('GET /a', 5.0, 200);
        $s = $this->flushWindow($c, $tr)[0]['samples'][0];
        $this->assertSame($id, $s['trace_id']);
        $this->assertSame('server', $s['kind']);
        $this->assertSame('http.server', $s['op']);
        $this->assertMatchesRegularExpression('/^[0-9a-f]{16}$/', $s['span_id']);
        $this->assertArrayNotHasKey('parent_span_id', $s);
    }

    public function testInvalidHeaderStartsANewTrace(): void
    {
        [$c] = $this->make();
        $c->beginTrace($this->t, 'http.server', '00-' . str_repeat('0', 32) . '-' . self::PID . '-01');
        $this->assertNotSame(str_repeat('0', 32), $c->traceId());
        $this->assertMatchesRegularExpression('/^00-[0-9a-f]{32}-[0-9a-f]{16}-00$/', (string) $c->traceparent());
    }

    public function testPlainPhpReadsServerVariable(): void
    {
        $_SERVER['HTTP_TRACEPARENT'] = strtoupper($this->header());
        [$c, $tr] = $this->make();
        $c->beginTrace($this->t); // what Insight::startTransaction() does
        $this->assertSame(self::TID, $c->traceId());
        $c->recordTransaction('GET /plain', 1.0, 200);
        $s = $this->flushWindow($c, $tr)[0]['samples'];
        $this->assertSame(self::PID, $s[0]['parent_span_id']);
        $this->assertSame(self::TID, $s[0]['trace_id']);
    }

    public function testExplicitEmptyHeaderIgnoresServerVariable(): void
    {
        $_SERVER['HTTP_TRACEPARENT'] = $this->header();
        [$c] = $this->make();
        $c->beginTrace($this->t, 'http.server', ''); // what the Laravel middleware passes when the request has no header
        $this->assertNotSame(self::TID, $c->traceId());
    }

    public function testErrorEventCarriesTheAdoptedTraceId(): void
    {
        [$c, $tr] = $this->make();
        $c->beginTrace($this->t, 'http.server', $this->header());
        $this->assertTrue($c->captureException(new \RuntimeException('boom')));
        $this->assertTrue($c->captureMessage('hello'));
        $c->flush();
        $events = $tr->calls[0]['events'];
        $this->assertCount(2, $events);
        $this->assertSame(self::TID, $events[0]['trace_id']);
        $this->assertSame(self::TID, $events[1]['trace_id']);
    }

    public function testQueueWorkContinuesATrace(): void
    {
        [$c, $tr] = $this->make();
        $this->assertTrue(Insight::continueTrace($this->header()));
        $this->assertSame(self::TID, Insight::traceId());
        $c->recordTransaction('TASK App\Jobs\Pay', 20.0, null, false, 'queue.task');
        $s = $this->flushWindow($c, $tr)[0]['samples'];
        $found = null;
        foreach ($s as $x) {
            if ($x['trace_id'] === self::TID) {
                $found = $x;
            }
        }
        $this->assertNotNull($found);
        $this->assertSame('queue.task', $found['op']);
        $this->assertSame('server', $found['kind']);
        $this->assertSame(self::PID, $found['parent_span_id']);
        $this->assertTrue(isset($found['span_id']));
    }

    public function testContinueTraceInsideARunningTraceAdoptsInPlace(): void
    {
        [$c] = $this->make();
        $c->beginTrace($this->t, 'queue.task'); // Laravel JobProcessing
        $own = $c->traceparent();
        $this->assertTrue($c->continueTrace($this->header(self::TID, self::PID, '01')));
        $this->assertSame(self::TID, $c->traceId());
        $this->assertSame(substr($own, 36, 16), substr((string) $c->traceparent(), 36, 16)); // same root span id
        $this->assertSame('00-' . self::TID . '-' . substr($own, 36, 16) . '-01', $c->traceparent());
    }

    public function testTraceparentFacade(): void
    {
        $this->assertNull(Insight::traceparent()); // no client
        [$c] = $this->make();
        $this->assertNull(Insight::traceparent()); // no trace (CLI)
        $c->beginTrace($this->t, 'http.server', $this->header(self::TID, self::PID, '00'));
        $v = (string) Insight::traceparent();
        $this->assertMatchesRegularExpression('/^00-' . self::TID . '-[0-9a-f]{16}-00$/', $v);
        $this->assertFalse(strpos($v, self::PID) !== false);
    }

    // ---------------------------------------------------------- forced samples

    /** Four slow requests first (slowest 3 + one more), then a fast sampled/unsampled one. */
    private function warm(Client $c): void
    {
        for ($i = 0; $i < 4; $i++) {
            $c->beginTrace($this->t);
            $c->recordTransaction('GET /slow', 500.0 + $i, 200);
        }
    }

    public function testSampledFlagForcesAFastSuccessfulRequest(): void
    {
        mt_srand(11);
        [$c, $tr] = $this->make();
        $this->warm($c);
        $c->beginTrace($this->t, 'http.server', $this->header(self::TID, self::PID, '01'));
        $c->recordTransaction('GET /fast', 1.0, 200);
        $samples = $this->flushWindow($c, $tr)[0]['samples'];
        $ids = array_column($samples, 'trace_id');
        $this->assertContains(self::TID, $ids);
        $this->assertSame(self::TID, $samples[0]['trace_id']); // forced samples come first
    }

    public function testFlagZeroIsNotForced(): void
    {
        mt_srand(11);
        [$c, $tr] = $this->make();
        $this->warm($c);
        $c->beginTrace($this->t, 'http.server', $this->header(self::TID, self::PID, '00'));
        $c->recordTransaction('GET /fast', 1.0, 200);
        $samples = $this->flushWindow($c, $tr)[0]['samples'];
        $ids = array_column($samples, 'trace_id');
        $this->assertFalse(in_array(self::TID, $ids, true));
        // and at the window level nothing was forced
        $data = [];
        Metrics::ingest($data, ['name' => 'GET /x', 'op' => 'http.server', 'ms' => 1.0, 'status' => 200, 'error' => false, 'ended' => 1000020.0, 'trace_id' => self::TID, 'span_id' => self::PID], 1000020, 60);
        $this->assertArrayNotHasKey('f', $data['s']);
    }

    public function testForcedSamplesAreCappedAt20AndAllReachTheServer(): void
    {
        [$c, $tr] = $this->make();
        $want = [];
        for ($i = 0; $i < 25; $i++) {
            $tid = sprintf('%032x', 0xabc000 + $i);
            if ($i < 20) {
                $want[] = $tid;
            }
            $c->beginTrace($this->t, 'http.server', $this->header($tid, self::PID, '01'));
            $c->recordTransaction('GET /fast', 1.0, 200);
        }
        $bodies = $this->flushWindow($c, $tr);
        $got = [];
        foreach ($bodies as $i => $b) {
            $this->assertTrue(count($b['samples']) <= Metrics::SAMPLES_PER_BODY, 'the server reads 10 samples per body');
            foreach ($b['samples'] as $s) {
                $got[$s['trace_id']] = true;
            }
            if ($i > 0) {
                $this->assertSame([], $b['transactions']); // extra bodies carry samples only
            }
        }
        foreach ($want as $tid) {
            $this->assertArrayHasKey($tid, $got);
        }
        $forced = 0;
        foreach (array_keys($got) as $tid) {
            if (strpos($tid, '0000000000000000000000000') === 0) {
                $forced++;
            }
        }
        $this->assertTrue($forced === 20, 'exactly 20 forced samples, got ' . $forced); // #21..25 were not kept as forced
        $this->assertTrue(count($got) <= 30);
    }

    public function testForcedSamplesAreTheLastToGoWhenThePayloadIsTooBig(): void
    {
        [$c, $tr] = $this->make([], [['status' => 413]]);
        $this->warm($c);
        $c->beginTrace($this->t, 'http.server', $this->header(self::TID, self::PID, '01'));
        $c->recordSpan('db.query', 'SELECT 1', 2.0);
        $c->recordTransaction('GET /fast', 1.0, 200);
        $bodies = $this->flushWindow($c, $tr); // first POST gets 413, the retry drops the ordinary samples
        $this->assertCount(2, $bodies);
        $this->assertTrue(count($bodies[0]['samples']) > 1);
        $this->assertCount(1, $bodies[1]['samples']);
        $this->assertSame(self::TID, $bodies[1]['samples'][0]['trace_id']);
        $this->assertArrayNotHasKey('spans', $bodies[1]['samples'][0]);
        $this->assertSame($bodies[0]['transactions'], $bodies[1]['transactions']);
    }

    public function testSamplesLevelsKeepForcedFirst(): void
    {
        $f = ['trace_id' => 'f', 'si' => 'a', 'name' => 'n', 'duration_ms' => 1.0, 'status_code' => 200, 'ok' => true, 'started_at' => 'x', 'sp' => [['aaaaaaaaaaaaaaaa', 'db.query', 'SELECT ?', 0.0, 1.0, 1]]];
        $o = ['trace_id' => 'o', 'si' => 'b', 'name' => 'n', 'duration_ms' => 9.0, 'status_code' => 200, 'ok' => true, 'started_at' => 'x', 'sp' => $f['sp']];
        $s = ['slow' => [$o], 'fail' => [], 'rand' => null, 'f' => [$f]];
        $l0 = Metrics::samplesAt($s, 0);
        $this->assertSame(['f', 'o'], array_column($l0, 'trace_id'));
        $this->assertArrayHasKey('spans', $l0[0]);
        $this->assertArrayHasKey('spans', $l0[1]);
        $l1 = Metrics::samplesAt($s, 1);
        $this->assertArrayHasKey('spans', $l1[0]);
        $this->assertArrayNotHasKey('spans', $l1[1]);
        $l2 = Metrics::samplesAt($s, 2);
        $this->assertArrayNotHasKey('spans', $l2[0]);
        $this->assertCount(2, $l2);
        $this->assertSame(['f'], array_column(Metrics::samplesAt($s, 3), 'trace_id'));
        $this->assertSame([], Metrics::samplesAt($s, 4));
        $this->assertCount(2, Metrics::samples($s)); // the old entry point keeps working
    }

    public function testSameTraceHitTwiceKeepsBothSamples(): void
    {
        [$c, $tr] = $this->make();
        for ($i = 0; $i < 2; $i++) {
            $c->beginTrace($this->t, 'http.server', $this->header());
            $c->recordTransaction('GET /twice', 5.0, 200);
        }
        $samples = $this->flushWindow($c, $tr)[0]['samples'];
        $mine = array_values(array_filter($samples, function ($s) {
            return $s['trace_id'] === self::TID;
        }));
        $this->assertCount(2, $mine);
        $this->assertNotSame($mine[0]['span_id'], $mine[1]['span_id']);
    }

    public function testWindowRecordStaysSmall(): void
    {
        $data = [];
        $sp = [];
        for ($i = 0; $i < 100; $i++) {
            $sp[] = [bin2hex(random_bytes(8)), 'db.query', 'SELECT ?', (float) $i, 1.0, 1];
        }
        for ($i = 0; $i < 40; $i++) {
            Metrics::ingest($data, ['name' => 'GET /x', 'op' => 'http.server', 'ms' => 1.0, 'status' => 200, 'error' => false, 'ended' => 1000020.0,
                'trace_id' => sprintf('%032x', $i + 1), 'span_id' => self::PID, 'parent_span_id' => self::PID, 'sampled' => true, 'sp' => $sp], 1000020, 60);
        }
        $this->assertCount(20, $data['s']['f']);
        $this->assertCount(Metrics::FORCED_SAMPLE_SPANS, $data['s']['f'][0]['sp']);
        $this->assertTrue(strlen(json_encode($data)) < 400000);
    }

    // ------------------------------------------------------ outbound (Guzzle)

    private function guzzle(array $options, ?string $headerOnRequest, string $url, array $reqHeaders = []): array
    {
        [$c, $tr] = $this->make($options);
        $c->beginTrace($this->t, 'http.server', $headerOnRequest ?? '');
        $seen = null;
        $response = new class {
            public function getStatusCode(): int
            {
                return 200;
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
        $handler = GuzzleMiddleware::create()(function ($req, array $opts) use (&$seen, $promise) {
            $seen = $req;

            return $promise;
        });
        $handler(new TracingFakeRequest($url, 'POST', $reqHeaders), []);
        $c->recordTransaction('GET /g', 30.0, 200);
        $bodies = $this->flushWindow($c, $tr);

        return [$seen, $bodies[0], $c];
    }

    public function testGuzzleAddsHeaderForMatchingHostAndTiesItToTheSpan(): void
    {
        [$req, $body] = $this->guzzle(['trace_propagation_targets' => ['api.example.com']], $this->header(), 'https://api.example.com/v1/pay?x=1');
        $h = $req->getHeaderLine('traceparent');
        $this->assertMatchesRegularExpression('/^00-' . self::TID . '-[0-9a-f]{16}-01$/', $h);
        $spanId = substr($h, 36, 16);
        $this->assertNotSame(self::PID, $spanId);
        $sample = null;
        foreach ($body['samples'] as $s) {
            if ($s['trace_id'] === self::TID) {
                $sample = $s;
            }
        }
        $this->assertNotNull($sample);
        $client = null;
        foreach ($sample['spans'] as $sp) {
            if ($sp['op'] === 'http.client') {
                $client = $sp;
            }
        }
        $this->assertNotNull($client);
        $this->assertSame($spanId, $client['span_id']);
        $this->assertSame('POST api.example.com', $client['description']);
        $this->assertSame($sample['span_id'] !== $spanId, true);
    }

    public function testGuzzleFlagMirrorsTheSampledBit(): void
    {
        [$req] = $this->guzzle(['trace_propagation_targets' => 'api.example.com'], $this->header(self::TID, self::PID, '00'), 'https://api.example.com/x');
        $this->assertMatchesRegularExpression('/-00$/', $req->getHeaderLine('traceparent'));
        [$req2] = $this->guzzle(['trace_propagation_targets' => ['api.example.com']], null, 'https://api.example.com/x'); // own new trace
        $this->assertMatchesRegularExpression('/^00-[0-9a-f]{32}-[0-9a-f]{16}-00$/', $req2->getHeaderLine('traceparent'));
    }

    public function testGuzzleSkipsNonMatchingHostsAndDefaultsToNone(): void
    {
        [$req] = $this->guzzle(['trace_propagation_targets' => ['api.example.com']], $this->header(), 'https://other.example.org/x');
        $this->assertFalse($req->hasHeader('traceparent'));
        [$req2] = $this->guzzle([], $this->header(), 'https://api.example.com/x');
        $this->assertFalse($req2->hasHeader('traceparent'));
        [$req3] = $this->guzzle(['trace_propagation_targets' => ['api.example.com']], $this->header(), 'https://evilapi.example.com.attacker.io/x');
        $this->assertFalse($req3->hasHeader('traceparent'));
    }

    public function testGuzzleKeepsAnExistingHeader(): void
    {
        [$req] = $this->guzzle(['trace_propagation_targets' => ['api.example.com']], $this->header(), 'https://api.example.com/x', ['traceparent' => 'mine']);
        $this->assertSame('mine', $req->getHeaderLine('traceparent'));
    }

    public function testNoHeaderWithoutAnActiveTraceOrWhenSpansAreOff(): void
    {
        [$c] = $this->make(['trace_propagation_targets' => ['api.example.com']]);
        $this->assertNull($c->outgoingTrace('api.example.com')); // CLI, no trace
        $c->beginTrace($this->t);
        $this->assertNotNull($c->outgoingTrace('api.example.com'));
        [$c2] = $this->make(['trace_propagation_targets' => ['api.example.com'], 'performance_spans' => false]);
        $c2->beginTrace($this->t);
        $this->assertNull($c2->outgoingTrace('api.example.com'));
    }

    public function testTargetMatching(): void
    {
        [$c] = $this->make(['trace_propagation_targets' => ['api.example.com', '*.svc.internal', 'localhost:8000', '#^pay[0-9]+\.example\.net(:\d+)?$#', '', '#(unterminated']]);
        $yes = ['api.example.com', 'API.example.com', 'api.example.com:8443', 'a.svc.internal', 'a.svc.internal:80', 'localhost:8000', 'pay7.example.net', 'pay12.example.net:444'];
        $no = ['example.com', 'x.api.example.com', 'svc.internal', 'localhost', 'localhost:8001', 'localhost:80000', 'payx.example.net', 'api.example.com.evil.io', ''];
        foreach ($yes as $h) {
            $this->assertTrue($c->matchesTarget($h), $h);
        }
        foreach ($no as $h) {
            $this->assertFalse($c->matchesTarget($h), $h);
        }
    }

    public function testInsightsOwnHostIsNeverTraced(): void
    {
        [$c] = $this->make(['trace_propagation_targets' => ['*', 'insight.test']]);
        $c->beginTrace($this->t);
        $this->assertNull($c->outgoingTrace('insight.test'));
        $this->assertNotNull($c->outgoingTrace('api.example.com'));
    }

    public function testSpanListenerTakesTheSpanIdFromTheHeader(): void
    {
        [$c, $tr] = $this->make(['trace_propagation_targets' => ['api.example.com']]);
        $c->beginTrace($this->t, 'http.server', $this->header());
        $out = $c->outgoingTrace('api.example.com');
        $request = new class($out['header']) {
            private $h;

            public function __construct($h)
            {
                $this->h = $h;
            }

            public function method(): string
            {
                return 'get';
            }

            public function url(): string
            {
                return 'https://api.example.com/v1/x?y=1';
            }

            public function header($k)
            {
                return strtolower($k) === 'traceparent' ? [$this->h] : [];
            }
        };
        $l = new SpanListeners();
        $sending = (object) ['request' => $request];
        $l->onHttpSending($sending);
        $this->t += 0.05;
        $l->onHttpResponse((object) ['request' => $request, 'response' => new class {
            public function status(): int
            {
                return 200;
            }
        }]);
        $c->recordTransaction('GET /l', 80.0, 200);
        $body = $this->flushWindow($c, $tr)[0];
        $spans = [];
        foreach ($body['samples'] as $s) {
            if ($s['trace_id'] === self::TID) {
                $spans = $s['spans'];
            }
        }
        $this->assertCount(1, $spans);
        $this->assertSame($out['span_id'], $spans[0]['span_id']);
    }

    public function testMiddlewareReadsTheRequestHeader(): void
    {
        $req = new class {
            public $headers;

            public function __construct()
            {
                $this->headers = new class {
                    public function get($k)
                    {
                        return $k === 'traceparent' ? '00-' . TracingTest::tid() . '-' . TracingTest::pid() . '-01' : null;
                    }
                };
            }
        };
        $this->assertSame('00-' . self::TID . '-' . self::PID . '-01', PerformanceMiddleware::incomingTraceparent($req));
        $this->assertSame('', PerformanceMiddleware::incomingTraceparent(new \stdClass()));
        $this->assertSame('', PerformanceMiddleware::incomingTraceparent(null));
    }

    public static function tid(): string
    {
        return self::TID;
    }

    public static function pid(): string
    {
        return self::PID;
    }

    // ------------------------------------------------------------ never throws

    public function testNeverThrowsOnGarbage(): void
    {
        $this->assertFalse(Insight::continueTrace('garbage')); // no client
        $this->assertNull(Insight::traceparent());
        [$c, $tr] = $this->make(['trace_propagation_targets' => [['array'], null, 12, '#(bad', '*.', 'ho st']]);
        foreach (['', 'x', "\xff\xfe", str_repeat('9', 100000), '00-' . "\0" . '-1-1', $this->header() . "\n" . $this->header()] as $g) {
            $this->assertFalse($c->continueTrace($g));
            $this->assertFalse($c->beginTrace($this->t, 'http.server', $g) === false);
            $this->assertNotSame(self::TID, $c->traceId());
        }
        $this->assertNull($c->outgoingTrace("\xff\xfe"));
        $this->assertNull($c->outgoingTrace(''));
        $this->assertTrue($c->recordSpan('http.client', 'GET x', 1.0, true, [], null, 'not-a-span-id')); // falls back to a random id
        $c->recordTransaction('GET /g', 1.0, 200);
        $body = $this->flushWindow($c, $tr)[0];
        $this->assertMatchesRegularExpression('/^[0-9a-f]{16}$/', $body['samples'][0]['spans'][0]['span_id']);
        // a request object that throws everywhere
        $bad = new class {
            public function getMethod()
            {
                throw new \RuntimeException('x');
            }

            public function getUri()
            {
                throw new \RuntimeException('x');
            }
        };
        $_SERVER['HTTP_TRACEPARENT'] = ['not', 'a string'];
        $h = GuzzleMiddleware::create()(function ($r, $o) {
            return new class {
                public function then($a, $b)
                {
                    return 'p';
                }
            };
        });
        $this->assertSame('p', $h($bad, []));
        $c->beginTrace($this->t);
        $this->assertNotNull($c->traceId());
    }
}
