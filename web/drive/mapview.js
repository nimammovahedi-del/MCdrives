// The block map: a flat, chunky 2D map of the whole area. No 3D is drawn while it is open, so it costs
// far less than the driving view, and zooming out shows the entire city.
//   base:  the build's drawn layer (meta.map.illus: flat parks, trees, water and outlined buildings at 1 block per
//          pixel close up, blended at 4 for the middle zooms, an overview at 16), or its block colour layer
//          (meta.map, style 'blocks'), otherwise parchment
//   roads: roads.json, drawn as pixel lines, minor roads fading out as you zoom out
//   top:   the route, stops, destination, a dropped pin and the car, as pixel sprites; street names in DOM
// The same painter draws the minimap shown while driving.

const PIN = ['..####..', '.#++++#.', '#++##++#', '#++##++#', '.#++++#.', '..#++#..', '...##...', '...##...'];
const DOT = ['..####..', '.#++++#.', '#++++++#', '#++++++#', '#++++++#', '#++++++#', '.#++++#.', '..####..'];
const FLAG = ['#.......', '#####...', '#++++#..', '#+++++#.', '#++++#..', '#####...', '#.......', '#.......'];
const ROAD = [ // by road class: [fill, outline, width in map pixels when close]
  ['#f2b84b', '#7a4a12', 3], ['#f2c65e', '#7a4a12', 3], ['#fbf3dc', '#6b5a3a', 2], ['#fbf3dc', '#6b5a3a', 2],
  ['#f4ecd6', '#8a7a5a', 1], ['#efe6cf', '#8a7a5a', 1], ['#ebe1c8', null, 1], ['#e3d8bd', null, 1]];

import { incidentIcon } from './traffic.js';

// streets on the drawn layer: [fill, casing, width in blocks] by road class, kept at least 1 map pixel wide
const STREET = [['#f7c65a', '#b9802a', 24], ['#f9d77e', '#b9802a', 20], ['#ffffff', '#b8ab92', 15], ['#ffffff', '#b8ab92', 13],
  ['#ffffff', '#c3b79f', 10], ['#fdfbf6', '#c9bea8', 8], ['#fbf8f1', '#d0c6b2', 6], ['#f6f1e6', null, 4]];
const LAND = '#f1ead7';
// a pixel sprite on its own canvas (for the map key)
function spriteCanvas(rows, ink, light, px = 3) {
  const c = document.createElement('canvas'); c.width = rows[0].length * px; c.height = rows.length * px; const g = c.getContext('2d');
  rows.forEach((r, y) => [...r].forEach((ch, x) => { if (ch === '.') return; g.fillStyle = ch === '#' ? ink : light; g.fillRect(x * px, y * px, px, px); }));
  c.className = 'pix'; return c;
}
const CAR = ['...##...', '..#++#..', '..#++#..', '.#++++#.', '.#++++#.', '#++++++#', '#++##++#', '##....##'];
const swatch = (fill, line, kind = 'box') => {
  const c = document.createElement('canvas'); c.width = 24; c.height = 24; const g = c.getContext('2d');
  if (kind === 'line') { g.fillStyle = line ?? fill; g.fillRect(0, 8, 24, 8); g.fillStyle = fill; g.fillRect(0, 10, 24, 4); }
  else { g.fillStyle = fill; g.fillRect(2, 2, 20, 20); if (line) { g.strokeStyle = line; g.lineWidth = 2; g.strokeRect(3, 3, 18, 18); } }
  c.className = 'pix'; return c;
};
const INCIDENT_NAMES = [['closure', 'Road closed'], ['crash', 'Crash'], ['roadwork', 'Road work'], ['lanes', 'Lane closed'], ['fire', 'Fire'], ['animal', 'Animal on the road'], ['hazard', 'Hazard']];
const JAM = r => r < 0 ? '#4a0a0a' : r >= 0.75 ? '#3fbf5a' : r >= 0.45 ? '#f2c230' : '#e5483b';

export function createPainter({ G, N, meta, base = '' }) {
  // roads bucketed by 256-block cell so a view only walks what it can see
  const CELL = 256, cells = new Map(), nameClass = new Map();
  let minX = Infinity, minZ = Infinity, maxX = -Infinity, maxZ = -Infinity;
  for (const p of N) { if (p.x < minX) minX = p.x; if (p.z < minZ) minZ = p.z; if (p.x > maxX) maxX = p.x; if (p.z > maxZ) maxZ = p.z; }
  for (const [i, [a, b, nm, cl]] of G.edges.entries()) {
    const A = N[a], B = N[b];
    for (let cx = Math.floor(Math.min(A.x, B.x) / CELL); cx <= Math.floor(Math.max(A.x, B.x) / CELL); cx++)
      for (let cz = Math.floor(Math.min(A.z, B.z) / CELL); cz <= Math.floor(Math.max(A.z, B.z) / CELL); cz++) {
        const k = cx * 65536 + cz; if (!cells.has(k)) cells.set(k, []); cells.get(k).push(i);
      }
    if (G.names[nm]) nameClass.set(nm, Math.min(nameClass.get(nm) ?? 9, cl));
  }
  // the built land: the area's chunks (a grid of equal cells; meta.chunks lists the built ones). Outside it, a
  // hatched "not built yet" ground, and the land has a dark rim and a shadow, so the map ends on purpose.
  // (chunk offsets, like the map pictures and the packs, are in the world's own frame, the same as N)
  const land = (() => {
    const cs = meta.chunks ?? []; if (!cs.length) return null;
    const step = vs => { const u = [...new Set(vs)].sort((a, b) => a - b), d = u.slice(1).map((v, i) => v - u[i]), m = d.length ? Math.min(...d) : 0;
      return m ? (u[u.length - 1] - u[0]) / Math.round((u[u.length - 1] - u[0]) / m) : 0; };
    const sx = step(cs.map(c => c.dx)) || meta.width, sz = step(cs.map(c => c.dz)) || meta.depth;
    const x0 = Math.min(...cs.map(c => c.dx)), z0 = Math.min(...cs.map(c => c.dz));
    const cell = new Map(cs.map(c => [Math.round((c.dx - x0) / sx) + ',' + Math.round((c.dz - z0) / sz), c]));
    // a chunk's far side is where the next one starts (or one step on, at the outside)
    const right = (i, j, c) => cell.get(`${i + 1},${j}`)?.dx ?? c.dx + Math.round(sx), bottom = (i, j, c) => cell.get(`${i},${j + 1}`)?.dz ?? c.dz + Math.round(sz);
    const rects = [], edges = [];
    for (const [k, c] of cell) {
      const [i, j] = k.split(',').map(Number), a = c.dx, b = c.dz, r = right(i, j, c), e = bottom(i, j, c);
      rects.push([a, b, r, e]);
      if (!cell.has(`${i},${j - 1}`)) edges.push([a, b, r, b]);
      if (!cell.has(`${i},${j + 1}`)) edges.push([a, e, r, e]);
      if (!cell.has(`${i - 1},${j}`)) edges.push([a, b, a, e]);
      if (!cell.has(`${i + 1},${j}`)) edges.push([r, b, r, e]);
    }
    return { rects, edges };
  })();
  const OUTSIDE = (() => {   // 12 x 12 sprite pixels: pale slate with a pixel diagonal hatch
    const c = document.createElement('canvas'); c.width = c.height = 12; const g = c.getContext('2d');
    g.fillStyle = '#d3dade'; g.fillRect(0, 0, 12, 12); g.fillStyle = '#c0c9ce';
    for (let i = 0; i < 12; i++) { g.fillRect(i, 11 - i, 1, 1); g.fillRect((i + 6) % 12, 11 - i, 1, 1); }
    return c;
  })();
  let outPat = null;
  // baked colour layer (optional)
  const M = meta.map, IL = M?.illus, imgs = new Map();
  const img = file => {
    let e = imgs.get(file);
    if (!e) { e = { im: new Image(), ok: false, used: 0 }; e.im.onload = () => { e.ok = true; painter.onload?.(); }; e.im.src = base + 'data/' + file; imgs.set(file, e); }
    e.used = performance.now(); return e;
  };
  function trim() { if (imgs.size < 160) return; const old = [...imgs].sort((a, b) => a[1].used - b[1].used).slice(0, imgs.size - 120); for (const [k] of old) imgs.delete(k); }

  const painter = {
    bounds: { minX, minZ, maxX, maxZ }, nameClass, onload: null, outside: OUTSIDE,
    // 'drawn': the illustrated layer when the site has one; 'blocks': the block colour layer
    style: IL ? 'drawn' : 'blocks', hasDrawn: !!IL,
    /** v: { cx, cz, mpp (blocks per canvas pixel), w, h (canvas pixels), u (canvas pixels per sprite pixel; the symbols' size) };
     *  data: { route?, stops?, dest?, pin?, car?, heading? }. Zoom-dependent choices use mpp * u, the blocks under one sprite pixel. */
    paint(ctx, v, data) {
      const { cx, cz, mpp: cmpp, w, h, u = 1 } = v, X = x => (x - cx) / cmpp + w / 2, Z = z => (z - cz) / cmpp + h / 2;
      const mpp = cmpp * u, d = u / 3;   // mpp: the zoom in sprite pixels; d: canvas pixels per screen pixel
      ctx.imageSmoothingEnabled = false;
      const drawn = IL && painter.style === 'drawn';
      const ground = drawn ? LAND : M ? '#b9b49c' : '#d9c99a';
      const x0 = cx - w / 2 * cmpp, x1 = cx + w / 2 * cmpp, z0 = cz - h / 2 * cmpp, z1 = cz + h / 2 * cmpp;
      if (land) {
        // outside: the hatch, fixed to the ground as it pans, 2 screen pixels per pattern pixel
        outPat ??= ctx.createPattern(OUTSIDE, 'repeat');
        const ps = Math.max(1, Math.round(2 * d)), ox = X(0), oz = Z(0);
        outPat.setTransform(new DOMMatrix([ps, 0, 0, ps, ox - Math.floor(ox / (12 * ps)) * 12 * ps, oz - Math.floor(oz / (12 * ps)) * 12 * ps]));
        ctx.fillStyle = outPat; ctx.fillRect(0, 0, w, h);
        const sh = Math.max(3, Math.round(5 * d));
        ctx.fillStyle = 'rgba(40,52,60,.28)';
        for (const [a, b, c, e] of land.rects) if (c >= x0 && a <= x1 && e >= z0 && b <= z1) ctx.fillRect(Math.round(X(a)) + sh, Math.round(Z(b)) + sh, Math.round(X(c)) - Math.round(X(a)), Math.round(Z(e)) - Math.round(Z(b)));
        ctx.fillStyle = ground;
        for (const [a, b, c, e] of land.rects) if (c >= x0 && a <= x1 && e >= z0 && b <= z1) ctx.fillRect(Math.round(X(a)), Math.round(Z(b)), Math.round(X(c)) - Math.round(X(a)), Math.round(Z(e)) - Math.round(Z(b)));
      } else { ctx.fillStyle = ground; ctx.fillRect(0, 0, w, h); }
      if (drawn) {
        // shrinking a picture blends it (smoothing on); enlarging keeps the crisp block pixels
        const mx = 0, mz = 0, size = meta.pack ?? 512;   // the pictures are in the world's own frame, like N
        // the picture that has about one of its pixels per canvas pixel
        ctx.imageSmoothingEnabled = cmpp > 1; ctx.imageSmoothingQuality = 'high';
        if (cmpp > 6 && IL.overview) { const o = IL.overview, e = img(o.file); if (e.ok) ctx.drawImage(e.im, X(o.x - mx), Z(o.z - mz), e.im.width * o.scale / cmpp, e.im.height * o.scale / cmpp); }
        else for (const [file, wx, wz] of cmpp > 1.5 ? IL.mid : IL.tiles) {
          const tx = wx - mx, tz = wz - mz; if (tx > x1 || tz > z1 || tx + size < x0 || tz + size < z0) continue;
          const e = img(file); if (e.ok) ctx.drawImage(e.im, Math.round(X(tx)), Math.round(Z(tz)), Math.ceil(size / cmpp), Math.ceil(size / cmpp));
        }
        ctx.imageSmoothingEnabled = false; trim();
      } else if (M) {
        // image x/z are in the world's own frame, the same as N (roads.json is shifted into it by meta.minx/minz)
        const mx = 0, mz = 0;
        if (mpp >= 10 && M.overview) { const o = M.overview, e = img(o.file); if (e.ok) ctx.drawImage(e.im, X(o.x - mx), Z(o.z - mz), e.im.width * o.scale / cmpp, e.im.height * o.scale / cmpp); }
        else for (const [file, wx, wz] of M.tiles) {
          const size = meta.pack ?? 512, tx = wx - mx, tz = wz - mz; if (tx > x1 || tz > z1 || tx + size < x0 || tz + size < z0) continue;
          const e = img(file); if (e.ok) ctx.drawImage(e.im, Math.round(X(tx)), Math.round(Z(tz)), Math.ceil(size / cmpp), Math.ceil(size / cmpp));
        }
        trim();
      } else {
        // no colour layer: parchment with a faint block grid every 64 blocks when close
        if (mpp < 4) { ctx.fillStyle = 'rgba(120,95,50,.12)'; const s = 64; ctx.fillStyle = 'rgba(120,95,50,.12)'; for (let gx = Math.ceil(x0 / s) * s; gx < x1; gx += s) ctx.fillRect(Math.round(X(gx)), 0, 1, h); for (let gz = Math.ceil(z0 / s) * s; gz < z1; gz += s) ctx.fillRect(0, Math.round(Z(gz)), w, 1); }
      }
      // roads: which classes show depends on zoom; outlines first, then fills
      const maxCl = drawn ? (mpp > 24 ? 1 : mpp > 12 ? 3 : mpp > 6 ? 4 : mpp > 3 ? 5 : 7) : M ? (mpp > 24 ? 1 : mpp > 12 ? 3 : mpp > 5 ? 4 : -1) : mpp > 24 ? 1 : mpp > 12 ? 3 : mpp > 6 ? 5 : 7, seen = new Set(), list = [];
      for (let gx = Math.floor(x0 / CELL); gx <= Math.floor(x1 / CELL); gx++) for (let gz = Math.floor(z0 / CELL); gz <= Math.floor(z1 / CELL); gz++)
        for (const i of cells.get(gx * 65536 + gz) ?? []) { if (seen.has(i)) continue; seen.add(i); if (G.edges[i][3] <= maxCl) list.push(i); }
      // drawn streets keep their true width in blocks, never thinner than 1 (2 for freeways) screen pixels; casing 1 screen pixel a side
      const width = cl => drawn ? Math.max((cl < 2 ? 2.5 : 1.5) * d, STREET[cl][2] / cmpp) : u * (M ? (cl < 2 ? 2 : 1) : Math.max(1, Math.round(ROAD[cl][2] * Math.min(1.6, 4 / mpp))));
      ctx.lineCap = drawn ? 'round' : 'square'; ctx.lineJoin = 'round';
      const isRamp = e => G.names[e[2]] === 'ramp', rampW = maxCl >= 1 ? Math.max(1.5 * d, 9 / cmpp) : 0;
      for (const pass of [0, 1]) for (let cl = maxCl; cl >= 0; cl--) {
        const [fill, edge] = drawn ? STREET[cl] : ROAD[cl]; if (pass === 0 && (!edge || (M && !drawn))) continue;
        // drawn: freeway ramps are their own narrower lines, under the freeways
        if (drawn && cl === 1 && rampW) {
          ctx.strokeStyle = pass ? STREET[1][0] : STREET[1][1]; ctx.lineWidth = rampW + (pass ? 0 : 2 * d); ctx.beginPath();
          for (const i of list) { const e = G.edges[i]; if (!isRamp(e)) continue; const A = N[e[0]], B = N[e[1]]; ctx.moveTo(X(A.x), Z(A.z)); ctx.lineTo(X(B.x), Z(B.z)); }
          ctx.stroke();
        }
        ctx.strokeStyle = pass ? fill : edge; ctx.lineWidth = width(cl) + (pass ? 0 : drawn ? 2 * d : 2 * u); ctx.beginPath();
        for (const i of list) { const e = G.edges[i]; if (e[3] !== cl || (drawn && isRamp(e))) continue; const A = N[e[0]], B = N[e[1]]; ctx.moveTo(X(A.x), Z(A.z)); ctx.lineTo(X(B.x), Z(B.z)); }
        ctx.stroke();
      }
      // the edge of the built land: a dark rim over the picture's own ragged edge
      if (land) {
        ctx.strokeStyle = '#5b4a33'; ctx.lineWidth = Math.max(2, Math.round(2 * d)); ctx.lineCap = 'square'; ctx.beginPath();
        for (const [a, b, c, e] of land.edges) if (Math.max(a, c) >= x0 && Math.min(a, c) <= x1 && Math.max(b, e) >= z0 && Math.min(b, e) <= z1) { ctx.moveTo(Math.round(X(a)), Math.round(Z(b))); ctx.lineTo(Math.round(X(c)), Math.round(Z(e))); }
        ctx.stroke();
      }
      // live traffic: freeway speeds as green / yellow / red over the road, closed stretches dark red
      if (data.traffic?.segs.length) {
        ctx.lineCap = 'butt'; ctx.lineWidth = Math.max(4 * d, u * Math.round(2 * Math.min(1.6, 4 / mpp)), drawn ? 16 / cmpp : 0);
        const by = new Map(); for (const sg of data.traffic.segs) { const c = JAM(sg[4]); if (!by.has(c)) by.set(c, []); by.get(c).push(sg); }
        for (const [c, list] of by) { ctx.strokeStyle = c; ctx.beginPath(); for (const [ax, az, bx, bz] of list) { if (Math.max(ax, bx) < x0 || Math.min(ax, bx) > x1 || Math.max(az, bz) < z0 || Math.min(az, bz) > z1) continue; ctx.moveTo(X(ax), Z(az)); ctx.lineTo(X(bx), Z(bz)); } ctx.stroke(); }
      }
      // sprites: pixel art at u canvas pixels per sprite pixel; (x, y) is the bottom centre
      const U = Math.max(1, Math.round(u)), cell = (x, y, c) => { ctx.fillStyle = c; ctx.fillRect(x, y, U, U); };
      const sprite = (rows, x, y, ink, light) => { const ox = Math.round(x - rows[0].length * U / 2), oy = Math.round(y) - (rows.length - 1) * U; rows.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== '.') cell(ox + i * U, oy + j * U, ch === '#' ? ink : light); })); };
      if (data.route?.length) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        for (const [c, lw] of drawn ? [['#0b2a66', 9 * d], ['#4aa3ff', 5.5 * d]] : [['#0b2a66', 5 * u], ['#4aa3ff', 3 * u]]) { ctx.strokeStyle = c; ctx.lineWidth = lw; ctx.beginPath(); data.route.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, X(p.x), Z(p.z))); ctx.stroke(); }
      }
      for (const it of data.traffic?.incidents ?? []) { if (it.x < x0 - 20 || it.x > x1 + 20 || it.z < z0 - 20 || it.z > z1 + 20) continue; { const ic = incidentIcon(it.kind, Math.max(1, Math.round(0.75 * u))); ctx.drawImage(ic, Math.round(X(it.x) - ic.width / 2), Math.round(Z(it.z) - ic.height / 2)); } }
      // incidents on the route being previewed: larger, on a white plate, over everything else on the map
      for (const it of data.signs ?? []) {
        const ic = incidentIcon(it.kind, Math.max(1, Math.round(0.9 * u))), x = Math.round(X(it.x) - ic.width / 2), y = Math.round(Z(it.z) - ic.height / 2), m = Math.round(2 * d);
        ctx.fillStyle = '#0b2a66'; ctx.fillRect(x - 2 * m, y - 2 * m, ic.width + 4 * m, ic.height + 4 * m); ctx.fillStyle = '#fff'; ctx.fillRect(x - m, y - m, ic.width + 2 * m, ic.height + 2 * m);
        ctx.drawImage(ic, x, y);
      }
      for (const s of data.stops ?? []) sprite(FLAG, X(s.x) + 3 * U, Z(s.z), '#3b2a12', '#ffd84a');
      if (data.dest) sprite(FLAG, X(data.dest.x) + 3 * U, Z(data.dest.z), '#14361a', '#5fd35b');
      if (data.start) sprite(DOT, X(data.start.x), Z(data.start.z) + 3 * U, '#0b2a66', '#ffffff');
      if (data.me) {   // where the phone says you are: a blue dot with a pale ring
        const x = X(data.me.x), y = Z(data.me.z), r = data.me.acc / cmpp;
        if (r > 6 * U && r < 400 * U) { ctx.fillStyle = 'rgba(74,163,255,.15)'; ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill(); }
        sprite(DOT, x, y + 3 * U, '#ffffff', '#2f6fe0');
      }
      if (data.pin) sprite(PIN, X(data.pin.x), Z(data.pin.z), '#8e1b1b', '#ff6b5e');
      if (data.car) {
        const x = X(data.car.x), y = Z(data.car.z), a = data.heading ?? 0, f = [-Math.sin(a), -Math.cos(a)], r = [-f[1], f[0]];
        for (let i = -4; i <= 4; i++) for (let j = -4; j <= 4; j++) {
          const u = i * f[0] + j * f[1], s = i * r[0] + j * r[1], half = (3.4 - u) * 0.6;
          if (u >= -2.6 && u <= 3.4 && Math.abs(s) <= half) cell(Math.round(x + (i - 0.5) * U), Math.round(y + (j - 0.5) * U), Math.abs(s) > half - 1 || u < -1.6 ? '#000' : '#ffd84a');
        }
      }
    },
  };
  return painter;
}

export function createMapView({ stage, painter, labels, onTap }) {
  // one canvas pixel per screen pixel, so the drawn map and its streets are sharp; the symbols keep their
  // chunky pixel-art look at U canvas pixels per sprite pixel (3 screen pixels, as the map's pixels used to be)
  const DPR = Math.min(3, window.devicePixelRatio || 1), PX = 1 / DPR, U = 3 / PX;
  const cv = document.createElement('canvas'); cv.id = 'mapCanvas'; const g = cv.getContext('2d');
  const names = document.createElement('div'); names.id = 'mapNames';
  const frame = document.createElement('div'); frame.id = 'mapFrame';
  const ctl = document.createElement('div'); ctl.id = 'mapCtl';
  const st = { on: false, mpp: 4 / U, w: 1, h: 1, dirty: true };   // mpp: blocks per canvas pixel
  // the centre stays over the land, so the map never drifts off into the empty ground around it
  const center = { x: 0, z: 0, set(x, _y, z) { const B = painter.bounds; this.x = Math.max(B.minX, Math.min(B.maxX, x)); this.z = Math.max(B.minZ, Math.min(B.maxZ, z)); st.dirty = true; } };
  const mk = (txt, label, fn) => { const b = document.createElement('button'); b.className = 'mb'; b.textContent = txt; b.title = label; b.setAttribute('aria-label', label); b.onclick = e => { e.stopPropagation(); fn(); }; ctl.append(b); return b; };
  mk('+', 'Zoom in', () => zoomAt(1 / 1.6)); mk('−', 'Zoom out', () => zoomAt(1.6));
  // words, not symbols: what each button does is plain at a glance
  mk('Car', 'Center on the car', () => { if (api.car) center.set(api.car.x, 0, api.car.z); }).classList.add('word');
  mk('All', 'Show the whole map', () => fitAll()).classList.add('word');
  const keyBtn = mk('Key', 'What the symbols mean', () => toggleKey()); keyBtn.classList.add('word');
  // the map key: every symbol on the map with its name
  const key = document.createElement('div'); key.id = 'mapKey'; key.className = 'panel';
  const row = (ic, text) => { const r = document.createElement('div'); r.className = 'mk-row'; const t = document.createElement('span'); t.textContent = text; r.append(ic, t); return r; };
  const head = (text) => { const d = document.createElement('div'); d.className = 'mk-h'; d.textContent = text; return d; };
  function buildKey() {
    const close = document.createElement('button'); close.className = 'mb mk-x'; close.textContent = '×'; close.setAttribute('aria-label', 'Close the key'); close.onclick = () => toggleKey(false);
    const title = document.createElement('div'); title.className = 'mk-t'; title.append('Map key', close);
    key.replaceChildren(title,
      head('Your trip'), row(swatch('#4aa3ff', '#0b2a66', 'line'), 'Route'), row(spriteCanvas(FLAG, '#14361a', '#5fd35b'), 'Destination'), row(spriteCanvas(FLAG, '#3b2a12', '#ffd84a'), 'Stop on the way'),
      row(spriteCanvas(CAR, '#000', '#ffd84a'), 'The car'), row(spriteCanvas(DOT, '#ffffff', '#2f6fe0'), 'You (your location)'), row(spriteCanvas(DOT, '#0b2a66', '#ffffff'), 'Start of directions'), row(spriteCanvas(PIN, '#8e1b1b', '#ff6b5e'), 'Dropped pin'),
      head('Traffic'), row(swatch('#3fbf5a', null, 'line'), 'Moving well'), row(swatch('#f2c230', null, 'line'), 'Slow'), row(swatch('#e5483b', null, 'line'), 'Jammed'), row(swatch('#4a0a0a', null, 'line'), 'Closed'),
      ...INCIDENT_NAMES.map(([k, n]) => { const c = incidentIcon(k, 2); c.classList.add('pix'); return row(c, n); }),
      ...(painter.hasDrawn && painter.style === 'drawn' ? [head('The map'), row(swatch('#f7c65a', '#b9802a', 'line'), 'Freeway'), row(swatch('#ffffff', '#b8ab92', 'line'), 'Street'),
        row(swatch('#d8cab4', '#8a785e'), 'Building'), row(swatch('#cde4ac'), 'Park or grass'), row(swatch('#a0cd80'), 'Trees'), row(swatch('#96cbee', '#62a5dc'), 'Water')] : []),
      row((() => { const c = document.createElement('canvas'); c.width = c.height = 24; const g = c.getContext('2d'); g.imageSmoothingEnabled = false; g.drawImage(painter.outside, 0, 0, 24, 24); g.strokeStyle = '#5b4a33'; g.lineWidth = 2; g.strokeRect(1, 1, 22, 22); c.className = 'pix'; return c; })(), 'Not built yet'));
  }
  function toggleKey(on = !key.classList.contains('open')) { if (on) buildKey(); key.classList.toggle('open', on); keyBtn.classList.toggle('on', on); }
  stage.append(cv, names, frame, ctl, key);
  for (const e of [cv, names, frame, ctl]) e.style.display = 'none';
  const pool = Array.from({ length: 18 }, () => { const d = document.createElement('div'); d.className = 'maplabel'; names.append(d); return d; });
  const B = painter.bounds, MAXMPP = Math.max(8, Math.max(B.maxX - B.minX, B.maxZ - B.minZ) / 60) / U, MINMPP = 0.5 / U;

  function fit() { st.w = stage.clientWidth; st.h = stage.clientHeight; cv.width = Math.ceil(st.w / PX); cv.height = Math.ceil(st.h / PX); st.dirty = true; }
  // fit a box of the world into the part of the map not under panels and buttons (pad: CSS pixels covered on each side)
  function fitBox(x0, z0, x1, z1, { top = 0, left = 0, right = 0, bottom = 0 } = {}) {
    const w = (st.w - left - right - 36) / PX, hh = (st.h - top - bottom - 36) / PX;
    st.mpp = Math.max(MINMPP, Math.min(MAXMPP, Math.max((x1 - x0) / Math.max(60, w), (z1 - z0) / Math.max(60, hh))));
    center.set((x0 + x1) / 2 - (left - right) / 2 / PX * st.mpp, 0, (z0 + z1) / 2 - (top - bottom) / 2 / PX * st.mpp);
  }
  function fitAll() { st.mpp = Math.min(MAXMPP, Math.max((B.maxX - B.minX) / ((st.w - 30) / PX), (B.maxZ - B.minZ) / ((st.h - 90) / PX))); center.set((B.minX + B.maxX) / 2, 0, (B.minZ + B.maxZ) / 2); }
  // sx, sy in CSS pixels
  const toWorld = (sx, sy) => ({ x: center.x + (sx - st.w / 2) / PX * st.mpp, z: center.z + (sy - st.h / 2) / PX * st.mpp });
  const toScreen = (x, z) => [(x - center.x) / st.mpp * PX + st.w / 2, (z - center.z) / st.mpp * PX + st.h / 2];
  function zoomAt(f, sx = st.w / 2, sy = st.h / 2) {
    const before = toWorld(sx, sy); st.mpp = Math.max(MINMPP, Math.min(MAXMPP, st.mpp * f));
    const after = toWorld(sx, sy); center.set(center.x + before.x - after.x, 0, center.z + before.z - after.z);
  }

  const ptr = new Map(); let tap = null, pinch = null, lastTap = 0;
  cv.addEventListener('pointerdown', e => { try { cv.setPointerCapture(e.pointerId); } catch {} ptr.set(e.pointerId, { x: e.clientX, y: e.clientY }); tap = ptr.size === 1 ? { x: e.clientX, y: e.clientY, t: performance.now() } : null; pinch = null; });
  cv.addEventListener('pointermove', e => {
    const p = ptr.get(e.pointerId); if (!p) return;
    if (ptr.size === 1) center.set(center.x - (e.clientX - p.x) / PX * st.mpp, 0, center.z - (e.clientY - p.y) / PX * st.mpp);
    p.x = e.clientX; p.y = e.clientY;
    if (ptr.size === 2) { const [a, b] = [...ptr.values()], d = Math.hypot(a.x - b.x, a.y - b.y), r = stage.getBoundingClientRect(); if (pinch) zoomAt(pinch / d, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top); pinch = d; }
    if (tap && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > 8) tap = null;
  });
  cv.addEventListener('pointerup', e => {
    ptr.delete(e.pointerId); pinch = null;
    if (tap && performance.now() - tap.t < 450) {
      const r = stage.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top, now = performance.now();
      if (now - lastTap < 300) { zoomAt(1 / 2, sx, sy); lastTap = 0; } else { lastTap = now; const w = toWorld(sx, sy); onTap?.(w.x, w.z); }
    }
    tap = null;
  });
  cv.addEventListener('pointercancel', e => { ptr.delete(e.pointerId); tap = null; pinch = null; });
  cv.addEventListener('wheel', e => { e.preventDefault(); const r = stage.getBoundingClientRect(); zoomAt(Math.exp(e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  painter.onload = () => { st.dirty = true; mini?.markDirty(); };

  function paintNames() {
    const used = [], seen = new Set(); let n = 0;
    const m = st.mpp * U, maxCl = m > 24 ? 1 : m > 12 ? 3 : m > 6 ? 5 : 7;
    const cands = labels.filter(l => (painter.nameClass.get(l.nm) ?? 9) <= maxCl).map(l => ({ l, s: toScreen(l.pos.x, l.pos.z) }))
      .filter(({ s }) => s[0] > 30 && s[0] < st.w - (document.body.classList.contains('L-phone') ? 110 : 30) && s[1] > 60 && s[1] < st.h - 40)   // clear of the button column on a phone
      .sort((a, b) => (painter.nameClass.get(a.l.nm) - painter.nameClass.get(b.l.nm)) || Math.hypot(a.s[0] - st.w / 2, a.s[1] - st.h / 2) - Math.hypot(b.s[0] - st.w / 2, b.s[1] - st.h / 2));
    for (const { l, s } of cands) {
      if (n >= pool.length || seen.has(l.name)) continue;
      const w = l.name.length * 7 + 10, r = [s[0] - w / 2, s[1] - 9, s[0] + w / 2, s[1] + 9];
      if (used.some(q => r[0] < q[2] + 6 && r[2] > q[0] - 6 && r[1] < q[3] + 4 && r[3] > q[1] - 4)) continue;
      used.push(r); seen.add(l.name); const d = pool[n++]; d.textContent = l.name; d.style.transform = `translate(${Math.round(r[0])}px, ${Math.round(r[1])}px)`; d.style.display = '';
    }
    for (; n < pool.length; n++) pool[n].style.display = 'none';
  }

  let mini = null;
  const api = {
    center, car: null, heading: 0, pin: null, me: null, data: null, traffic: null,
    get active() { return st.on; },
    get mpp() { return st.mpp * U; },   // blocks per sprite pixel (3 screen pixels)
    enter(at) { if (st.on) return; st.on = true; for (const e of [cv, names, frame, ctl]) e.style.display = ''; document.body.classList.add('mapmode'); fit(); center.set(at.x, 0, at.z); },
    exit() { if (!st.on) return; st.on = false; for (const e of [cv, names, frame, ctl]) e.style.display = 'none'; toggleKey(false); document.body.classList.remove('mapmode'); api.pin = null; },
    // the map's look: 'drawn' or 'blocks'
    get style() { return painter.style; }, setStyle(v) { painter.style = v; st.dirty = true; mini?.markDirty(); if (key.classList.contains('open')) buildKey(); },
    resize() { if (st.on) fit(); },
    fitBox,
    markDirty() { st.dirty = true; },
    /** call every frame while open; repaints only when something changed */
    update() {
      if (!st.on || !st.dirty) return false; st.dirty = false;
      painter.paint(g, { cx: center.x, cz: center.z, mpp: st.mpp, w: cv.width, h: cv.height, u: U }, { ...api.data, traffic: api.traffic, pin: api.pin, me: api.me, car: api.car, heading: api.heading });
      paintNames(); return true;
    },
    /** a small always-on map while driving; tap opens the full map */
    minimap(el, onOpen) {
      const c = document.createElement('canvas'), x = c.getContext('2d'); el.append(c); el.onclick = onOpen;
      let dirty = true, last = 0;
      mini = {
        markDirty() { dirty = true; },
        update(now, show) {
          el.style.display = show ? 'block' : 'none'; if (!show) return;
          const w = Math.ceil(el.clientWidth * DPR), h = Math.ceil(el.clientHeight * DPR);
          if (c.width !== w || c.height !== h) { c.width = w; c.height = h; dirty = true; }
          if (!dirty && now - last < 250) return; dirty = false; last = now;   // 4 times a second is plenty
          if (api.car) painter.paint(x, { cx: api.car.x, cz: api.car.z, mpp: 5 / (2 * DPR), w, h, u: 2 * DPR }, { ...api.data, traffic: api.traffic, me: api.me, car: api.car, heading: api.heading });
        },
      };
      return mini;
    },
  };
  return api;
}
