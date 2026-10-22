import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { templateFromParams, fromColonPath, fromReactRouterMatches, fromNextBracketPath } from '../src/route-template.js';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');

test('Next.js App Router: params become {name}, catch-alls become {...name}', () => {
  assert.equal(templateFromParams('/orders/8231', { id: '8231' }), '/orders/{id}');
  assert.equal(templateFromParams('/shop/shoes/42', { category: 'shoes', productId: '42' }), '/shop/{category}/{productId}');
  assert.equal(templateFromParams('/docs/guide/intro/setup', { slug: ['guide', 'intro', 'setup'] }), '/docs/{...slug}');
  assert.equal(templateFromParams('/blog/2026/10/hello', { year: '2026', month: '10', slug: 'hello' }), '/blog/{year}/{month}/{slug}');
  assert.equal(templateFromParams('/u/a%20b', { name: 'a b' }), '/u/{name}'); // encoded path, decoded param
  assert.equal(templateFromParams('/pricing', {}), '/pricing');
  assert.equal(templateFromParams('/', undefined), '/');
  assert.equal(templateFromParams('/x/5/5', { a: '5', b: '5' }), '/x/{a}/{b}'); // two params with the same value take their own segment
  assert.equal(templateFromParams('/orders/8231?x=1#h', { id: '8231' }), '/orders/{id}');
  assert.equal(templateFromParams('/team/5', { id: '5', optional: undefined }), '/team/{id}'); // missing optional params are ignored
  // React Router's catch-all param is "*" and holds several segments
  assert.equal(templateFromParams('/shop/shoes/42', { '*': 'shoes/42' }), '/shop/{...splat}');
  assert.equal(templateFromParams('/org/7/files/a/b/c', { orgId: '7', '*': 'a/b/c' }), '/org/{orgId}/files/{...splat}');
});

test('Next.js Pages Router: [id], [...slug], [[...slug]]', () => {
  assert.equal(fromNextBracketPath('/orders/[id]'), '/orders/{id}');
  assert.equal(fromNextBracketPath('/docs/[...slug]'), '/docs/{...slug}');
  assert.equal(fromNextBracketPath('/docs/[[...slug]]'), '/docs/{...slug}');
  assert.equal(fromNextBracketPath('/a/[x]/b/[y]'), '/a/{x}/b/{y}');
  assert.equal(fromNextBracketPath('/'), '/');
});

test('colon patterns (React Router, Vue Router)', () => {
  assert.equal(fromColonPath('/orders/:id'), '/orders/{id}');
  assert.equal(fromColonPath('/users/:userId/posts/:postId/edit'), '/users/{userId}/posts/{postId}/edit');
  assert.equal(fromColonPath('/files/:path*'), '/files/{...path}');
  assert.equal(fromColonPath('/u/:name?'), '/u/{name}');
  assert.equal(fromColonPath('/items/:id(\\d+)'), '/items/{id}');
  assert.equal(fromColonPath('/:pathMatch(.*)*'), '/{...pathMatch}');
  assert.equal(fromColonPath('/shop/*'), '/shop/{...splat}');
  assert.equal(fromColonPath('/'), '/');
});

test('React Router: the matched routes are joined into the full pattern', () => {
  const m = (...paths) => paths.map((p) => ({ route: { path: p } }));
  assert.equal(fromReactRouterMatches(m('/', 'orders', ':orderId')), '/orders/{orderId}');
  assert.equal(fromReactRouterMatches(m('/app', undefined, 'users/:id', 'settings')), '/app/users/{id}/settings');
  assert.equal(fromReactRouterMatches(m('/', '/abs/:id')), '/abs/{id}'); // an absolute child path resets the chain
  assert.equal(fromReactRouterMatches(m('/', 'shop', '*')), '/shop/{...splat}');
  assert.equal(fromReactRouterMatches(m('/')), '/');
  assert.equal(fromReactRouterMatches([]), null);
  assert.equal(fromReactRouterMatches([{ route: {} }]), null);
});

/** Bundle a helper with `insight-sdk` replaced by a recorder, so we can see what it tells the SDK. */
async function load(entry, externals = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'helper-'));
  fs.writeFileSync(path.join(dir, 'stub.mjs'), "export const calls = (globalThis.__insightCalls = globalThis.__insightCalls || []); export const setRoute = (n) => calls.push(['setRoute', n]); export const captureException = (e, c) => calls.push(['captureException', e.message, c]);");
  const out = path.join(dir, 'out.mjs');
  await build({ entryPoints: [path.join(root, 'src', entry)], outfile: out, bundle: true, format: 'esm', platform: 'node', alias: { 'insight-sdk': path.join(dir, 'stub.mjs') }, external: externals, logLevel: 'silent' });
  globalThis.__insightCalls = [];
  return import(pathToFileURL(out).href + '?' + Math.random());
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test('Vue: a real Vue Router names every navigation by its record pattern, and component errors are reported', async () => {
  const req = createRequire(path.join(root, 'package.json'));
  const mod = await load('vue.js');
  const { createRouter, createMemoryHistory } = await import(pathToFileURL(req.resolve('vue-router')).href);
  const C = { template: '<div/>' };
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: C },
    { path: '/orders/:orderId', component: C },
    { path: '/users/:id', component: C, children: [{ path: 'posts/:postId', component: C }] },
    { path: '/:pathMatch(.*)*', component: C },
  ] });
  const app = { config: {} };
  mod.InsightVue.install(app, { router });
  await router.push('/');
  await router.push('/orders/8231');
  await router.push('/users/7/posts/99');
  await router.push('/definitely/not/here');
  await sleep(10);
  const names = globalThis.__insightCalls.filter((c) => c[0] === 'setRoute').map((c) => c[1]);
  assert.deepEqual(names.filter((n, i, a) => a.indexOf(n) === i), ['/', '/orders/{orderId}', '/users/{id}/posts/{postId}', '/{...pathMatch}']);
  // errors thrown inside components go to Insight, then to the app's own handler
  let previousSaw = null;
  const app2 = { config: { errorHandler: (e) => { previousSaw = e.message; } } };
  mod.InsightVue.install(app2, {});
  app2.config.errorHandler(new Error('component blew up'), { $options: { name: 'CheckoutForm' } }, 'render function');
  const rep = globalThis.__insightCalls.find((c) => c[0] === 'captureException');
  assert.equal(rep[1], 'component blew up');
  assert.deepEqual(rep[2].tags, { mechanism: 'vue' });
  assert.equal(rep[2].extra.component, 'CheckoutForm');
  assert.equal(previousSaw, 'component blew up');
});

test('Vue: a failure inside the helper never breaks the app', async () => {
  const mod = await load('vue.js');
  const app = { config: {} };
  assert.doesNotThrow(() => mod.InsightVue.install(app, { router: { afterEach() { throw new Error('router broke'); }, isReady: () => Promise.reject(new Error('x')) } }) );
  assert.doesNotThrow(() => mod.InsightVue.install(app, {}));
  assert.doesNotThrow(() => app.config.errorHandler(null, null, undefined));
});
