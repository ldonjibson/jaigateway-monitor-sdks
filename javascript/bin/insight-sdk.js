#!/usr/bin/env node
/**
 * insight-sdk CLI
 *   insight-sdk send-test-event [--host URL] [--api-key KEY]
 *   insight-sdk sourcemaps upload <dir> --release <name> [--host URL] [--api-key KEY]
 * Defaults come from INSIGHT_HOST, INSIGHT_API_KEY and INSIGHT_RELEASE.
 */
import fs from 'node:fs';
import path from 'node:path';
import { resolveEndpoints } from '../src/core.js';

const args = process.argv.slice(2);
const flag = (name, env) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : process.env[env];
};
const die = (msg) => { console.error(msg); process.exit(1); };
const help = () => console.log(`insight-sdk
  send-test-event               send one test error to check your setup
  sourcemaps upload <dir>       upload every *.map under <dir> for a release
options: --host URL  --api-key KEY  --release NAME   (or INSIGHT_HOST / INSIGHT_API_KEY / INSIGHT_RELEASE)`);

const host = flag('host', 'INSIGHT_HOST');
const apiKey = flag('api-key', 'INSIGHT_API_KEY');
const [cmd, sub, dir] = args;

async function post(url, body) {
  let res;
  try {
    res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey }, body: JSON.stringify(body) });
  } catch (e) {
    die(`Could not reach ${url}: ${e.message}`);
  }
  let json = null;
  try { json = await res.json(); } catch { /* not JSON */ }
  return { status: res.status, json };
}

if (!cmd || cmd === '--help' || cmd === 'help') { help(); process.exit(0); }
if (!host || !apiKey) die('Need --host and --api-key (or INSIGHT_HOST and INSIGHT_API_KEY).');
const ep = resolveEndpoints({ host });

if (cmd === 'send-test-event') {
  const err = new Error('Insight test event: if you can see this in your dashboard, the SDK is set up correctly.');
  const { parseStack } = await import('../src/stack.js');
  const { status, json } = await post(ep.events, {
    event_type: 'error', action: 'exception', status: 'error', level: 'info', platform: 'node', sdk: { name: 'insight.cli', version: '1.0.0' },
    description: `Error: ${err.message}`, exception: { type: 'Error', value: err.message, stacktrace: parseStack(err.stack) }, tags: { test: 'true' },
  });
  if (status === 201) console.log(`✓ Event accepted (HTTP 201). Open your dashboard → Errors.`);
  else die(`✗ HTTP ${status}: ${(json && (json.error || json.detail)) || 'unexpected response'}${status === 401 ? '\n  Check the API key and that it has write permission.' : status === 429 ? '\n  Quota or rate limit reached.' : ''}`);
} else if (cmd === 'sourcemaps' && sub === 'upload') {
  const release = flag('release', 'INSIGHT_RELEASE');
  if (!dir) die('Usage: insight-sdk sourcemaps upload <dir> --release <name>');
  if (!release) die('Need --release (or INSIGHT_RELEASE). It must equal the `release` option you pass to init().');
  const found = [];
  (function walk(d) {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) { if (e.name !== 'node_modules') walk(p); } else if (e.name.endsWith('.map')) found.push(p);
    }
  })(path.resolve(dir));
  if (!found.length) die(`No .map files found under ${dir}. Enable source maps in your bundler (e.g. build.sourcemap: true).`);
  const base = path.resolve(dir);
  const files = found.map((p) => ({ name: path.relative(base, p).split(path.sep).join('/'), content: fs.readFileSync(p, 'utf8') }));
  // stay under the server's 25 MB request limit, 50 files per request
  const chunks = [];
  let cur = [], size = 0;
  for (const f of files) {
    if (cur.length && (cur.length >= 50 || size + f.content.length > 20 * 1024 * 1024)) { chunks.push(cur); cur = []; size = 0; }
    cur.push(f); size += f.content.length;
  }
  if (cur.length) chunks.push(cur);
  let total = 0;
  for (const c of chunks) {
    const { status, json } = await post(ep.sourcemaps, { release, files: c });
    if (status !== 201) die(`✗ Upload failed (HTTP ${status}): ${(json && (json.error || json.detail)) || 'unexpected response'}`);
    total += c.length;
  }
  console.log(`✓ Uploaded ${total} source map${total === 1 ? '' : 's'} for release "${release}".`);
} else {
  help();
  process.exit(1);
}
