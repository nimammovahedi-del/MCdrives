// Live traffic for the driving app, from the mcmaps-traffic Worker (worker/):
//   /v1/incidents.json  CHP incidents + Caltrans lane closures in Orange County (polled every 60 s)
//   /v1/speeds.json     PeMS freeway detector speeds (polled every 5 min)
// Everything is placed on our own road graph: detector speeds spread along the freeway they sit on, full
// closures shut the edges they cover, and incidents snap to the nearest road. The router asks
// speed(entry) and closed(entry) per directed edge; the 2D map and the 3D view draw what overlay() returns.

const R = 6371000;
const hav = (dlat, dlon, lat) => { const r = Math.PI / 180, a = Math.sin(dlat * r / 2) ** 2 + Math.cos(lat * r) ** 2 * Math.sin(dlon * r / 2) ** 2; return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)); };
const MPS = 0.44704, FREE_MPH = 65;
const DIRV = { N: [0, -1], S: [0, 1], E: [1, 0], W: [-1, 0], North: [0, -1], South: [0, 1], East: [1, 0], West: [-1, 0] };

// what a CHP log type means for a driver (codes are CHP radio codes)
export function chpKind(t) {
  if (/^(1179|1180|1181|1182|1183|20001|20002)/.test(t)) return 'crash';
  if (/^CLOSURE/.test(t)) return 'closure';
  if (/^(FIRE)/.test(t)) return 'fire';
  if (/^(1125A|ANIMAL)/.test(t)) return 'animal';
  if (/^(1125|23114|FLOOD|FOG|WIND|SNOW)/.test(t)) return 'hazard';
  return null;   // assists, Caltrans notifications, traffic breaks: not shown
}
export const KIND_LABEL = { crash: 'Crash', closure: 'Road closed', fire: 'Fire', animal: 'Animal on road', hazard: 'Hazard', lanes: 'Lane closure', roadwork: 'Road closed' };

// latitude and longitude to the map's block frame (x east, z south) and back: the bbox spans the map's own width and depth
export function geoFrame(meta) {
  const [S, W, Nn, E] = meta.bbox;
  const sx = Math.floor(hav(0, E - W, (S + Nn) / 2)), sz = Math.floor(hav(Nn - S, 0, 0));
  return {
    toXZ: (lat, lon) => ({ x: (lon - W) / (E - W) * sx - meta.minx, z: (1 - (lat - S) / (Nn - S)) * sz - meta.minz }),
    toLL: (x, z) => ({ lat: S + (1 - (z + meta.minz) / sz) * (Nn - S), lon: W + (x + meta.minx) / sx * (E - W) }),
    // metres from the map's box to a point outside it (0 inside)
    outside: (lat, lon) => hav(Math.max(0, S - lat, lat - Nn), Math.max(0, W - lon, lon - E), lat),
  };
}

export function createTraffic({ url, G, N, meta, adj, onChange }) {
  const { toXZ } = geoFrame(meta);

  // directed edges: key = edge index * 2 + (0 for a->b, 1 for b->a); adj entries carry it as e.k
  const CELL = 128, grid = new Map();
  let bx0 = Infinity, bz0 = Infinity, bx1 = -Infinity, bz1 = -Infinity;
  for (const p of N) { bx0 = Math.min(bx0, p.x); bz0 = Math.min(bz0, p.z); bx1 = Math.max(bx1, p.x); bz1 = Math.max(bz1, p.z); }
  const inside = p => p.x > bx0 - 300 && p.x < bx1 + 300 && p.z > bz0 - 300 && p.z < bz1 + 300;   // feeds cover the county; the map may not
  G.edges.forEach(([a, b], i) => {
    const A = N[a], B = N[b];
    for (let cx = Math.floor(Math.min(A.x, B.x) / CELL); cx <= Math.floor(Math.max(A.x, B.x) / CELL); cx++)
      for (let cz = Math.floor(Math.min(A.z, B.z) / CELL); cz <= Math.floor(Math.max(A.z, B.z) / CELL); cz++) {
        const k = cx * 65536 + cz; if (!grid.has(k)) grid.set(k, []); grid.get(k).push(i);
      }
  });
  const segDist = (px, pz, A, B) => { const dx = B.x - A.x, dz = B.z - A.z, l2 = dx * dx + dz * dz || 1; const t = Math.max(0, Math.min(1, ((px - A.x) * dx + (pz - A.z) * dz) / l2)); return Math.hypot(px - A.x - t * dx, pz - A.z - t * dz); };
  function near(x, z, r, keep = () => true) {
    const out = [];
    for (let cx = Math.floor((x - r) / CELL); cx <= Math.floor((x + r) / CELL); cx++)
      for (let cz = Math.floor((z - r) / CELL); cz <= Math.floor((z + r) / CELL); cz++)
        for (const i of grid.get(cx * 65536 + cz) ?? []) {
          const [a, b] = G.edges[i], d = segDist(x, z, N[a], N[b]);
          if (d <= r && keep(i)) out.push({ i, d });
        }
    return out.sort((p, q) => p.d - q.d);
  }
  // Directions in the feeds are signed route directions ("I-405 N"), which in Irvine can run nearly west, so
  // nothing here uses a cut-off angle: of the directed edges on offer, the one that heads most that way wins.
  // dirKeys(i, v): the keys of edge i that head along vector v (any amount); both ways of a two-way edge without v
  function dirKeys(i, v) {
    const [a, b, , , ow] = G.edges[i];
    if (!v) return ow ? [i * 2] : [i * 2, i * 2 + 1];
    const dot = (N[b].x - N[a].x) * v[0] + (N[b].z - N[a].z) * v[1];
    return dot >= 0 ? [i * 2] : ow ? [] : [i * 2 + 1];
  }
  // bestKey(cands, dir): the directed edge among nearby candidates that best matches a compass direction, nearer wins ties
  function bestKey(cands, dir) {
    const v = DIRV[dir]; let best = null, bs = -Infinity;
    for (const { i, d } of cands.slice(0, 8)) {
      const [a, b, , , ow] = G.edges[i], dx = N[b].x - N[a].x, dz = N[b].z - N[a].z, l = Math.hypot(dx, dz) || 1;
      for (const [k, sg] of ow ? [[i * 2, 1]] : [[i * 2, 1], [i * 2 + 1, -1]]) { const sc = (v ? sg * (dx * v[0] + dz * v[1]) / l : 0) - d / 200; if (sc > bs) { bs = sc; best = k; } }
    }
    return best;
  }
  // walk along the road from a directed edge, forward or backward, calling fn(key, metres) until it returns false
  const rev = new Map();
  for (const [u, list] of adj.entries()) for (const e of list) { if (!rev.has(e.to)) rev.set(e.to, []); rev.get(e.to).push({ from: u, e }); }
  const ends = k => { const [a, b] = G.edges[k >> 1]; return k & 1 ? [b, a] : [a, b]; };
  function walk(k0, maxD, fn) {
    const nm = G.edges[k0 >> 1][2];
    for (const fwd of [true, false]) {
      let k = k0, d = 0;
      for (let steps = 0; steps < 200 && d < maxD; steps++) {
        const [a, b] = ends(k), from = fwd ? b : a;
        const next = fwd ? adj[from].find(e => e.nm === nm && e.k !== (k ^ 1)) : rev.get(from)?.find(r => r.e.nm === nm && r.e.k !== (k ^ 1))?.e;
        if (!next) break;
        d += next.len; if (fn(next.k, d) === false) break; k = next.k;
      }
    }
  }

  const st = { speed: new Map(), closed: new Set(), slow: new Map(), incidents: [], closures: [], at: 0, speedAt: '', ok: false, err: null };
  // detector speeds: each station sets its own edge and spreads up to 800 m each way along the same road
  function applySpeeds(j) {
    const best = new Map();
    const col = Object.fromEntries(j.cols.map((c, i) => [c, i]));
    for (const r of j.stations) {
      const p = toXZ(r[col.lat], r[col.lon]), mph = r[col.mph], dir = r[col.dir];
      const k0 = bestKey(near(p.x, p.z, 60, i => G.edges[i][3] <= 1 && G.names[G.edges[i][2]] !== 'ramp'), dir);
      if (k0 == null) continue;
      const set = (k, d) => { const o = best.get(k); if (!o || d < o.d) best.set(k, { mph, d }); };
      set(k0, 0); walk(k0, 800, (k, d) => { set(k, d); return d < 800; });
    }
    st.speed = new Map([...best].map(([k, v]) => [k, Math.max(2, v.mph * MPS)]));
    st.ratio = new Map([...best].map(([k, v]) => [k, Math.min(1, v.mph / FREE_MPH)]));
    st.speedAt = j.at;
  }
  function applyIncidents(j) {
    const closed = new Set(), slow = new Map(), inc = [];
    for (const c of j.chp ?? []) {
      const kind = chpKind(c.type); if (!kind) continue;
      // "I405 N / Jamboree Rd": the travel direction picks the carriageway on divided roads
      const p = toXZ(c.lat, c.lon); if (!inside(p)) continue;
      // candidates: roads of the nearest road's class (a crash on the freeway is not on the frontage road beside it)
      const dir = /^\S+\s+([NSEW])\b/.exec(c.loc ?? '')?.[1], all = near(p.x, p.z, 80, i => G.names[G.edges[i][2]] !== 'ramp');
      const cands = all.filter(q => q.d < all[0].d + 40 && G.edges[q.i][3] === G.edges[all[0].i][3]);
      const k0 = cands.length ? bestKey(cands, dir) : null, snap = k0 == null ? null : { i: k0 >> 1 };
      const at = snap ? (() => { const [a, b] = G.edges[snap.i], A = N[a], B = N[b], dx = B.x - A.x, dz = B.z - A.z, l2 = dx * dx + dz * dz || 1, t = Math.max(0, Math.min(1, ((p.x - A.x) * dx + (p.z - A.z) * dz) / l2)); return { x: A.x + t * dx, z: A.z + t * dz }; })() : p;
      // keys: the directed edges it affects, which is how the route ahead finds it
      // (both ways of a two-way street when the log gives no direction)
      const keys = new Set(snap ? (dir ? [k0] : dirKeys(snap.i, null)) : []);
      // a crash or hazard slows 150 m of road each side of it, in its direction, unless detectors already say how fast it moves
      if (snap && (kind === 'crash' || kind === 'hazard')) {
        for (const k of [...keys]) walk(k, 150, kk => { keys.add(kk); });
        for (const k of keys) slow.set(k, Math.min(slow.get(k) ?? 1, 0.5));
      }
      inc.push({ id: 'chp-' + c.id, kind, x: at.x, z: at.z, keys, title: KIND_LABEL[kind], road: c.loc, sub: [c.loc, c.desc].filter(Boolean).join(' · '), time: c.time, details: c.details, src: 'CHP' });
    }
    for (const c of j.closures ?? []) {
      if (!c.active || (c.kind !== 'Full' && !(c.lanes > 0))) continue;
      // Caltrans facility: Mainline, On Ramp, Off Ramp, Connector, Conventional Hwy, Surface Street, ...
      const A = toXZ(c.lat0, c.lon0), B = toXZ(c.lat1, c.lon1), full = c.kind === 'Full' || (c.of > 0 && c.lanes >= c.of), ramp = /Ramp|Connector/i.test(c.facility ?? '');
      const fwy = /Mainline|Freeway/i.test(c.facility ?? '');
      const isRamp = i => G.names[G.edges[i][2]] === 'ramp', keep = i => ramp ? isRamp(i) : fwy ? G.edges[i][3] <= 1 && !isRamp(i) : !isRamp(i);
      if (!inside(A) && !inside(B)) continue;
      const mid = { x: (A.x + B.x) / 2, z: (A.z + B.z) / 2 }, span = Math.hypot(B.x - A.x, B.z - A.z) / 2;
      const hits = near(mid.x, mid.z, span + 40, keep).filter(({ i }) => { const [a, b] = G.edges[i]; return segDist(N[a].x, N[a].z, A, B) < 40 && segDist(N[b].x, N[b].z, A, B) < 40 || span < 1; });
      // travel direction: along the closure's own line, signed by the compass direction it is given for
      const keys = new Set(), cv = DIRV[c.dir], u = [B.x - A.x, B.z - A.z], sgn = cv ? Math.sign(u[0] * cv[0] + u[1] * cv[1]) || 1 : 0;
      const ks = ramp ? hits.flatMap(({ i }) => dirKeys(i, null)) : span < 1 ? (hits.length ? (cv ? [bestKey(hits, c.dir)] : dirKeys(hits[0].i, null)) : [])
        : hits.flatMap(({ i }) => dirKeys(i, cv ? [u[0] * sgn, u[1] * sgn] : null));
      for (const k of ks) {
        keys.add(k);
        if (full || ramp) closed.add(k); else slow.set(k, Math.min(slow.get(k) ?? 1, Math.max(0.3, 1 - 0.6 * c.lanes / Math.max(1, c.of))));
      }
      const kind = full || ramp ? 'roadwork' : 'lanes';
      inc.push({ id: 'lcs-' + c.id, kind, x: A.x, z: A.z, keys, title: ramp ? 'Ramp closed' : full ? 'Road closed' : `${c.lanes} of ${c.of} lanes closed`, road: c.route,
        sub: [c.route, c.dir && c.dir !== 'N/A' ? c.dir + 'bound' : '', c.from && c.to ? `${c.from} to ${c.to}` : c.from].filter(Boolean).join(' · '), time: c.start, until: c.end, details: [c.work].filter(Boolean), src: 'Caltrans' });
    }
    st.closed = closed; st.slow = slow; st.incidents = inc; st.at = j.t;
  }

  let timers = [], on = false;
  async function pull(path, apply) {
    try { const r = await fetch(url + path, { cache: 'no-store' }); if (!r.ok) throw new Error(r.status); apply(await r.json()); st.ok = true; st.err = null; onChange?.(); }
    catch (e) { st.err = String(e.message ?? e); }
  }
  const api = {
    state: st,
    get on() { return on; },
    start() {
      if (on || !url) return; on = true;
      const inc = () => document.hidden || pull('/v1/incidents.json', applyIncidents), spd = () => document.hidden || pull('/v1/speeds.json', applySpeeds);
      inc(); spd(); timers = [setInterval(inc, 60000), setInterval(spd, 300000)];
    },
    stop() { on = false; timers.forEach(clearInterval); timers = []; st.speed = new Map(); st.ratio = new Map(); st.closed = new Set(); st.slow = new Map(); st.incidents = []; onChange?.(); },
    /** m/s on a directed edge, or undefined when there is no live data for it */
    speed(e, base) { if (!on) return base; const s = st.speed.get(e.k); const f = st.slow.get(e.k) ?? 1; return s !== undefined ? Math.min(s, base * 1.15) : f < 1 ? base * f : base; },
    closed(e) { return on && st.closed.has(e.k); },
    /** for the 2D map: coloured freeway segments and incident sprites */
    overlay() {
      if (!on) return null;
      const segs = [];
      for (const [k, r] of st.ratio ?? []) { const [a, b] = ends(k); segs.push([N[a].x, N[a].z, N[b].x, N[b].z, r]); }
      for (const k of st.closed) { const [a, b] = ends(k); segs.push([N[a].x, N[a].z, N[b].x, N[b].z, -1]); }
      return { segs, incidents: st.incidents };
    },
    /** incidents on a route: ahead = [{ k (directed edge key), at (metres from here) }] in driving order; nearest first */
    along(ahead) {
      const out = [];
      for (const it of st.incidents) {
        if (it.kind === 'lanes' || !it.keys.size) continue;
        const hit = ahead.find(a => it.keys.has(a.k)); if (hit) out.push({ it, at: hit.at });
      }
      return out.sort((a, b) => a.at - b.at);
    },
    toXZ,
  };
  return api;
}

// 12x12 pixel icons for incidents, drawn on canvases (shared by the 2D map and the 3D markers)
const ICON = {
  crash: { ink: '#3a0d0d', fill: '#ff5a4e', rows: ['....####....', '...#++++#...', '..#+#++#+#..', '.#++#++#++#.', '#+++####+++#', '#++++##++++#', '#++++##++++#', '#+++#..#+++#', '.#++#..#++#.', '..#++##++#..', '...#++++#...', '....####....'] },
  roadwork: { ink: '#3a1d00', fill: '#ff9a1f', rows: ['.....##.....', '....#++#....', '....#++#....', '...#####....', '...#++++#...', '..#######...', '..#++++++#..', '.#########..', '.#++++++++#.', '############', '#++++++++++#', '############'] },
  lanes: { ink: '#3a2a00', fill: '#ffd23a', rows: ['.....##.....', '....#++#....', '....#++#....', '...#####....', '...#++++#...', '..#######...', '..#++++++#..', '.#########..', '.#++++++++#.', '############', '#++++++++++#', '############'] },
  closure: { ink: '#3a0d0d', fill: '#e53935', rows: ['..########..', '.#++++++++#.', '#++++++++++#', '#++++++++++#', '#+########+#', '#+########+#', '#++++++++++#', '#++++++++++#', '.#++++++++#.', '..########..', '............', '............'] },
  fire: { ink: '#3a1000', fill: '#ff7a1a', rows: ['.....#......', '....#+#.....', '....#+#..#..', '...#++#.#+#.', '..#+++##++#.', '..#++++++#..', '.#+++##++#..', '.#++#..#++#.', '.#++#..#++#.', '.#+++##+++#.', '..#++++++#..', '...######...'] },
  animal: { ink: '#2a1a08', fill: '#c98a4a', rows: ['............', '.##......##.', '#++#....#++#', '#+++####+++#', '.#++++++++#.', '.#+#++++#+#.', '.#++++++++#.', '..#++##++#..', '..#++++++#..', '...#++++#...', '....####....', '............'] },
  hazard: { ink: '#3a2a00', fill: '#ffd23a', rows: ['.....##.....', '....#++#....', '....#++#....', '...#+##+#...', '...#+##+#...', '..#++##++#..', '..#++##++#..', '.#++++++++#.', '.#+++##+++#.', '#++++++++++#', '############', '............'] },
};
const iconCache = new Map();
export function incidentIcon(kind, px = 1) {
  const key = kind + px; if (iconCache.has(key)) return iconCache.get(key);
  const d = ICON[kind] ?? ICON.hazard, c = document.createElement('canvas'); c.width = c.height = 12 * px;
  const g = c.getContext('2d');
  d.rows.forEach((r, y) => [...r].forEach((ch, x) => { if (ch === '.') return; g.fillStyle = ch === '#' ? d.ink : d.fill; g.fillRect(x * px, y * px, px, px); }));
  iconCache.set(key, c); return c;
}
