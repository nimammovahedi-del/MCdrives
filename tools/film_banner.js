// Films the homepage background: the camera keeps one height and angle and follows the Wedge EV as it drives
// a freeway at a steady speed. Frames are rendered one at a time (the clock does not matter), so any range of
// frames can be filmed on its own and the pieces joined later.
//
//   node tools/film_banner.js <site dir> <config.json> <out dir> [first frame] [last frame]
//
// The site dir is a built site (a built/<area> branch). It is served on a local port; its map.html is copied to
// film.html with a few test hooks added. The config (tools/film/*.json) names the drive and the camera:
//   from, to: [x, z] points near the freeway where the drive starts and ends; via: optional [x, z] points between
//   roads: road names the start and end may snap to; speed: m/s; startAt: metres into the route for frame 0
//   seconds, fps, width, height; offset: camera position relative to the point it looks at, [x, y, z] in metres
//   carY: where the car sits on screen, 0 top to 1 bottom; smooth: metres of route averaged for the camera path
//   fov, fog: [near, far], stream: metres of map kept loaded, sky: day or night
//   carScale: draws the car larger so it reads from high up; clouds: { scale, y } spreads the block clouds over
//   the whole drive at a fixed height, so the sky above the horizon is not empty
// With STILLS set to a JSON list of config overrides (each with a name), only the first frame is shot once per
// override, as <name>.jpg, to compare framings.
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path'), http = require('http');

const [siteDir, cfgPath, outDir, first = '0', last = ''] = process.argv.slice(2);
const C = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
const total = Math.round(C.seconds * C.fps), f0 = +first, f1 = Math.min(total - 1, last === '' ? total - 1 : +last);
fs.mkdirSync(outDir, { recursive: true });

// film.html: map.html plus hooks to reach the scene, hide labels and widen the loaded area
let src = fs.readFileSync(path.join(siteDir, 'map.html'), 'utf8');
const patch = (a, b) => { if (!src.includes(a)) throw new Error('map.html changed, hook not found: ' + a); src = src.replace(a, b); };
patch('else updateLabels(focus, curNm, nextNm);',
  'else if (!window.__nolabels) updateLabels(focus, curNm, nextNm); else for (const l of labels) { if (l.plain) l.plain.visible = false; if (l.hot) l.hot.visible = false; }');
patch('window.__ready = true;',
  'window.__film = { scene, camera, orbit, car, routeLine, sky, setStream: r => { streamR = r; }, dirty: () => { dirty = true; } }; window.__ready = true;');
fs.writeFileSync(path.join(siteDir, 'film.html'), src);

// a plain static server (python's http.server cannot keep up with the many small requests)
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.bin': 'application/octet-stream' };
const server = http.createServer((req, res) => {
  const f = path.join(siteDir, decodeURIComponent(req.url.split('?')[0]));
  fs.readFile(f, (err, buf) => {
    if (err) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(f)] || 'application/octet-stream' }); res.end(buf);
  });
}).listen(0);

// start and end nodes: on a one-way carriageway of the named roads, heading the way of the drive; the end (and
// each via) is the nearest such node that can be reached from the one before (some carriageway pieces are cut off)
function pickNodes() {
  const R = JSON.parse(fs.readFileSync(path.join(siteDir, 'roads.json'), 'utf8')), N = R.nodes;
  const ok = new Set(R.names.map((n, i) => (C.roads.includes(n) ? i : -1)).filter(i => i >= 0));
  const adj = N.map(() => []);
  for (const [a, b, , , ow] of R.edges) { adj[a].push(b); if (!ow) adj[b].push(a); }
  const reach = from => { const seen = new Uint8Array(N.length), st = [from]; seen[from] = 1;
    while (st.length) for (const v of adj[st.pop()]) if (!seen[v]) { seen[v] = 1; st.push(v); }
    return seen; };
  const cands = (pt, toward, end) => {
    const out = [];
    for (const [a, b, nm, , ow] of R.edges) {
      if (!ok.has(nm) || !ow) continue;
      const ex = N[b][0] - N[a][0], ez = N[b][1] - N[a][1], l = Math.hypot(ex, ez) || 1;
      if ((ex * toward[0] + ez * toward[1]) / l < 0.5) continue;
      const n = end ? a : b; out.push([Math.hypot(N[n][0] - pt[0], N[n][1] - pt[1]), n]);
    }
    return out.sort((x, y) => x[0] - y[0]).map(x => x[1]);
  };
  const pts = [C.from, ...(C.via || []), C.to];
  const dir = (a, b) => { const x = b[0] - a[0], z = b[1] - a[1], l = Math.hypot(x, z); return [x / l, z / l]; };
  const chain = [];
  for (const [i, pt] of pts.entries()) {
    const list = cands(pt, dir(pts[Math.max(0, i - 1)], pts[Math.min(pts.length - 1, i + 1)]), i === pts.length - 1);
    if (!list.length) throw new Error('no node near ' + pt);
    if (i === 0) { chain.push(list[0]); continue; }
    const seen = reach(chain[i - 1]), n = list.find(n => seen[n]);
    if (n == null) throw new Error('nothing reachable near ' + pt);
    chain.push(n);
  }
  console.log('nodes', chain.map(n => N[n].map(Math.round).join(',')).join(' -> '));
  return { from: chain[0], to: chain[chain.length - 1], via: chain.slice(1, -1) };
}

(async () => {
  const nodes = pickNodes();
  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const p = await b.newPage({ viewport: { width: C.width, height: C.height }, deviceScaleFactor: 1 });
  p.on('pageerror', e => console.log('pageerror:', e.message));
  p.on('console', m => { if (m.type() === 'error') console.log('console:', m.text().slice(0, 300)); });
  await p.goto(`http://localhost:${server.address().port}/film.html?mode=orbit&sky=${C.sky || 'day'}&car=wedge&traffic=off&dpr=1&fps=60&budget=400`);
  await p.waitForFunction(() => window.__ready, null, { timeout: 600000 });
  await p.addStyleTag({ content: 'body > *:not(#stage), #stage > *:not(canvas) { display: none !important }' });
  const info = await p.evaluate(async ({ nodes, C }) => {
    window.__nolabels = true;
    const A = window.__api, F = window.__film;
    A.setStart(nodes.from);
    const ok = await A.startTrip(nodes.to, 'film', nodes.via.map(n => ({ node: n, name: '' })));
    if (!ok) return { error: 'no route' };
    window.__setMode('orbit'); F.routeLine.clear(); F.sky.setAnimate(false);
    F.setStream(C.stream); F.scene.fog.near = C.fog[0]; F.scene.fog.far = C.fog[1];
    F.orbit.maxDistance = 1e5; F.orbit.minDistance = 1; F.orbit.maxPolarAngle = Math.PI;
    const T = A.trip();
    // the sky dome is 3000 m across and follows the camera: keep it inside the far plane
    window.__setLook = c => {
      F.camera.far = Math.max(c.fog[1] + 200, 3200, c.clouds ? 9000 : 0); F.camera.fov = c.fov; F.camera.updateProjectionMatrix();
      F.car.scale.setScalar(c.carScale || 1);
    };
    window.__setLook(C);
    if (C.clouds) {
      // one big cloud deck pinned over the middle of the drive (the sky module moves its small deck with the camera)
      const cl = F.scene.children.find(o => o.isGroup && o.renderOrder === 2 && o.children.length === 4 && o.children[0].material?.depthWrite === false);
      const mid = A.along(C.startAt + C.speed * C.seconds / 2).p, up = F.sky.update.bind(F.sky);
      cl.scale.set(C.clouds.scale, 1, C.clouds.scale);
      F.sky.update = (dt, cam) => { up(dt, cam); cl.position.set(mid.x, C.clouds.y, mid.z); };
    }
    // where the camera looks for route distance d: the route averaged over C.smooth metres, at the car's height
    const V = F.car.position.constructor;
    window.__look = d => {
      const s = new V(); let n = 0, y = 0;
      for (let k = -C.smooth / 2; k <= C.smooth / 2; k += 5) { s.add(A.along(d + k).p); y += A.yAt(d + k); n++; }
      s.multiplyScalar(1 / n); s.y = y / n; return s;
    };
    // the look point sits ahead of the car (away from the camera) so the car shows at height carY on screen
    window.__offset = C.offset;
    window.__place = (d, lead) => {
      const t = window.__look(d), o = new V(...window.__offset), fwd = o.clone().setY(0).normalize().multiplyScalar(-1);
      t.addScaledVector(fwd, lead);
      F.orbit.target.copy(t); F.camera.position.copy(t).add(o); F.camera.lookAt(t); F.orbit.update(); F.dirty();
    };
    window.__carY = () => { F.camera.updateMatrixWorld(); const v = F.car.position.clone().project(F.camera); return (1 - v.y) / 2; };
    return { L: T.L, steps: T.steps.map(s => [Math.round(s.at), s.kind, s.name]) };
  }, { nodes, C });
  console.log(JSON.stringify(info));
  if (info.error) process.exit(1);
  const need = C.startAt + C.speed * C.seconds;
  if (info.L < need) console.log(`warning: route is ${Math.round(info.L)} m, the film needs ${Math.round(need)} m`);

  // one animation frame draws the new pose; more only while map near the camera is still loading
  const raf = () => p.evaluate(() => new Promise(r => requestAnimationFrame(() => r(window.__idle()))));
  const settle = async () => {
    const t = Date.now();
    while (!(await raf())) if (Date.now() - t > 900000) { console.log('still loading after 15 min'); break; }
  };
  // find the lead (metres) that puts the car at height carY, once, at the first frame
  const findLead = async () => {
  let lead = 0;
  for (let it = 0; it < 6; it++) {
    await p.evaluate(([d, v, l]) => { window.__freeze(d, v); window.__place(d, l); }, [C.startAt + C.speed * f0 / C.fps, C.speed, lead]);
    await raf(); await raf();
    const y = await p.evaluate(() => window.__carY());
    lead += (C.carY - y) * Math.hypot(C.offset[0], C.offset[2]) * 0.9;
  }
  return lead;
  };
  if (process.env.STILLS) {
    for (const v of JSON.parse(process.env.STILLS)) {
      Object.assign(C, v);
      await p.evaluate(c => { window.__offset = c.offset; window.__setLook(c); }, C);
      const lead = await findLead();
      await p.evaluate(([d, v, l]) => { window.__freeze(d, v); window.__place(d, l); }, [C.startAt + C.speed * f0 / C.fps, C.speed, lead]);
      await settle();
      await p.screenshot({ path: path.join(outDir, v.name + '.jpg'), type: 'jpeg', quality: 94, timeout: 300000 });
      console.log('still', v.name);
    }
    await b.close(); server.close(); return;
  }
  const lead = await findLead();
  console.log('lead', Math.round(lead));
  const t0 = Date.now();
  for (let f = f0; f <= f1; f++) {
    const d = C.startAt + C.speed * f / C.fps;
    await p.evaluate(([d, v, l]) => { window.__freeze(d, v); window.__place(d, l); }, [d, C.speed, lead]);
    await settle();
    await p.screenshot({ path: path.join(outDir, `f${String(f).padStart(5, '0')}.jpg`), type: 'jpeg', quality: 94, timeout: 300000 });
    if ((f - f0) % 50 === 0) console.log(`frame ${f} of ${f1} (${((Date.now() - t0) / 1000 / (f - f0 + 1)).toFixed(2)} s each)`);
  }
  await b.close(); server.close();
})().catch(e => { console.error(e); process.exit(1); });
