// Audit photos of a built site, for reviewers to look for glitches.
//   node tools/audit_shots.js SITE_URL OUT_DIR [SHOTS=60] [SHARD=0] [SHARDS=1] [SEED=1] [options]
// SITE_URL serves a built site with tools/survey.html copied next to its index.html. Picks places of several kinds
// (junctions, bridges, residential streets, named places, the seams between chunks), spread over the whole area by
// taking them from a grid of cells in turn, and writes one JPEG each plus index_<SHARD>.json describing every shot.
// Only targets on built land are used: inside one of the chunk rectangles of meta.chunks.
//
// Options:
//   --spots FILE|URL|JSON  shoot these spots instead of sampling: a JSON list of {x, z, label, yaw?, cat?, views?, id?}
//                          in the area frame (the frame of roads.json and of index.json's x, z), or an index.json
//                          (a list of its entries or {"shots": [...]}) from an earlier run, which re-takes the exact
//                          same pictures under the same file names: for before/after comparisons of a fix.
//                          Spots are dealt to the shards like sampled places (SHOTS is then ignored). A spot keeps
//                          its yaw unless it has "turn": true (then it may be looked at from another side, as sampled ones).
//   --views low,high,top   which views to take of each spot (default: top for seams, low and high for the rest)
//   --radius N             streaming radius in blocks (default 600; the viewer's own is 750), fog pulled in to match
//   --idle MS              longest wait for the packs around a spot to load (default 60000)
const { chromium } = require('playwright');
const fs = require('fs');
const argv = process.argv.slice(2), opt = {}, pos = [];
for (let i = 0; i < argv.length; i++) argv[i].startsWith('--') ? (opt[argv[i].slice(2)] = argv[++i]) : pos.push(argv[i]);
const [url, out, nShots = '60', shard = '0', shards = '1', seed0 = '1'] = pos;
const RADIUS = +(opt.radius ?? 600), IDLE_MS = +(opt.idle ?? 60000), SHOT_MS = 90000;
let seed = +seed0 * 2654435761 % 2 ** 32;
const rnd = () => (seed = (seed * 1664525 + 1013904223) % 2 ** 32) / 2 ** 32;
const pickFrom = a => a[Math.floor(rnd() * a.length)];
// [height above the target, degrees below the horizon]; the camera stands h / tan(pitch) behind the target
const VIEWS = { low: [18, 18], high: [55, 38], top: [70, 85] };
const withTimeout = (p, ms, what) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error(`${what}: no answer in ${ms} ms`)), ms))]);

(async () => {
  const get = async p => (await fetch(`${url}/${p}`)).json();
  const meta = await get('data/meta.json'), G = await get('roads.json');
  const places = await get('places.json').catch(() => []);
  const ox = meta.minx || 0, oz = meta.minz || 0;      // roads and places are in the area frame, the camera in the built world's

  // ---- the built land, in the built world's frame: one rectangle per chunk. meta.chunks gives each chunk's corner
  // (dx, dz) and, from newer joins, its size (w, d); otherwise the size is the grid step, worked out as the 2D map
  // does (web/drive/mapview.js). owner() is the chunk whose grid cell holds a point (null for a hole or the outside).
  const cs = meta.chunks?.length ? meta.chunks : [{ id: 'all', dx: 0, dz: 0, w: meta.width, d: meta.depth }];
  const step = vs => { const u = [...new Set(vs)].sort((a, b) => a - b), d = u.slice(1).map((v, i) => v - u[i]), m = d.length ? Math.min(...d) : 0;
    return m ? (u[u.length - 1] - u[0]) / Math.round((u[u.length - 1] - u[0]) / m) : 0; };
  const SX = step(cs.map(c => c.dx)) || cs[0].w || meta.width, SZ = step(cs.map(c => c.dz)) || cs[0].d || meta.depth;
  const X0 = Math.min(...cs.map(c => c.dx)), Z0 = Math.min(...cs.map(c => c.dz));
  const rects = cs.map(c => ({ id: c.id, x0: c.dx, z0: c.dz, x1: c.dx + (c.w ?? SX), z1: c.dz + (c.d ?? SZ) }));
  const byCell = new Map(rects.map(r => [`${Math.round((r.x0 - X0) / SX)},${Math.round((r.z0 - Z0) / SZ)}`, r]));
  const owner = (x, z) => byCell.get(`${Math.floor((x - X0) / SX)},${Math.floor((z - Z0) / SZ)}`) ?? null;
  // the chunk a point is on, or null when it is not on built land (m: blocks it must be inside by)
  const chunkAt = (x, z, m = 0) => { const o = owner(x, z); const ok = r => r && x >= r.x0 + m && x <= r.x1 - m && z >= r.z0 + m && z <= r.z1 - m;
    return ok(o) ? o.id : (rects.find(ok)?.id ?? null); };

  // ---- spots to photograph
  const viewsOf = v => (opt.views ? opt.views.split(',') : v.views ?? (v.cat === 'seam' ? ['top'] : ['low', 'high'])).filter(t => VIEWS[t]);
  let mine;
  if (opt.spots) {
    const src = opt.spots.trim();
    let list = src.startsWith('[') || src.startsWith('{') ? JSON.parse(src) : /^https?:/.test(src) ? await (await fetch(src)).json() : JSON.parse(fs.readFileSync(src, 'utf8'));
    if (!Array.isArray(list)) list = list.shots ?? list.spots ?? [];
    // index.json entries are one per picture: fold them back into one spot per place, with its views
    const spots = new Map();
    list.forEach((s, i) => {
      const id = s.id ?? (s.file ? s.file.split('_')[0] : `p${String(i).padStart(4, '0')}`);
      const k = s.file ? id : `${id}#${i}`;
      const v = spots.get(k) ?? { id, x: +s.x - ox, z: +s.z - oz, yaw: s.yaw ?? 0, yawOf: {}, cat: s.cat ?? 'spot', what: s.what ?? s.label ?? '', views: s.views ? [...s.views] : s.view ? [] : undefined, deck: s.deck, turn: !!s.turn };
      if (s.view && !v.views.includes(s.view)) v.views.push(s.view);
      if (s.view && s.yaw !== undefined) v.yawOf[s.view] = s.yaw;   // an index entry: the heading that view was taken from
      spots.set(k, v);
    });
    mine = [...spots.values()].filter((_, i) => i % +shards === +shard);
  } else {
    const deg = new Map(); for (const e of G.edges) { deg.set(e[0], (deg.get(e[0]) || 0) + 1); deg.set(e[1], (deg.get(e[1]) || 0) + 1); }
    const mid = e => { const a = G.nodes[e[0]], b = G.nodes[e[1]]; return { x: (a[0] + b[0]) / 2 - ox, z: (a[1] + b[1]) / 2 - oz, yaw: Math.atan2(b[0] - a[0], b[1] - a[1]), len: Math.hypot(b[0] - a[0], b[1] - a[1]) }; };
    const cands = { junction: [], bridge: [], freeway: [], residential: [], place: [], seam: [] };
    for (const e of G.edges) {
      const m = mid(e), name = G.names[e[2]] || 'unnamed road';
      if (e[7]) continue;                                   // private streets are not audited for now
      if (e[6]) cands.bridge.push({ ...m, deck: true, what: `bridge or raised road, ${name}` });
      else if (e[3] <= 1 && m.len > 30) cands.freeway.push({ ...m, what: `freeway or ramp, ${name}` });
      else if (e[3] === 6 && m.len > 25) cands.residential.push({ ...m, what: `residential street, ${name}` });
      if (e[3] <= 4 && deg.get(e[0]) >= 3) { const a = G.nodes[e[0]]; cands.junction.push({ x: a[0] - ox, z: a[1] - oz, yaw: m.yaw, what: `junction on ${name}` }); }
    }
    for (const p of places) cands.place.push({ x: p[2] - ox, z: p[3] - oz, yaw: rnd() * 6.28, what: `${p[1]}: ${p[0]}` });
    // seams: a road edge whose ends lie in the grid cells of two different chunks that were both built (an edge
    // that runs out of the built land is the outside border, not a seam)
    if (cs.length > 1) for (const e of G.edges) {
      const a = G.nodes[e[0]], b = G.nodes[e[1]], ca = owner(a[0] - ox, a[1] - oz), cb = owner(b[0] - ox, b[1] - oz);
      if (ca && cb && ca !== cb) cands.seam.push({ ...mid(e), what: `seam between ${ca.id} and ${cb.id}, on ${G.names[e[2]] || 'unnamed road'}` });
    }
    // only targets on built land (places and arrival points of older joins can lie past it)
    for (const k in cands) cands[k] = cands[k].filter(c => chunkAt(c.x, c.z) !== null);
    // spread: bucket every kind into 3 km cells and take cells in shuffled turn
    const kinds = Object.keys(cands).filter(k => cands[k].length), views = [], total = +nShots * +shards;
    const queues = {};
    for (const k of kinds) {
      const cells = new Map(); for (const c of cands[k]) { const key = `${Math.floor(c.x / 3000)},${Math.floor(c.z / 3000)}`; (cells.get(key) || cells.set(key, []).get(key)).push(c); }
      queues[k] = [...cells.values()].sort(() => rnd() - .5);
    }
    for (let i = 0; views.length < total && i < total * 4; i++) {
      const k = kinds[i % kinds.length], q = queues[k]; if (!q.length) continue;
      const c = pickFrom(q[Math.floor(i / kinds.length) % q.length]);
      if (c.taken) continue; c.taken = true;   // one picture set per spot
      // whole blocks in both frames, so the index's x, z re-take exactly this spot (--spots)
      views.push({ ...c, x: Math.round(c.x), z: Math.round(c.z), yaw: Math.round(c.yaw * 1e4) / 1e4, cat: k, id: `s${String(views.length).padStart(4, '0')}` });
    }
    mine = views.filter((_, i) => i % +shards === +shard);
    for (const v of mine) v.turn = true;   // sampled spots may be looked at from another side (see yawsFor)
  }
  // the headings a view may take: an explicit spot keeps its own; a sampled one may also turn around or aside, but
  // never so that the camera stands off the built land (it would look at the void). survey.html's __aim picks the
  // one whose camera need not rise over a building or a slope.
  const norm = a => Math.round(Math.atan2(Math.sin(a), Math.cos(a)) * 1e4) / 1e4;
  const yawsFor = (v, tag) => {
    if (!v.turn) return [v.yawOf?.[tag] ?? v.yaw];
    const d = VIEWS[tag][0] / Math.tan(VIEWS[tag][1] * Math.PI / 180);
    const ok = [0, Math.PI, Math.PI / 2, -Math.PI / 2].map(a => norm(v.yaw + a)).filter(y => chunkAt(v.x + Math.sin(y) * d, v.z + Math.cos(y) * d) !== null);
    return ok.length ? ok : [v.yaw];
  };
  // visit the spots nearest first, so neighbours reuse the packs already loaded
  for (let i = 1; i < mine.length; i++) {
    let b = i, bd = Infinity;
    for (let j = i; j < mine.length; j++) { const d = Math.hypot(mine[j].x - mine[i - 1].x, mine[j].z - mine[i - 1].z); if (d < bd) { bd = d; b = j; } }
    [mine[i], mine[b]] = [mine[b], mine[i]];
  }
  fs.mkdirSync(out, { recursive: true });

  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const index = [];
  let pg = null;
  const open = async () => {
    if (pg) await pg.close().catch(() => {});
    pg = await b.newPage({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 });
    pg.on('pageerror', e => console.log('PAGEERR', e.message));
    await pg.goto(`${url}/survey.html?budget=400&notrip=1&r=${RADIUS}`);
    await pg.waitForFunction(() => window.__ready, null, { timeout: 600000 });
    await pg.evaluate(() => { window.__hold = true; });   // draw only in __shot
  };
  const shoot = async (v, tag, file) => {
    const [h, p] = VIEWS[tag], t0 = Date.now();
    const yaws = yawsFor(v, tag);
    const cam = await withTimeout(pg.evaluate(a => window.__aim(a), { x: v.x, z: v.z, yaw: yaws[0], yaws, h, pitch: p, deck: !!v.deck }), 60000, 'aim');
    let idle = false;
    for (const dl = Date.now() + IDLE_MS; Date.now() < dl;) {
      if (await withTimeout(pg.evaluate(() => window.__idle()), 30000, 'idle')) { idle = true; break; }
      await pg.waitForTimeout(500);
    }
    const jpg = await withTimeout(pg.evaluate(() => window.__shot(0.82)), SHOT_MS, 'shot');
    fs.writeFileSync(`${out}/${file}`, Buffer.from(jpg.slice(jpg.indexOf(',') + 1), 'base64'));
    return { cam, idle, ms: Date.now() - t0 };
  };
  await open();
  let failed = 0, skipped = 0;
  for (const v of mine) {
    const chunk = chunkAt(v.x, v.z);
    if (chunk === null) { skipped++; console.log('SKIPPED', v.id, `(${Math.round(v.x + ox)}, ${Math.round(v.z + oz)}) is not on built land`); continue; }
    for (const tag of viewsOf(v)) {
      const file = `${v.id}_${v.cat}_${tag}.jpg`;
      let r = null;
      for (let attempt = 0; attempt < 2 && !r; attempt++) {
        try { r = await shoot(v, tag, file); }
        catch (e) { console.log(attempt ? 'FAILED' : 'RETRY', file, e.message.split('\n')[0]); await open().catch(e2 => console.log('REOPEN FAILED', e2.message.split('\n')[0])); }
      }
      if (!r) { failed++; continue; }
      index.push({ file, id: v.id, cat: v.cat, view: tag, what: v.what, x: Math.round(v.x + ox), z: Math.round(v.z + oz), yaw: r.cam.yaw, chunk,
                   ...(v.deck ? { deck: true } : {}), cam: { h: Math.round(r.cam.h * 10) / 10, pitch: Math.round(r.cam.pitch * 10) / 10 },
                   ground: r.cam.ground, loaded: r.idle, ms: r.ms });
      console.log(file, chunk, `${(r.ms / 1000).toFixed(1)}s`, r.cam.h > VIEWS[tag][0] + 0.5 ? `(raised to ${r.cam.h.toFixed(0)})` : '', r.idle ? '' : '(not fully loaded)', r.cam.ground ? '' : '(no ground found)');
    }
  }
  fs.writeFileSync(`${out}/index_${shard}.json`, JSON.stringify(index, null, 1));
  console.log(`${index.length} pictures, ${failed} failed, ${skipped} spots skipped`);
  await b.close();
})();
