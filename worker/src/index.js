// MC MAP live traffic: one small Cloudflare Worker between the public feeds and the app.
//
//   GET /v1/incidents.json   CHP incidents (about every minute) + Caltrans District 12 lane closures (5 min),
//                            trimmed to Orange County
//   GET /v1/speeds.json      Caltrans PeMS District 12 freeway detector speeds (5 min), with station positions
//   GET /v1/tomtom/incidents.json        TomTom incident details for the county (cached 20 min)
//   GET /v1/tomtom/flow/{z}/{x}/{y}.pbf  TomTom vector flow tiles, Orange County only (cached 5 min)
//   GET /v1/health           which feeds answered and whether the TomTom key is set
//
// The phone never calls a feed directly. Each answer is kept in this isolate's memory and in the Cloudflare
// cache for its refresh period, so the feeds see one request per period per data centre, not one per user,
// and the TomTom key (a Worker secret, TOMTOM_KEY) never reaches the app.
import STATIONS from './stations.json';

const OC = { s: 33.38, w: -118.13, n: 33.95, e: -117.40 };  // Orange County, with a small margin
const inOC = (lat, lon) => lat >= OC.s && lat <= OC.n && lon >= OC.w && lon <= OC.e;
const UA = { 'User-Agent': 'mcmaps-traffic (hobby navigation demo)' };
const memo = new Map();  // key -> { t, body, type }

const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, OPTIONS', 'Access-Control-Max-Age': '86400' };
const send = (body, type, ttl, status = 200) => new Response(body, { status, headers: { ...cors, 'Content-Type': type, 'Cache-Control': `public, max-age=${ttl}` } });

// one cached value per key and period: memory first, then the data centre's cache, then the source
async function cached(key, ttl, type, make, ctx) {
  const now = Date.now(), m = memo.get(key);
  if (m && now - m.t < ttl * 1000) return send(m.body, m.type, Math.max(5, ttl - Math.round((now - m.t) / 1000)));
  const ck = new Request('https://mcmaps-traffic.cache/' + key);
  const hit = await caches.default.match(ck).catch(() => null);
  if (hit) { const body = await hit.arrayBuffer(); memo.set(key, { t: now, body, type }); return send(body, type, ttl); }
  let body;
  try { body = await make(); }
  catch (e) {
    if (m) return send(m.body, m.type, 15);   // keep serving the last good answer while a feed is down
    return send(JSON.stringify({ error: String(e?.message ?? e) }), 'application/json', 15, 502);
  }
  memo.set(key, { t: now, body, type });
  ctx.waitUntil(caches.default.put(ck, send(body, type, ttl)).catch(() => {}));
  return send(body, type, ttl);
}

async function get(url, as = 'text') {
  const r = await fetch(url, { headers: UA, cf: { cacheTtl: 30 } });
  if (!r.ok) throw new Error(`${new URL(url).host} answered ${r.status}`);
  return as === 'json' ? r.json() : as === 'buf' ? r.arrayBuffer() : r.text();
}

// ---------- CHP: https://media.chp.ca.gov/sa_xml/sa.xml (statewide; values come wrapped in quotes,
// positions as "33684512:117826433" = 33.684512, -117.826433). Kept: Orange County, last 6 hours.
const MON = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
function chpTime(s) {  // "Nov 24 2026  9:24AM", Pacific time -> epoch seconds (PST/PDT by the US rule)
  const m = /(\w{3}) (\d+) (\d{4})\s+(\d+):(\d+)(AM|PM)/.exec(s); if (!m) return 0;
  let h = +m[4] % 12; if (m[6] === 'PM') h += 12;
  const y = +m[3], mo = MON[m[1]], d = +m[2];
  const sunday = (month, nth) => { const f = new Date(Date.UTC(y, month, 1)).getUTCDay(); return 1 + ((7 - f) % 7) + 7 * (nth - 1); };
  const dst = (mo > 2 && mo < 10) || (mo === 2 && d >= sunday(2, 2)) || (mo === 10 && d < sunday(10, 1));
  return Date.UTC(y, mo, d, h + (dst ? 7 : 8), +m[5]) / 1000;
}
const tag = (s, t) => { const m = new RegExp(`<${t}>"?([^<]*?)"?</${t}>`).exec(s); return m ? m[1].trim() : ''; };
async function chp() {
  const xml = await get('https://media.chp.ca.gov/sa_xml/sa.xml'), out = [], cut = Date.now() / 1000 - 6 * 3600;
  let center = '', dispatch = '';
  for (const m of xml.matchAll(/<Center ID\s*=\s*"([^"]+)"|<Dispatch ID\s*=\s*"([^"]+)"|<Log ID\s*=\s*"([^"]+)">([\s\S]*?)<\/Log>/g)) {
    if (m[1]) { center = m[1]; continue; }
    if (m[2]) { dispatch = m[2]; continue; }
    const body = m[4], ll = /<LATLON>"?(\d+):(\d+)"?<\/LATLON>/.exec(body);
    if (!ll) continue;
    const lat = +ll[1] / 1e6, lon = -ll[2] / 1e6;
    if (!inOC(lat, lon)) continue;
    const time = chpTime(tag(body, 'LogTime'));
    if (time && time < cut) continue;
    const details = [...body.matchAll(/<IncidentDetail>"?([^<]*?)"?<\/IncidentDetail>/g)].map(d => d[1].replace(/^\[\d+\]\s*/, '').trim()).filter(Boolean).slice(0, 3);
    out.push({ id: m[3], center, dispatch, time, type: tag(body, 'LogType'), loc: tag(body, 'Location'), desc: tag(body, 'LocationDesc'), area: tag(body, 'Area'), lat, lon, details });
  }
  return out;
}

// ---------- Caltrans lane closures, District 12: active now, or starting within 24 hours
async function closures() {
  const j = await get('https://cwwp2.dot.ca.gov/data/d12/lcs/lcsStatusD12.json', 'json'), now = Date.now() / 1000, out = [];
  for (const { lcs } of j.data ?? []) {
    const c = lcs?.closure, b = lcs?.location?.begin, e = lcs?.location?.end; if (!c || !b) continue;
    const ts = c.closureTimestamp ?? {}, start = +ts.closureStartEpoch || 0, end = ts.isClosureEndIndefinite === 'true' ? 0 : +ts.closureEndEpoch || 0;
    if (c.code1022?.isCode1022 === 'true' || c.code1098?.isCode1098 === 'true') continue;   // cancelled or finished
    // 10-97 = crew on scene; a closure past its end time with no 10-98 is treated as over
    const active = (c.code1097?.isCode1097 === 'true' || start <= now) && (!end || end > now);
    if (!active && !(start > now && start < now + 86400)) continue;
    out.push({
      id: lcs.index, active, route: b.beginRoute, dir: lcs.location.travelFlowDirection, from: b.beginLocationName, to: e?.endLocationName ?? '',
      lat0: +b.beginLatitude, lon0: +b.beginLongitude, lat1: +(e?.endLatitude ?? b.beginLatitude), lon1: +(e?.endLongitude ?? b.beginLongitude),
      facility: c.facility, kind: c.typeOfClosure, work: c.typeOfWork, lanes: +c.lanesClosed || 0, of: +c.totalExistingLanes || 0, delay: c.estimatedDelay, start, end,
    });
  }
  return out;
}

// ---------- PeMS District 12 five-minute file: a timestamp line, then one line per station
//   station, total flow, avg occupancy, avg speed (mph), ...  (columns after speed are not used)
async function speeds() {
  const txt = await get('https://pems.dot.ca.gov/feeds/D12/Data/5min/5minagg_latest.txt'), lines = txt.split('\n');
  const at = lines[0].trim(), out = [];
  for (let i = 1; i < lines.length; i++) {
    const f = lines[i].split(','); if (f.length < 4) continue;
    const id = f[0], speed = parseFloat(f[3] || f[6]); if (!(speed > 0)) continue;
    const s = STATIONS[id]; if (!s) continue;
    out.push([+id, s[0], s[1], s[2], s[3], Math.round(speed * 10) / 10, +f[2] || 0, +f[1] || 0]);
  }
  return { at, cols: ['id', 'lat', 'lon', 'fwy', 'dir', 'mph', 'occ', 'flow'], stations: out };
}

// ---------- TomTom (key in the TOMTOM_KEY secret)
const OCBOX = `${OC.w},${OC.s},${OC.e},${OC.n}`;
const TTFIELDS = '{incidents{type,geometry{type,coordinates},properties{id,iconCategory,magnitudeOfDelay,events{description,code},startTime,endTime,from,to,length,delay,roadNumbers}}}';
const tileInOC = (z, x, y) => {
  const n = 2 ** z, lon = v => v / n * 360 - 180, lat = v => Math.atan(Math.sinh(Math.PI * (1 - 2 * v / n))) * 180 / Math.PI;
  return lon(x + 1) >= OC.w && lon(x) <= OC.e && lat(y) >= OC.s && lat(y + 1) <= OC.n;
};

export default {
  async fetch(req, env, ctx) {
    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
    const p = new URL(req.url).pathname, json = 'application/json';
    if (p === '/v1/incidents.json') return cached('incidents', 60, json, async () => {
      const [a, b] = await Promise.allSettled([chp(), cachedClosures()]);
      return JSON.stringify({ t: Math.round(Date.now() / 1000), chp: a.value ?? [], closures: b.value ?? [],
        errors: { chp: a.reason ? String(a.reason.message ?? a.reason) : null, closures: b.reason ? String(b.reason.message ?? b.reason) : null } });
    }, ctx);
    if (p === '/v1/speeds.json') return cached('speeds', 300, json, async () => JSON.stringify(await speeds()), ctx);
    if (p === '/v1/tomtom/incidents.json') {
      if (!env.TOMTOM_KEY) return send('{"error":"TOMTOM_KEY is not set"}', json, 60, 503);
      return cached('tt-incidents', 1200, json, () => get(`https://api.tomtom.com/traffic/services/5/incidentDetails?key=${env.TOMTOM_KEY}&bbox=${OCBOX}&fields=${encodeURIComponent(TTFIELDS)}&language=en-US&timeValidityFilter=present`), ctx);
    }
    const t = /^\/v1\/tomtom\/flow\/(\d+)\/(\d+)\/(\d+)\.pbf$/.exec(p);
    if (t) {
      const [z, x, y] = t.slice(1).map(Number);
      if (!env.TOMTOM_KEY) return send('', 'text/plain', 60, 503);
      if (z < 10 || z > 16 || !tileInOC(z, x, y)) return send('', 'text/plain', 3600, 404);
      return cached(`tt-flow-${z}-${x}-${y}`, 300, 'application/x-protobuf', () => get(`https://api.tomtom.com/traffic/map/4/tile/flow/relative0/${z}/${x}/${y}.pbf?key=${env.TOMTOM_KEY}`, 'buf'), ctx);
    }
    if (p === '/v1/health') {
      const r = {};
      for (const [k, f] of [['chp', chp], ['closures', closures], ['speeds', speeds]]) {
        try { const v = await f(); r[k] = { ok: true, count: Array.isArray(v) ? v.length : v.stations.length }; } catch (e) { r[k] = { ok: false, error: String(e.message ?? e) }; }
      }
      r.stations = Object.keys(STATIONS).length; r.tomtomKey = !!env.TOMTOM_KEY;
      return send(JSON.stringify(r), json, 0);
    }
    return send('{"error":"not found","routes":["/v1/incidents.json","/v1/speeds.json","/v1/tomtom/incidents.json","/v1/tomtom/flow/{z}/{x}/{y}.pbf","/v1/health"]}', json, 60, 404);
  },
};

// closures change every 5 minutes; incidents every minute, so closures keep their own longer memo
async function cachedClosures() {
  const m = memo.get('closures');
  if (m && Date.now() - m.t < 300000) return m.v;
  const v = await closures(); memo.set('closures', { t: Date.now(), v }); return v;
}
