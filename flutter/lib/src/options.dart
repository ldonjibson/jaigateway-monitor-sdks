import 'dart:async';

import 'package:http/http.dart' as http;

/// SDK version, sent as `sdk.version`.
const String insightSdkVersion = '1.0.0';

/// Called with the finished event map just before it is queued. Return the
/// (possibly modified) event, or `null` to drop it.
typedef BeforeSendCallback = Map<String, dynamic>? Function(
    Map<String, dynamic> event);

/// Turns a host such as `https://insight.example.com`, `https://x.com/api/` or a
/// full endpoint into `https://x.com/api/ingest/events/`.
///
/// Throws [FormatException] if the result is not a valid URI.
Uri normalizeIngestUrl(String input) {
  var s = input.trim();
  if (s.isEmpty) {
    throw const FormatException('empty host');
  }
  if (!s.contains('://')) {
    s = 'https://$s';
  }
  while (s.endsWith('/')) {
    s = s.substring(0, s.length - 1);
  }
  final lower = s.toLowerCase();
  if (lower.endsWith('/api/ingest/events')) {
    // already a full endpoint
  } else if (lower.endsWith('/api')) {
    s = '$s/ingest/events';
  } else {
    s = '$s/api/ingest/events';
  }
  return Uri.parse('$s/');
}

/// Configuration for [Insight.init] / [Insight.run].
class InsightOptions {
  InsightOptions({
    required this.apiKey,
    this.host,
    this.endpoint,
    this.release,
    this.environment,
    this.sampleRate = 1.0,
    this.maxBreadcrumbs = 100,
    this.beforeSend,
    this.sendDefaultPii = false,
    this.debug = false,
    this.appRunner,
    this.appPackage,
    this.httpClient,
  });

  /// Write-permission API key (sent as `X-API-Key`).
  final String apiKey;

  /// Server base URL. `/api/ingest/events/` is appended. Trailing slashes and a
  /// host that already ends in `/api` are tolerated.
  final String? host;

  /// Alternative to [host]: a full ingest endpoint. Takes precedence over [host].
  final String? endpoint;

  final String? release;
  final String? environment;

  /// 0.0 .. 1.0, fraction of error events that are sent.
  final double sampleRate;

  /// Breadcrumbs kept in memory (the server accepts at most 100 per event).
  final int maxBreadcrumbs;

  final BeforeSendCallback? beforeSend;

  /// When false (default) user email / ip_address and URL query strings in
  /// breadcrumbs are never sent.
  final bool sendDefaultPii;

  /// Print SDK diagnostics with debugPrint.
  final bool debug;

  /// If given, [Insight.init] runs it inside the guarded zone (same as [Insight.run]).
  final FutureOr<void> Function()? appRunner;

  /// Dart package name of your app (the `name:` in pubspec.yaml). When set, only
  /// `package:<appPackage>/` frames are marked in_app. When unset, every package
  /// frame except well-known libraries is in_app.
  final String? appPackage;

  /// Custom HTTP client used for sending events (mainly for tests).
  final http.Client? httpClient;

  /// The resolved ingest URL, or `null` when no usable host/endpoint was given.
  Uri? resolveIngestUri() {
    final raw = (endpoint != null && endpoint!.trim().isNotEmpty)
        ? endpoint!
        : host;
    if (raw == null || raw.trim().isEmpty) return null;
    try {
      return normalizeIngestUrl(raw);
    } catch (_) {
      return null;
    }
  }
}
