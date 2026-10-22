// Next.js App Router. Put <InsightRoute /> once in a client layout (e.g. app/providers.tsx) to name pages by their route pattern.
import { useEffect } from 'react';
import { usePathname, useParams } from 'next/navigation';
import { setRoute } from 'insight-sdk';
import { templateFromParams } from './route-template.js';

/** `/orders/8231` with params { id: '8231' } -> `/orders/{id}`. Also tells Insight, so browser tracing records the pattern. */
export function useInsightRoute() {
  const pathname = usePathname();
  const params = useParams();
  const template = templateFromParams(pathname || '/', params || {});
  // re-announce on every navigation, even when the pattern is the same (/orders/1 -> /orders/2): each page record needs its name
  useEffect(() => { setRoute(template); }, [template, pathname]);
  return template;
}

export function InsightRoute() {
  useInsightRoute();
  return null;
}
