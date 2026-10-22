import 'package:http/http.dart' as http;

import 'breadcrumbs.dart';
import 'event.dart';
import 'insight.dart';

final RegExp _sensitiveKey = RegExp(
    r'token|key|secret|pass|auth|sig|session|code|credential',
    caseSensitive: false);

/// URL for breadcrumbs: no user info, no fragment; query dropped unless
/// [keepQuery], in which case sensitive-looking values are redacted.
String sanitizeUrl(Uri uri, {bool keepQuery = false}) {
  try {
    var out = Uri(
      scheme: uri.scheme,
      host: uri.host,
      port: uri.hasPort ? uri.port : null,
      path: uri.path,
    ).toString();
    if (keepQuery && uri.queryParameters.isNotEmpty) {
      final parts = <String>[];
      uri.queryParameters.forEach((k, v) {
        parts.add(
            '${Uri.encodeQueryComponent(k)}=${_sensitiveKey.hasMatch(k) ? '[redacted]' : Uri.encodeQueryComponent(v)}');
      });
      out = '$out?${parts.join('&')}';
    }
    return out;
  } catch (_) {
    return '[unparseable url]';
  }
}

/// Wraps a package:http client and records every request as an `http`
/// breadcrumb (method, url, status, duration). Network exceptions are recorded
/// as breadcrumbs only and then rethrown unchanged.
class InsightHttpClient extends http.BaseClient {
  InsightHttpClient([http.Client? inner, InsightClient? client])
      : _inner = inner ?? http.Client(),
        _client = client;

  final http.Client _inner;
  final InsightClient? _client;

  void _record(http.BaseRequest request, int? status, int ms, String? error) {
    try {
      final c = _client ?? Insight.client;
      if (c == null) return;
      final pii = c.options.sendDefaultPii;
      final data = <String, dynamic>{
        'method': request.method,
        'url': sanitizeUrl(request.url, keepQuery: pii),
        'duration_ms': ms,
      };
      if (status != null) data['status_code'] = status;
      if (error != null) data['error'] = error;
      final level = error != null
          ? InsightLevel.error
          : (status != null && status >= 400
              ? InsightLevel.warning
              : InsightLevel.info);
      c.addBreadcrumb(Breadcrumb(
        category: 'http',
        level: level,
        message: '${request.method} ${data['url']}',
        data: data,
      ));
    } catch (_) {}
  }

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final sw = Stopwatch()..start();
    try {
      final response = await _inner.send(request);
      sw.stop();
      _record(request, response.statusCode, sw.elapsedMilliseconds, null);
      return response;
    } catch (e) {
      sw.stop();
      _record(request, null, sw.elapsedMilliseconds, e.runtimeType.toString());
      rethrow;
    }
  }

  @override
  void close() => _inner.close();
}
