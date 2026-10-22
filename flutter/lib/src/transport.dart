import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

typedef DelayFunction = Future<void> Function(Duration duration);

class _Outcome {
  const _Outcome(this.status, {this.code, this.retryAfter});

  /// HTTP status, or 0 for network error / timeout.
  final int status;
  final String? code;
  final int? retryAfter;
}

Object? _encodeFallback(Object? o) {
  if (o is DateTime) return o.toUtc().toIso8601String();
  return o.toString();
}

/// Bounded in-memory queue + batched sender. Never throws into the app.
class InsightTransport {
  InsightTransport({
    required this.url,
    required this.apiKey,
    required http.Client client,
    this.timeout = const Duration(seconds: 10),
    this.maxQueueSize = 50,
    this.batchSize = 10,
    this.maxAttempts = 5,
    this.maxEventsPerMinute = 30,
    this.debug = false,
    Random? random,
    DateTime Function()? clock,
    DelayFunction? delay,
    void Function(String message)? log,
  })  : _client = client,
        _random = random ?? Random(),
        _clock = clock ?? DateTime.now,
        _delay = delay ?? ((Duration d) => Future<void>.delayed(d)),
        _log = log ?? ((String m) => debugPrint('[insight] $m'));

  final Uri url;
  final String apiKey;
  final Duration timeout;
  final int maxQueueSize;
  final int batchSize;
  final int maxAttempts;
  final int maxEventsPerMinute;
  final bool debug;

  final http.Client _client;
  final Random _random;
  final DateTime Function() _clock;
  final DelayFunction _delay;
  final void Function(String message) _log;

  final Queue<Map<String, dynamic>> _queue = Queue<Map<String, dynamic>>();
  final List<DateTime> _recent = <DateTime>[];
  final Set<String> _warned = <String>{};
  Future<void>? _draining;
  DateTime? _pausedUntil;
  bool _disabled = false;
  bool _closed = false;
  int _dropped = 0;

  int get queueLength => _queue.length;
  int get droppedCount => _dropped;
  bool get isDisabled => _disabled;
  DateTime? get pausedUntil => _pausedUntil;

  void _debugLog(String m) {
    if (debug) _log(m);
  }

  void _warnOnce(String key, String m) {
    if (_warned.add(key)) _log(m);
  }

  /// Queues an event. Returns false when it was dropped (disabled, paused for
  /// quota, closed, or over the client-side rate guard).
  bool enqueue(Map<String, dynamic> event) {
    try {
      if (_closed || _disabled) return false;
      final now = _clock();
      final p = _pausedUntil;
      if (p != null) {
        if (now.isBefore(p)) {
          _dropped++;
          return false;
        }
        _pausedUntil = null;
      }
      _recent.removeWhere((t) => now.difference(t) >= const Duration(minutes: 1));
      if (_recent.length >= maxEventsPerMinute) {
        _dropped++;
        _warnOnce('rate',
            'Insight: more than $maxEventsPerMinute events per minute, dropping extra events.');
        return false;
      }
      _recent.add(now);
      while (_queue.length >= maxQueueSize) {
        _queue.removeFirst();
        _dropped++;
      }
      _queue.addLast(event);
      _schedule();
      return true;
    } catch (e) {
      _debugLog('enqueue failed: $e');
      return false;
    }
  }

  void _schedule() {
    if (_draining != null || _closed || _disabled || _queue.isEmpty) return;
    // microtask: _drain must not finish synchronously before _draining is set.
    _draining = Future<void>.microtask(_drain);
  }

  Future<void> _drain() async {
    try {
      while (!_closed && !_disabled && _queue.isNotEmpty) {
        final p = _pausedUntil;
        if (p != null) {
          if (_clock().isBefore(p)) {
            _dropped += _queue.length;
            _queue.clear();
            break;
          }
          _pausedUntil = null;
        }
        final batch = <Map<String, dynamic>>[];
        while (batch.length < batchSize && _queue.isNotEmpty) {
          batch.add(_queue.removeFirst());
        }
        await _sendBatch(batch);
      }
    } catch (e) {
      _debugLog('drain failed: $e');
    } finally {
      _draining = null;
    }
  }

  Duration _backoff(int attempt) {
    final secs = min(60, 1 << min(attempt - 1, 6));
    final factor = 0.5 + 0.5 * _random.nextDouble();
    return Duration(milliseconds: (secs * 1000 * factor).round());
  }

  void _disable(int status) {
    _disabled = true;
    _dropped += _queue.length;
    _queue.clear();
    _warnOnce('auth',
        'Insight: the server rejected the API key (HTTP $status). Sending is disabled. Check apiKey and that it has write permission.');
  }

  Future<void> _sendBatch(List<Map<String, dynamic>> batch) async {
    var attempt = 0;
    while (true) {
      if (_closed || _disabled) return;
      attempt++;
      final r = await _post(batch);
      final s = r.status;
      if (s >= 200 && s < 300) return;
      if (s == 400) {
        _dropped += batch.length;
        _debugLog('server rejected batch as malformed (400), dropped');
        return;
      }
      if (s == 401 || s == 403) {
        _dropped += batch.length;
        _disable(s);
        return;
      }
      if (s == 413) {
        if (batch.length > 1) {
          final mid = batch.length ~/ 2;
          await _sendBatch(batch.sublist(0, mid));
          await _sendBatch(batch.sublist(mid));
        } else {
          _dropped++;
          _debugLog('event too large (413), dropped');
        }
        return;
      }
      if (s == 429) {
        if (r.code == 'quota_exceeded') {
          final secs = min(max(r.retryAfter ?? 3600, 1), 3600);
          _pausedUntil = _clock().add(Duration(seconds: secs));
          _dropped += batch.length;
          _warnOnce('quota',
              'Insight: monthly event quota exceeded. Pausing for ${secs}s and dropping events meanwhile.');
          return;
        }
        if (attempt >= maxAttempts) {
          _dropped += batch.length;
          _debugLog('rate limited, giving up after $attempt attempts');
          return;
        }
        final wait = min(max(r.retryAfter ?? (1 << (attempt - 1)), 1), 60);
        await _delay(Duration(seconds: wait));
        continue;
      }
      if (s == 0 || s >= 500) {
        if (attempt >= maxAttempts) {
          _dropped += batch.length;
          _debugLog('send failed (status $s), giving up after $attempt attempts');
          return;
        }
        await _delay(_backoff(attempt));
        continue;
      }
      // Any other status (404, 422, 3xx ...): not retryable.
      _dropped += batch.length;
      _debugLog('unexpected status $s, batch dropped');
      return;
    }
  }

  String? _encode(List<Map<String, dynamic>> batch) {
    try {
      return jsonEncode(batch, toEncodable: _encodeFallback);
    } catch (_) {
      return null;
    }
  }

  Future<_Outcome> _post(List<Map<String, dynamic>> batch) async {
    final body = _encode(batch);
    if (body == null) return const _Outcome(400);
    try {
      final resp = await _client
          .post(
            url,
            headers: <String, String>{
              'Content-Type': 'application/json',
              'X-API-Key': apiKey,
            },
            body: body,
          )
          .timeout(timeout);
      return _Outcome(
        resp.statusCode,
        code: _codeOf(resp),
        retryAfter: int.tryParse((resp.headers['retry-after'] ?? '').trim()),
      );
    } catch (e) {
      _debugLog('network error: $e');
      return const _Outcome(0);
    }
  }

  String? _codeOf(http.Response resp) {
    try {
      final decoded = jsonDecode(resp.body);
      if (decoded is Map) {
        final c = decoded['code'];
        if (c is String) return c;
      }
    } catch (_) {}
    return null;
  }

  /// Sends everything queued, waiting at most [timeout]. Returns true when the
  /// queue is empty afterwards.
  Future<bool> flush({Duration timeout = const Duration(seconds: 2)}) async {
    try {
      _schedule();
      final d = _draining;
      if (d != null) await d.timeout(timeout);
    } on TimeoutException {
      return false;
    } catch (_) {}
    return _queue.isEmpty && _draining == null;
  }

  /// Stops accepting events. Call [flush] first to send what is queued.
  void close() {
    _closed = true;
  }
}
