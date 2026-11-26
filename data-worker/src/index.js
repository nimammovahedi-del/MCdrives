// GET /<key> returns the object <key> of the bucket. Every build writes under its own prefix
// (<area>/<commit>/), so an object never changes once written: it is cached for a year at the edge
// and in the browser. Any site may read (the viewer is on another host), nobody may write.
const TYPES = { bin: 'application/octet-stream', png: 'image/png', json: 'application/json' };
const CORS = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS' };

export default {
  async fetch(req, env, ctx) {
    if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: { ...CORS, 'Access-Control-Max-Age': '86400' } });
    if (req.method !== 'GET' && req.method !== 'HEAD') return new Response('method not allowed', { status: 405, headers: CORS });
    const key = decodeURIComponent(new URL(req.url).pathname.slice(1));
    if (!key || key.endsWith('/')) return new Response('mcmaps world data', { headers: CORS });
    const cache = caches.default, ck = new Request(new URL(req.url).origin + '/' + key);
    let res = await cache.match(ck);
    if (!res) {
      const o = await env.WORLD.get(key);
      if (!o) return new Response('not found', { status: 404, headers: CORS });
      const h = new Headers(CORS);
      h.set('Content-Type', TYPES[key.split('.').pop()] ?? 'application/octet-stream');
      h.set('Cache-Control', 'public, max-age=31536000, immutable');
      h.set('ETag', o.httpEtag);
      res = new Response(o.body, { headers: h });
      ctx.waitUntil(cache.put(ck, res.clone()));
    }
    return req.method === 'HEAD' ? new Response(null, { headers: res.headers }) : res;
  },
};
