import 'event.dart';

/// A trail entry attached to later error events.
class Breadcrumb {
  Breadcrumb({
    this.message,
    this.category = 'custom',
    this.level = InsightLevel.info,
    this.data,
    DateTime? timestamp,
  }) : timestamp = timestamp ?? DateTime.now();

  /// One of: console, http, ui.click, navigation, log, query, custom.
  final String category;
  final String? message;
  final InsightLevel level;
  final Map<String, dynamic>? data;
  final DateTime timestamp;

  Map<String, dynamic> toJson() {
    final json = <String, dynamic>{
      'timestamp': timestamp.toUtc().toIso8601String(),
      'category': category,
      'level': level.name,
    };
    if (message != null) json['message'] = message;
    if (data != null && data!.isNotEmpty) json['data'] = data;
    return json;
  }
}

/// Bounded list of breadcrumbs, newest last. The oldest entry is dropped when full.
class BreadcrumbBuffer {
  BreadcrumbBuffer(int maxBreadcrumbs)
      : maxBreadcrumbs = maxBreadcrumbs < 0 ? 0 : maxBreadcrumbs;

  final int maxBreadcrumbs;
  final List<Breadcrumb> _items = <Breadcrumb>[];

  int get length => _items.length;

  void add(Breadcrumb crumb) {
    if (maxBreadcrumbs == 0) return;
    _items.add(crumb);
    while (_items.length > maxBreadcrumbs) {
      _items.removeAt(0);
    }
  }

  void clear() => _items.clear();

  /// A copy, oldest first.
  List<Breadcrumb> snapshot() => List<Breadcrumb>.of(_items);
}
