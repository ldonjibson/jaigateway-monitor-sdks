/** Parse JavaScript stack strings (V8, Firefox, Safari) into protocol frames, most recent call first. */

const V8 = /^\s*at (?:(.+?) \()?(?:(.+?):(\d+)(?::(\d+))?|<anonymous>|native)\)?\s*$/;
const LOC = /^(.*?):(\d+)(?::(\d+))?$/;

const internal = /(^|[\\/])node_modules[\\/]|^node:|^internal\/|\(<anonymous>\)|^native$|^\[native code\]$/;

/** @param {string} filename */
export function isInApp(filename) {
  return !!filename && !internal.test(filename);
}

/**
 * @param {string|undefined} stack
 * @param {{ skip?: number, strip?: (f: string) => string }} [opts]  skip leading frames (SDK internals); strip rewrites filenames
 */
export function parseStack(stack, opts = {}) {
  if (!stack || typeof stack !== 'string') return [];
  const frames = [];
  const lines = stack.split('\n');
  for (const raw of lines) {
    const line = raw.length > 1000 ? raw.slice(0, 1000) : raw;
    let m = V8.exec(line);
    let fn, file, lineno, colno;
    if (m && /^\s*at /.test(line)) {
      fn = m[1]; file = m[2]; lineno = m[3]; colno = m[4];
      if (!file) continue;
      // "eval at foo (file:1:2), <anonymous>:3:4" and "async fn"
      if (fn) fn = fn.replace(/^async /, '');
      else if (/^\s*at async /.test(line)) { /* anonymous async frame */ }
      if (file.startsWith('async ')) file = file.slice(6);
      const ev = /^eval at .*?\((.*?):(\d+):(\d+)\)/.exec(file);
      if (ev) { file = ev[1]; lineno = ev[2]; colno = ev[3]; }
    } else if (!/^\s*at /.test(line) && line.includes('@')) {
      // Firefox / Safari: "fn@https://host/a.js:10:5"  or  "@https://host/a.js:10:5"
      const at = line.indexOf('@');
      const loc = LOC.exec(line.slice(at + 1).trim());
      if (!loc) continue;
      fn = line.slice(0, at).trim() || undefined; file = loc[1]; lineno = loc[2]; colno = loc[3];
    } else continue;
    if (file && opts.strip) file = opts.strip(file);
    const f = { filename: file, function: fn && fn !== '<anonymous>' ? fn : undefined, in_app: isInApp(file) };
    if (lineno) f.lineno = Number(lineno);
    if (colno) f.colno = Number(colno);
    frames.push(f);
  }
  const skip = opts.skip || 0;
  return (skip ? frames.slice(skip) : frames).slice(0, 100);
}
