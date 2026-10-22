/**
 * Turn a router's idea of the current route into the pattern Insight records: `/orders/{id}`.
 * Pure functions, shared by the Next.js, React Router and Vue helpers.
 */

const decode = (s) => { try { return decodeURIComponent(s); } catch { return s; } };

/** Pattern from the URL path plus the router's params: ("/orders/8231/items/a", { id: "8231", slug: ["a"] }) -> "/orders/{id}/items/{...slug}". */
export function templateFromParams(pathname, params) {
  const segs = String(pathname || '/').split('?')[0].split('#')[0].split('/');
  const used = new Set();
  const entries = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '');
  for (const [rawName, rawValue] of entries) {
    // React Router's catch-all param is called "*" and holds the rest of the path ("shoes/42"): treat it as a list of segments
    const name = rawName === '*' ? 'splat' : rawName;
    const value = typeof rawValue === 'string' && rawName === '*' ? rawValue.split('/').filter(Boolean) : rawValue;
    if (Array.isArray(value)) {
      const want = value.map(String);
      for (let i = 1; i + want.length <= segs.length; i++) {
        if (want.every((w, k) => !used.has(i + k) && decode(segs[i + k]) === w)) {
          segs.splice(i, want.length, `{...${name}}`);
          // keep indexes stable for later params: mark the replaced slot as used
          used.add(i);
          break;
        }
      }
    } else {
      const want = String(value);
      const i = segs.findIndex((s, k) => k > 0 && !used.has(k) && !/^\{/.test(s) && (s === want || decode(s) === want));
      if (i > 0) { segs[i] = `{${name}}`; used.add(i); }
    }
  }
  return segs.join('/') || '/';
}

/** Router patterns written with colons ("/orders/:id", "/files/:path*", "/:pathMatch(.*)*", "/u/:name?") -> "{id}" style. */
export function fromColonPath(path) {
  let p = String(path || '/');
  p = p.replace(/\/:([A-Za-z0-9_]+)(\([^)]*\))?([?*+]?)/g, (_, name, _re, mod) => `/{${mod === '*' || mod === '+' ? '...' : ''}${name}}`);
  p = p.replace(/\/\*$/, '/{...splat}').replace(/\/\*\//g, '/{...splat}/');
  return p === '' ? '/' : p;
}

/** React Router: join the matched routes' (relative) paths into the full pattern. */
export function fromReactRouterMatches(matches) {
  let acc = '';
  for (const m of matches || []) {
    const rp = m && m.route && m.route.path;
    if (rp === undefined || rp === null || rp === '') continue;
    acc = String(rp).startsWith('/') ? String(rp) : `${acc.replace(/\/$/, '')}/${rp}`;
  }
  return acc ? fromColonPath(acc.replace(/\/+/g, '/')) : null;
}

/** Next.js Pages Router pathname: "/orders/[id]", "/docs/[...slug]", "/docs/[[...slug]]". */
export function fromNextBracketPath(pathname) {
  return String(pathname || '/').replace(/\[\[\.\.\.([^\]]+)\]\]/g, '{...$1}').replace(/\[\.\.\.([^\]]+)\]/g, '{...$1}').replace(/\[([^\]]+)\]/g, '{$1}');
}
