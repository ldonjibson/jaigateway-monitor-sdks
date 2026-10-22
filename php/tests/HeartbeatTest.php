<?php

namespace Insight\Tests;

use Insight\Heartbeat;
use Insight\Insight;
use PHPUnit\Framework\TestCase;

class HeartbeatTest extends TestCase
{
    private $calls = [];

    protected function setUp(): void
    {
        $this->calls = [];
        $self = $this;
        Heartbeat::$sender = function (string $method, string $url, ?string $body) use ($self) {
            $self->calls[] = [$method, $url, $body];

            return 200;
        };
        Insight::setClient(null);
    }

    protected function tearDown(): void
    {
        Heartbeat::$sender = null;
        Insight::setClient(null);
    }

    public function testBaseAcceptsUrlsWithOrWithoutAction(): void
    {
        $this->assertSame('https://i.test/api/ping/abc/', Heartbeat::base('https://i.test/api/ping/abc'));
        $this->assertSame('https://i.test/api/ping/abc/', Heartbeat::base('https://i.test/api/ping/abc/start/'));
        $this->assertSame('https://i.test/api/ping/abc/', Heartbeat::base('https://i.test/api/ping/abc/fail'));
        $this->assertNull(Heartbeat::base('tokenonly'));
        $this->assertNull(Heartbeat::base(''));
    }

    public function testBareTokenUsesTheConfiguredHost(): void
    {
        Insight::init(['api_key' => 'ak_x', 'host' => 'https://insight.test', 'enabled' => false]);
        $this->assertSame('https://insight.test/api/ping/tok123/', Heartbeat::base('tok123'));
    }

    public function testMonitorSendsStartThenSuccessAndReturnsTheValue(): void
    {
        $r = Insight::monitor('https://i.test/api/ping/abc/', function () {
            return 42;
        });
        $this->assertSame(42, $r);
        $this->assertSame(['https://i.test/api/ping/abc/start/', 'https://i.test/api/ping/abc/'], array_column($this->calls, 1));
        $this->assertSame('GET', $this->calls[0][0]);
    }

    public function testFailureSendsTheErrorAndRethrows(): void
    {
        try {
            Insight::monitor('https://i.test/api/ping/abc/', function () {
                throw new \RuntimeException('job broke');
            });
            $this->fail('the job exception must propagate');
        } catch (\RuntimeException $e) {
            $this->assertSame('job broke', $e->getMessage());
        }
        $this->assertSame(['https://i.test/api/ping/abc/start/', 'https://i.test/api/ping/abc/fail/'], array_column($this->calls, 1));
        $this->assertSame('POST', $this->calls[1][0]);
        $this->assertStringContainsString('RuntimeException: job broke', (string) $this->calls[1][2]);
    }

    public function testCheckInNeverThrowsAndReportsAcceptance(): void
    {
        Heartbeat::$sender = function () {
            throw new \RuntimeException('network down');
        };
        $this->assertFalse(Insight::checkIn('https://i.test/api/ping/abc/'));
        Heartbeat::$sender = function () {
            return 404;
        };
        $this->assertFalse(Insight::checkIn('https://i.test/api/ping/abc/'));
        Heartbeat::$sender = function () {
            return 200;
        };
        $this->assertTrue(Insight::checkIn('https://i.test/api/ping/abc/', 'log', 'processed 5 rows'));
    }
}
