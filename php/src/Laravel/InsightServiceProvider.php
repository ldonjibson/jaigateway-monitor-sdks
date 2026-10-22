<?php

namespace Insight\Laravel;

use Illuminate\Database\Events\QueryExecuted;
use Illuminate\Log\Events\MessageLogged;
use Illuminate\Support\ServiceProvider;
use Insight\Client;
use Insight\Insight;
use Throwable;

class InsightServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        $this->mergeConfigFrom(__DIR__ . '/../../config/insight.php', 'insight');

        $this->app->singleton(Client::class, function ($app) {
            $config = $app['config']->get('insight', []);
            if (!is_array($config)) {
                $config = [];
            }
            unset($config['breadcrumbs']);

            return Insight::init($config);
        });
        $this->app->alias(Client::class, 'insight');
    }

    public function boot(): void
    {
        if ($this->app->runningInConsole()) {
            $this->publishes([
                __DIR__ . '/../../config/insight.php' => $this->app->configPath('insight.php'),
            ], 'insight-config');
            $this->commands([InsightTestCommand::class]);
        }

        $client = $this->app->make(Client::class);
        if (!$client->isEnabled()) {
            return;
        }

        $crumbs = $this->app['config']->get('insight.breadcrumbs', []);
        $events = $this->app['events'];

        if (!is_array($crumbs) || !empty($crumbs['logs'])) {
            $events->listen(MessageLogged::class, function ($event) use ($client) {
                $level = (string) $event->level;
                if ($level === 'debug') {
                    $mapped = 'debug';
                } elseif ($level === 'info' || $level === 'notice') {
                    $mapped = 'info';
                } elseif ($level === 'warning') {
                    $mapped = 'warning';
                } else {
                    $mapped = 'error';
                }
                $client->addBreadcrumb((string) $event->message, 'log', $mapped);
            });
        }

        if (!is_array($crumbs) || !empty($crumbs['sql'])) {
            $events->listen(QueryExecuted::class, function ($query) use ($client) {
                $client->addBreadcrumb(
                    Client::truncate((string) $query->sql, 200),
                    'query',
                    'info',
                    ['time_ms' => $query->time, 'connection' => $query->connectionName]
                );
            });
        }

        if ($client->getOption('performance')) {
            $this->registerPerformance($client, $events);
        }

        // PHP's shutdown hook never fires between jobs or Octane requests, so flush explicitly.
        $flush = function () use ($client) {
            $client->flush();
            $client->resetScope();
        };
        $flushEvents = [
            'Illuminate\Queue\Events\Looping',
            'Illuminate\Queue\Events\JobProcessed',
            'Illuminate\Queue\Events\JobFailed',
            'Illuminate\Queue\Events\JobExceptionOccurred',
            'Illuminate\Queue\Events\WorkerStopping',
            'Laravel\Octane\Events\RequestTerminated',
            'Laravel\Octane\Events\TaskTerminated',
            'Laravel\Octane\Events\TickTerminated',
        ];
        foreach ($flushEvents as $name) {
            $events->listen($name, $flush);
        }
    }
    /**
     * Global terminable middleware + queue job timing. Laravel 10, 11 and 12: the HTTP kernel has
     * pushMiddleware() in all three (in 11/12 bootstrap/app.php's withMiddleware() sets the global
     * list when the kernel is resolved, which is before providers boot, so a push here survives).
     * Without an HTTP kernel the middleware goes to the web and api groups instead.
     */
    private function registerPerformance(Client $client, $events): void
    {
        try {
            if (!$this->app->runningInConsole() || $this->app->runningUnitTests()) {
                $this->pushMiddleware();
            }
        } catch (Throwable $e) {
            // never break the app over monitoring
        }

        if ($client->getOption('performance_spans')) {
            (new SpanListeners())->register($events);
            $this->registerHttpPropagation($client);
            if ($client->getOption('performance_cache_spans')) {
                $this->enableRedisEvents();
            }
        }

        $starts = new \WeakMap();
        $seen = new \WeakMap();
        $events->listen('Illuminate\Queue\Events\JobProcessing', function ($event) use ($starts, $seen) {
            if (is_object($event->job)) {
                $starts[$event->job] = microtime(true);
                unset($seen[$event->job]);
                Insight::beginTrace($starts[$event->job], 'queue.task'); // DB and HTTP spans of the job; no-op inside a sync request
            }
        });
        // One transaction per ATTEMPT. A JobExceptionOccurred attempt is an error even when the job
        // will be retried (attempt-level error rate); JobFailed (final failure) only records when the
        // attempt did not already record via JobExceptionOccurred (e.g. $job->fail(), max attempts).
        $record = function ($event, bool $error, bool $once) use ($starts, $seen) {
            try {
                $job = $event->job ?? null;
                if (!is_object($job) || !isset($starts[$job]) || ($once && isset($seen[$job]))) {
                    return;
                }
                $seen[$job] = true;
                $name = method_exists($job, 'resolveName') ? (string) $job->resolveName() : get_class($job);
                Insight::recordTransaction('TASK ' . $name, (microtime(true) - $starts[$job]) * 1000, null, $error, 'queue.task');
                $client = Insight::getClient();
                if ($client !== null) {
                    $client->flushMetrics();
                }
            } catch (Throwable $e) {
                // never throw into the queue worker
            }
        };
        $events->listen('Illuminate\Queue\Events\JobProcessed', function ($event) use ($record) {
            $record($event, false, true);
        });
        $events->listen('Illuminate\Queue\Events\JobExceptionOccurred', function ($event) use ($record) {
            $record($event, true, true);
        });
        $events->listen('Illuminate\Queue\Events\JobFailed', function ($event) use ($record) {
            $record($event, true, true);
        });
    }

    /**
     * Laravel only dispatches Redis CommandExecuted when events are enabled on the Redis manager.
     * No-op without Redis (class or binding missing) and never throws.
     */
    private function enableRedisEvents(): void
    {
        try {
            $facade = 'Illuminate\\Support\\Facades\\Redis';
            if (!class_exists($facade) || !$this->app->bound('redis')) {
                return;
            }
            $root = $facade::getFacadeRoot();
            if (is_object($root) && method_exists($root, 'enableEvents')) {
                $root->enableEvents();
            }
        } catch (Throwable $e) {
            // never break the app over monitoring
        }
    }

    /**
     * Distributed tracing, outgoing side for the Laravel HTTP client (Http::get() etc.): when
     * trace_propagation_targets is set, a global REQUEST middleware adds `traceparent` to calls to matching
     * hosts. Http::globalRequestMiddleware() exists from Laravel 10.14 (10.x before that lacks it; 11 and 12
     * have it). Where it is missing nothing happens: use \Insight\Tracing\GuzzleMiddleware on the client
     * (Http::withMiddleware(GuzzleMiddleware::create())) or Insight::traceparent(). The span recorded for the
     * call takes its id from the header (SpanListeners), so both halves join.
     */
    private function registerHttpPropagation(Client $client): void
    {
        try {
            $targets = $client->getOption('trace_propagation_targets');
            if (empty($targets)) {
                return;
            }
            $facade = 'Illuminate\\Support\\Facades\\Http';
            if (!class_exists($facade)) {
                return;
            }
            $root = $facade::getFacadeRoot();
            if (!is_object($root) || !method_exists($root, 'globalRequestMiddleware')) {
                return;
            }
            $root->globalRequestMiddleware(function ($request) {
                try {
                    $c = Insight::getClient();
                    if ($c === null || !is_object($request) || !method_exists($request, 'withHeader')
                        || (method_exists($request, 'hasHeader') && $request->hasHeader('traceparent'))) {
                        return $request;
                    }
                    $uri = $request->getUri();
                    $host = strtolower((string) $uri->getHost());
                    $port = $uri->getPort();
                    $out = $host === '' ? null : $c->outgoingTrace($port !== null ? $host . ':' . $port : $host);
                    if ($out !== null) {
                        return $request->withHeader('traceparent', $out['header']);
                    }
                } catch (Throwable $e) {
                    // send the request without the header
                }

                return $request;
            });
        } catch (Throwable $e) {
            // never break the app over monitoring
        }
    }

    private function pushMiddleware(): void
    {
        $class = PerformanceMiddleware::class;
        $kernelContract = 'Illuminate\Contracts\Http\Kernel';
        if ($this->app->bound($kernelContract)) {
            $kernel = $this->app->make($kernelContract);
            if (method_exists($kernel, 'pushMiddleware')) {
                $kernel->pushMiddleware($class); // no-op when already present
                return;
            }
        }
        $router = $this->app['router'];
        if (method_exists($router, 'pushMiddlewareToGroup')) {
            foreach (['web', 'api'] as $group) {
                $router->pushMiddlewareToGroup($group, $class);
            }
        }
    }
}
