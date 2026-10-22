// Next.js Pages Router. Put <InsightPagesRoute /> in pages/_app.tsx; the router already knows the pattern ("/orders/[id]").
import { useEffect } from 'react';
import { useRouter } from 'next/router';
import { setRoute } from 'insight-sdk';
import { fromNextBracketPath } from './route-template.js';

export function useInsightRoute() {
  const router = useRouter();
  const template = fromNextBracketPath(router.pathname);
  useEffect(() => { setRoute(template); }, [template, router.asPath]); // every navigation, even to the same pattern
  return template;
}

export function InsightPagesRoute() {
  useInsightRoute();
  return null;
}
