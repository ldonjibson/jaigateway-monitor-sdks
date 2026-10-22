import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizePath, clsFromEntries, inpFromDurations } from '../src/tracing.js';

test('page names are route templates, never raw URLs', () => {
  assert.equal(normalizePath('/orders/8231'), '/orders/{id}');
  assert.equal(normalizePath('/orders/8231/items/3f2504e0-4f89-41d3-9a0c-0305e82c3301'), '/orders/{id}/items/{id}');
  assert.equal(normalizePath('/users/deadbeef12345678/settings'), '/users/{id}/settings');
  assert.equal(normalizePath('/s/AbCdEfGhIjKlMnOpQrStUv'), '/s/{id}');
  assert.equal(normalizePath('/pricing'), '/pricing');
  assert.equal(normalizePath('/blog/how-to-monitor-apis'), '/blog/how-to-monitor-apis');
  assert.equal(normalizePath('/'), '/');
  assert.equal(normalizePath('/a?token=abc#frag'), '/a'); // query and hash never leave the browser
  assert.ok(normalizePath('/' + Array(30).fill('x').join('/')).split('/').length <= 10);
});

test('CLS is the largest session window of shifts, ignoring shifts right after input', () => {
  const e = (t, v, input = false) => ({ startTime: t, value: v, hadRecentInput: input });
  assert.equal(clsFromEntries([]), 0);
  assert.equal(clsFromEntries([e(100, 0.05), e(600, 0.05)]).toFixed(2), '0.10'); // one window
  assert.equal(clsFromEntries([e(100, 0.05), e(3000, 0.2)]).toFixed(2), '0.20'); // 1 s gap: a new window, the larger wins
  assert.equal(clsFromEntries([e(100, 0.5, true), e(200, 0.01)]).toFixed(2), '0.01'); // user-caused shifts do not count
  // a window is capped at 5 seconds even if shifts keep coming
  const long = Array.from({ length: 12 }, (_, i) => e(i * 800, 0.01));
  assert.ok(clsFromEntries(long) < 0.12 && clsFromEntries(long) > 0.05);
});

test('INP is the slowest interaction, or about the 98th percentile for many', () => {
  assert.equal(inpFromDurations([]), null);
  assert.equal(inpFromDurations([80, 400, 120]), 400);
  const many = Array.from({ length: 100 }, (_, i) => i + 1);
  assert.equal(inpFromDurations(many), 98); // the worst two of 100 (one per 50 interactions) are ignored
});
