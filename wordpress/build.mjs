// Builds dist/jaigateway-monitor-<version>.zip: the plugin folder plus the bundled PHP SDK and browser scripts.
//   node sdks/wordpress/build.mjs        (run `npm --prefix sdks/javascript run build` first if the browser scripts changed)
import fs from "fs";
import path from "path";
import { execFileSync } from "child_process";
import { fileURLToPath } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));
const plugin = path.join(here, "jaigateway-monitor");
const root = path.join(here, "..", "..");
const rm = (p) => fs.rmSync(p, { recursive: true, force: true });

// 1. the PHP SDK (everything except the Laravel integration)
const lib = path.join(plugin, "lib", "Insight");
rm(path.join(plugin, "lib"));
fs.mkdirSync(lib, { recursive: true });
const copy = (from, to) => {
  fs.mkdirSync(to, { recursive: true });
  for (const f of fs.readdirSync(from, { withFileTypes: true })) {
    if (f.name === "Laravel") continue;
    if (f.isDirectory()) copy(path.join(from, f.name), path.join(to, f.name));
    else if (f.name.endsWith(".php")) fs.copyFileSync(path.join(from, f.name), path.join(to, f.name));
  }
};
copy(path.join(root, "sdks", "php", "src"), lib);

// 2. the browser scripts (served from the site itself: nothing is loaded from another server)
const assets = path.join(plugin, "assets");
fs.mkdirSync(assets, { recursive: true });
for (const f of ["insight.min.js", "insight.tracing.min.js"]) fs.copyFileSync(path.join(root, "public", "sdk", f), path.join(assets, f));

// 3. the zip
const version = /Version:\s+([0-9.]+)/.exec(fs.readFileSync(path.join(plugin, "jaigateway-monitor.php"), "utf8"))[1];
const dist = path.join(here, "dist");
fs.mkdirSync(dist, { recursive: true });
const out = path.join(dist, `jaigateway-monitor-${version}.zip`);
rm(out);
execFileSync("zip", ["-qr", out, "jaigateway-monitor", "-x", "*.DS_Store"], { cwd: here });
console.log(`built ${path.relative(root, out)} (${(fs.statSync(out).size / 1024).toFixed(0)} KB)`);
