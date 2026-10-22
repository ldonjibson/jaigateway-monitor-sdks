<?php

return [
    // Write-enabled API key from the Insight dashboard.
    'api_key' => env('INSIGHT_API_KEY'),

    // Base URL of your Insight server, e.g. https://insight.example.com
    'host' => env('INSIGHT_HOST'),

    'release' => env('INSIGHT_RELEASE'),
    'environment' => env('INSIGHT_ENVIRONMENT', app()->environment()),

    // 0.0 - 1.0 share of events to send.
    'sample_rate' => (float) env('INSIGHT_SAMPLE_RATE', 1.0),

    // When false (default) no user, e-mail or IP is attached automatically.
    'send_default_pii' => (bool) env('INSIGHT_SEND_PII', false),

    'max_breadcrumbs' => 100,
    'connect_timeout' => 2,
    'timeout' => 3,
    'debug' => (bool) env('INSIGHT_DEBUG', false),

    // Backend performance monitoring: one summary per route per minute, no per-request network
    // call (see the README). On by default in Laravel; INSIGHT_PERFORMANCE=false turns it off.
    'performance' => (bool) env('INSIGHT_PERFORMANCE', true),

    // Spans (where the time goes): database queries, outbound HTTP calls and queue jobs, as
    // normalised summaries. SQL literals and bindings are never sent; HTTP is "METHOD host" only.
    // Only used when `performance` is on. INSIGHT_PERFORMANCE_SPANS=false turns it off.
    'performance_spans' => (bool) env('INSIGHT_PERFORMANCE_SPANS', true),

    // Cache spans: Redis commands (command name only, with timing) and cache hit/miss/write/forget
    // markers (never keys or values). Only used when `performance_spans` is on.
    // INSIGHT_PERFORMANCE_CACHE_SPANS=false turns it off.
    'performance_cache_spans' => (bool) env('INSIGHT_PERFORMANCE_CACHE_SPANS', true),

    // Comma separated path prefixes (or #regex#) that are never measured. Leave unset for the
    // defaults: /health, /healthz, /ping, /status, /metrics, /favicon.ico, /robots.txt, /ready,
    // /live, /up, /storage/, /build/, /assets/. Setting it REPLACES the defaults.
    'performance_ignore' => env('INSIGHT_PERFORMANCE_IGNORE') !== null && env('INSIGHT_PERFORMANCE_IGNORE') !== ''
        ? array_values(array_filter(array_map('trim', explode(',', (string) env('INSIGHT_PERFORMANCE_IGNORE'))), 'strlen'))
        : null,

    // Distributed tracing: send a `traceparent` header on outgoing HTTP calls ONLY to these hosts
    // (default: none). Entries are host[:port] strings ("api.example.com", "*.example.com",
    // "localhost:8000") or #regex# matched against host[:port]. Env: comma separated list
    // INSIGHT_TRACE_PROPAGATION_TARGETS. Incoming traceparent headers are always honoured.
    'trace_propagation_targets' => env('INSIGHT_TRACE_PROPAGATION_TARGETS') !== null && env('INSIGHT_TRACE_PROPAGATION_TARGETS') !== ''
        ? array_values(array_filter(array_map('trim', explode(',', (string) env('INSIGHT_TRACE_PROPAGATION_TARGETS'))), 'strlen'))
        : [],

    // Length of the shared aggregation window in seconds.
    'performance_window_seconds' => 60,

    // Optional callable(array $event): array|null. Return null to drop. (Closures cannot be cached
    // with `config:cache`; register one at runtime with Insight\Insight::getClient() instead.)
    'before_send' => null,

    // Automatic breadcrumbs.
    'breadcrumbs' => [
        'logs' => true,
        'sql' => true,
    ],
];
