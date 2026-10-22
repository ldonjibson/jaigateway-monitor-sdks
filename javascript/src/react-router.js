// React Router (v6.4+ / v7+). Render <InsightRoute /> once inside your router so pages are named by their route pattern.
import { useContext, useEffect } from 'react';
import * as RR from 'react-router';
import { setRoute } from 'insight-sdk';
import { fromReactRouterMatches, templateFromParams } from './route-template.js';

export function useInsightRoute() {
  const location = RR.useLocation();
  const params = RR.useParams();
  // useMatches() does not carry route patterns, so ask the data router for its route objects and match the location ourselves.
  // (<BrowserRouter><Routes> apps have no route objects: they fall back to the params, which is enough for most patterns.)
  let matches = null;
  try {
    const ctx = RR.UNSAFE_DataRouterContext ? useContext(RR.UNSAFE_DataRouterContext) : null;
    const routes = ctx && ctx.router && ctx.router.routes;
    if (routes && RR.matchRoutes) matches = RR.matchRoutes(routes, location);
  } catch { matches = null; }
  const template = (matches && fromReactRouterMatches(matches)) || templateFromParams(location.pathname, params);
  useEffect(() => { setRoute(template); }, [template, location.pathname]); // every navigation, even to the same pattern
  return template;
}

export function InsightRoute() {
  useInsightRoute();
  return null;
}
