// Where a trip ends for an address, a place or a tapped point (the same rule as tools/arrival.py):
// an address is snapped to the closest point ON an edge of its own street (not to the nearest node of
// whatever road is closest, which is often the street behind the house or the cross street of a corner
// lot), within REACH metres and within MARGIN of the nearest drivable road; otherwise it falls back to the
// nearest drivable road on the side of the block facing its street. Motorways and ramps are never arrival roads. The result knows which side of the street
// the destination is on, so guidance can say "on the right".
const REACH = 150, FAR_REACH = 300, FALLBACK_BONUS = 15, ENDPOINT = 3, CELL = 50;
// a lot: an unnamed service road closer than the street by LOT_GAIN, not behind the address (LOT_COS), that
// leads out to the address's own street within LOT_LINK metres of service road (an apartment complex's or a
// plaza's own drive, not the alley behind a row of houses)
const LOT_GAIN = 5, LOT_COS = -0.3, LOT_LINK = 300, LOT_MIN = 35;
// own street only if within MARGIN of the nearest drivable road (a driver 200 m away on the right street is worse
// off than one 45 m away on the next road); FAR: the 150-300 m second look (off); fallback keeps to the side of
// the block facing the own street (roads within FACE_SLACK of the nearest, cosine >= FACE_COS), DIR_REACH to find it
const MARGIN = 60, FAR = false, DIR_REACH = 500, FACE_SLACK = 30, FACE_COS = -0.3;
const ABBR = { st: 'street', ave: 'avenue', av: 'avenue', pkwy: 'parkway', pky: 'parkway', dr: 'drive', blvd: 'boulevard', rd: 'road', ln: 'lane',
  ctr: 'center', cntr: 'center', centre: 'center', pl: 'place', ct: 'court', hwy: 'highway', fwy: 'freeway', n: 'north', s: 'south', e: 'east', w: 'west',
  cir: 'circle', ter: 'terrace', trl: 'trail', sq: 'square', mt: 'mount', plz: 'plaza', crk: 'creek', gln: 'glen', vw: 'view', grv: 'grove', rdg: 'ridge',
  spgs: 'springs', sprgs: 'springs', pne: 'pine', vly: 'valley', cyn: 'canyon', hl: 'hill', mdw: 'meadow', mdws: 'meadows', xing: 'crossing', lp: 'loop',
  aly: 'alley', hts: 'heights', pt: 'point', pk: 'park', lk: 'lake', est: 'estates', cv: 'cove', wy: 'way', trce: 'trace', rnch: 'ranch', hbr: 'harbor',
  is: 'island', lndg: 'landing', rte: 'route', sta: 'station', smt: 'summit', vis: 'vista', vlg: 'village', wl: 'well', wls: 'wells', bnd: 'bend',
  brk: 'brook', byp: 'bypass', cswy: 'causeway', frst: 'forest', gdn: 'garden', gdns: 'gardens', hvn: 'haven', knl: 'knoll', lgt: 'light', mnr: 'manor',
  orch: 'orchard', psge: 'passage', shr: 'shore', spg: 'spring', strm: 'stream', tr: 'trail', tunl: 'tunnel', vl: 'ville', cres: 'crescent', crst: 'crest',
  ests: 'estates', expy: 'expressway', fls: 'falls', flt: 'flat', ft: 'fort', jct: 'junction', mtn: 'mountain', rdgs: 'ridges', stn: 'station',
  trak: 'track', un: 'union', wk: 'walk', ste: 'suite' };
const DIRS = new Set(['north', 'south', 'east', 'west']);
const TYPES = new Set(['street', 'avenue', 'drive', 'road', 'lane', 'court', 'place', 'circle', 'terrace', 'trail', 'way', 'parkway', 'boulevard',
  'highway', 'loop', 'plaza', 'square', 'alley', 'walk', 'row', 'path', 'crossing', 'center']);
const words = n => n.toLowerCase().replace(/&/g, ' and ').replace(/['’]/g, '').split(/[^a-z0-9]+/).filter(Boolean).map(w => ABBR[w] ?? w);
/** [tight, loose]: tight ignores case, abbreviations and spaces; loose also drops a directional and a trailing street type */
export function streetKeys(name) {
  const w = words(name), tight = w.join('');
  let l = w.slice();
  if (l.length > 1 && DIRS.has(l[0])) l = l.slice(1);
  if (l.length > 1 && DIRS.has(l[l.length - 1])) l = l.slice(0, -1);
  if (l.length > 1 && TYPES.has(l[l.length - 1])) l = l.slice(0, -1);
  return [tight, l.join('')];
}
function lev1(a, b) {   // edit distance, only whether it is <= 1
  if (Math.abs(a.length - b.length) > 1) return 2;
  let p = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) { const c = [i]; for (let j = 1; j <= b.length; j++) c[j] = Math.min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (a[i - 1] !== b[j - 1])); p = c; }
  return p[b.length];
}
const sameStreet = (ka, kb) => ka[0] === kb[0] ? 0 : ka[1] && ka[1] === kb[1] ? 1 : ka[0].length >= 7 && kb[0].length >= 7 && lev1(ka[0], kb[0]) <= 1 ? 2 : null;

/** G: roads.json; N: node positions (x, z) in the same frame as the points asked about */
export function createArrival(G, N) {
  const nkeys = G.names.map(n => n ? streetKeys(n) : ['', '']);
  const grid = new Map();
  for (const [i, [a, b]] of G.edges.entries()) {
    const A = N[a], B = N[b];
    for (let gx = Math.floor(Math.min(A.x, B.x) / CELL); gx <= Math.floor(Math.max(A.x, B.x) / CELL); gx++)
      for (let gz = Math.floor(Math.min(A.z, B.z) / CELL); gz <= Math.floor(Math.max(A.z, B.z) / CELL); gz++) {
        const k = gx * 65536 + gz; let l = grid.get(k); if (!l) grid.set(k, l = []); l.push(i);
      }
  }
  const nodeEdges = new Map();
  for (const [i, [a, b]] of G.edges.entries()) for (const n of [a, b]) { let l = nodeEdges.get(n); if (!l) nodeEdges.set(n, l = []); l.push(i); }
  const drivable = i => G.edges[i][3] !== 0 && G.names[G.edges[i][2]] !== 'ramp';
  function* near(x, z, r) {
    const seen = new Set(), r0 = Math.floor(r / CELL) + 1, gx = Math.floor(x / CELL), gz = Math.floor(z / CELL);
    for (let a = -r0; a <= r0; a++) for (let b = -r0; b <= r0; b++) for (const i of grid.get((gx + a) * 65536 + gz + b) ?? []) if (!seen.has(i)) { seen.add(i); yield i; }
  }
  function project(i, x, z) {
    const [a, b] = G.edges[i], A = N[a], B = N[b], dx = B.x - A.x, dz = B.z - A.z, l2 = dx * dx + dz * dz || 1;
    const t = Math.max(0, Math.min(1, ((x - A.x) * dx + (z - A.z) * dz) / l2)), px = A.x + t * dx, pz = A.z + t * dz;
    return { d: Math.hypot(x - px, z - pz), t, x: px, z: pz };
  }
  /** +1: (x, z) is on the right of a driver going a -> b, -1 on the left (x east, z south) */
  const sideOf = (i, x, z) => { const [a, b] = G.edges[i], A = N[a], B = N[b]; return (B.x - A.x) * (z - A.z) - (B.z - A.z) * (x - A.x) > 0 ? 1 : -1; };
  function finish(best, x, z) {
    const { i, p, how } = best, [a, b] = G.edges[i], L = N[a].distanceTo(N[b]);
    const node = p.t * L < ENDPOINT ? a : (1 - p.t) * L < ENDPOINT ? b : null;
    return { edge: i, t: p.t, point: { x: p.x, z: p.z }, d: p.d, side: sideOf(i, x, z), how, node, near: p.t < 0.5 ? a : b, addr: { x, z }, road: G.names[G.edges[i][2]] };
  }
  // does edge i lead, along unnamed service roads within LOT_LINK, to an edge of the street (k)
  function reaches(i, k) {
    const [a, b] = G.edges[i], seen = new Map([[a, 0], [b, 0]]), todo = [a, b];
    while (todo.length) {
      const u = todo.pop();
      for (const j of nodeEdges.get(u) ?? []) {
        const f = G.edges[j];
        if (nkeys[f[2]][0] && sameStreet(k, nkeys[f[2]]) != null && drivable(j)) return true;
        if (f[3] !== 7 || G.names[f[2]]) continue;
        const v = f[0] === u ? f[1] : f[0], dd = seen.get(u) + Math.hypot(N[u].x - N[v].x, N[u].z - N[v].z);
        if (dd <= LOT_LINK && (!seen.has(v) || dd < seen.get(v))) { seen.set(v, dd); todo.push(v); }
      }
    }
    return false;
  }
  function lot(x, z, k, best) {
    const cands = [];
    for (const i of near(x, z, best.p.d)) {
      const e = G.edges[i]; if (e[3] !== 7 || G.names[e[2]] || !drivable(i)) continue;
      const p = project(i, x, z); if (p.d > best.p.d - LOT_GAIN) continue;
      const v1x = p.x - x, v1z = p.z - z, v2x = best.p.x - x, v2z = best.p.z - z;
      if ((v1x * v2x + v1z * v2z) / ((Math.hypot(v1x, v1z) || 1) * (Math.hypot(v2x, v2z) || 1)) < LOT_COS) continue;
      cands.push({ i, p });
    }
    cands.sort((a, b) => a.p.d - b.p.d);
    for (const c of cands) if (reaches(c.i, k)) return { ...c, how: 'lot' };
    return null;
  }
  // the fallback road: drivable, named roads preferred by `bonus`; with `toward` (a point on the own street),
  // roads within FACE_SLACK of the nearest that lie on the street's side of the address win (the side of the
  // block facing the address's street). Returns [best, plain nearest distance].
  function nearest(x, z, toward = null, bonus = FALLBACK_BONUS) {
    let cands, dmin;
    for (let r = 200; ; r *= 2) {
      cands = [];
      for (const i of near(x, z, r)) if (drivable(i)) cands.push({ i, p: project(i, x, z) });
      dmin = cands.length ? Math.min(...cands.map(c => c.p.d)) : null;
      if ((dmin != null && dmin + FACE_SLACK <= r - CELL) || r > 5000) break;
    }
    if (!cands.length) return [null, null];
    const score = c => c.p.d - (G.names[G.edges[c.i][2]] ? bonus : 0);
    let pool = cands, how = 'fallback';
    if (toward) {
      const vx = toward.x - x, vz = toward.z - z, lv = Math.hypot(vx, vz) || 1;
      const face = cands.filter(c => c.p.d <= dmin + FACE_SLACK && ((c.p.x - x) * vx + (c.p.z - z) * vz) / ((Math.hypot(c.p.x - x, c.p.z - z) || 1) * lv) >= FACE_COS);
      if (face.length) { pool = face; how = 'fallback-facing'; }
    }
    let b = pool[0]; for (const c of pool) if (score(c) < score(b)) b = c;
    return [{ ...b, how }, dmin];
  }
  /** street: the address's own street name (any style: "SUMMER HOUSE", "E Coastal Peak"); null for a tap on the map.
   *  The own street wins when its point is within REACH and no more than MARGIN farther than the nearest drivable
   *  road; its lot only when the street point is more than LOT_MIN from the address. */
  function snap(x, z, street = null, reach = REACH) {
    let best = null, toward = null;
    const onStreet = (k, reach) => {
      let best = null;
      for (const i of near(x, z, reach)) {
        if (!drivable(i)) continue;
        const nk = nkeys[G.edges[i][2]]; if (!nk[0]) continue;
        const tier = sameStreet(k, nk); if (tier == null) continue;
        const p = project(i, x, z);
        if (p.d <= reach && (!best || p.d < best.p.d)) best = { i, p, how: tier ? 'street~' : 'street' };
      }
      return best;
    };
    if (street) {
      const k = streetKeys(street);
      let s = onStreet(k, reach);
      if (!s && FAR) { s = onStreet(k, FAR_REACH); if (s) s.how = 'street-far'; }
      const dmin = nearest(x, z, null, 0)[1];
      if (s && s.p.d <= dmin + MARGIN) {
        best = s;
        if (s.p.d > LOT_MIN) best = lot(x, z, k, s) ?? s;
      } else {   // too far or missing: which way is the street, to keep to its side of the block
        const d = s ?? onStreet(k, DIR_REACH);
        if (d) toward = { x: d.p.x, z: d.p.z };
      }
    }
    if (!best) { best = nearest(x, z, toward)[0]; if (!best) return null; }
    return finish(best, x, z);
  }
  return { snap, sideOf, streetKeys };
}
