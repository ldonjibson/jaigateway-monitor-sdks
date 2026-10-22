<?php

namespace Insight\Laravel;

use Insight\Client;
use Insight\Insight;
use Throwable;

/**
 * Facade-free glue for Laravel's exception handler.
 *
 * Laravel 11/12 (bootstrap/app.php):
 *   ->withExceptions(function (Exceptions $e) { \Insight\Laravel\Integration::handles($e); })
 *
 * Laravel 10 (app/Exceptions/Handler.php, inside register()):
 *   $this->reportable(function (Throwable $e) { \Insight\Laravel\Integration::report($e); });
 */
final class Integration
{
    private static ?\WeakMap $seen = null;

    /** Laravel 11/12. $exceptions is Illuminate\Foundation\Configuration\Exceptions. */
    public static function handles($exceptions): void
    {
        $exceptions->report(function (Throwable $e) {
            Integration::report($e);
        });
    }

    public static function report(Throwable $e, array $context = []): void
    {
        try {
            $client = Insight::getClient();
            if ($client === null || !$client->isEnabled()) {
                return;
            }
            if (self::$seen === null) {
                self::$seen = new \WeakMap();
            }
            if (isset(self::$seen[$e])) {
                return;
            }
            self::$seen[$e] = true;
            if (!self::shouldReport($e)) {
                return;
            }
            Insight::markRequestFailed(); // the current request counts as failed in performance metrics
            if (!isset($context['request'])) {
                $request = self::request();
                if ($request !== null) {
                    $context['request'] = $request;
                }
            }
            if (!isset($context['user']) && $client->getOption('send_default_pii')) {
                $user = self::user();
                if ($user !== null) {
                    $context['user'] = $user;
                }
            }
            $client->captureException($e, $context);
        } catch (Throwable $t) {
            // never throw into the app
        }
    }

    /** Respects $dontReport / dontReport() of the application's exception handler. */
    private static function shouldReport(Throwable $e): bool
    {
        try {
            $handler = \app(\Illuminate\Contracts\Debug\ExceptionHandler::class);
            if (is_object($handler) && method_exists($handler, 'shouldReport')) {
                return (bool) $handler->shouldReport($e);
            }
        } catch (Throwable $t) {
            // fall through
        }

        return true;
    }

    private static function request(): ?array
    {
        try {
            $app = \app();
            if ($app->runningInConsole() || !$app->bound('request')) {
                return null;
            }
            $request = $app->make('request');

            return [
                'url' => $request->url(),
                'method' => $request->method(),
                'query' => (string) $request->getQueryString(),
                'headers' => Client::scrubHeaders($request->headers->all()),
            ];
        } catch (Throwable $t) {
            return null;
        }
    }

    private static function user(): ?array
    {
        try {
            $user = \app('auth')->user();
            if (!is_object($user)) {
                return null;
            }
            $out = [];
            if (method_exists($user, 'getAuthIdentifier')) {
                $out['id'] = (string) $user->getAuthIdentifier();
            }
            if (isset($user->email)) {
                $out['email'] = (string) $user->email;
            }
            if (isset($user->username)) {
                $out['username'] = (string) $user->username;
            } elseif (isset($user->name)) {
                $out['username'] = (string) $user->name;
            }
            $app = \app();
            if (!$app->runningInConsole() && $app->bound('request')) {
                $ip = $app->make('request')->ip();
                if (is_string($ip)) {
                    $out['ip_address'] = $ip;
                }
            }

            return $out ? $out : null;
        } catch (Throwable $t) {
            return null;
        }
    }
}
