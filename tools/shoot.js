const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const p = await b.newPage({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 });
  p.on('console', m => { if (m.type() === 'error') console.log('console:', m.text()); });
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto(`http://localhost:8765/?budget=400&to=${encodeURIComponent(process.argv[2] || 'Alton Parkway')}`);
  await p.waitForFunction(() => window.__ready, null, { timeout: 600000 });
  const idle = () => p.waitForFunction(() => window.__idle(), null, { timeout: 600000 });
  await idle(); console.log(JSON.stringify(await p.evaluate(() => window.__stats())), JSON.stringify(await p.evaluate(() => window.__trip())));
  const out = (process.env.SHOTS || '.') + '/';
  for (const [i, d] of (process.argv[3] || '60,300').split(',').entries()) {
    await p.evaluate(d => window.__seek(d), +d); await p.waitForTimeout(800); await idle(); await p.waitForTimeout(1500);
    await p.screenshot({ path: `${out}nav${i + 1}.png` });
  }
  await p.evaluate(() => window.__setMode('walk')); await p.waitForTimeout(2000);
  await p.screenshot({ path: out + 'street.png' });
  await p.evaluate(() => window.__setMode('orbit')); await p.waitForTimeout(800); await idle(); await p.waitForTimeout(1500);
  await p.screenshot({ path: out + 'overview.png' });
  await b.close();
})();
