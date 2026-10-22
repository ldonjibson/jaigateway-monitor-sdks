// <script src=".../insight.tracing.min.js"></script>  (load it BEFORE the core script, or after: both orders work)
import { enableTracing } from './tracing.js';

const g = typeof window !== 'undefined' ? window : globalThis;
g.InsightTracing = { enableTracing };
// the core script may already have run init({ tracing }): start now
if (g.Insight && g.Insight.getClient) {
  const c = g.Insight.getClient();
  if (c && c.options && c.options.tracing) enableTracing(g.Insight, c.options.tracing === true ? {} : c.options.tracing);
}
