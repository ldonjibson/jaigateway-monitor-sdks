import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:http/http.dart' as http;

import 'breadcrumbs.dart';
import 'event.dart';
import 'options.dart';
import 'transport.dart';

/// Instance-based client (what [Insight] wraps). Usable directly in tests.
class InsightClient {
  InsightClient(
    this.options, {
    InsightTransport? transport,
    Random? random,
    DateTime Function()? clock,
  })  : _random = random ?? Random(),
        _clock = clock ?? DateTime.now,
        breadcrumbs = BreadcrumbBuffer(options.maxBreadcrumbs) {
    if (transport != null) {
      _transport = transport;
    } else {
      final uri = options.resolveIngestUri();
      if (options.apiKey.isEmpty || uri == null) {
        debugPrint('[insight] apiKey and host (or endpoint) are required; SDK disabled.');
      } else {
        final created = options.httpClient == null ? http.Client() : null;
        _createdHttp = created;
        _transport = InsightTransport(
          url: uri,
          apiKey: options.apiKey,
          client: options.httpClient ?? created!,
          debug: options.debug,
        );
      }
    }
  }

  final InsightOptions options;
  final BreadcrumbBuffer breadcrumbs;
  final Random _random;
  final DateTime Function() _clock;

  InsightTransport? _transport;
  http.Client? _createdHttp;
  InsightUser? _user;
  final Map<String, String> _tags = <String, String>{};
  final Map<String, dynamic> _extra = <String, dynamic>{};
  final List<Object> _recentErrors = <Object>[];

  bool get enabled => _transport != null;
  InsightTransport? get transport => _transport;

  void setUser(InsightUser? user) => _user = user;
  void setTag(String key, String value) => _tags[key] = value;
  void setTags(Map<String, String> tags) => _tags.addAll(tags);
  void setExtra(String key, Object? value) => _extra[key] = value;

  void addBreadcrumb(Breadcrumb crumb) {
    try {
      breadcrumbs.add(crumb);
    } catch (_) {}
  }

  void captureException(
    Object error, {
    StackTrace? stackTrace,
    Map<String, dynamic>? context,
    InsightLevel level = InsightLevel.error,
  }) {
    _captureError(error, stackTrace, level, context);
  }

  void captureMessage(
    String message, {
    InsightLevel level = InsightLevel.info,
    Map<String, dynamic>? context,
  }) {
    try {
      if (!enabled || !_sampled()) return;
      _finish(buildEvent(
        options: options,
        level: level,
        timestamp: _clock(),
        message: message,
        user: _user,
        tags: _tags,
        scopeExtra: _extra,
        extra: context,
        breadcrumbs: breadcrumbs.snapshot(),
      ));
    } catch (e) {
      _debug('captureMessage failed: $e');
    }
  }

  /// Used by the FlutterError.onError hook.
  void captureFlutterError(FlutterErrorDetails details) {
    final extra = <String, dynamic>{};
    final lib = details.library;
    if (lib != null) extra['library'] = lib;
    final ctx = details.context;
    if (ctx != null) {
      try {
        extra['context'] = ctx.toDescription();
      } catch (_) {
        extra['context'] = ctx.toString();
      }
    }
    if (details.silent) extra['silent'] = true;
    _captureError(details.exception, details.stack,
        details.silent ? InsightLevel.warning : InsightLevel.error, extra);
  }

  Future<bool> flush({Duration timeout = const Duration(seconds: 2)}) async {
    final t = _transport;
    if (t == null) return true;
    return t.flush(timeout: timeout);
  }

  Future<void> close({Duration timeout = const Duration(seconds: 2)}) async {
    final t = _transport;
    if (t == null) return;
    await t.flush(timeout: timeout);
    t.close();
    _createdHttp?.close();
  }

  void _debug(String m) {
    if (options.debug) debugPrint('[insight] $m');
  }

  bool _sampled() {
    final rate = options.sampleRate;
    if (rate >= 1.0) return true;
    if (rate <= 0.0) return false;
    return _random.nextDouble() < rate;
  }

  /// Identity-based de-duplication of the last 20 non-primitive errors.
  bool _isDuplicate(Object error) {
    if (error is String || error is num || error is bool) return false;
    for (final e in _recentErrors) {
      if (identical(e, error)) return true;
    }
    _recentErrors.add(error);
    if (_recentErrors.length > 20) _recentErrors.removeAt(0);
    return false;
  }

  void _captureError(Object error, StackTrace? stackTrace, InsightLevel level,
      Map<String, dynamic>? extra) {
    try {
      if (!enabled) return;
      if (_isDuplicate(error)) return;
      if (!_sampled()) return;
      _finish(buildEvent(
        options: options,
        level: level,
        timestamp: _clock(),
        error: error,
        stackTrace: stackTrace,
        user: _user,
        tags: _tags,
        scopeExtra: _extra,
        extra: extra,
        breadcrumbs: breadcrumbs.snapshot(),
      ));
    } catch (e) {
      _debug('capture failed: $e');
    }
  }

  void _finish(Map<String, dynamic> event) {
    Map<String, dynamic>? out = event;
    final hook = options.beforeSend;
    if (hook != null) {
      try {
        out = hook(event);
      } catch (e) {
        _debug('beforeSend threw, event dropped: $e');
        out = null;
      }
    }
    if (out == null) return;
    _transport?.enqueue(out);
  }
}

class _LifecycleObserver with WidgetsBindingObserver {
  _LifecycleObserver(this.client);
  final InsightClient client;

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused ||
        state == AppLifecycleState.detached) {
      unawaited(client.flush());
    }
  }
}

/// Static facade. All methods are safe to call before init (they do nothing).
class Insight {
  Insight._();

  static InsightClient? _client;
  static _LifecycleObserver? _observer;
  static FlutterExceptionHandler? _flutterHandler;
  static FlutterExceptionHandler? _previousFlutterHandler;
  static bool Function(Object, StackTrace)? _dispatchHandler;
  static bool Function(Object, StackTrace)? _previousDispatchHandler;

  static InsightClient? get client => _client;
  static bool get isInitialized => _client != null;

  /// Initialises the SDK. If [InsightOptions.appRunner] is set it is run inside
  /// the guarded zone (same as [run]).
  static Future<void> init(InsightOptions options) async {
    final runner = options.appRunner;
    if (runner != null) {
      await run(options, runner);
      return;
    }
    _setup(options);
  }

  /// Recommended entry point:
  /// `await Insight.run(options, () => runApp(MyApp()));`
  static Future<void> run(
      InsightOptions options, FutureOr<void> Function() appRunner) async {
    Future<void> body() async {
      WidgetsFlutterBinding.ensureInitialized();
      _setup(options);
      await appRunner();
    }

    void onZoneError(Object error, StackTrace stack) {
      try {
        _client?.captureException(error, stackTrace: stack);
      } catch (_) {}
      if (kDebugMode) debugPrint('Unhandled error: $error\n$stack');
    }

    try {
      final result = runZonedGuarded<Future<void>>(body, onZoneError);
      if (result != null) await result;
    } catch (error, stack) {
      onZoneError(error, stack);
    }
  }

  static void _setup(InsightOptions options) {
    try {
      _teardown();
      final client = InsightClient(options);
      _client = client;
      WidgetsFlutterBinding.ensureInitialized();

      final previousFlutter = FlutterError.onError;
      void flutterHandler(FlutterErrorDetails details) {
        try {
          client.captureFlutterError(details);
        } catch (_) {}
        if (previousFlutter != null) {
          previousFlutter(details);
        } else {
          FlutterError.presentError(details);
        }
      }

      _previousFlutterHandler = previousFlutter;
      _flutterHandler = flutterHandler;
      FlutterError.onError = flutterHandler;

      final previousDispatch = PlatformDispatcher.instance.onError;
      bool dispatchHandler(Object error, StackTrace stack) {
        try {
          client.captureException(error, stackTrace: stack);
        } catch (_) {}
        if (previousDispatch != null) return previousDispatch(error, stack);
        return false;
      }

      _previousDispatchHandler = previousDispatch;
      _dispatchHandler = dispatchHandler;
      PlatformDispatcher.instance.onError = dispatchHandler;

      final observer = _LifecycleObserver(client);
      _observer = observer;
      WidgetsBinding.instance.addObserver(observer);
    } catch (e) {
      debugPrint('[insight] init failed: $e');
    }
  }

  static void _teardown() {
    final old = _client;
    if (old == null) return;
    try {
      final obs = _observer;
      if (obs != null) WidgetsBinding.instance.removeObserver(obs);
      if (identical(FlutterError.onError, _flutterHandler)) {
        FlutterError.onError = _previousFlutterHandler;
      }
      if (identical(PlatformDispatcher.instance.onError, _dispatchHandler)) {
        PlatformDispatcher.instance.onError = _previousDispatchHandler;
      }
    } catch (_) {}
    _observer = null;
    _flutterHandler = null;
    _dispatchHandler = null;
    _client = null;
    unawaited(old.close());
  }

  static void captureException(
    Object error, {
    StackTrace? stackTrace,
    Map<String, dynamic>? context,
    InsightLevel level = InsightLevel.error,
  }) {
    _client?.captureException(error,
        stackTrace: stackTrace, context: context, level: level);
  }

  static void captureMessage(
    String message, {
    InsightLevel level = InsightLevel.info,
    Map<String, dynamic>? context,
  }) {
    _client?.captureMessage(message, level: level, context: context);
  }

  static void setUser(InsightUser? user) => _client?.setUser(user);
  static void setTag(String key, String value) => _client?.setTag(key, value);
  static void setTags(Map<String, String> tags) => _client?.setTags(tags);
  static void setExtra(String key, Object? value) =>
      _client?.setExtra(key, value);
  static void addBreadcrumb(Breadcrumb crumb) => _client?.addBreadcrumb(crumb);

  static Future<bool> flush(
      {Duration timeout = const Duration(seconds: 2)}) async {
    final c = _client;
    if (c == null) return true;
    return c.flush(timeout: timeout);
  }

  /// Flushes, removes the hooks (restoring previous handlers) and disables the SDK.
  static Future<void> close() async {
    _teardown();
  }
}
