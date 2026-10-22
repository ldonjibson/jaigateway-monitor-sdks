import 'package:flutter/widgets.dart';

import 'breadcrumbs.dart';
import 'insight.dart';

/// Records route push / pop / replace as `navigation` breadcrumbs.
///
/// `MaterialApp(navigatorObservers: [InsightNavigatorObserver()])`
class InsightNavigatorObserver extends NavigatorObserver {
  InsightNavigatorObserver({InsightClient? client}) : _client = client;

  final InsightClient? _client;

  String _name(Route<dynamic>? route) {
    if (route == null) return 'none';
    return route.settings.name ?? route.runtimeType.toString();
  }

  void _record(String action, Route<dynamic>? to, Route<dynamic>? from) {
    try {
      final crumb = Breadcrumb(
        category: 'navigation',
        message: '$action ${_name(to)}',
        data: <String, dynamic>{
          'action': action,
          'to': _name(to),
          'from': _name(from),
        },
      );
      final c = _client;
      if (c != null) {
        c.addBreadcrumb(crumb);
      } else {
        Insight.addBreadcrumb(crumb);
      }
    } catch (_) {}
  }

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _record('push', route, previousRoute);
  }

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _record('pop', previousRoute, route);
  }

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) {
    _record('replace', newRoute, oldRoute);
  }
}
