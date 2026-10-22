import 'package:flutter_test/flutter_test.dart';
import 'package:insight_flutter/insight_flutter.dart';

void main() {
  group('normalizeIngestUrl', () {
    const expected = 'https://x.com/api/ingest/events/';
    test('plain host', () {
      expect(normalizeIngestUrl('https://x.com').toString(), expected);
    });
    test('trailing slashes', () {
      expect(normalizeIngestUrl('https://x.com///').toString(), expected);
    });
    test('host ending in /api', () {
      expect(normalizeIngestUrl('https://x.com/api').toString(), expected);
      expect(normalizeIngestUrl('https://x.com/api/').toString(), expected);
    });
    test('full endpoint, with or without slash', () {
      expect(normalizeIngestUrl('https://x.com/api/ingest/events').toString(), expected);
      expect(normalizeIngestUrl('https://x.com/api/ingest/events/').toString(), expected);
    });
    test('path prefix and port', () {
      expect(normalizeIngestUrl('http://localhost:3144/').toString(),
          'http://localhost:3144/api/ingest/events/');
      expect(normalizeIngestUrl('https://x.com/insight').toString(),
          'https://x.com/insight/api/ingest/events/');
    });
    test('missing scheme defaults to https', () {
      expect(normalizeIngestUrl('x.com').toString(), expected);
    });
    test('empty host throws', () {
      expect(() => normalizeIngestUrl('  '), throwsFormatException);
    });
  });

  test('endpoint wins over host; nothing given gives null', () {
    final both = InsightOptions(
        apiKey: 'k', host: 'https://a.com', endpoint: 'https://b.com/api/');
    expect(both.resolveIngestUri().toString(), 'https://b.com/api/ingest/events/');
    expect(InsightOptions(apiKey: 'k').resolveIngestUri(), isNull);
  });
}
