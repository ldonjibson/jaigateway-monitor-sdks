<?php

namespace Insight\Tests;

use Insight\Client;
use LogicException;
use PHPUnit\Framework\TestCase;
use RuntimeException;
use Throwable;

class ClientTest extends TestCase
{
    /** @return array{0: Client, 1: FakeTransport} */
    private function make(array $options = [], array $responses = []): array
    {
        $transport = new FakeTransport($responses);
        $client = new Client(array_merge([
            'api_key' => 'key-123',
            'host' => 'https://insight.test',
            'register_shutdown' => false,
            'state_mode' => 'memory',
            'retry_delay_ms' => 0,
            'server_name' => 'web-1',
            'release' => 'app@1.0.0',
            'environment' => 'testing',
        ], $options), $transport);

        return [$client, $transport];
    }

    public static function inner(): void
    {
        throw new RuntimeException('boom');
    }

    public static function outer(): void
    {
        self::inner();
    }

    private function thrown(): Throwable
    {
        try {
            self::outer();
        } catch (Throwable $e) {
            return $e;
        }
        throw new \LogicException('unreachable');
    }

    public function testUrlBuilding(): void
    {
        $this->assertSame('https://h.test/api/ingest/events/', Client::buildUrl('https://h.test'));
        $this->assertSame('https://h.test/api/ingest/events/', Client::buildUrl('https://h.test/'));
        $this->assertSame('https://h.test/api/ingest/events/', Client::buildUrl('https://h.test//'));
        $this->assertSame('https://h.test/api/ingest/events/', Client::buildUrl('https://h.test/api'));
        $this->assertSame('https://h.test/api/ingest/events/', Client::buildUrl('https://h.test/api/'));
        $this->assertSame('https://h.test/api/ingest/events/', Client::buildUrl('https://h.test/api/ingest/events/'));
        $this->assertSame('', Client::buildUrl('  '));
    }

    public function testEndpointOptionIsAccepted(): void
    {
        [$client] = $this->make(['host' => '', 'endpoint' => 'https://e.test/']);
        $this->assertSame('https://e.test/api/ingest/events/', $client->getUrl());
    }

    public function testEventShape(): void
    {
        [$client, $transport] = $this->make();
        $client->setTag('feature', 'checkout');
        $client->setExtra('orderId', 991);
        $client->addBreadcrumb('clicked pay', 'ui.click');
        $this->assertTrue($client->captureException($this->thrown()));
        $this->assertSame(201, $client->flush());

        $this->assertCount(1, $transport->calls);
        $call = $transport->calls[0];
        $this->assertSame('https://insight.test/api/ingest/events/', $call['url']);
        $this->assertContains('X-API-Key: key-123', $call['headers']);
        $this->assertContains('Content-Type: application/json', $call['headers']);

        $event = $call['events'][0];
        $this->assertSame('error', $event['event_type']);
        $this->assertSame('exception', $event['action']);
        $this->assertSame('error', $event['level']);
        $this->assertSame('php', $event['platform']);
        $this->assertSame('insight.php', $event['sdk']['name']);
        $this->assertSame(Client::VERSION, $event['sdk']['version']);
        $this->assertSame('app@1.0.0', $event['release']);
        $this->assertSame('testing', $event['environment']);
        $this->assertSame('web-1', $event['server_name']);
        $this->assertSame('RuntimeException: boom', $event['description']);
        $this->assertSame('RuntimeException', $event['exception']['type']);
        $this->assertSame('boom', $event['exception']['value']);
        $this->assertSame(PHP_VERSION, $event['tags']['php_version']);
        $this->assertSame(PHP_OS_FAMILY, $event['tags']['os']);
        $this->assertSame('checkout', $event['tags']['feature']);
        $this->assertSame(991, $event['extra']['orderId']);
        $this->assertSame('clicked pay', $event['breadcrumbs'][0]['message']);
        $this->assertSame('ui.click', $event['breadcrumbs'][0]['category']);
        $this->assertStringContainsString('boom', $event['metadata']['traceback']);
        $this->assertMatchesRegularExpression('/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/', $event['timestamp']);
    }

    public function testFramesAreMostRecentFirst(): void
    {
        $e = $this->thrown();
        $frames = Client::frames($e);

        $this->assertSame(__CLASS__ . '::inner', $frames[0]['function']);
        $this->assertSame($e->getLine(), $frames[0]['lineno']);
        $this->assertSame(__FILE__, $frames[0]['filename']);
        $this->assertSame(__CLASS__ . '::outer', $frames[1]['function']);
        $this->assertSame(__CLASS__ . '::thrown', $frames[2]['function']);
        $this->assertSame('{main}', $frames[count($frames) - 1]['function']);
    }

    public function testInApp(): void
    {
        $this->assertTrue(Client::isInApp('/srv/app/src/Foo.php'));
        $this->assertFalse(Client::isInApp('/srv/app/vendor/laravel/framework/X.php'));
        $this->assertFalse(Client::isInApp('C:\\app\\vendor\\x\\Y.php'));
        $this->assertFalse(Client::isInApp('[internal]'));
        $frames = Client::frames($this->thrown());
        $this->assertTrue($frames[0]['in_app']);
    }

    public function testPreviousExceptionsGoToExtraChain(): void
    {
        [$client, $transport] = $this->make();
        $e = new RuntimeException('outer', 0, new LogicException('inner'));
        $client->captureException($e);
        $client->flush();

        $chain = $transport->calls[0]['events'][0]['extra']['exception_chain'];
        $this->assertCount(1, $chain);
        $this->assertSame('LogicException', $chain[0]['type']);
        $this->assertSame('inner', $chain[0]['value']);
    }

    public function testScrubHeaders(): void
    {
        $out = Client::scrubHeaders([
            'Cookie' => 'a=b',
            'Authorization' => 'Bearer x',
            'X-Api-Key' => 'secret',
            'User-Agent' => ['ua'],
            'Accept' => 'text/html',
        ]);
        $this->assertSame(['user-agent' => 'ua', 'accept' => 'text/html'], $out);
    }

    public function testRequestContextIsUsedAndScrubbed(): void
    {
        [$client, $transport] = $this->make();
        $client->captureMessage('hello', 'warning', ['request' => ['url' => 'https://app/x', 'method' => 'GET']]);
        $client->flush();
        $event = $transport->calls[0]['events'][0];
        $this->assertSame('https://app/x', $event['request']['url']);
        $this->assertSame('warning', $event['level']);
        $this->assertSame('message', $event['action']);
        $this->assertArrayNotHasKey('exception', $event);
    }

    public function testSampleRateZeroDropsEverything(): void
    {
        [$client, $transport] = $this->make(['sample_rate' => 0.0]);
        $this->assertFalse($client->captureMessage('x'));
        $this->assertNull($client->flush());
        $this->assertCount(0, $transport->calls);
    }

    public function testBeforeSendNullDrops(): void
    {
        [$client, $transport] = $this->make(['before_send' => function (array $event) {
            return null;
        }]);
        $this->assertFalse($client->captureMessage('x'));
        $client->flush();
        $this->assertCount(0, $transport->calls);
    }

    public function testBeforeSendCanModify(): void
    {
        [$client, $transport] = $this->make(['before_send' => function (array $event) {
            $event['description'] = 'changed';

            return $event;
        }]);
        $client->captureMessage('x');
        $client->flush();
        $this->assertSame('changed', $transport->calls[0]['events'][0]['description']);
    }

    public function testNotConfiguredIsDisabled(): void
    {
        $transport = new FakeTransport();
        $client = new Client(['register_shutdown' => false, 'state_mode' => 'memory'], $transport);
        $this->assertFalse($client->isEnabled());
        $this->assertFalse($client->captureMessage('x'));
        $this->assertNull($client->flush());
        $this->assertCount(0, $transport->calls);
    }

    public function testBatchingSendsOnePost(): void
    {
        [$client, $transport] = $this->make();
        $client->captureMessage('a');
        $client->captureMessage('b');
        $client->captureException(new RuntimeException('c'));
        $this->assertSame(3, $client->pendingCount());
        $client->finishAndFlush(); // what the shutdown hook calls
        $this->assertCount(1, $transport->calls);
        $this->assertCount(3, $transport->calls[0]['events']);
        $this->assertSame(0, $client->pendingCount());
    }

    public function testBreadcrumbsAreBounded(): void
    {
        [$client, $transport] = $this->make(['max_breadcrumbs' => 3]);
        for ($i = 1; $i <= 5; $i++) {
            $client->addBreadcrumb('b' . $i);
        }
        $client->captureMessage('x');
        $client->flush();
        $crumbs = $transport->calls[0]['events'][0]['breadcrumbs'];
        $this->assertCount(3, $crumbs);
        $this->assertSame('b3', $crumbs[0]['message']);
        $this->assertSame('b5', $crumbs[2]['message']);
    }

    public function testUnauthorizedDisablesForTheProcess(): void
    {
        foreach ([401, 403] as $status) {
            [$client, $transport] = $this->make([], [['status' => $status]]);
            $client->captureMessage('a');
            $this->assertSame($status, $client->flush());
            $this->assertTrue($client->isDisabled());
            $this->assertFalse($client->captureMessage('b'));
            $this->assertNull($client->flush());
            $this->assertCount(1, $transport->calls);
        }
    }

    public function testPayloadTooLargeSingleEventIsDropped(): void
    {
        [$client, $transport] = $this->make([], [['status' => 413, 'body' => '{"code":"payload_too_large"}']]);
        $client->captureMessage('a');
        $this->assertSame(413, $client->flush());
        $this->assertCount(1, $transport->calls);
        $this->assertFalse($client->isDisabled());
    }

    public function testPayloadTooLargeBatchIsSplit(): void
    {
        [$client, $transport] = $this->make([], [['status' => 413, 'body' => '{"code":"batch_too_large"}']]);
        $client->captureMessage('a');
        $client->captureMessage('b');
        $client->flush();
        $this->assertCount(3, $transport->calls);
        $this->assertCount(1, $transport->calls[1]['events']);
        $this->assertCount(1, $transport->calls[2]['events']);
    }

    public function testRateLimitedPausesAndDrops(): void
    {
        [$client, $transport] = $this->make([], [['status' => 429, 'body' => '{"code":"rate_limited"}', 'retry_after' => 120]]);
        $client->captureMessage('a');
        $this->assertSame(429, $client->flush());
        $this->assertFalse($client->captureMessage('b'));
        $this->assertNull($client->flush());
        $this->assertCount(1, $transport->calls);
        $this->assertFalse($client->isDisabled());
    }

    public function testQuotaExceededPausesAndRetryAfterIsCapped(): void
    {
        [$client, $transport] = $this->make([], [['status' => 429, 'body' => '{"code":"quota_exceeded"}', 'retry_after' => 999999]]);
        $client->captureMessage('a');
        $client->flush();
        $this->assertFalse($client->captureMessage('b'));
        $this->assertCount(1, $transport->calls);
    }

    public function testServerErrorRetriesOnce(): void
    {
        [$client, $transport] = $this->make([], [['status' => 500], ['status' => 201]]);
        $client->captureMessage('a');
        $this->assertSame(201, $client->flush());
        $this->assertCount(2, $transport->calls);
    }

    public function testServerErrorGivesUpAfterOneRetry(): void
    {
        [$client, $transport] = $this->make([], [['status' => 503], ['status' => 503], ['status' => 201]]);
        $client->captureMessage('a');
        $this->assertSame(503, $client->flush());
        $this->assertCount(2, $transport->calls);
        $this->assertFalse($client->isDisabled());
    }

    public function testNetworkErrorRetriesOnce(): void
    {
        [$client, $transport] = $this->make([], [['status' => 0], ['status' => 0]]);
        $client->captureMessage('a');
        $this->assertSame(0, $client->flush());
        $this->assertCount(2, $transport->calls);
    }

    public function testBadRequestIsDroppedWithoutRetry(): void
    {
        [$client, $transport] = $this->make([], [['status' => 400]]);
        $client->captureMessage('a');
        $this->assertSame(400, $client->flush());
        $this->assertCount(1, $transport->calls);
    }

    public function testPerRequestCapDropsExtraEvents(): void
    {
        [$client] = $this->make(['max_events_per_request' => 3, 'rate_limit_per_minute' => 1000]);
        $results = [];
        for ($i = 0; $i < 5; $i++) {
            $results[] = $client->captureMessage('m' . $i);
        }
        $this->assertSame([true, true, true, false, false], $results);
    }

    public function testPerMinuteRateGuard(): void
    {
        [$client, $transport] = $this->make(['rate_limit_per_minute' => 2]);
        $this->assertTrue($client->captureMessage('1'));
        $client->flush();
        $this->assertTrue($client->captureMessage('2'));
        $client->flush();
        $this->assertFalse($client->captureMessage('3'));
        $this->assertNull($client->flush());
        $this->assertCount(2, $transport->calls);
    }

    public function testForceBypassesSamplingAndRateGuard(): void
    {
        [$client] = $this->make(['sample_rate' => 0.0, 'rate_limit_per_minute' => 0]);
        $this->assertTrue($client->captureMessage('x', 'info', ['force' => true]));
    }

    public function testResetScopeClearsEverything(): void
    {
        [$client, $transport] = $this->make();
        $client->setUser(['id' => '1']);
        $client->setTag('a', 'b');
        $client->addBreadcrumb('x');
        $client->resetScope();
        $client->captureMessage('m');
        $client->flush();
        $event = $transport->calls[0]['events'][0];
        $this->assertArrayNotHasKey('user', $event);
        $this->assertArrayNotHasKey('a', $event['tags']);
        $this->assertSame([], $event['breadcrumbs']);
    }
}
