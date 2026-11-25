// Bridge audit: drives seeded long routes (they take the freeways, which have the bridges) and finds every stretch
// where the two height layers under the route differ by 4 blocks or more (a deck over a ground surface), reading
// the height tiles itself rather than through the viewer's groundY. Where the lowest layer is unknown (none, or
// the world floor where sky leaked under the terrain) a stretch is two-level where the top surface stands 4 or more
// above the lowest top surface within 30 m along the route, and the ground there is the top surface just outside it.
// Each stretch is classed by the route edge's bridge flag (on a bridge edge the car should ride the deck, elsewhere
// stay on the ground; off one, a lower layer that is a pit below the road on both sides is a culvert or untagged
// bridge, which the road crosses on top) and by the level the trip profile takes; wrong = more than 2 blocks off.
//   node tools/audit_bridges.js OUT.json [N] [SEED]      extra routes as "From|To;From|To" in env ROUTES
const { chromium } = require('playwright');
const fs = require('fs'), zlib = require('zlib');
(async () => {
  const out = process.argv[2] || 'bridges.json', N = +(process.argv[3] || 30), seed = +(process.argv[4] || 4242);
  const extra = (process.env.ROUTES || '').split(';').filter(Boolean).map(s => s.split('|'));
  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const p = await b.newPage({ viewport: { width: 480, height: 320 }, deviceScaleFactor: 1 });
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto(`${process.env.URL || 'http://localhost:8765'}/?budget=400&htiles=400`);
  await p.waitForFunction(() => window.__ready, null, { timeout: 600000 });
  // pairs: the extra ones, then seeded pairs of destinations 3 to 12 km apart
  const pairs = await p.evaluate(([N, seed, extra]) => {
    const A = window.__api; let s = seed; const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
    const by = n => A.dests.find(d => d.name === n), res = extra.map(([a, c]) => [by(a), by(c)]).filter(([a, c]) => a && c);
    for (let i = 0; i < N * 20 && res.length < N + extra.length; i++) {
      const a = A.dests[Math.floor(rnd() * A.dests.length)], c = A.dests[Math.floor(rnd() * A.dests.length)];
      const D = Math.hypot(A.nodes[a.node].x - A.nodes[c.node].x, A.nodes[a.node].z - A.nodes[c.node].z);
      if (D > 3000 && D < 12000) res.push([a, c]);
    }
    return res.map(([a, c]) => ({ from: a.name, to: c.name, fromNode: a.node, toNode: c.node }));
  }, [N, seed, extra]);
  const base = process.env.URL || 'http://localhost:8765', meta = await (await fetch(`${base}/data/meta.json`)).json(), PK = meta.pack, packs = [...meta.packs].reverse();
  const tiles = new Map(), cover = (x, z) => packs.filter(pk => x >= pk.x && x < pk.x + PK && z >= pk.z && z < pk.z + PK);
  const need = async S => { const want = new Set(S.flatMap(s => [-2, 2].flatMap(dx => [-2, 2].flatMap(dz => cover(s.x + dx, s.z + dz)))));
    for (const k of [...tiles.keys()]) if (![...want].some(pk => pk.h === k)) tiles.delete(k);
    for (const pk of want) if (!tiles.has(pk.h)) { const buf = zlib.gunzipSync(Buffer.from(await (await fetch(`${base}/data/${pk.h}.bin`)).arrayBuffer())); tiles.set(pk.h, new Int16Array(buf.buffer, buf.byteOffset, buf.length / 2)); } };
  // layer L at a column: the first pack (latest wins) with a surface above the world floor there, else NaN
  const lay = (x, z, L) => { x = Math.floor(x); z = Math.floor(z); for (const pk of cover(x, z)) { const v = tiles.get(pk.h)[L * PK * PK + (z - pk.z) * PK + x - pk.x]; if (v > 1) return v + (pk.dy ?? 0) + 1; } return NaN; };
  // median of the five columns the viewer's smoothY reads
  const med = (x, z, L) => { const v = [[0,0],[2,0],[-2,0],[0,2],[0,-2]].map(([dx, dz]) => lay(x + dx, z + dz, L)).filter(y => y > 2).sort((a, c) => a - c); return v.length ? v[v.length >> 1] : NaN; };
  const all = [], t0 = Date.now();
  for (const [ri, r] of pairs.entries()) {
    const res = await p.evaluate(async r => {
      const A = window.__api; A.setStart(r.fromNode); if (!await A.startTrip(r.toNode)) return null;
      const T = A.trip(), S = [];
      for (let d = 0; d <= T.L; d += 2) { const q = A.along(d), e = T.edges[Math.min(q.k - 1, T.edges.length - 1)]; S.push({ d, x: q.p.x, z: q.p.z, dk: !!e.dk, y: A.yAt(d), nm: A.names[e.nm] || '', cl: e.cl }); }
      return { L: T.L, S };
    }, r);
    if (!res) { console.log(`route ${ri}: ${r.from} -> ${r.to}: no route`); continue; }
    const { S } = res; await need(S);
    for (const s of S) { s.g0 = med(s.x, s.z, 0); s.g1 = med(s.x, s.z, 1); }
    for (const [i, s] of S.entries()) {
      if (!isNaN(s.g0)) { s.g = s.g0; continue; }
      let m = Infinity; for (let j = Math.max(0, i - 15); j <= Math.min(S.length - 1, i + 15); j++) if (S[j].g1 < m) m = S[j].g1;
      s.g = m; s.est = true;
    }
    const two = s => s.g1 - s.g >= 4, runs = [];
    for (let i = 0; i < S.length; i++) if (two(S[i])) { const l = runs[runs.length - 1]; if (l && i - l[1] <= 4) l[1] = i; else runs.push([i, i]); }
    const r1 = v => Math.round(v * 10) / 10;
    res.runs = runs.map(([i, j]) => {
      const ss = S.slice(i, j + 1).filter(two), dk = ss.filter(s => s.dk).length / ss.length, a = S[Math.max(0, i - 1)], c = S[Math.min(S.length - 1, j + 1)];
      // off a bridge edge, a lower layer that dips 4 or more below the road on both sides of the stretch while the top
      // carries straight on is a culvert or a bridge OSM does not tag: the road is the top there. The road level on each
      // side is read at the nearest sample within 30 m where both layers agree, not at the next sample, which can still
      // be under the deck or on an abutment beside the stretch (Culver Drive under a railway: road 29, deck 34, 33 blocks)
      const one = (k, st) => { for (let n = 0; n < 15 && k >= 0 && k < S.length; n++, k += st) if (!S[k].est && S[k].g1 === S[k].g0) return S[k]; return S[Math.max(0, Math.min(S.length - 1, k - st))]; };
      const a2 = one(i - 1, -1), c2 = one(j + 1, 1);
      const kind = dk >= 0.5 ? 'bridge' : ss.every(s => !s.est && Math.abs(s.g1 - a2.g1) <= 2 && Math.abs(s.g1 - c2.g1) <= 2 && Math.min(a2.g1, c2.g1) - s.g >= 4) ? 'untagged' : 'under';
      const exp = kind === 'under' ? 'ground' : 'deck';
      // (unknown ground under a bridge: the top surface just outside the stretch, interpolated)
      for (const s of ss) if (s.est) s.g = a.g1 + (c.g1 - a.g1) * (s.d - a.d) / ((c.d - a.d) || 1);
      // (that estimate fails under a long deck, so there being well under the deck counts as on the ground too)
      const err = s => exp === 'deck' ? Math.abs(s.y - s.g1) : s.est ? Math.min(Math.abs(s.y - s.g), Math.max(0, s.y - s.g1 + 3)) : Math.abs(s.y - s.g);
      let worst = ss[0]; for (const s of ss) if (err(s) > err(worst)) worst = s;
      const m = ss[ss.length >> 1], took = s => Math.abs(s.y - s.g1) < Math.abs(s.y - s.g) ? 'deck' : 'ground';
      return { d0: S[i].d, d1: S[j].d, d: m.d, x: Math.round(m.x), z: Math.round(m.z), road: m.nm, cl: m.cl, dkFrac: r1(dk), kind, expected: exp,
        groundKnown: ss.some(s => !s.est), took: took(worst), maxErr: r1(err(worst)),
        worst: { d: worst.d, x: Math.round(worst.x), z: Math.round(worst.z), y: r1(worst.y), g: worst.g, g1: worst.g1, dk: worst.dk, est: !!worst.est },
        g: m.g, g1: m.g1, y: r1(m.y),
        // profile, ground (lowest layer, NaN if unknown), top layer and bridge flag from 30 m before to 30 m after
        trace: S.slice(Math.max(0, i - 15), j + 16).map(s => [s.d, r1(s.y), s.g0, s.g1, +s.dk]) };
    });
    for (const c of res.runs) { c.wrong = c.maxErr > 2; all.push({ route: ri, from: r.from, to: r.to, fromNode: r.fromNode, toNode: r.toNode, ...c }); }
    console.log(`route ${ri}: ${r.from} -> ${r.to}, ${Math.round(res.L)} m, ${res.runs.length} two-level stretches, ${res.runs.filter(c => c.wrong).length} wrong (${Math.round((Date.now() - t0) / 1000)} s)`);
    fs.writeFileSync(out, JSON.stringify(all));
  }
  const n = k => all.filter(k).length;
  console.log(`${all.length} two-level stretches (${n(c => !c.groundKnown)} with the lowest layer unknown): ${n(c => c.kind === 'bridge')} on a bridge edge (${n(c => c.kind === 'bridge' && c.wrong)} wrong), ` +
    `${n(c => c.kind === 'under')} under a bridge (${n(c => c.kind === 'under' && c.wrong)} wrong), ${n(c => c.kind === 'untagged')} over a culvert or untagged bridge (${n(c => c.kind === 'untagged' && c.wrong)} wrong)`);
  for (const c of all.filter(c => c.wrong)) console.log(`  WRONG route ${c.route} ${c.from} -> ${c.to} at ${c.worst.d} m (${c.worst.x}, ${c.worst.z}): ${c.kind}, expected ${c.expected}, y ${c.worst.y}, ground ${c.worst.g}${c.worst.est ? ' (est)' : ''}, deck ${c.worst.g1}, road ${c.road}`);
  await b.close();
})();
