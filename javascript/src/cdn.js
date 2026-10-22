// <script src="https://YOUR-INSIGHT/sdk/insight.min.js" data-api-key="…" data-host="https://YOUR-INSIGHT" data-release="web@1.0.0" data-environment="production"></script>
import api from './browser.js';

const g = typeof window !== 'undefined' ? window : globalThis;
g.Insight = api;
const s = typeof document !== 'undefined' ? document.currentScript : null;
if (s && s.dataset && s.dataset.apiKey) {
  const d = s.dataset;
  api.init({ apiKey: d.apiKey, host: d.host || (s.src ? new URL(s.src).origin : undefined), release: d.release, environment: d.environment, debug: d.debug === 'true', ...(d.replay === 'true' ? { replay: { sampleRate: d.replaySampleRate ? Number(d.replaySampleRate) : undefined, errorSampleRate: d.replayErrorSampleRate ? Number(d.replayErrorSampleRate) : undefined, manualStart: d.replayManualStart === 'true' } } : {}), ...(d.tracing === 'true' ? { tracing: { sampleRate: d.tracesSampleRate ? Number(d.tracesSampleRate) : undefined, tracePropagationTargets: d.tracePropagationTargets ? d.tracePropagationTargets.split(',').map((x) => x.trim()).filter(Boolean) : undefined } } : {}) });
}
