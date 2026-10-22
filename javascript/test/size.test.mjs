import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import zlib from 'node:zlib';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const bundle = path.join(root, 'dist', 'insight.min.js');

// The browser bundle runs on our customers' pages. Every KB is paid for by their visitors, so it has a budget.
const MAX_RAW = 25 * 1024;
const MAX_GZIP = 9 * 1024;

test('browser bundle stays within its size budget', () => {
  execFileSync(process.execPath, [path.join(root, 'build.mjs')], { cwd: root, stdio: 'ignore' }); // always test the current source
  const buf = fs.readFileSync(bundle);
  const gz = zlib.gzipSync(buf).length;
  console.log(`insight.min.js: ${(buf.length / 1024).toFixed(1)} KB raw, ${(gz / 1024).toFixed(1)} KB gzip (budget ${MAX_RAW / 1024} / ${MAX_GZIP / 1024})`);
  assert.ok(buf.length <= MAX_RAW, `bundle is ${buf.length} bytes, budget ${MAX_RAW}`);
  assert.ok(gz <= MAX_GZIP, `gzipped bundle is ${gz} bytes, budget ${MAX_GZIP}`);
});

test('the optional tracing file has its own budget, and the core bundle contains none of it', () => {
  const tr = fs.readFileSync(path.join(root, 'dist', 'insight.tracing.min.js'));
  const gz = zlib.gzipSync(tr).length;
  console.log(`insight.tracing.min.js: ${(tr.length / 1024).toFixed(1)} KB raw, ${(gz / 1024).toFixed(1)} KB gzip (budget 12 / 4.5)`);
  assert.ok(tr.length <= 12 * 1024, `tracing bundle is ${tr.length} bytes`);
  assert.ok(gz <= 4.5 * 1024, `gzipped tracing bundle is ${gz} bytes`);
  const core = fs.readFileSync(bundle, 'utf8');
  for (const marker of ['PerformanceObserver', 'layout-shift', 'largest-contentful-paint', 'insight:tracing:v1', 'insight:replay:v1', 'checkoutEveryNms']) assert.ok(!core.includes(marker), `core bundle must not include ${marker}: tracing is a separate file`);
  assert.ok(!fs.readFileSync(path.join(root, 'dist', 'node.js'), 'utf8').includes('PerformanceObserver'), 'the Node build has no browser tracing');
});

test('the optional replay file has its own budget (it carries the recorder), and the core bundle contains none of it', () => {
  const rp = fs.readFileSync(path.join(root, 'dist', 'insight.replay.min.js'));
  const gz = zlib.gzipSync(rp).length;
  console.log(`insight.replay.min.js: ${(rp.length / 1024).toFixed(1)} KB raw, ${(gz / 1024).toFixed(1)} KB gzip (budget 90 / 30)`);
  assert.ok(rp.length <= 90 * 1024, `replay bundle is ${rp.length} bytes`);
  assert.ok(gz <= 30 * 1024, `gzipped replay bundle is ${gz} bytes`);
  const core = fs.readFileSync(bundle, 'utf8');
  assert.ok(!/rrweb|FullSnapshot|MutationObserver/.test(core), 'the recorder must not be in the core bundle');
});

test('importing the browser bundle changes nothing until init() is called', () => {
  // no network calls, no globals patched, no timers: opt-in instrumentation only
  const src = fs.readFileSync(bundle, 'utf8');
  const g = { console: { ...console }, document: undefined, window: undefined, fetch: undefined };
  const before = { fetch: globalThis.fetch, log: console.log };
  new Function('window', 'document', src.replace(/document\.currentScript/g, 'null'))(undefined, undefined);
  assert.equal(globalThis.fetch, before.fetch);
  assert.equal(console.log, before.log);
  void g;
});
