import { build } from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const common = { bundle: true, logLevel: 'warning', legalComments: 'none' };
fs.rmSync(path.join(root, 'dist'), { recursive: true, force: true });

await Promise.all([
  build({ ...common, entryPoints: ['src/browser.js'], outfile: 'dist/browser.js', format: 'esm', platform: 'browser', target: 'es2019', external: ['insight-sdk/tracing', 'insight-sdk/replay'] }),
  build({ ...common, entryPoints: ['src/replay.js'], outfile: 'dist/replay.js', format: 'esm', platform: 'browser', target: 'es2019' }),
  build({ ...common, entryPoints: ['src/cdn-replay.js'], outfile: 'dist/insight.replay.min.js', format: 'iife', platform: 'browser', target: 'es2017', minify: true, sourcemap: 'external' }),
  build({ ...common, entryPoints: ['src/tracing.js'], outfile: 'dist/tracing.js', format: 'esm', platform: 'browser', target: 'es2019' }),
  build({ ...common, entryPoints: ['src/cdn-tracing.js'], outfile: 'dist/insight.tracing.min.js', format: 'iife', platform: 'browser', target: 'es2017', minify: true, sourcemap: 'external' }),
  build({ ...common, entryPoints: ['src/node.js'], outfile: 'dist/node.js', format: 'esm', platform: 'node', target: 'node18' }),
  build({ ...common, entryPoints: ['src/node.js'], outfile: 'dist/node.cjs', format: 'cjs', platform: 'node', target: 'node18' }),
  build({ ...common, entryPoints: ['src/next.js'], outfile: 'dist/next.js', format: 'esm', platform: 'browser', external: ['react', 'next/navigation', 'insight-sdk'], banner: { js: '"use client";' } }),
  build({ ...common, entryPoints: ['src/next-pages.js'], outfile: 'dist/next-pages.js', format: 'esm', platform: 'browser', external: ['react', 'next/router', 'insight-sdk'], banner: { js: '"use client";' } }),
  build({ ...common, entryPoints: ['src/react-router.js'], outfile: 'dist/react-router.js', format: 'esm', platform: 'browser', external: ['react', 'react-router', 'insight-sdk'] }),
  build({ ...common, entryPoints: ['src/vue.js'], outfile: 'dist/vue.js', format: 'esm', platform: 'browser', external: ['insight-sdk'] }),
  build({ ...common, entryPoints: ['src/react.js'], outfile: 'dist/react.js', format: 'esm', platform: 'browser', external: ['react', 'insight-sdk'] }),
  build({ ...common, entryPoints: ['src/cdn.js'], outfile: 'dist/insight.min.js', format: 'iife', platform: 'browser', target: 'es2017', minify: true, sourcemap: 'external', define: { __INSIGHT_IIFE__: 'true' } }),
]);

// the app serves the browser bundle at /sdk/insight.min.js
const pub = path.join(root, '..', '..', 'public', 'sdk');
if (fs.existsSync(pub)) for (const f of ['insight.min.js', 'insight.tracing.min.js', 'insight.replay.min.js']) fs.copyFileSync(path.join(root, 'dist', f), path.join(pub, f));
const size = (f) => (fs.statSync(path.join(root, 'dist', f)).size / 1024).toFixed(1) + ' KB';
console.log('built:', ['browser.js', 'tracing.js', 'node.js', 'node.cjs', 'react.js', 'insight.min.js', 'insight.tracing.min.js', 'insight.replay.min.js'].map((f) => `${f} ${size(f)}`).join(', '));
