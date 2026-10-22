import 'package:flutter_test/flutter_test.dart';
import 'package:insight_flutter/insight_flutter.dart';

const vmTrace = '''
#0      _MyState.build.<anonymous closure> (package:myapp/main.dart:10:5)
#1      _InkResponseState.handleTap (package:flutter/src/material/ink_well.dart:1170:21)
#2      _rootRunUnary (dart:async/zone.dart:1407:47)
#3      Helper.run (file:///Users/me/proj/lib/helper.dart:7:3)
#4      Dep.call (file:///Users/me/.pub-cache/hosted/pub.dev/dep-1.0.0/lib/dep.dart:3:9)
<asynchronous suspension>
#5      main (package:myapp/main.dart:3:3)
''';

const webTrace = '''
Error: boom
    at Object.wrapException (http://localhost:5000/main.dart.js:4328:17)
    at main.pay (http://localhost:5000/main.dart.js:120:7)
    at http://localhost:5000/main.dart.js:99:3
''';

const geckoTrace = '''
wrapException@http://localhost:5000/main.dart.js:4328:17
pay@http://localhost:5000/main.dart.js:120:7
''';

const friendlyTrace = '''
package:myapp/main.dart 12:5       main
dart:async                          _rootRun
''';

void main() {
  test('VM format, most recent first', () {
    final frames = parseStackTrace(vmTrace, appPackage: 'myapp');
    expect(frames.length, 6);
    expect(frames[0]['function'], '_MyState.build.<anonymous closure>');
    expect(frames[0]['filename'], 'package:myapp/main.dart');
    expect(frames[0]['lineno'], 10);
    expect(frames[0]['colno'], 5);
    expect(frames[0]['in_app'], true);
    expect(frames[1]['in_app'], false); // package:flutter
    expect(frames[2]['filename'], 'dart:async/zone.dart');
    expect(frames[2]['in_app'], false);
    expect(frames[3]['filename'], 'file:///Users/me/proj/lib/helper.dart');
    expect(frames[3]['in_app'], true);
    expect(frames[4]['in_app'], false); // pub-cache
    expect(frames[5]['function'], 'main');
    expect(frames[5]['lineno'], 3);
  });

  test('VM format without appPackage treats unknown packages as in_app', () {
    final frames = parseStackTrace(vmTrace);
    expect(frames[0]['in_app'], true);
    expect(frames[1]['in_app'], false);
  });

  test('web / dart2js (V8) format', () {
    final frames = parseStackTrace(webTrace);
    expect(frames.length, 3);
    expect(frames[0]['function'], 'Object.wrapException');
    expect(frames[0]['filename'], 'http://localhost:5000/main.dart.js');
    expect(frames[0]['lineno'], 4328);
    expect(frames[0]['colno'], 17);
    expect(frames[0]['in_app'], true);
    expect(frames[1]['function'], 'main.pay');
    expect(frames[2].containsKey('function'), false);
    expect(frames[2]['lineno'], 99);
  });

  test('Firefox/Safari format', () {
    final frames = parseStackTrace(geckoTrace);
    expect(frames.length, 2);
    expect(frames[0]['function'], 'wrapException');
    expect(frames[1]['colno'], 7);
  });

  test('friendly format', () {
    final frames = parseStackTrace(friendlyTrace, appPackage: 'myapp');
    expect(frames.first['filename'], 'package:myapp/main.dart');
    expect(frames.first['lineno'], 12);
    expect(frames.first['colno'], 5);
    expect(frames.first['function'], 'main');
  });

  test('garbage and empty input give no frames and do not throw', () {
    expect(parseStackTrace(''), isEmpty);
    expect(parseStackTrace('hello world\n\n???'), isEmpty);
  });

  test('frame count is capped at 100', () {
    final text = List.generate(300, (i) => '#$i      f$i (package:a/a.dart:$i:1)').join('\n');
    expect(parseStackTrace(text).length, 100);
  });
}
