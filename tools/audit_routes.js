// Route audit: drives N seeded routes in the viewer and records, without any judgement calls,
// (1) the height profile under the car and every sudden jump in it, (2) screenshots at each jump
// and at even spacing, for a human or model reviewer.   node tools/audit_routes.js OUT_DIR [N] [SHOTS_PER_ROUTE]
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const out = process.argv[2] || 'audit', N = +(process.argv[3] || 12), SH = +(process.argv[4] || 4);
  fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const p = await b.newPage({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 });
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto(`${process.env.URL || 'http://localhost:8765'}/?budget=400`);
  await p.waitForFunction(() => window.__ready, null, { timeout: 600000 });
  const idle = () => p.waitForFunction(() => window.__idle(), null, { timeout: 600000 });
  const routes = await p.evaluate(async N => {
    const A = window.__api; let s = 12345; const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
    const res = [];
    for (let i = 0; i < N * 4 && res.length < N; i++) {
      const a = A.dests[Math.floor(rnd() * A.dests.length)], c = A.dests[Math.floor(rnd() * A.dests.length)];
      if (a === c) continue;
      A.setStart(a.node); if (!await A.startTrip(c.node)) continue;
      const t = A.trip(); if (t.L < 600) continue;
      const prof = []; for (let d = 0; d <= t.L; d += 2) { const q = A.along(d).p; prof.push([Math.round(q.x), Math.round(q.z), +A.yAt(d).toFixed(2), A.groundY(q.x, q.z)]); }
      const jumps = []; for (let k = 1; k < prof.length; k++) { const dy = prof[k][2] - prof[k - 1][2]; if (Math.abs(dy) >= 1.2) jumps.push({ at: k * 2, dy: +dy.toFixed(2), x: prof[k][0], z: prof[k][1] }); }
      res.push({ id: res.length, from: a.name, to: c.name, fromNode: a.node, toNode: c.node, L: Math.round(t.L), steps: t.steps.map(s => ({ at: Math.round(s.at), kind: s.kind, name: s.name })), jumps, minY: Math.min(...prof.map(q => q[2])), maxY: Math.max(...prof.map(q => q[2])) });
    }
    return res;
  }, N);
  for (const r of routes) {
    await p.evaluate(async r => { window.__api.setStart(r.fromNode); await window.__api.startTrip(r.toNode); window.__setMode('nav'); }, r);
    // cluster jumps into incidents 40 m apart, then shoot the worst ones plus evenly spaced points
    const inc = []; for (const j of r.jumps) { const l = inc[inc.length - 1]; if (l && j.at - l.end < 40) { l.end = j.at; l.max = Math.max(l.max, Math.abs(j.dy)); l.n++; } else inc.push({ at: j.at, end: j.at, max: Math.abs(j.dy), n: 1 }); }
    r.incidents = inc;
    const pts = inc.slice().sort((a, c) => c.max - a.max).slice(0, 2).map(i => ({ d: Math.max(0, i.at - 12), why: `height jump of ${i.max.toFixed(1)} blocks` }));
    for (let k = 1; pts.length < SH; k++) { if (k > SH) break; pts.push({ d: Math.round(r.L * k / (SH + 1)), why: 'even spacing' }); }
    r.shots = [];
    for (const [i, s] of pts.entries()) {
      await p.evaluate(d => window.__seek(d), s.d); await p.waitForTimeout(600); await idle(); await p.waitForTimeout(1200);
      const f = `r${r.id}_${i}.png`; await p.screenshot({ path: `${out}/${f}` }); r.shots.push({ file: f, at: s.d, why: s.why });
    }
    r.heightTiles = await p.evaluate(() => window.__stats().heightTiles);
    console.log(`route ${r.id}: ${r.from} -> ${r.to}, ${r.L} m, ${r.jumps.length} jumps in ${inc.length} incidents, ${r.heightTiles} height tiles held`);
    fs.writeFileSync(`${out}/routes.json`, JSON.stringify(routes, null, 1));
  }
  await b.close();
})();
