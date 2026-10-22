<?php

namespace Insight\Tracing;

use Insight\Insight;

/**
 * Guzzle middleware that records one "http.client" span per call, described as "METHOD host".
 * Guzzle is not a dependency of this package: its classes are only referenced inside the closures.
 *
 *   $stack = \GuzzleHttp\HandlerStack::create();
 *   $stack->push(\Insight\Tracing\GuzzleMiddleware::create(), 'insight');
 *   $client = new \GuzzleHttp\Client(['handler' => $stack]);
 */
final class GuzzleMiddleware
{
    public static function create(): callable
    {
        return static function (callable $handler) {
            return static function ($request, array $options) use ($handler) {
                $start = microtime(true);
                $method = 'GET';
                $host = '';
                try {
                    $method = strtoupper((string) $request->getMethod());
                    $uri = $request->getUri();
                    $host = strtolower((string) $uri->getHost());
                    $port = $uri->getPort();
                    if ($port !== null) {
                        $host .= ':' . $port;
                    }
                } catch (\Throwable $e) {
                    // describe the call as just the method
                }
                $spanId = null;
                try {
                    $client = Insight::getClient();
                    if ($client !== null && $host !== '') {
                        $out = $client->outgoingTrace($host); // null unless host matches trace_propagation_targets and a trace is active
                        if ($out !== null && method_exists($request, 'withHeader')
                            && !(method_exists($request, 'hasHeader') && $request->hasHeader('traceparent'))) { // the app's own header wins
                            $request = $request->withHeader('traceparent', $out['header']);
                            $spanId = $out['span_id'];
                        }
                    }
                } catch (\Throwable $e) {
                    // send the request without the header
                }
                $promise = $handler($request, $options);
                try {
                    return $promise->then(
                        static function ($response) use ($method, $host, $start, $spanId) {
                            $status = null;
                            try {
                                $status = (int) $response->getStatusCode();
                            } catch (\Throwable $e) {
                            }
                            self::finish($method, $host, $start, $status !== null && $status < 500, $status, $spanId);

                            return $response;
                        },
                        static function ($reason) use ($method, $host, $start, $spanId) {
                            $status = null;
                            try {
                                if (is_object($reason) && method_exists($reason, 'getResponse') && is_object($reason->getResponse())) {
                                    $status = (int) $reason->getResponse()->getStatusCode();
                                }
                            } catch (\Throwable $e) {
                            }
                            self::finish($method, $host, $start, $status !== null && $status < 500, $status, $spanId);
                            if ($reason instanceof \Throwable) {
                                throw $reason;
                            }
                            if (class_exists('GuzzleHttp\Promise\Create')) {
                                return \GuzzleHttp\Promise\Create::rejectionFor($reason);
                            }

                            return \GuzzleHttp\Promise\rejection_for($reason);
                        }
                    );
                } catch (\Throwable $e) {
                    return $promise; // never break the request over monitoring
                }
            };
        };
    }

    private static function finish(string $method, string $host, float $start, bool $ok, ?int $status, ?string $spanId = null): void
    {
        try {
            $data = ['http.method' => $method];
            if ($host !== '') {
                $data['host'] = $host;
            }
            if ($status !== null) {
                $data['http.status_code'] = $status;
            }
            Insight::recordSpan('http.client', $method . ' ' . $host, (microtime(true) - $start) * 1000, $ok, $data, $start, $spanId);
        } catch (\Throwable $e) {
            // never throw into the app
        }
    }
}
