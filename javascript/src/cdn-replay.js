// <script src=".../insight.replay.min.js"></script>  (load it BEFORE the core script, or after: both orders work)
import { enableReplay } from './replay.js';

const g = typeof window !== 'undefined' ? window : globalThis;
g.InsightReplay = { enableReplay };
// the core script may already have run init({ replay }): start now
if (g.Insight && g.Insight.getClient) {
  const c = g.Insight.getClient();
  if (c && c.options && c.options.replay) enableReplay(g.Insight, c.options.replay === true ? {} : c.options.replay);
}
