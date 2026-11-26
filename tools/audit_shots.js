// Audit photos of a built site, for reviewers to look for glitches.
//   node tools/audit_shots.js SITE_URL OUT_DIR [SHOTS=60] [SHARD=0] [SHARDS=1] [SEED=1]
// SITE_URL serves a built site with tools/survey.html copied next to its index.html. Picks places of several kinds
// (junctions, bridges, residential streets, named places, the seams between chunks), spread over the whole area by
// taking them from a grid of cells in turn, and writes one JPEG each plus index.json describing every shot.
const { chromium } = require('playwright');
const fs = require('fs');
const [url, out, nShots = '60', shard = '0', shards = '1', seed0 = '1'] = process.argv.slice(2);
let seed = +seed0 * 2654435761 % 2 ** 32;
const rnd = () => (seed = (seed * 1664525 + 1013904223) % 2 ** 32) / 2 ** 32;
const pickFrom = a => a[Math.floor(rnd() * a.length)];

(async () => {
  const get = async p => (await fetch(`${url}/${p}`)).json();
  const meta = await get('data/meta.json'), G = await get('roads.json');
  const places = await get('places.json').catch(() => []);
  const ox = meta.minx || 0, oz = meta.minz || 0;      // roads and places are in the area frame, the camera in the built world's
  const deg = new Map(); for (const e of G.edges) { deg.set(e[0], (deg.get(e[0]) || 0) + 1); deg.set(e[1], (deg.get(e[1]) || 0) + 1); }
  const mid = e => { const a = G.nodes[e[0]], b = G.nodes[e[1]]; return { x: (a[0] + b[0]) / 2 - ox, z: (a[1] + b[1]) / 2 - oz, yaw: Math.atan2(b[0] - a[0], b[1] - a[1]), len: Math.hypot(b[0] - a[0], b[1] - a[1]) }; };
  const cands = { junction: [], bridge: [], freeway: [], residential: [], place: [], seam: [] };
  for (const e of G.edges) {
    const m = mid(e), name = G.names[e[2]] || 'unnamed road';
    if (e[7]) continue;                                   // private streets are not audited for now
    if (e[6]) cands.bridge.push({ ...m, what: `bridge or raised road, ${name}` });
    else if (e[3] <= 1 && m.len > 30) cands.freeway.push({ ...m, what: `freeway or ramp, ${name}` });
    else if (e[3] === 6 && m.len > 25) cands.residential.push({ ...m, what: `residential street, ${name}` });
    if (e[3] <= 4 && deg.get(e[0]) >= 3) { const a = G.nodes[e[0]]; cands.junction.push({ x: a[0] - ox, z: a[1] - oz, yaw: m.yaw, what: `junction on ${name}` }); }
  }
  for (const p of places) cands.place.push({ x: p[2] - ox, z: p[3] - oz, yaw: rnd() * 6.28, what: `${p[1]}: ${p[0]}` });
  // seams: a road edge whose ends lie in different chunks
  const ch = (meta.chunks || []).map(c => [c.dx, c.dz]); const xs = [...new Set(ch.map(c => c[0]))].sort((a, b) => a - b), zs = [...new Set(ch.map(c => c[1]))].sort((a, b) => a - b);
  const cell = (v, cuts) => { let i = -1; for (const c of cuts) if (v >= c) i++; return i; };
  if (ch.length > 1) for (const e of G.edges) {
    const a = G.nodes[e[0]], b = G.nodes[e[1]];
    if (cell(a[0] - ox, xs) !== cell(b[0] - ox, xs) || cell(a[1] - oz, zs) !== cell(b[1] - oz, zs)) cands.seam.push({ ...mid(e), what: `seam between build areas, on ${G.names[e[2]] || 'unnamed road'}`, top: true });
  }
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
    views.push({ ...c, cat: k, id: `s${String(views.length).padStart(4, '0')}` });
  }
  const mine = views.filter((_, i) => i % +shards === +shard);
  fs.mkdirSync(out, { recursive: true });

  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const index = [];
  let pg = null;
  const open = async () => {
    pg = await b.newPage({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 });
    pg.on('pageerror', e => console.log('PAGEERR', e.message));
    await pg.goto(`${url}/survey.html?budget=400`);
    await pg.waitForFunction(() => window.__ready, null, { timeout: 600000 });
  };
  await open();
  for (const v of mine) {
    const shots = v.top ? [['top', 70, 85]] : [['low', 18, 18], ['high', 55, 38]];   // street level, then from above
    for (const [tag, h, p] of shots) {
      const file = `${v.id}_${v.cat}_${tag}.jpg`;
      try {
        await pg.evaluate(async ({ x, z, yaw, h, p }) => {
          window.__free = { x, y: 0, z, yaw, h, pitch: p * Math.PI / 180 };
          await window.__api.loadHeights(x, z, 0);
          const y = window.__api.smoothY(x, z); if (!isNaN(y)) window.__free.y = y;
        }, { x: v.x, z: v.z, yaw: v.yaw, h, p });
        await pg.waitForTimeout(1200);
        const dl = Date.now() + 90000; let idle = false;
        while (Date.now() < dl) { if (await pg.evaluate(() => window.__idle())) { idle = true; break; } await pg.waitForTimeout(800); }
        await pg.evaluate(() => { const F = window.__free, y = window.__api.smoothY(F.x, F.z); if (!isNaN(y)) F.y = y; });
        await pg.waitForTimeout(2000);
        await pg.screenshot({ path: `${out}/${file}`, type: 'jpeg', quality: 82 });
        index.push({ file, cat: v.cat, view: tag, what: v.what, x: Math.round(v.x + ox), z: Math.round(v.z + oz), loaded: idle });
        console.log(file, idle ? '' : '(not fully loaded)');
      } catch (e) { console.log('FAILED', file, e.message.split('\n')[0]); await pg.close().catch(() => {}); await open(); }
    }
  }
  fs.writeFileSync(`${out}/index_${shard}.json`, JSON.stringify(index, null, 1));
  await b.close();
})();
