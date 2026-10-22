import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'breadcrumbs.dart';
import 'options.dart';
import 'stacktrace.dart';

enum InsightLevel { fatal, error, warning, info, debug }

/// The user attached to events (see [InsightClient.setUser]).
class InsightUser {
  const InsightUser({this.id, this.email, this.username, this.ipAddress});

  final String? id;
  final String? email;
  final String? username;
  final String? ipAddress;

  /// Email and ip_address are only included when [includePii] is true.
  Map<String, dynamic> toJson({required bool includePii}) {
    final json = <String, dynamic>{};
    if (id != null) json['id'] = id;
    if (username != null) json['username'] = username;
    if (includePii && email != null) json['email'] = email;
    if (includePii && ipAddress != null) json['ip_address'] = ipAddress;
    return json;
  }
}

String _truncate(String s, int max) => s.length <= max ? s : s.substring(0, max);

String _safeToString(Object o) {
  try {
    return o.toString();
  } catch (_) {
    return 'Instance of \'${o.runtimeType}\'';
  }
}

String _firstLine(String s) {
  for (final line in s.split('\n')) {
    if (line.trim().isNotEmpty) return line.trim();
  }
  return s.trim();
}

Object? _encodeFallback(Object? o) {
  if (o is DateTime) return o.toUtc().toIso8601String();
  return o.toString();
}

/// Keeps `extra` under the server's 16 KB limit.
Map<String, dynamic> _capExtra(Map<String, dynamic> extra) {
  try {
    final text = jsonEncode(extra, toEncodable: _encodeFallback);
    if (text.length <= 15000) return extra;
    return <String, dynamic>{
      '_truncated': true,
      'keys': extra.keys.toList(),
    };
  } catch (_) {
    return <String, dynamic>{'_truncated': true};
  }
}

String _buildMode() =>
    kReleaseMode ? 'release' : (kProfileMode ? 'profile' : 'debug');

/// Builds one protocol event. Exactly one of [error] / [message] is expected.
Map<String, dynamic> buildEvent({
  required InsightOptions options,
  required InsightLevel level,
  required DateTime timestamp,
  Object? error,
  StackTrace? stackTrace,
  String? message,
  InsightUser? user,
  Map<String, String> tags = const <String, String>{},
  Map<String, dynamic> scopeExtra = const <String, dynamic>{},
  Map<String, dynamic>? extra,
  List<Breadcrumb> breadcrumbs = const <Breadcrumb>[],
}) {
  final event = <String, dynamic>{
    'event_type': 'error',
    'action': error != null ? 'exception' : 'message',
    'status': (level == InsightLevel.error || level == InsightLevel.fatal)
        ? 'error'
        : level.name,
    'level': level.name,
    'timestamp': timestamp.toUtc().toIso8601String(),
    'platform': 'dart',
    'sdk': <String, dynamic>{
      'name': 'insight.flutter',
      'version': insightSdkVersion,
    },
  };
  if (options.release != null) event['release'] = options.release;
  if (options.environment != null) event['environment'] = options.environment;

  final allTags = <String, String>{
    'os': defaultTargetPlatform.name,
    'runtime': kIsWeb ? 'web' : 'native',
    'build_mode': _buildMode(),
  };
  allTags.addAll(tags);
  final tagOut = <String, String>{};
  for (final entry in allTags.entries) {
    if (tagOut.length >= 50) break;
    tagOut[_truncate(entry.key, 200)] = _truncate(entry.value, 200);
  }
  event['tags'] = tagOut;

  final allExtra = <String, dynamic>{
    'flutter': <String, dynamic>{
      'target_platform': defaultTargetPlatform.name,
      'is_web': kIsWeb,
      'build_mode': _buildMode(),
    },
  };
  allExtra.addAll(scopeExtra);
  if (extra != null) allExtra.addAll(extra);
  event['extra'] = _capExtra(allExtra);

  if (user != null) {
    final u = user.toJson(includePii: options.sendDefaultPii);
    if (u.isNotEmpty) event['user'] = u;
  }

  final crumbs = breadcrumbs.length > 100
      ? breadcrumbs.sublist(breadcrumbs.length - 100)
      : breadcrumbs;
  event['breadcrumbs'] = crumbs.map((b) => b.toJson()).toList();

  if (error != null) {
    final type = error.runtimeType.toString();
    final value = _safeToString(error);
    event['description'] = _truncate(_firstLine('$type: $value'), 255);

    StackTrace? st = stackTrace;
    if (st == null && error is Error) st = error.stackTrace;
    final raw = st == null ? '' : st.toString();
    event['exception'] = <String, dynamic>{
      'type': type,
      'value': _truncate(value, 2000),
      'stacktrace': raw.isEmpty
          ? <Map<String, dynamic>>[]
          : parseStackTrace(raw, appPackage: options.appPackage),
    };
    event['metadata'] = <String, dynamic>{'traceback': _truncate(raw, 8000)};
  } else {
    event['description'] = _truncate(message ?? '', 255);
  }
  return event;
}
