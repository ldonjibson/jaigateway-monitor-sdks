import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:insight_flutter/insight_flutter.dart';

class Harness {
  Harness(this.responder, {int maxPerMinute = 1000, int maxQueue = 50, DateTime Function()? clock}) {
    client = MockClient((request) async {
      requests.add(request);
      return responder(request, requests.length);
    });
    transport = InsightTransport(
      url: Uri.parse('https://x.com/api/ingest/events/'),
      apiKey: 'KEY',
      client: client,
      maxEventsPerMinute: maxPerMinute,
      maxQueueSize: maxQueue,
      random: Random(1),
      clock: clock,
      delay: (d) async => delays.add(d),
      log: logs.add,
    );
  }

  final http.Response Function(http.Request request, int n) responder;
  late final MockClient client;
  late final InsightTransport transport;
  final List<http.Request> requests = <http.Request>[];
  final List<Duration> delays = <Duration>[];
  final List<String> logs = <String>[];

  List<dynamic> bodyOf(int i) => jsonDecode(requests[i].body) as List<dynamic>;
}

Map<String, dynamic> ev(int i) => <String, dynamic>{'event_type': 'error', 'description': 'e$i'};

http.Response resp(int status, {String body = '', Map<String, String>? headers}) =>
    http.Response(body, status, headers: headers ?? <String, String>{});

void main() {
  test('201: sends array with X-API-Key and JSON content type', () async {
    final h = Harness((r, n) => resp(201));
    h.transport.enqueue(ev(1));
    expect(await h.transport.flush(), true);
    expect(h.requests.length, 1);
    expect(h.requests[0].headers['X-API-Key'], 'KEY');
    expect(h.requests[0].headers['Content-Type'], contains('application/json'));
    expect(h.requests[0].url.toString(), 'https://x.com/api/ingest/events/');
    expect(h.bodyOf(0).length, 1);
  });

  test('batching: 25 events go out as 10 + 10 + 5', () async {
    final h = Harness((r, n) => resp(201));
    for (var i = 0; i < 25; i++) {
      h.transport.enqueue(ev(i));
    }
    await h.transport.flush();
    expect(h.requests.map((r) => (jsonDecode(r.body) as List<dynamic>).length).toList(), [10, 10, 5]);
  });

  test('queue is bounded at 50, oldest dropped', () async {
    final h = Harness((r, n) => resp(201));
    for (var i = 0; i < 60; i++) {
      h.transport.enqueue(ev(i));
    }
    expect(h.transport.queueLength, 50);
    expect(h.transport.droppedCount, 10);
    await h.transport.flush();
    final all = <dynamic>[];
    for (var i = 0; i < h.requests.length; i++) {
      all.addAll(h.bodyOf(i));
    }
    expect(all.length, 50);
    expect((all.first as Map<String, dynamic>)['description'], 'e10');
    expect((all.last as Map<String, dynamic>)['description'], 'e59');
  });

  test('400: dropped, not retried', () async {
    final h = Harness((r, n) => resp(400));
    h.transport.enqueue(ev(1));
    await h.transport.flush();
    expect(h.requests.length, 1);
    expect(h.transport.queueLength, 0);
    expect(h.transport.isDisabled, false);
  });

  for (final status in <int>[401, 403]) {
    test('$status: disables, logs once, no retry', () async {
      final h = Harness((r, n) => resp(status));
      h.transport.enqueue(ev(1));
      await h.transport.flush();
      expect(h.transport.isDisabled, true);
      expect(h.transport.enqueue(ev(2)), false);
      await h.transport.flush();
      expect(h.requests.length, 1);
      expect(h.logs.length, 1);
    });
  }

  test('413 splits the batch until single events are dropped', () async {
    final h = Harness((r, n) {
      final count = (jsonDecode(r.body) as List<dynamic>).length;
      return count > 2 ? resp(413, body: '{"code":"batch_too_large"}') : resp(201);
    });
    for (var i = 0; i < 4; i++) {
      h.transport.enqueue(ev(i));
    }
    await h.transport.flush();
    // 4 (413) -> 2 + 2 (both ok)
    expect(h.requests.length, 3);
    expect(h.transport.queueLength, 0);
  });

  test('413 on a single event drops it', () async {
    final h = Harness((r, n) => resp(413, body: '{"code":"payload_too_large"}'));
    h.transport.enqueue(ev(1));
    await h.transport.flush();
    expect(h.requests.length, 1);
    expect(h.transport.queueLength, 0);
  });

  test('429 rate_limited waits Retry-After then retries', () async {
    final h = Harness((r, n) => n == 1
        ? resp(429, body: '{"code":"rate_limited"}', headers: {'retry-after': '7'})
        : resp(201));
    h.transport.enqueue(ev(1));
    await h.transport.flush();
    expect(h.requests.length, 2);
    expect(h.delays, [const Duration(seconds: 7)]);
  });

  test('429 quota_exceeded pauses, drops meanwhile, resumes later', () async {
    var now = DateTime.utc(2026, 1, 1);
    final h = Harness(
      (r, n) => n == 1
          ? resp(429, body: '{"code":"quota_exceeded"}', headers: {'retry-after': '600'})
          : resp(201),
      clock: () => now,
    );
    h.transport.enqueue(ev(1));
    await h.transport.flush();
    expect(h.requests.length, 1);
    expect(h.transport.pausedUntil, now.add(const Duration(seconds: 600)));
    expect(h.transport.enqueue(ev(2)), false);
    await h.transport.flush();
    expect(h.requests.length, 1);
    expect(h.logs.length, 1);
    now = now.add(const Duration(seconds: 601));
    expect(h.transport.enqueue(ev(3)), true);
    await h.transport.flush();
    expect(h.requests.length, 2);
  });

  test('429 quota_exceeded caps the pause at one hour', () async {
    final now = DateTime.utc(2026, 1, 1);
    final h = Harness(
      (r, n) => resp(429, body: '{"code":"quota_exceeded"}', headers: {'retry-after': '999999'}),
      clock: () => now,
    );
    h.transport.enqueue(ev(1));
    await h.transport.flush();
    expect(h.transport.pausedUntil, now.add(const Duration(hours: 1)));
  });

  test('5xx retries with growing jittered backoff and gives up after 5 attempts', () async {
    final h = Harness((r, n) => resp(503));
    h.transport.enqueue(ev(1));
    await h.transport.flush();
    expect(h.requests.length, 5);
    expect(h.delays.length, 4);
    for (var i = 0; i < 4; i++) {
      final maxMs = 1000 * (1 << i);
      expect(h.delays[i].inMilliseconds, lessThanOrEqualTo(maxMs));
      expect(h.delays[i].inMilliseconds, greaterThanOrEqualTo(maxMs ~/ 2));
    }
    expect(h.transport.queueLength, 0);
  });

  test('5xx then success stops retrying', () async {
    final h = Harness((r, n) => n < 3 ? resp(500) : resp(201));
    h.transport.enqueue(ev(1));
    await h.transport.flush();
    expect(h.requests.length, 3);
  });

  test('network error is retried and never thrown', () async {
    var calls = 0;
    final client = MockClient((request) async {
      calls++;
      if (calls < 3) throw http.ClientException('offline');
      return resp(201);
    });
    final transport = InsightTransport(
      url: Uri.parse('https://x.com/api/ingest/events/'),
      apiKey: 'K',
      client: client,
      delay: (d) async {},
      log: (m) {},
    );
    transport.enqueue(ev(1));
    expect(await transport.flush(), true);
    expect(calls, 3);
  });

  test('timeout counts as a network failure', () async {
    final client = MockClient((request) => Completer<http.Response>().future);
    final transport = InsightTransport(
      url: Uri.parse('https://x.com/api/ingest/events/'),
      apiKey: 'K',
      client: client,
      timeout: const Duration(milliseconds: 20),
      maxAttempts: 2,
      delay: (d) async {},
      log: (m) {},
    );
    transport.enqueue(ev(1));
    expect(await transport.flush(timeout: const Duration(seconds: 5)), true);
  });

  test('rate guard: at most 30 events per minute, then window slides', () async {
    var now = DateTime.utc(2026, 1, 1);
    final h = Harness((r, n) => resp(201), maxPerMinute: 30, clock: () => now);
    var accepted = 0;
    for (var i = 0; i < 40; i++) {
      if (h.transport.enqueue(ev(i))) accepted++;
    }
    expect(accepted, 30);
    expect(h.logs.length, 1);
    now = now.add(const Duration(seconds: 61));
    expect(h.transport.enqueue(ev(99)), true);
  });

  test('flush on empty queue returns true immediately', () async {
    final h = Harness((r, n) => resp(201));
    expect(await h.transport.flush(), true);
    expect(h.requests, isEmpty);
  });
}
