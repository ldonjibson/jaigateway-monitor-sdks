<?php

namespace Insight\Tests;

use Insight\Client;
use Insight\Insight;
use Insight\Laravel\SpanListeners;
use PHPUnit\Framework\TestCase;

class CacheSpansTest extends TestCase
{
    private string $dir = '';
    private float $t = 1000020.0;
    private $rtf = null;

    protected function setUp(): void
    {
        $this->dir = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'insight-cache-' . bin2hex(random_bytes(4));
        mkdir($this->dir);
        $this->t = 1000020.0;
        Insight::setClient(null);
        Insight::resetTransaction();
        $this->rtf = $_SERVER['REQUEST_TIME_FLOAT'] ?? null;
        unset($_SERVER['REQUEST_TIME_FLOAT']);
    }

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
    private function make(array $options = []): array
    {
        $transport = new FakeTransport([]);
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

    private function flushWindow(Client $c, FakeTransport $tr): array
    {
        $this->t += 61;
        $c->recordTransaction('GET /tick', 1.0);
        $c->flushMetrics();
        $this->assertGreaterThan(0, count($tr->calls));

        return $tr->calls[count($tr->calls) - 1]['events'];
    }

    private function redis(string $command, float $ms = 1.5): \stdClass
    {
        $e = new \stdClass();
        $e->command = $command;
        $e->time = $ms;
        $e->connectionName = 'default';
        $e->parameters = ['user:42:session-secret-key', 'value-secret'];

        return $e;
    }

    private function summary(array $p, string $description): ?array
    {
        foreach ($p['spans'] as $s) {
            if ($s['op'] === 'cache' && $s['description'] === $description && $s['transaction'] === 'GET /x') {
                return $s;
            }
        }

        return null;
    }

    public function testRedisCommandRecordsOnlyTheUppercaseCommandName(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $c->beginTrace($this->t - 1);
        $l->onRedisCommand($this->redis('get', 2.5));
        $l->onRedisCommand($this->redis('HGETALL user:42:session-secret-key', 1.0)); // name only, never the rest
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        $json = json_encode($p);
        $this->assertFalse(strpos($json, 'secret') !== false);
        $this->assertFalse(strpos($json, 'user:42') !== false);
        $get = $this->summary($p, 'GET');
        $this->assertNotNull($get);
        $this->assertSame(1, $get['count']);
        $this->assertSame(0, $get['errors']);
        $this->assertEquals(2.5, $get['sum_ms']);
        $this->assertNotNull($this->summary($p, 'HGETALL'));
        $spans = $p['samples'][0]['spans'];
        $this->assertSame('cache', $spans[0]['op']);
        $this->assertSame('GET', $spans[0]['description']);
        $this->assertEquals(2.5, $spans[0]['duration_ms']);
        $this->assertEquals(997.5, $spans[0]['start_ms']);
        $this->assertSame('ok', $spans[0]['status']);
        $this->assertArrayNotHasKey('data', $spans[0]);
    }

    public function testHandshakeAndGarbageRedisEventsAreIgnoredAndNeverThrow(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $c->beginTrace($this->t - 1);
        foreach (['AUTH', 'auth', 'select', 'HELLO', 'CLIENT SETNAME', '', '!bad command', str_repeat('A', 80)] as $cmd) {
            $l->onRedisCommand($this->redis($cmd));
        }
        $l->onRedisCommand(new \stdClass());
        $l->onRedisCommand(null);
        $bad = new \stdClass();
        $bad->command = ['GET'];
        $l->onRedisCommand($bad);
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        $this->assertFalse(isset($p['spans']));
    }

    public function testCacheMarkersAreZeroDurationAndNeverCarryTheKey(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $ev = new class {
            public function __get($n)
            {
                throw new \RuntimeException('event data must not be read');
            }
        };
        $c->beginTrace($this->t - 1);
        $l->onCacheHit($ev);
        $l->onCacheMissed($ev);
        $l->onCacheMissed($ev);
        $l->onCacheWritten($ev);
        $l->onCacheForgotten($ev);
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        $this->assertSame(1, $this->summary($p, 'GET hit')['count']);
        $this->assertSame(2, $this->summary($p, 'GET miss')['count']);
        $this->assertSame(1, $this->summary($p, 'PUT')['count']);
        $this->assertSame(1, $this->summary($p, 'FORGET')['count']);
        $this->assertEquals(0, $this->summary($p, 'GET hit')['sum_ms']);
        $byDesc = [];
        foreach ($p['samples'][0]['spans'] as $s) {
            $byDesc[$s['description']][] = $s;
        }
        $this->assertSame(true, $byDesc['GET hit'][0]['data']['cache.hit']);
        $this->assertSame(false, $byDesc['GET miss'][0]['data']['cache.hit']);
        $this->assertArrayNotHasKey('data', $byDesc['PUT'][0]);
        $this->assertSame('ok', $byDesc['FORGET'][0]['status']);
    }

    public function testMarkersAreCappedInTheSpanListButAllCounted(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $c->beginTrace($this->t - 1);
        for ($i = 0; $i < 500; $i++) {
            $l->onCacheHit(new \stdClass());
        }
        for ($i = 0; $i < 30; $i++) {
            $l->onCacheMissed(new \stdClass());
        }
        $c->recordSpan('db.query', 'select 1', 1.0);
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        $this->assertSame(500, $this->summary($p, 'GET hit')['count']);
        $this->assertSame(30, $this->summary($p, 'GET miss')['count']);
        $hits = 0;
        $miss = 0;
        $db = 0;
        foreach ($p['samples'][0]['spans'] as $s) {
            $hits += $s['description'] === 'GET hit' ? 1 : 0;
            $miss += $s['description'] === 'GET miss' ? 1 : 0;
            $db += $s['op'] === 'db.query' ? 1 : 0;
        }
        $this->assertSame(Client::MAX_CACHE_MARKERS, $hits);
        $this->assertSame(Client::MAX_CACHE_MARKERS, $miss);
        $this->assertSame(1, $db);
    }

    public function testCapIsPerRequest(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        for ($r = 0; $r < 2; $r++) {
            $c->beginTrace($this->t - 1);
            for ($i = 0; $i < 25; $i++) {
                $l->onCacheHit(new \stdClass());
            }
            $c->recordTransaction('GET /x', 300.0, 200);
        }
        $p = $this->flushWindow($c, $tr);
        $this->assertSame(50, $this->summary($p, 'GET hit')['count']);
        foreach ($p['samples'] as $sample) {
            if ($sample['name'] !== 'GET /x') {
                continue;
            }
            $this->assertSame(Client::MAX_CACHE_MARKERS, count($sample['spans']));
        }
    }

    public function testRedisStoreDoesNotDoubleCount(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $c->beginTrace($this->t - 1);
        // Redis cache store: the command fires first, then the store event.
        $l->onRedisCommand($this->redis('GET'));
        $l->onCacheHit(new \stdClass());
        $l->onRedisCommand($this->redis('SETEX'));
        $l->onCacheWritten(new \stdClass());
        $l->onRedisCommand($this->redis('DEL'));
        $l->onCacheForgotten(new \stdClass());
        // A non-Redis store later in the request: its marker is kept.
        $this->t += 1.0;
        $l->onCacheMissed(new \stdClass());
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        $this->assertNull($this->summary($p, 'GET hit'));
        $this->assertNull($this->summary($p, 'PUT'));
        $this->assertNull($this->summary($p, 'FORGET'));
        $this->assertSame(1, $this->summary($p, 'GET')['count']);
        $this->assertSame(1, $this->summary($p, 'SETEX')['count']);
        $this->assertSame(1, $this->summary($p, 'DEL')['count']);
        $this->assertSame(1, $this->summary($p, 'GET miss')['count']);
    }

    public function testRedisFromAnotherRequestDoesNotSuppressMarkers(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $c->beginTrace($this->t - 1);
        $l->onRedisCommand($this->redis('GET'));
        $c->recordTransaction('GET /y', 10.0, 200);
        $c->beginTrace($this->t - 1); // next request, same instant, new trace
        $l->onCacheHit(new \stdClass());
        $c->recordTransaction('GET /x', 10.0, 200);
        $p = $this->flushWindow($c, $tr);
        $this->assertSame(1, $this->summary($p, 'GET hit')['count']);
    }

    public function testNonCacheRedisCommandsDoNotSuppressMarkers(): void
    {
        [$c, $tr] = $this->make();
        $l = new SpanListeners();
        $c->beginTrace($this->t - 1);
        $l->onRedisCommand($this->redis('LPUSH'));
        $l->onCacheHit(new \stdClass());
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        $this->assertSame(1, $this->summary($p, 'GET hit')['count']);
        $this->assertSame(1, $this->summary($p, 'LPUSH')['count']);
    }

    public function testConfigSwitchTurnsCacheSpansOff(): void
    {
        [$c, $tr] = $this->make(['performance_cache_spans' => false]);
        $this->assertFalse($c->isCacheSpansEnabled());
        $l = new SpanListeners();
        $c->beginTrace($this->t - 1);
        $l->onRedisCommand($this->redis('GET'));
        $l->onCacheHit(new \stdClass());
        $this->assertFalse($c->recordCacheMarker('PUT'));
        $c->recordSpan('db.query', 'select 1', 1.0);
        $c->recordTransaction('GET /x', 300.0, 200);
        $p = $this->flushWindow($c, $tr);
        foreach ($p['spans'] as $s) {
            $this->assertFalse($s['op'] === 'cache');
        }
        [$c2] = $this->make(['performance_spans' => false]);
        $this->assertFalse($c2->isCacheSpansEnabled());
        [$c3] = $this->make();
        $this->assertTrue($c3->isCacheSpansEnabled());
        $this->assertSame('1.5.0', Client::VERSION);
    }

    public function testRegisterListensOnlyWhenEnabled(): void
    {
        $events = new class {
            public array $names = [];

            public function listen($name, $cb): void
            {
                $this->names[] = $name;
            }
        };
        $this->make();
        (new SpanListeners())->register($events);
        $this->assertContains('Illuminate\Redis\Events\CommandExecuted', $events->names);
        $this->assertContains('Illuminate\Cache\Events\CacheHit', $events->names);
        $this->assertContains('Illuminate\Cache\Events\KeyForgotten', $events->names);

        $off = new class {
            public array $names = [];

            public function listen($name, $cb): void
            {
                $this->names[] = $name;
            }
        };
        $this->make(['performance_cache_spans' => false]);
        (new SpanListeners())->register($off);
        $this->assertContains('Illuminate\Database\Events\QueryExecuted', $off->names);
        $this->assertFalse(in_array('Illuminate\Cache\Events\CacheHit', $off->names, true));
        $this->assertFalse(in_array('Illuminate\Redis\Events\CommandExecuted', $off->names, true));
    }

    public function testListenersAreInertWithoutClientAndNeverThrow(): void
    {
        $l = new SpanListeners();
        $l->onRedisCommand($this->redis('GET'));
        $l->onCacheHit(new \stdClass());
        $l->onCacheMissed(null);
        $l->onCacheWritten(null);
        $l->onCacheForgotten(null);
        $this->make();
        $l->onRedisCommand(null);
        $this->assertTrue(true);
    }

    public function testMarkersOutsideARequestAreAggregatedWithoutTransaction(): void
    {
        [$c, $tr] = $this->make();
        $this->assertTrue($c->recordCacheMarker('GET hit', true));
        $this->assertTrue($c->recordCacheMarker('GET hit', true));
        $c->recordTransaction('GET /x', 10.0, 200);
        $p = $this->flushWindow($c, $tr);
        $found = 0;
        foreach ($p['spans'] as $s) {
            if ($s['op'] === 'cache' && $s['description'] === 'GET hit') {
                $found += $s['count'];
            }
        }
        $this->assertSame(2, $found);
    }
}
