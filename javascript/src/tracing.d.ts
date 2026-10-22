import type { Insight } from './index';
export interface TracingOptions {
  /** Fraction of visitor sessions that send data (0..1). Default 0.1. One decision per browser session. */
  sampleRate?: number;
  /** Hosts ("api.example.com", "*.example.com", "localhost:8000", or RegExp) that calls get a `traceparent` header. Same-origin requests always do. Cross-origin targets must allow the header in Access-Control-Allow-Headers. */
  tracePropagationTargets?: (string | RegExp)[];
  /** Name pages by your router's pattern instead of guessing: (location) => "/orders/[id]". */
  routeName?: (location: Location) => string | undefined;
  /** Measure Core Web Vitals (default true). */
  vitals?: boolean;
}
export interface TracingHandle { setRoute(name: string | null): void; disable(): void; readonly traceId: string | null; readonly sampled: boolean }
export function enableTracing(sdk: Pick<Insight, 'getClient'> & Record<string, any>, options?: TracingOptions): TracingHandle | null;
export function normalizePath(pathname: string): string;
