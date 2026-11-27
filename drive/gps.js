// Live location driving: the car follows the phone's real position.
//
// watchPosition gives a fix about once a second, a few metres off and sometimes late. Each fix is matched to
// the road graph (nearest edge, weighed by heading and by staying on the road it was on), and between fixes the
// car keeps moving along the graph at the GPS speed, easing toward where the latest fix says it should be. So the
// car glides along the road at 30 fps instead of hopping once a second, and never sits beside the road.
//
// World frame: x east, z south, 1 block = 1 m. A heading here is the app's car yaw: atan2(-dx, -dz).

const yawOf = (dx, dz) => Math.atan2(-dx, -dz);
const wrap = a => Math.atan2(Math.sin(a), Math.cos(a));

export function createGps({ G, N, adj, geo, outsideAt = 300 }) {
  // ---------- edge grid for matching (built on first use: a city has ~100k edges)
  const CELL = 64; let grid = null;
  function buildGrid() {
    grid = new Map();
    G.edges.forEach(([a, b], i) => {
      const A = N[a], B = N[b];
      for (let cx = Math.floor(Math.min(A.x, B.x) / CELL); cx <= Math.floor(Math.max(A.x, B.x) / CELL); cx++)
        for (let cz = Math.floor(Math.min(A.z, B.z) / CELL); cz <= Math.floor(Math.max(A.z, B.z) / CELL); cz++) {
          const k = cx * 100003 + cz; let l = grid.get(k); if (!l) grid.set(k, l = []); l.push(i);
        }
    });
  }
  // best road under (x, z): { e, t, x, z, fwd (1 when travelling a->b, 0 for b->a), d }; null when none within r
  function match(x, z, yaw, moving, r, prev) {
    if (!grid) buildGrid();
    const seen = new Set(); let best = null;
    for (let cx = Math.floor((x - r) / CELL); cx <= Math.floor((x + r) / CELL); cx++)
      for (let cz = Math.floor((z - r) / CELL); cz <= Math.floor((z + r) / CELL); cz++)
        for (const i of grid.get(cx * 100003 + cz) ?? []) {
          if (seen.has(i)) continue; seen.add(i);
          const [a, b, , , ow] = G.edges[i], A = N[a], B = N[b], dx = B.x - A.x, dz = B.z - A.z, l2 = dx * dx + dz * dz || 1;
          const t = Math.max(0, Math.min(1, ((x - A.x) * dx + (z - A.z) * dz) / l2)), px = A.x + t * dx, pz = A.z + t * dz;
          const d = Math.hypot(x - px, z - pz); if (d > r) continue;
          // travel direction: the one closer to the heading (a one-way edge only its own way)
          const ey = yawOf(dx, dz); let fwd = 1, pen = 0;
          if (yaw != null && moving) {
            const cf = Math.cos(wrap(yaw - ey)); fwd = cf >= 0 ? 1 : 0;
            if (!fwd && ow) { fwd = 1; pen += 30; }
            pen += 18 * (1 - Math.abs(cf));   // up to 18 m for a road at right angles to the way we are going
          } else if (prev && prev.e === i) fwd = prev.fwd;
          // staying on the same road, or moving to one joined to it, beats a parallel road a few metres closer
          if (prev) { if (prev.e === i) pen -= 6; else { const [pa, pb] = G.edges[prev.e]; if (a === pa || a === pb || b === pa || b === pb) pen -= 3; } }
          const s = d + pen; if (!best || s < best.s) best = { e: i, t, x: px, z: pz, fwd, d, s };
        }
    return best;
  }
  // a point dist metres on from the match along the graph: at each node the edge that bends least (never back)
  function advance(m, dist) {
    let e = m.e, t = m.t, fwd = m.fwd, guard = 0;
    for (;;) {
      const [a, b] = G.edges[e], A = N[a], B = N[b], L = A.distanceTo(B) || 0.01;
      const left = (fwd ? 1 - t : t) * L;
      if (dist <= left || guard++ > 20) {
        const tt = fwd ? t + Math.min(dist, left) / L : t - Math.min(dist, left) / L;
        const dx = (B.x - A.x) * (fwd ? 1 : -1), dz = (B.z - A.z) * (fwd ? 1 : -1);
        return { x: A.x + (B.x - A.x) * tt, z: A.z + (B.z - A.z) * tt, yaw: yawOf(dx, dz), e, t: tt, fwd };
      }
      dist -= left;
      const at = fwd ? b : a, from = fwd ? a : b, inYaw = yawOf(N[at].x - N[from].x, N[at].z - N[from].z);
      let nxt = null, bestBend = Infinity;
      for (const o of adj[at] ?? []) {
        if (o.to === from || o.k == null || o.to >= G.nodes.length) continue;   // skip trip-made mid nodes
        const bend = Math.abs(wrap(yawOf(N[o.to].x - N[at].x, N[o.to].z - N[at].z) - inYaw));
        if (bend < bestBend) { bestBend = bend; nxt = o; }
      }
      // a dead end or a turn sharper than 100 degrees: stop at the node and let the next fix decide
      if (!nxt || bestBend > 1.75) { const p = N[at]; return { x: p.x, z: p.z, yaw: inYaw, e, t: fwd ? 1 : 0, fwd }; }
      e = nxt.k >> 1; fwd = nxt.k & 1 ? 0 : 1; t = fwd ? 0 : 1;
    }
  }

  // ---------- state
  let watch = null, wake = null, on = false;
  let status = 'off', why = '', fix = null, prevRaw = null, lastAt = 0;
  const est = { x: 0, z: 0, yaw: 0, speed: 0, has: false, e: null, fwd: 1, onRoad: false, acc: 0 };
  const listeners = new Set();
  const emit = () => { for (const f of listeners) f(api.state()); };

  async function keepAwake() {
    try { if (on && document.visibilityState === 'visible' && navigator.wakeLock && !wake) { wake = await navigator.wakeLock.request('screen'); wake.addEventListener('release', () => { wake = null; }); } } catch { wake = null; }
  }
  const onVis = () => { if (document.visibilityState === 'visible') keepAwake(); };

  function onFix(p) {
    const now = performance.now(), c = p.coords;
    const off = geo.outside(c.latitude, c.longitude);
    if (off > outsideAt) { status = 'outside'; why = off; fix = null; est.has = false; lastAt = now; emit(); return; }
    const q = geo.toXZ(c.latitude, c.longitude);
    // speed: the phone's own when it gives one (Doppler, steady), else from the last two fixes
    let spd = c.speed != null && c.speed >= 0 && isFinite(c.speed) ? c.speed : null;
    let mvYaw = null;
    if (prevRaw) {
      const dt = (p.timestamp - prevRaw.ts) / 1000, dd = Math.hypot(q.x - prevRaw.x, q.z - prevRaw.z);
      // a jump faster than 55 m/s (a first fix after a gap, a tunnel exit) says nothing about speed or heading
      if (dt > 0.3 && dd / dt < 55) { if (spd == null) spd = dd / dt; if (dd > Math.max(3, c.accuracy * 0.5)) mvYaw = yawOf(q.x - prevRaw.x, q.z - prevRaw.z); }
    }
    if (spd == null) spd = 0;
    if (spd < 0.8) spd = 0;   // standing still: GPS wander is not motion
    prevRaw = { x: q.x, z: q.z, ts: p.timestamp };
    const gYaw = c.heading != null && !isNaN(c.heading) && spd > 1 ? Math.atan2(-Math.sin(c.heading * Math.PI / 180), Math.cos(c.heading * Math.PI / 180)) : mvYaw;
    const yaw = gYaw ?? (est.has ? est.yaw : null), moving = spd > 1.5;
    const r = Math.max(25, Math.min(60, c.accuracy * 1.5));
    const m = match(q.x, q.z, yaw, moving, r, est.e != null ? { e: est.e, fwd: est.fwd } : null);
    fix = { x: m ? m.x : q.x, z: m ? m.z : q.z, raw: q, m, yaw: m ? null : yaw, speed: spd, acc: c.accuracy, at: now };
    status = c.accuracy > 50 ? 'weak' : 'ok'; why = '';
    if (!est.has) { est.x = fix.x; est.z = fix.z; est.speed = spd; est.has = true; if (m) { const a = advance(m, 0); est.yaw = a.yaw; } else if (yaw != null) est.yaw = yaw; }
    est.e = m?.e ?? null; est.fwd = m?.fwd ?? 1; est.onRoad = !!m; est.acc = c.accuracy;
    lastAt = now; emit();
  }
  function onErr(e) {
    if (e.code === 1) { status = 'denied'; why = 'Location is blocked for this site. Allow it in your browser’s site settings.'; api.stop(true); }
    else { status = fix ? 'lost' : 'waiting'; why = e.code === 3 ? 'Waiting for a GPS fix' : 'Could not read your location'; }
    emit();
  }

  const api = {
    get on() { return on; },
    get status() { return status; },
    onChange(f) { listeners.add(f); return () => listeners.delete(f); },
    start() {
      if (on) return true;
      if (!navigator.geolocation) { status = 'unsupported'; why = 'This browser cannot share your location.'; emit(); return false; }
      on = true; status = 'waiting'; why = ''; fix = null; prevRaw = null; est.has = false; est.e = null; lastAt = performance.now();
      watch = navigator.geolocation.watchPosition(onFix, onErr, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
      keepAwake(); document.addEventListener('visibilitychange', onVis);
      emit(); return true;
    },
    stop(keepStatus = false) {
      if (watch != null) navigator.geolocation.clearWatch(watch); watch = null;
      wake?.release().catch(() => {}); wake = null; document.removeEventListener('visibilitychange', onVis);
      on = false; if (!keepStatus) status = 'off'; emit();
    },
    /** advance the smoothed car pose by dt seconds; null until there is a fix inside the map */
    step(dt) {
      if (!on || !fix || !est.has) return null;
      const now = performance.now(), age = (now - fix.at) / 1000;
      if (age > 10 && status !== 'lost') { status = 'lost'; why = 'GPS signal lost'; emit(); }
      // where the latest fix says the car is now: carried on along the road at its speed, for at most 3 s
      const go = fix.speed * Math.min(age, 3);
      let tx = fix.x, tz = fix.z, tyaw = fix.yaw ?? est.yaw;
      if (fix.m) { const a = advance(fix.m, go); tx = a.x; tz = a.z; if (fix.speed > 0.5 || age < 0.1) tyaw = a.yaw; }
      else if (fix.yaw != null) { tx += -Math.sin(fix.yaw) * go; tz += -Math.cos(fix.yaw) * go; }
      // ease toward it (about a third of a second), jump if it is far (a tunnel exit, a first fix)
      const gap = Math.hypot(tx - est.x, tz - est.z), k = gap > 80 ? 1 : 1 - Math.exp(-dt * 3.5);
      est.x += (tx - est.x) * k; est.z += (tz - est.z) * k;
      est.yaw += wrap(tyaw - est.yaw) * (gap > 80 ? 1 : 1 - Math.exp(-dt * 4));
      const want = age > 4 ? 0 : fix.speed;   // no news for a while: show the car stopping, not cruising
      est.speed += (want - est.speed) * (1 - Math.exp(-dt * 2.5));
      return { x: est.x, z: est.z, yaw: est.yaw, speed: est.speed, onRoad: est.onRoad, edge: est.e, fwd: est.fwd, acc: est.acc, raw: fix.raw };
    },
    /** where a new route should start: the node ahead when moving, else a point on the road under the car */
    startPoint() {
      if (!fix) return null;
      // off the road (a car park, a road the map lacks): the nearest road within 300 m
      const m = fix.m ?? match(fix.raw.x, fix.raw.z, est.yaw, fix.speed > 1.5, 300, null); if (!m) return null;
      const [a, b] = G.edges[m.e];
      if (fix.speed > 3) return { node: m.fwd ? b : a };
      return { edge: m.e, t: m.t, point: { x: m.x, z: m.z } };
    },
    state: () => ({ on, status, why, speed: est.speed, acc: est.acc, onRoad: est.onRoad, edge: est.e, has: est.has, age: fix ? (performance.now() - fix.at) / 1000 : null }),
    // test hook: feed a fix as if from the phone
    _fix: onFix, _match: match, _advance: advance,
  };
  return api;
}
