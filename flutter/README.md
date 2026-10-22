# insight_flutter

Error and breadcrumb reporting for Flutter apps, sent to your JaiGateway Monitor server. Android, iOS, web and desktop. No native plugins, no disk access, no `dart:io`.

## Install

```yaml
dependencies:
  insight_flutter:
    path: ../path/to/sdks/flutter   # or your git / pub source
```

## Quickstart

```dart
import 'package:flutter/material.dart';
import 'package:insight_flutter/insight_flutter.dart';

Future<void> main() async {
  await Insight.run(
    InsightOptions(apiKey: 'YOUR_WRITE_KEY', host: 'https://monitor.jaigateway.com', release: 'myapp@1.0.0', appPackage: 'myapp'),
    () => runApp(const MyApp()),
  );
}
```

`Insight.run` calls `WidgetsFlutterBinding.ensureInitialized()`, installs `FlutterError.onError` (the previous handler, by default `presentError`, still runs), `PlatformDispatcher.instance.onError`, and runs your app in `runZonedGuarded`. The three sources are de-duplicated by object identity, so each error is reported once.

Add breadcrumbs:

```dart
MaterialApp(navigatorObservers: [InsightNavigatorObserver()], ...);
final client = InsightHttpClient();           // wraps package:http
await client.get(Uri.parse('https://api.example.com/items'));
```

Manual API: `Insight.captureException(e, stackTrace: st, context: {...})`, `captureMessage('x', level: InsightLevel.warning)`, `setUser(InsightUser(id: '42'))`, `setTag`, `setTags`, `setExtra`, `addBreadcrumb(Breadcrumb(...))`, `await Insight.flush()`, `await Insight.close()`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `apiKey` | required | Write-permission key, sent as `X-API-Key` |
| `host` | | Server URL; `/api/ingest/events/` is appended. Trailing slashes and a trailing `/api` are fine |
| `endpoint` | | Full ingest URL instead of `host` (wins if both are set) |
| `release`, `environment` | null | Shown in the dashboard; `release` enables "new in release" |
| `sampleRate` | 1.0 | Fraction of events sent |
| `maxBreadcrumbs` | 100 | Breadcrumbs kept (server accepts 100) |
| `beforeSend` | null | `(event) => event` or `null` to drop. The event is the wire `Map<String, dynamic>` |
| `sendDefaultPii` | false | Allows user email / ip_address and URL query strings in breadcrumbs |
| `debug` | false | Print SDK diagnostics |
| `appRunner` | null | With `Insight.init`, runs the app in the guarded zone |
| `appPackage` | null | Your pubspec `name`; only its frames are `in_app` (otherwise all non-library packages are) |

## Behaviour on failures

Queue of 50 events in memory (oldest dropped), batches of 10, 10 s timeout, never throws into your app.

| Response | SDK does |
|---|---|
| 2xx | done |
| 400 | drops the batch |
| 401 / 403 | disables sending, logs once |
| 413 | splits the batch; a single oversized event is dropped |
| 429 `rate_limited` | waits `Retry-After` (max 60 s) and retries |
| 429 `quota_exceeded` | pauses until `Retry-After` (max 1 h), drops events meanwhile, logs once |
| 5xx / network | backoff 1 s, 2 s, 4 s ... max 60 s with jitter, 5 attempts, then dropped |

At most 30 events per minute are accepted client-side. Nothing is written to disk, so events queued when the process dies are lost; the queue is flushed when the app goes to the background (`paused`/`detached`).

## Privacy

Without `sendDefaultPii`, `setUser` sends only `id` and `username`; breadcrumb URLs lose their query string and user info. Device data is limited to target platform, web/native and build mode. Never put secrets into `setExtra`; the server scrubs common ones as a safety net.

Notes: in release web builds `runtimeType` names are minified, so error types may look odd; stack frames are still grouped by file and function.
