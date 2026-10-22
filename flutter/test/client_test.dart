import 'dart:convert';
import 'dart:math';

import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:insight_flutter/insight_flutter.dart';

class Rig {
  Rig({
    double sampleRate = 1.0,
    int maxBreadcrumbs = 100,
    BeforeSendCallback? beforeSend,
    bool pii = false,
    Random? random,
  }) {
    final mock = MockClient((request) async {
      requests.add(request);
      return http.Response('', 201);
    });
    final options = InsightOptions(
      apiKey: 'KEY',
      host: 'https://x.com',
      release: 'app@1.2.3',
      environment: 'test',
      sampleRate: sampleRate,
      maxBreadcrumbs: maxBreadcrumbs,
      beforeSend: beforeSend,
      sendDefaultPii: pii,
      appPackage: 'myapp',
    );
    client = InsightClient(
      options,
      random: random,
      clock: () => DateTime.utc(2026, 10, 8, 12),
      transport: InsightTransport(
        url: Uri.parse('https://x.com/api/ingest/events/'),
        apiKey: 'KEY',
        client: mock,
        delay: (d) async {},
        log: (m) {},
      ),
    );
  }

  late final InsightClient client;
  final List<http.Request> requests = <http.Request>[];

  Future<List<Map<String, dynamic>>> sent() async {
    await client.flush();
    final out = <Map<String, dynamic>>[];
    for (final r in requests) {
      for (final e in jsonDecode(r.body) as List<dynamic>) {
        out.add(e as Map<String, dynamic>);
      }
    }
    return out;
  }
}

class _Sampler implements Random {
  _Sampler(this.value);
  final double value;
  @override
  double nextDouble() => value;
  @override
  bool nextBool() => false;
  @override
  int nextInt(int max) => 0;
}

void main() {
  test('exception event has the protocol shape', () async {
    final rig = Rig();
    rig.client.setUser(const InsightUser(id: '42', email: 'a@b.com', ipAddress: '1.2.3.4'));
    rig.client.setTag('feature', 'checkout');
    rig.client.setExtra('orderId', 991);
    rig.client.addBreadcrumb(Breadcrumb(message: 'tapped pay', category: 'ui.click'));
    final stack = StackTrace.fromString(
        '#0      pay (package:myapp/pay.dart:10:5)\n#1      _rootRun (dart:async/zone.dart:1:1)\n');
    rig.client.captureException(StateError('boom'), stackTrace: stack, context: {'k': 'v'});
    final events = await rig.sent();
    expect(events.length, 1);
    final e = events.single;
    expect(rig.requests.single.headers['X-API-Key'], 'KEY');
    expect(e['event_type'], 'error');
    expect(e['action'], 'exception');
    expect(e['status'], 'error');
    expect(e['level'], 'error');
    expect(e['platform'], 'dart');
    expect(e['sdk'], {'name': 'insight.flutter', 'version': insightSdkVersion});
    expect(e['release'], 'app@1.2.3');
    expect(e['environment'], 'test');
    expect(e['timestamp'], '2026-10-08T12:00:00.000Z');
    expect(e['description'], 'StateError: Bad state: boom');
    final ex = e['exception'] as Map<String, dynamic>;
    expect(ex['type'], 'StateError');
    expect(ex['value'], 'Bad state: boom');
    final frames = ex['stacktrace'] as List<dynamic>;
    expect((frames.first as Map<String, dynamic>)['function'], 'pay');
    expect((frames.first as Map<String, dynamic>)['in_app'], true);
    expect((e['metadata'] as Map<String, dynamic>)['traceback'], contains('pay.dart'));
    final tags = e['tags'] as Map<String, dynamic>;
    expect(tags['feature'], 'checkout');
    expect(tags['runtime'], isNotNull);
    expect(tags['build_mode'], isNotNull);
    final extra = e['extra'] as Map<String, dynamic>;
    expect(extra['orderId'], 991);
    expect(extra['k'], 'v');
    expect((extra['flutter'] as Map<String, dynamic>)['target_platform'], isNotNull);
    final crumbs = e['breadcrumbs'] as List<dynamic>;
    expect((crumbs.single as Map<String, dynamic>)['category'], 'ui.click');
    // PII is withheld by default
    expect(e['user'], {'id': '42'});
  });

  test('sendDefaultPii keeps email and ip', () async {
    final rig = Rig(pii: true);
    rig.client.setUser(const InsightUser(id: '1', email: 'a@b.com', ipAddress: '9.9.9.9'));
    rig.client.captureMessage('hi');
    final e = (await rig.sent()).single;
    expect(e['user'], {'id': '1', 'email': 'a@b.com', 'ip_address': '9.9.9.9'});
  });

  test('captureMessage has no exception and honours level', () async {
    final rig = Rig();
    rig.client.captureMessage('Checkout opened', level: InsightLevel.warning);
    final e = (await rig.sent()).single;
    expect(e['description'], 'Checkout opened');
    expect(e['level'], 'warning');
    expect(e.containsKey('exception'), false);
  });

  test('FlutterErrorDetails: library, context and silent become warning', () async {
    final rig = Rig();
    rig.client.captureFlutterError(FlutterErrorDetails(
      exception: ArgumentError('bad'),
      stack: StackTrace.fromString('#0      f (package:myapp/a.dart:1:1)'),
      library: 'widgets library',
      context: ErrorDescription('while building Foo'),
      silent: true,
    ));
    rig.client.captureFlutterError(FlutterErrorDetails(exception: ArgumentError('loud')));
    final events = await rig.sent();
    final first = events[0];
    expect(first['level'], 'warning');
    final extra = first['extra'] as Map<String, dynamic>;
    expect(extra['library'], 'widgets library');
    expect(extra['context'], contains('while building Foo'));
    expect(extra['silent'], true);
    expect(events[1]['level'], 'error');
  });

  test('beforeSend can modify and can drop (null)', () async {
    final rig = Rig(beforeSend: (event) {
      if (event['description'] == 'drop me') return null;
      event['description'] = 'changed';
      return event;
    });
    rig.client.captureMessage('drop me');
    rig.client.captureMessage('keep');
    final events = await rig.sent();
    expect(events.length, 1);
    expect(events.single['description'], 'changed');
  });

  test('beforeSend that throws drops the event', () async {
    final rig = Rig(beforeSend: (event) => throw StateError('hook bug'));
    rig.client.captureMessage('x');
    expect(await rig.sent(), isEmpty);
  });

  test('sampleRate 0 drops everything, 1 keeps everything', () async {
    final none = Rig(sampleRate: 0.0);
    none.client.captureMessage('a');
    expect(await none.sent(), isEmpty);
    final all = Rig(sampleRate: 1.0);
    all.client.captureMessage('a');
    expect((await all.sent()).length, 1);
  });

  test('sampleRate 0.5 uses the random draw', () async {
    final low = Rig(sampleRate: 0.5, random: _Sampler(0.2));
    low.client.captureMessage('a');
    expect((await low.sent()).length, 1);
    final high = Rig(sampleRate: 0.5, random: _Sampler(0.7));
    high.client.captureMessage('a');
    expect(await high.sent(), isEmpty);
  });

  test('the same error object is captured once', () async {
    final rig = Rig();
    final err = StateError('same');
    rig.client.captureException(err);
    rig.client.captureException(err, stackTrace: StackTrace.current);
    rig.client.captureFlutterError(FlutterErrorDetails(exception: err));
    rig.client.captureException(StateError('same')); // different object
    expect((await rig.sent()).length, 2);
  });

  test('thrown strings are not de-duplicated', () async {
    final rig = Rig();
    rig.client.captureException('boom');
    rig.client.captureException('boom');
    expect((await rig.sent()).length, 2);
  });

  test('breadcrumbs are capped, newest kept, oldest first', () async {
    final rig = Rig(maxBreadcrumbs: 5);
    for (var i = 0; i < 12; i++) {
      rig.client.addBreadcrumb(Breadcrumb(message: 'b$i'));
    }
    rig.client.captureMessage('x');
    final crumbs = ((await rig.sent()).single['breadcrumbs']) as List<dynamic>;
    expect(crumbs.length, 5);
    expect((crumbs.first as Map<String, dynamic>)['message'], 'b7');
    expect((crumbs.last as Map<String, dynamic>)['message'], 'b11');
  });

  test('BreadcrumbBuffer never exceeds its cap', () {
    final b = BreadcrumbBuffer(3);
    for (var i = 0; i < 10; i++) {
      b.add(Breadcrumb(message: 'm$i'));
    }
    expect(b.length, 3);
    expect(b.snapshot().first.message, 'm7');
    final zero = BreadcrumbBuffer(0)..add(Breadcrumb(message: 'x'));
    expect(zero.length, 0);
  });

  test('InsightHttpClient records an http breadcrumb without query secrets', () async {
    final rig = Rig();
    final inner = MockClient((request) async => http.Response('ok', 200));
    final c = InsightHttpClient(inner, rig.client);
    await c.get(Uri.parse('https://api.example.com/v1/items?token=SECRET&x=1'));
    rig.client.captureMessage('after');
    final e = (await rig.sent()).single;
    final crumb = (e['breadcrumbs'] as List<dynamic>).single as Map<String, dynamic>;
    expect(crumb['category'], 'http');
    final data = crumb['data'] as Map<String, dynamic>;
    expect(data['method'], 'GET');
    expect(data['status_code'], 200);
    expect(data['url'], 'https://api.example.com/v1/items');
    expect(data['duration_ms'], isA<int>());
    expect(jsonEncode(e), isNot(contains('SECRET')));
  });

  test('InsightHttpClient records network errors as breadcrumbs only and rethrows', () async {
    final rig = Rig();
    final inner = MockClient((request) async => throw http.ClientException('down'));
    final c = InsightHttpClient(inner, rig.client);
    await expectLater(c.get(Uri.parse('https://api.example.com/x')), throwsA(isA<http.ClientException>()));
    expect(rig.client.breadcrumbs.length, 1);
    expect(await rig.sent(), isEmpty);
  });

  test('sanitizeUrl redacts sensitive query values when query is kept', () {
    expect(
      sanitizeUrl(Uri.parse('https://u:p@h.com/a?api_key=1&q=2#frag'), keepQuery: true),
      'https://h.com/a?api_key=[redacted]&q=2',
    );
    expect(sanitizeUrl(Uri.parse('https://h.com/a?q=2')), 'https://h.com/a');
  });

  test('InsightNavigatorObserver adds navigation breadcrumbs', () async {
    final rig = Rig();
    final observer = InsightNavigatorObserver(client: rig.client);
    Route<void> route(String name) => PageRouteBuilder<void>(
          settings: RouteSettings(name: name),
          pageBuilder: (context, a1, a2) => const SizedBox(),
        );
    final a = route('/a');
    final b = route('/b');
    observer.didPush(b, a);
    observer.didPop(b, a);
    final crumbs = rig.client.breadcrumbs.snapshot();
    expect(crumbs.length, 2);
    expect(crumbs[0].category, 'navigation');
    expect(crumbs[0].data!['to'], '/b');
    expect(crumbs[1].data!['to'], '/a');
  });

  test('a client without a host is disabled and never throws', () {
    final client = InsightClient(InsightOptions(apiKey: 'k'));
    expect(client.enabled, false);
    client.captureException(StateError('x'));
    client.captureMessage('x');
  });
}
