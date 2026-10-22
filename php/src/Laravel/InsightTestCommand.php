<?php

namespace Insight\Laravel;

use Illuminate\Console\Command;
use Insight\Client;

class InsightTestCommand extends Command
{
    protected $signature = 'insight:test';

    protected $description = 'Send a test exception to Insight and print the outcome';

    public function handle(): int
    {
        $client = $this->laravel->make(Client::class);

        if (!$client->isEnabled()) {
            $this->error('Insight is not configured. Set INSIGHT_API_KEY and INSIGHT_HOST in your .env (then run `php artisan config:clear`).');

            return 1;
        }

        $this->line('Sending to ' . $client->getUrl());
        $client->captureException(
            new \RuntimeException('Insight test exception from php artisan insight:test'),
            ['tags' => ['insight_test' => 'true'], 'force' => true]
        );
        $status = $client->flush();

        if ($status === null) {
            $this->warn('Nothing was sent (dropped by before_send, or sending is paused or disabled). Set INSIGHT_DEBUG=true for details.');

            return 1;
        }
        if ($status >= 200 && $status < 300) {
            $this->info('OK, HTTP ' . $status . '. Check the Errors page in your Insight dashboard.');

            return 0;
        }
        if ($status === 401 || $status === 403) {
            $this->error('HTTP ' . $status . ': the API key is invalid, expired or lacks write permission.');
        } elseif ($status === 429) {
            $this->error('HTTP 429: rate limited or monthly quota used up. Sending is paused.');
        } elseif ($status === 0) {
            $this->error('Could not reach the host (network error or timeout). Check INSIGHT_HOST.');
        } else {
            $this->error('HTTP ' . $status . ': event was not accepted.');
        }

        return 1;
    }
}
