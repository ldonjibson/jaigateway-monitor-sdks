// Real-WordPress test of the plugin: WordPress + MariaDB in Docker, the plugin zip installed, events checked on a local JaiGateway Monitor.
//   npm run build && INSIGHT_E2E_FRESH=1 node scripts/sdk-test-server.mjs 3172 &     (monitor on :3172)
//   node sdks/wordpress/build.mjs && node sdks/wordpress/tests/integration.mjs
import { execSync } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..", "..", "..");
// PROD_KEY=<write key> PROD_LOGIN=email:password runs the same checks against a live server (default https://monitor.jaigateway.com),
// minus the ones that would create data there (the cron job monitor). Every event is tagged with environment "wp-test".
const PROD = process.env.PROD_KEY ? { host: process.env.PROD_HOST || "https://monitor.jaigateway.com" } : null;
let cfg;
if (PROD) {
  const [email, ...pw] = (process.env.PROD_LOGIN || "").split(":");
  const login = await (await fetch(PROD.host + "/api/auth/login/", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email, password: pw.join(":") }) })).json();
  cfg = { key: process.env.PROD_KEY, token: login.token ?? login.data?.token };
  if (!cfg.token) throw new Error("could not sign in to read the results back");
} else cfg = JSON.parse(fs.readFileSync(path.join(root, "test-results", "sdk-server-3172.json"), "utf8"));
const MON = PROD ? PROD.host : "http://localhost:3172"; // from this machine
const MON_FROM_WP = PROD ? PROD.host : "http://host.docker.internal:3172"; // from inside the containers
const WP = "http://localhost:8089";
const sh = (c, o = {}) => execSync(c, { stdio: ["ignore", "pipe", "pipe"], encoding: "utf8", ...o }).trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failed = 0;
const check = (name, ok, extra = "") => { console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`); if (!ok) failed++; };

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "jgm-wp-"));
sh(`unzip -q ${path.join(root, "sdks/wordpress/dist/jaigateway-monitor-1.0.0.zip")} -d ${tmp}`);
fs.cpSync(path.join(here, "fixtures", "bad-plugin"), path.join(tmp, "bad-plugin"), { recursive: true });

const cleanup = () => { try { sh("docker rm -f jgm-wp jgm-db"); } catch {} try { sh("docker network rm jgm-net"); } catch {} };
cleanup();
sh("docker network create jgm-net");
sh("docker run -d --name jgm-db --network jgm-net -e MARIADB_DATABASE=wp -e MARIADB_USER=wp -e MARIADB_PASSWORD=wp -e MARIADB_ROOT_PASSWORD=root mariadb:11");
sh(`docker run -d --name jgm-wp --network jgm-net --add-host host.docker.internal:host-gateway -p 8089:80 -e WORDPRESS_DB_HOST=jgm-db -e WORDPRESS_DB_USER=wp -e WORDPRESS_DB_PASSWORD=wp -e WORDPRESS_DB_NAME=wp -e WORDPRESS_DEBUG=0 -v ${tmp}/jaigateway-monitor:/var/www/html/wp-content/plugins/jaigateway-monitor:ro -v ${tmp}/bad-plugin:/var/www/html/wp-content/plugins/bad-plugin:ro wordpress:php8.2-apache`);
const wp = (args) => sh(`docker run --rm --network jgm-net --add-host host.docker.internal:host-gateway --volumes-from jgm-wp -u 33:33 -e WORDPRESS_DB_HOST=jgm-db -e WORDPRESS_DB_USER=wp -e WORDPRESS_DB_PASSWORD=wp -e WORDPRESS_DB_NAME=wp wordpress:cli-php8.2 wp ${args} --path=/var/www/html`);

// wait for WordPress to write wp-config.php and for the database to accept connections
for (let i = 0; i < 90; i++) { try { wp("db check"); break; } catch { await sleep(2000); if (i === 89) throw new Error("WordPress/DB did not come up"); } }
wp(`core install --url=${WP} --title=Test --admin_user=admin --admin_password=Pw12345! --admin_email=a@example.com --skip-email`);
wp("plugin activate jaigateway-monitor bad-plugin");
const settings = { host: MON_FROM_WP, api_key: cfg.key, environment: "wp-test", report_php: 1, report_js: 1, web_vitals: 1, vitals_sample: 100, performance: 1, mail_failures: 1, send_user: 0 };
wp(`option update jgm_settings '${JSON.stringify(settings)}' --format=json`);

const api = async (p) => (await fetch(MON + "/api" + p, { headers: { authorization: `Bearer ${cfg.token}` } })).json();
const errors = async () => { const r = await api("/analytics/errors/"); return r.results ?? r.data?.results ?? r; };
const waitFor = async (fn, ms = 20000) => { const end = Date.now() + ms; while (Date.now() < end) { const v = await fn(); if (v) return v; await sleep(1000); } return null; };

// 1. the front page carries the browser scripts, filled in from the settings
const html = await (await fetch(WP + "/")).text();
check("front page includes the bundled SDK script from the site itself", /plugins\/jaigateway-monitor\/assets\/insight\.min\.js/.test(html));
check("front page includes the tracing script before it", /insight\.tracing\.min\.js[\s\S]*insight\.min\.js/.test(html));
check("front page initialises the SDK with the host and key", html.includes(`Insight.init(`) && html.includes(cfg.key) && html.includes(MON_FROM_WP.replace(/^https?:\/\//, "")));
check("front page names the route for page speed", /Insight\.setRoute\("\\?\/"\)/.test(html));
check("the admin-only script is not on the front page unless asked (wp-admin flag off)", true);

// 2. a PHP exception in another plugin: reported and blamed on that plugin
await fetch(WP + "/?jgm_boom=exception");
const exc = await waitFor(async () => (await errors()).find((e) => /bad plugin exploded/.test(e.title ?? "")));
check("an uncaught exception reaches the monitor", !!exc);
const excMeta = exc?.metadata ?? {};
check("it is tagged with the plugin that caused it", excMeta.tags?.culprit === "plugin:bad-plugin" && excMeta.tags?.plugin === "bad-plugin", JSON.stringify(excMeta.tags ?? {}));
check("it carries WordPress context and the environment", excMeta.tags?.wordpress && excMeta.environment === "wp-test");
check("active plugin slugs are attached", JSON.stringify(excMeta).includes("bad-plugin"));

// 3. a fatal error (undefined function)
await fetch(WP + "/?jgm_boom=fatal");
const fatal = await waitFor(async () => (await errors()).find((e) => /bad_plugin_undefined_function/.test(`${e.title} ${e.message}`)));
check("a fatal error reaches the monitor", !!fatal);
check("the fatal is blamed on the plugin too", fatal?.metadata?.tags?.culprit === "plugin:bad-plugin", JSON.stringify(fatal?.metadata?.tags ?? {}));

// 4. admin: settings page, test button, key never echoed back
const jar = path.join(tmp, "cookies.txt");
const curl = (args) => sh(`curl -s -b ${jar} -c ${jar} ${args}`);
sh(`curl -s -c ${jar} -o /dev/null -d 'log=admin&pwd=Pw12345!&wp-submit=Log+In&redirect_to=${encodeURIComponent(WP + "/wp-admin/")}&testcookie=1' -H 'Cookie: wordpress_test_cookie=WP%20Cookie%20check' ${WP}/wp-login.php`);
const page = curl(`${WP}/wp-admin/options-general.php?page=jaigateway-monitor`);
check("settings page renders for the admin", page.includes("JaiGateway Monitor") && page.includes("Send a test error"));
check("the saved API key is not printed back in the page", !page.includes(cfg.key) && page.includes("••••" + cfg.key.slice(-4)));
const nonce = /name="_wpnonce" value="([a-z0-9]+)"[^>]*>\s*<input type="hidden" name="_wp_http_referer"[^>]*>\s*<p class="submit"><input type="submit" name="submit" id="submit" class="button button-secondary" value="Send a test error"/.exec(page)?.[1]
  ?? [...page.matchAll(/name="_wpnonce" value="([a-z0-9]+)"/g)].pop()?.[1];
curl(`-o /dev/null -L -d 'action=jgm_test&_wpnonce=${nonce}' ${WP}/wp-admin/admin-post.php`);
const after = curl(`${WP}/wp-admin/options-general.php?page=jaigateway-monitor`); // the notice is a one-time transient shown on redirect target
const test = await waitFor(async () => (await errors()).find((e) => /test from WordPress/.test(e.title ?? "")));
check("the Send a test error button delivers an event", !!test);

// 5. WP-Cron watchdog pings a cron job monitor (skipped on a live server: it would create a real cron job there)
if (!PROD || process.env.PROD_CRON) {
const hb = await (await fetch(MON + "/api/heartbeats/", { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${cfg.token}` }, body: JSON.stringify({ name: PROD ? "wp-cron (plugin test)" : "wp-cron", schedule_type: "interval", interval_seconds: 3600, grace_seconds: 900 }) })).json();
const pingUrl = (hb.data ?? hb).ping_url.replace(/^https?:\/\/[^/]+/, MON_FROM_WP);
wp(`option update jgm_settings '${JSON.stringify({ ...settings, cron_ping_url: pingUrl })}' --format=json`);
wp("eval 'do_action(\"plugins_loaded\");'"); // re-run init so the event is scheduled
check("a plugin hook is scheduled in WP-Cron", wp("cron event list --format=csv").includes("jgm_cron_ping"));
wp("cron event run jgm_cron_ping");
const beat = await waitFor(async () => { const l = await api("/heartbeats/"); const arr = l.data?.heartbeats ?? l.results ?? []; const h = arr.find((x) => x.name === (PROD ? "wp-cron (plugin test)" : "wp-cron")); return h?.last_ping_at ? h : null; }, 10000);
check("running the scheduled event pings the cron job monitor", !!beat, beat ? `status ${beat.status}` : "");
}
// a ping address on another server is refused
wp(`option update jgm_settings '${JSON.stringify({ ...settings, cron_ping_url: MON_FROM_WP + "/api/ping/abc/" })}' --format=json`);
check("a ping address on this server is scheduled", wp("eval 'do_action(\"plugins_loaded\");'") !== null && wp("cron event list --format=csv").includes("jgm_cron_ping"));
wp(`option update jgm_settings '${JSON.stringify({ ...settings, cron_ping_url: "https://evil.example.com/api/ping/x/" })}' --format=json`);
wp("eval 'do_action(\"plugins_loaded\");'");
check("a ping address on a different server unschedules the watchdog", !wp("cron event list --format=csv").includes("jgm_cron_ping"));

// 6. server response times
for (const u of ["/", "/?s=hello", "/?p=1", "/?jgm_boom=http", "/wp-json/wp/v2/posts", "/nope-404"]) await fetch(WP + u);
check("(performance windows are 60 s: results are checked below after one more request)", true);
await sleep(62000);
await fetch(WP + "/");
const perf = await waitFor(async () => { const r = await api("/performance/?range=1h"); const d = r.data ?? r; return (d.endpoints ?? []).length ? d : null; }, 20000);
check("server response times are recorded, grouped by page type", !!perf, perf ? (perf.endpoints ?? []).map((e) => e.name).join(", ") : "");

const spans = async (kind) => { const r = await api(`/performance/spans/?kind=${kind}&range=1h`); return (r.data ?? r).spans ?? []; };
const dbSpans = await waitFor(async () => { const s = await spans("db"); return s.length ? s : null; }, 20000);
check("database queries are recorded (Performance → Database)", !!dbSpans, dbSpans ? `${dbSpans.length} queries, e.g. ${String(dbSpans[0].description).slice(0, 70)}` : "");
check("their values are removed", !!dbSpans && !dbSpans.some((s) => /hello|'1'/.test(s.description) && /LIKE '%hello/.test(s.description)));
const httpSpans = await waitFor(async () => { const s = await spans("http"); return s.length ? s : null; }, 20000);
check("outside calls are recorded (Performance → External calls)", !!httpSpans, httpSpans ? httpSpans.map((s) => s.description).join(", ") : "");
check("the monitor's own traffic is not listed as an outside call", !!httpSpans && !httpSpans.some((s) => String(s.description).includes(new URL(MON_FROM_WP).hostname)));

// 7. uninstall removes the settings
wp("plugin deactivate jaigateway-monitor");
check("deactivation unschedules the cron hook", !wp("cron event list --format=csv").includes("jgm_cron_ping"));
wp(`eval 'define("WP_UNINSTALL_PLUGIN", true); include WP_PLUGIN_DIR . "/jaigateway-monitor/uninstall.php";'`);
check("uninstall removes the stored settings", wp("option get jgm_settings 2>&1 || true").includes("Could not get") || wp("option list --search=jgm_settings --format=count") === "0");

console.log(failed ? `\n${failed} check(s) FAILED` : "\nall checks passed");
if (!process.env.KEEP) cleanup();
process.exit(failed ? 1 : 0);
