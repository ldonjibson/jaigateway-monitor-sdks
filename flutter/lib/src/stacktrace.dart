/// Parses `StackTrace.toString()` output into protocol frames (most recent first).
///
/// Supported formats:
///  * Dart VM:        `#0      main (package:app/main.dart:10:5)`
///  * V8 / dart2js:   `    at Object.wrapException (http://h/main.dart.js:4328:17)`
///  * Firefox/Safari: `wrapException@http://h/main.dart.js:4328:17`
///  * package:stack_trace "friendly": `package:app/main.dart 12:5  main`
///
/// All of these list the most recent call first, so the order is preserved.

const int _maxFrames = 100;

const Set<String> _libraryPackages = <String>{
  'flutter',
  'flutter_test',
  'flutter_web_plugins',
  'flutter_localizations',
  'insight_flutter',
  'sky_engine',
  'http',
  'http_parser',
  'async',
  'collection',
  'stack_trace',
  'characters',
  'meta',
  'path',
  'typed_data',
  'vector_math',
  'material_color_utilities',
  'stream_channel',
  'test_api',
  'fake_async',
  'matcher',
  'clock',
};

final RegExp _vmFrame = RegExp(r'^#\d+\s+(.*?)\s+\((.*)\)\s*$');
final RegExp _vmFrameNoLoc = RegExp(r'^#\d+\s+(\S.*?)\s*$');
final RegExp _v8Frame = RegExp(r'^\s*at\s+(.*)$');
final RegExp _friendlyFrame = RegExp(r'^(\S+)\s+(\d+)(?::(\d+))?\s+(\S.*)$');
final RegExp _locFull = RegExp(r'^(.*?):(\d+):(\d+)$');
final RegExp _locLine = RegExp(r'^(.*?):(\d+)$');

class _Loc {
  _Loc(this.file, this.line, this.col);
  final String file;
  final int? line;
  final int? col;
}

_Loc _parseLocation(String loc) {
  final full = _locFull.firstMatch(loc);
  if (full != null) {
    return _Loc(full.group(1)!, int.tryParse(full.group(2)!),
        int.tryParse(full.group(3)!));
  }
  final lineOnly = _locLine.firstMatch(loc);
  if (lineOnly != null) {
    return _Loc(lineOnly.group(1)!, int.tryParse(lineOnly.group(2)!), null);
  }
  return _Loc(loc, null, null);
}

/// Whether a frame belongs to the application (as opposed to the SDK, Flutter
/// or pub packages).
bool isInAppFrame(String? filename, {String? appPackage}) {
  if (filename == null || filename.isEmpty) return false;
  if (filename.startsWith('dart:')) return false;
  if (filename.startsWith('package:')) {
    final slash = filename.indexOf('/');
    final pkg =
        slash > 8 ? filename.substring(8, slash) : filename.substring(8);
    if (appPackage != null && appPackage.isNotEmpty) {
      return pkg == appPackage;
    }
    return !_libraryPackages.contains(pkg);
  }
  final lower = filename.toLowerCase().replaceAll('\\', '/');
  if (lower.contains('/.pub-cache/') ||
      lower.contains('/pub/cache/') ||
      lower.contains('/hosted/pub.dev/') ||
      lower.contains('/flutter/packages/') ||
      lower.contains('/flutter/bin/cache/') ||
      lower.contains('/dart-sdk/')) {
    return false;
  }
  if (lower.contains('dart_sdk') ||
      lower.endsWith('/flutter.js') ||
      lower.contains('canvaskit')) {
    return false;
  }
  return true;
}

Map<String, dynamic> _frame(String? function, _Loc loc, String? appPackage) {
  final frame = <String, dynamic>{'filename': loc.file};
  if (function != null && function.isNotEmpty) frame['function'] = function;
  if (loc.line != null) frame['lineno'] = loc.line;
  if (loc.col != null) frame['colno'] = loc.col;
  frame['in_app'] = isInAppFrame(loc.file, appPackage: appPackage);
  return frame;
}

Map<String, dynamic>? _parseLine(String raw, String? appPackage) {
  final line = raw.trim();
  if (line.isEmpty) return null;

  if (line.startsWith('#')) {
    final m = _vmFrame.firstMatch(line);
    if (m != null) {
      return _frame(m.group(1), _parseLocation(m.group(2)!), appPackage);
    }
    final n = _vmFrameNoLoc.firstMatch(line);
    if (n != null) {
      final fn = n.group(1)!;
      final out = <String, dynamic>{'function': fn, 'in_app': false};
      return out;
    }
    return null;
  }

  final v8 = _v8Frame.firstMatch(line);
  if (v8 != null) {
    final rest = v8.group(1)!.trim();
    String? fn;
    String loc = rest;
    if (rest.endsWith(')')) {
      final i = rest.indexOf(' (');
      if (i > 0) {
        fn = rest.substring(0, i);
        loc = rest.substring(i + 2, rest.length - 1);
      }
    }
    final parsed = _parseLocation(loc);
    if (parsed.line == null) return null;
    return _frame(fn, parsed, appPackage);
  }

  final friendly = _friendlyFrame.firstMatch(line);
  if (friendly != null) {
    final file = friendly.group(1)!;
    final loc = _Loc(file, int.tryParse(friendly.group(2)!),
        friendly.group(3) == null ? null : int.tryParse(friendly.group(3)!));
    return _frame(friendly.group(4), loc, appPackage);
  }

  final at = line.indexOf('@');
  if (at >= 0) {
    final fn = line.substring(0, at);
    final parsed = _parseLocation(line.substring(at + 1));
    if (parsed.line == null) return null;
    return _frame(fn, parsed, appPackage);
  }
  return null;
}

/// Parses [trace] (the text from `StackTrace.toString()`).
List<Map<String, dynamic>> parseStackTrace(String trace, {String? appPackage}) {
  final frames = <Map<String, dynamic>>[];
  for (final line in trace.split('\n')) {
    if (frames.length >= _maxFrames) break;
    try {
      final f = _parseLine(line, appPackage);
      if (f != null) frames.add(f);
    } catch (_) {
      // skip unparseable line
    }
  }
  return frames;
}
