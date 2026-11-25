// node shoot_crossings.js LIST.json OUTDIR FROM TO   shoots entries FROM..TO-1 of LIST (each {file, fromNode, toNode, distance})
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const L = JSON.parse(fs.readFileSync(process.argv[2])), out = process.argv[3], a = +process.argv[4], z = +process.argv[5];
  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const p = await b.newPage({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 });
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto(`${process.env.URL}/?budget=400`);
  await p.waitForFunction(() => window.__ready, null, { timeout: 600000 });
  await p.click('#pause'); // hold the car at the crossing
  const idle = () => p.waitForFunction(() => window.__idle(), null, { timeout: 600000 });
  for (const c of L.slice(a, z)) {
    const t = Date.now();
    const info = await p.evaluate(async c => { const A = window.__api; A.setStart(c.fromNode); await A.startTrip(c.toNode); window.__setMode('nav'); window.__seek(c.distance);
      const q = A.along(c.distance).p; return { x: Math.round(q.x), z: Math.round(q.z), y: +A.yAt(c.distance).toFixed(1) }; }, c);
    await p.waitForTimeout(800); await idle(); await p.evaluate(d => window.__seek(d), c.distance); await p.waitForTimeout(1500);
    await p.screenshot({ path: `${out}/${c.file}` });
    console.log(c.file, JSON.stringify(info), Math.round((Date.now() - t) / 1000) + ' s');
  }
  await b.close();
})();
