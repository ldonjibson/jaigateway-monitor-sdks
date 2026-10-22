# insight-sdk (JavaScript)

Error monitoring for **browsers, Node, React, Express and Next.js**. Zero dependencies, ~19 KB in the browser.

## Quick start

```bash
npm install insight-sdk
```

```js
import * as Insight from 'insight-sdk';

Insight.init({
  apiKey: 'ak_…',                          // a write key from Dashboard → API Keys
  host: 'https://insight.yourcompany.com', // your Insight server
  release: 'web@1.4.2',                    // your app version
  environment: 'production',
});
```

That's it: uncaught errors and unhandled promise rejections are reported, with a trail of breadcrumbs (console, clicks, navigation, fetch/XHR).

No bundler? Use the script tag (served by your JaiGateway Monitor server):

```html
<script src="https://insight.yourcompany.com/sdk/insight.min.js"
        data-api-key="ak_…" data-release="web@1.4.2" data-environment="production"></script>
```

Check your setup from a terminal: `npx insight-sdk send-test-event --host https://insight.yourcompany.com --api-key ak_…`

## Reporting things yourself

```js
try { await pay(); } catch (err) {
  Insight.captureException(err, { tags: { feature: 'checkout' }, extra: { orderId } });
}
Insight.captureMessage('Payment provider is slow', 'warning');

Insight.setUser({ id: user.id, email: user.email });
Insight.setTag('plan', 'pro');
Insight.addBreadcrumb({ category: 'cart', message: 'coupon applied' });

Insight.withScope((scope) => { scope.setTag('step', 'confirm'); Insight.captureException(err); });
```

## Node

```js
import * as Insight from 'insight-sdk';          // or: const Insight = require('insight-sdk')
Insight.init({ apiKey: process.env.INSIGHT_API_KEY, host: process.env.INSIGHT_HOST });
```

`init()` also reads `INSIGHT_API_KEY`, `INSIGHT_HOST`, `INSIGHT_RELEASE` (or your platform's commit SHA) and `INSIGHT_ENVIRONMENT`/`NODE_ENV` when you leave them out.

- **Uncaught exceptions** are reported and flushed before the process exits (exit code is still 1). **Unhandled rejections** are reported and the process keeps running.
- Each request can have its own scope (user, tags, breadcrumbs); it follows `await` automatically.

**Express / Connect**
```js
app.use(Insight.requestHandler());   // first
// … your routes …
app.use(Insight.errorHandler());     // after the routes, before your own error handler
```
Only errors that would be a 5xx are reported. Cookies and `Authorization` headers are never sent; secret-looking query values are filtered.

**Next.js (App Router)** — `instrumentation.ts`
```ts
import * as Insight from 'insight-sdk';
export async function register() { Insight.init({ apiKey: process.env.INSIGHT_API_KEY, host: process.env.INSIGHT_HOST }); }
export const onRequestError = Insight.captureRequestError;
```
In the browser, call `init` from a client component or `instrumentation-client.ts`.

**Serverless** — wrap the handler so errors are flushed before the runtime freezes:
```js
export const handler = Insight.wrap(async (event) => { … });
```

## React
```jsx
import { ErrorBoundary } from 'insight-sdk/react';
<ErrorBoundary fallback={<p>Something went wrong.</p>}><App /></ErrorBoundary>
```
The component stack is attached to the report.

## Readable stack traces (source maps)
Minified production errors point at `app.min.js:1:5821`. Upload your maps after each build and they show the original file, function and line:

```bash
npx insight-sdk sourcemaps upload ./dist --release web@1.4.2 --host https://insight.yourcompany.com --api-key ak_…
```
`--release` must equal the `release` you pass to `init()`. Maps are matched by file name, so `static/app.min.js.map` serves `https://yoursite/static/app.min.js?v=3`. Don't publish the `.map` files to the public; uploading them is enough.

## Options

| Option | Default | |
|---|---|---|
| `apiKey`, `host` | – | Required. `host` may include `/api`; trailing slashes are fine. (`endpoint` takes a full URL instead.) |
| `release`, `environment`, `serverName` | – | Shown on every error; `release` enables source maps. |
| `sampleRate` | `1` | Fraction of errors to send (0–1). |
| `beforeSend(event, hint)` | – | Edit the event, or return `null` to drop it. May be async. |
| `beforeBreadcrumb(crumb)` | – | Edit or drop a breadcrumb. |
| `ignoreErrors` | `["Script error.", "ResizeObserver loop…"]` | Strings/RegExps matched against `"Type: message"`. |
| `maxBreadcrumbs` | `100` | |
| `maxEventsPerMinute` | `60` | Safety valve so a crash loop can't burn your monthly quota. |
| `integrations` | all on | `{ console, fetch, xhr, dom, history, globalHandlers }: false` turns one off. |
| `sendDefaultPii` | `false` | Include element text in click breadcrumbs. |
| `shutdownTimeout`, `exitOnUncaughtException` | `2000`, `true` | Node only. |
| `debug` | `false` | Log what the SDK does. |

## What happens when things go wrong
The SDK never throws into your app and never blocks it. Events are queued (max 100, oldest dropped) and sent in small batches.

| Server says | SDK does |
|---|---|
| network error / 5xx | retries with backoff (1 s, 2 s, 4 s… up to 60 s), 5 attempts, then drops |
| 429 `rate_limited` | waits for `Retry-After`, then retries |
| 429 `quota_exceeded` | your plan's monthly events are used up: pauses (up to an hour at a time), drops events meanwhile, **warns once in the console** |
| 401 / 403 | wrong key or no write permission: **warns once** and turns itself off for the session |
| 413 | splits the batch; a single oversized event is dropped |

In browsers, unsent events are delivered with `keepalive` when the tab closes, and kept in `localStorage` (≤ 20 events, 24 h) if that fails. `await Insight.flush(2000)` waits for delivery.

## Privacy
No cookies, no IP collection by the SDK, request headers exclude cookies and credentials, query values like `token=` are filtered, local variables are never sent. Click breadcrumbs describe the element (`button#pay`), not its text, unless you set `sendDefaultPii`. The server also scrubs secrets and card numbers.

## Types
TypeScript definitions are included. Requires Node 18+ (global `fetch`) or a modern browser.

## Cron jobs
Wrap a scheduled job so JaiGateway Monitor alerts you when it does not run, fails, or gets stuck (create the job under **Cron jobs** in the dashboard and copy its ping URL):
```js
await Insight.monitor('https://insight.yourcompany.com/api/ping/<token>/', async () => { await runJob(); });
// or by hand: await Insight.checkIn(url, 'start' | 'success' | 'fail' | 'log', 'optional output')
```
After `init()` a bare token also works. These never throw.

## Performance monitoring (Node)
`requestHandler()` also times every request. About once a minute the SDK sends a small summary (counts, errors, a latency histogram, a few sample requests) to JaiGateway Monitor; there is no network call on the request path. Transactions are named `METHOD route-template` (`GET /items/{id}`); requests that match no route are `GET <unmatched>`; 4xx is not an error, 5xx and anything the `errorHandler()` saw is. Health checks and static files are skipped.

```js
Insight.init({ ..., performance: true,                 // default; false turns it off
               performanceIgnore: ['/internal/', /^\/v2\/ping/] });   // extra paths not to time
await Insight.transaction('TASK send-emails', async () => { /* a job, a serverless handler, a route handler */ }, { op: 'queue.task' });
Insight.recordTransaction('TASK nightly', 250);        // if you timed it yourself
```
Hitting the plan's monthly "tracked requests" quota pauses performance data only; errors keep flowing. The browser bundle does not include any of this.

### Spans and traces (Node)
Inside a request or `transaction()`, the SDK records outbound `http`/`https`/`fetch` calls and `pg` / `mysql2` queries as spans, and gives the request a `trace_id` that every error captured in it carries (the dashboard links the two). Descriptions are normalised before they leave your process: `SELECT * FROM t WHERE id = ?`, `POST api.stripe.com`. Add your own steps:
```js
await Insight.span('db.query', 'SELECT * FROM orders WHERE id = ?', () => prisma.order.findUnique({ where: { id } }));
const s = Insight.startSpan('cache', 'get'); try { /* ... */ } finally { s.finish(); }
```
Options: `performanceSpans: false` turns it off; `integrations: { http: false, fetch: false, pg: false, mysql2: false }` per source.

## Browser tracing and page performance (opt-in)
```js
Insight.init({ ..., tracing: { sampleRate: 0.1, tracePropagationTargets: ['api.mysite.com'] } });
```
npm users get the module through `insight-sdk/tracing` (loaded on demand; nothing is added to the core bundle). CDN users load `insight.tracing.min.js` first. It records page loads and navigations (route-pattern names), Core Web Vitals (LCP, INP, CLS, FCP, TTFB), the `fetch`/XHR calls a page makes, and gives errors the trace id of the page. `traceparent` is added to same-origin calls and to hosts listed in `tracePropagationTargets`; cross-origin targets must allow it in `Access-Control-Allow-Headers`. Sampling is per session (default 10%). `tracing.routeName: (location) => '/orders/[id]'` or the returned handle's `setRoute()` names pages by your router's pattern.

Server side (Node): `requestHandler()` continues an incoming `traceparent`, keeps requests of sampled traces, and `tracePropagationTargets: ['api.internal']` passes the header on to your other services. `Insight.getTraceparent()` returns the value for clients the SDK does not instrument.

### Router helpers (name pages by route pattern)
Without a helper, pages are named by guessing (`/orders/8231` becomes `/orders/{id}`). With one, the pattern comes from your router. Add it once, near the top of the app:

```jsx
// Next.js App Router: a client component rendered in your root layout
import { InsightRoute } from 'insight-sdk/next';            // <InsightRoute />
// Next.js Pages Router: in pages/_app
import { InsightPagesRoute } from 'insight-sdk/next-pages'; // <InsightPagesRoute />
// React Router (v6.4+ or v7): anywhere inside <RouterProvider> / <BrowserRouter>
import { InsightRoute } from 'insight-sdk/react-router';    // <InsightRoute />
// Vue 3 (Vue Router 3 or 4)
import { InsightVue } from 'insight-sdk/vue';               // app.use(InsightVue, { router })
```
Vue's helper also reports errors thrown inside components (tagged `vue`, with the component name). For any other router call `Insight.setRoute('/orders/{id}')` on every navigation. Patterns look like `/orders/{id}` and `/docs/{...slug}`. The helpers need the `tracing` option (page names are only used for browser tracing); call `Insight.init` anywhere, the order does not matter.

