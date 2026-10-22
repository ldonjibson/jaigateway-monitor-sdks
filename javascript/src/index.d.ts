export type Level = 'fatal' | 'error' | 'warning' | 'info' | 'debug';

export interface User { id?: string | number; email?: string; username?: string; ip_address?: string; [k: string]: unknown }

export interface Breadcrumb {
  timestamp?: string;
  category?: string;
  level?: Level;
  message?: string;
  data?: Record<string, unknown>;
}

export interface StackFrame { filename?: string; function?: string; lineno?: number; colno?: number; in_app?: boolean; module?: string }

export interface InsightEvent {
  event_type: 'error';
  action: string;
  status: 'error';
  description: string;
  level: Level;
  timestamp: string;
  platform: 'javascript' | 'node';
  sdk: { name: string; version: string };
  release?: string;
  environment?: string;
  server_name?: string;
  user?: User;
  tags?: Record<string, string>;
  extra?: Record<string, unknown>;
  request?: { url?: string; method?: string; query?: string; headers?: Record<string, string> };
  breadcrumbs?: Breadcrumb[];
  exception?: { type: string; value: string; stacktrace: StackFrame[] };
  fingerprint?: string[];
  metadata?: Record<string, unknown>;
}

export interface Options {
  /** A write-permission API key. Create one under API Keys in the dashboard. */
  apiKey?: string;
  /** Your Insight server, e.g. "https://insight.example.com". "/api/ingest/events/" is added for you. */
  host?: string;
  /** Alternative to `host`: the full events URL. */
  endpoint?: string;
  /** Your app's version. Must equal the --release you upload source maps with. */
  release?: string;
  environment?: string;
  serverName?: string;
  /** 0..1, fraction of errors to send. Default 1. */
  sampleRate?: number;
  /** Default 100. */
  maxBreadcrumbs?: number;
  /** Safety valve against crash loops. Default 60. */
  maxEventsPerMinute?: number;
  /** Include element text in click breadcrumbs. Default false. */
  sendDefaultPii?: boolean;
  /** Errors whose "Type: message" matches are dropped. Defaults skip cross-origin "Script error." and ResizeObserver noise. */
  ignoreErrors?: (string | RegExp)[];
  /** Return the event (optionally modified), or null to drop it. May be async. */
  beforeSend?: (event: InsightEvent, hint: { originalException?: unknown }) => InsightEvent | null | Promise<InsightEvent | null>;
  beforeBreadcrumb?: (crumb: Breadcrumb) => Breadcrumb | null;
  /** Turn automatic instrumentation off: { console: false, fetch: false, xhr: false, dom: false, history: false, globalHandlers: false } */
  integrations?: Partial<Record<'console' | 'fetch' | 'xhr' | 'dom' | 'history' | 'globalHandlers' | 'http' | 'pg' | 'mysql2', boolean>>;
  /** Node: milliseconds to wait for delivery on fatal errors and exit. Default 2000. */
  shutdownTimeout?: number;
  /** Node: exit after reporting an uncaught exception (like Node does). Default true. */
  exitOnUncaughtException?: boolean;
  /** Node: report request timing (default true). Needs requestHandler() or transaction(). */
  performance?: boolean;
  /** Node: extra paths not to time (strings are prefixes, RegExps are tested). Health checks and static files are ignored by default. */
  performanceIgnore?: (string | RegExp)[];
  /** Node: record database and outbound HTTP spans inside requests (default true). Needs performance. */
  performanceSpans?: boolean;
  /** Node: hosts ("api.example.com", "*.example.com", "localhost:8000", or RegExp) that outgoing calls add a `traceparent` header to, so their requests join this trace. Default none. */
  tracePropagationTargets?: (string | RegExp)[];
  /** Node: how often summaries are sent (default 60000). */
  performanceFlushIntervalMs?: number;
  /** Log what the SDK does to the console. */
  debug?: boolean;
  /** Browser: turn on page performance, Core Web Vitals and traces (a separate module; see insight-sdk/tracing or insight.tracing.min.js). `true` uses the defaults (10% of sessions). */
  tracing?: boolean | import('./tracing').TracingOptions;
  /** Browser: session replay (a separate module; see insight-sdk/replay or insight.replay.min.js). Masks all text and inputs by default. `true` uses the defaults (10% of sessions, plus the seconds before an error). */
  replay?: boolean | import('./replay').ReplayOptions;
  /** Browser: report release health. One session per tab, ok → errored → crashed, so the dashboard can show crash-free sessions/users per release. Needs `release`. */
  sessions?: boolean;
  /** Share of sessions to report when `sessions` is on (0 to 1, default 1). */
  sessionSampleRate?: number;
  /** Provide your own fetch (tests, proxies). */
  fetch?: typeof fetch;
}

export interface CaptureContext {
  level?: Level;
  user?: User;
  tags?: Record<string, string>;
  extra?: Record<string, unknown>;
  fingerprint?: string | string[];
}

export interface Scope {
  setTag(key: string, value: string | number | boolean): Scope;
  setTags(tags: Record<string, string | number | boolean>): Scope;
  setExtra(key: string, value: unknown): Scope;
  setUser(user: User | null): Scope;
  setLevel(level: Level): Scope;
  setFingerprint(fp: string | string[]): Scope;
}

export interface Insight {
  init(options?: Options): Insight;
  captureException(error: unknown, context?: CaptureContext): InsightEvent | null;
  captureMessage(message: string, level?: Level, context?: CaptureContext): InsightEvent | null;
  addBreadcrumb(crumb: Breadcrumb): void;
  setUser(user: User | null): void;
  setTag(key: string, value: string | number | boolean): void;
  setTags(tags: Record<string, string | number | boolean>): void;
  setExtra(key: string, value: unknown): void;
  setFingerprint(fp: string | string[]): void;
  /** Isolate tags/user/breadcrumbs for the callback. In browsers use it synchronously; in Node it also follows async calls. */
  withScope<T>(cb: (scope: Scope) => T): T;
  configureScope(cb: (scope: Scope) => void): void;
  /** Resolves true when everything is delivered, false on timeout. */
  flush(timeoutMs?: number): Promise<boolean>;
  close(timeoutMs?: number): Promise<boolean>;
  isEnabled(): boolean;
  /** Report a cron job run. `target` is the job's ping URL (or just its token once init() was called). Never throws; resolves true if accepted. */
  checkIn(target: string, status?: 'success' | 'start' | 'fail' | 'log', message?: string): Promise<boolean>;
  /** Run `fn` as a monitored cron job: start ping, then success; on a throw it reports failure with the error text and rethrows. */
  monitor<T>(target: string, fn: () => T | Promise<T>): Promise<T>;
  /** Report anything `fn` throws (or rejects with), then rethrow. In Node it also flushes first, which makes it right for serverless handlers. */
  wrap<F extends (...args: any[]) => any>(fn: F, opts?: { flush?: boolean; timeout?: number; context?: CaptureContext }): F;
}

export const init: Insight['init'];
export const captureException: Insight['captureException'];
export const captureMessage: Insight['captureMessage'];
export const addBreadcrumb: Insight['addBreadcrumb'];
export const setUser: Insight['setUser'];
export const setTag: Insight['setTag'];
export const setTags: Insight['setTags'];
export const setExtra: Insight['setExtra'];
export const setFingerprint: Insight['setFingerprint'];
export const withScope: Insight['withScope'];
export const configureScope: Insight['configureScope'];
export const flush: Insight['flush'];
export const close: Insight['close'];
export const isEnabled: Insight['isEnabled'];
export const wrap: Insight['wrap'];
export const checkIn: Insight['checkIn'];
export const monitor: Insight['monitor'];
export const SDK_VERSION: string;
/** Browser: name the current page by your router's route pattern, e.g. "/orders/{id}". The router helpers (insight-sdk/next, /react-router, /vue) call it for you. */
export function setRoute(name: string | null): void;

/** Node only: Express / Connect middleware. Put requestHandler() first and errorHandler() after your routes. */
export function requestHandler(): (req: any, res: any, next: (err?: unknown) => void) => void;
export function errorHandler(opts?: { shouldHandleError?: (err: any) => boolean }): (err: any, req: any, res: any, next: (err?: unknown) => void) => void;
/** Node only: time any async work (a job, a serverless handler, a route handler) as a transaction named e.g. "TASK send-emails". Rethrows what `fn` throws. */
export function transaction<T>(name: string, fn: () => T | Promise<T>, opts?: { op?: string; traceparent?: string }): Promise<T>;
/** Node only: the `traceparent` value of the current request, to pass on with an HTTP client the SDK does not instrument. */
export function getTraceparent(): string | undefined;
/** Node only: time a step inside the current request or transaction. `finish(false)` marks it as failed. Does nothing outside a transaction. */
export function startSpan(op: string, description: string, data?: Record<string, string | number | boolean>): { finish(ok?: boolean, data?: Record<string, string | number | boolean>): void };
/** Node only: time an async callback as a span; rethrows what it throws. Never put values in `description` (write `SELECT ... WHERE id = ?`, not the id). */
export function span<T>(op: string, description: string, fn: () => T | Promise<T>, data?: Record<string, string | number | boolean>): Promise<T>;
/** Node only: record a transaction you timed yourself. */
export function recordTransaction(name: string, durationMs: number, opts?: { status?: number | null; error?: boolean; op?: string }): void;
/** Node only: `export const onRequestError = Insight.captureRequestError;` in Next.js instrumentation.ts */
export function captureRequestError(err: unknown, request?: { path?: string; method?: string; headers?: Record<string, string | string[] | undefined> }, context?: { routerKind?: string; routePath?: string; routeType?: string }): void;

declare const Insight: Insight & { getTraceparent: typeof getTraceparent; startSpan: typeof startSpan; span: typeof span; transaction: typeof transaction; recordTransaction: typeof recordTransaction; requestHandler: typeof requestHandler; errorHandler: typeof errorHandler; captureRequestError: typeof captureRequestError };
export default Insight;
