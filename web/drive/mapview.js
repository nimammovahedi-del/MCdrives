// The block map: a flat, chunky 2D map of the whole area. No 3D is drawn while it is open, so it costs
// far less than the driving view, and zooming out shows the entire city.
//   base:  a baked colour layer when the build provides one (meta.map: per-pack PNGs at 4 blocks per pixel
//          and an overview at 16), otherwise parchment
//   roads: roads.json, drawn as pixel lines, minor roads fading out as you zoom out
//   top:   the route, stops, destination, a dropped pin and the car, as pixel sprites; street names in DOM
// The same painter draws the minimap shown while driving.

const PIN = ['..####..', '.#++++#.', '#++##++#', '#++##++#', '.#++++#.', '..#++#..', '...##...', '...##...'];
const FLAG = ['#.......', '#####...', '#++++#..', '#+++++#.', '#++++#..', '#####...', '#.......', '#.......'];
const ROAD = [ // by road class: [fill, outline, width in map pixels when close]
  ['#f2b84b', '#7a4a12', 3], ['#f2c65e', '#7a4a12', 3], ['#fbf3dc', '#6b5a3a', 2], ['#fbf3dc', '#6b5a3a', 2],
  ['#f4ecd6', '#8a7a5a', 1], ['#efe6cf', '#8a7a5a', 1], ['#ebe1c8', null, 1], ['#e3d8bd', null, 1]];

import { incidentIcon } from './traffic.js';

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
  // baked colour layer (optional)
  const M = meta.map, imgs = new Map();
  const img = file => {
    let e = imgs.get(file);
    if (!e) { e = { im: new Image(), ok: false, used: 0 }; e.im.onload = () => { e.ok = true; painter.onload?.(); }; e.im.src = base + 'data/' + file; imgs.set(file, e); }
    e.used = performance.now(); return e;
  };
  function trim() { if (imgs.size < 160) return; const old = [...imgs].sort((a, b) => a[1].used - b[1].used).slice(0, imgs.size - 120); for (const [k] of old) imgs.delete(k); }

  const painter = {
    bounds: { minX, minZ, maxX, maxZ }, nameClass, onload: null,
    /** v: { cx, cz, mpp (blocks per map pixel), w, h (map pixels) }; data: { route?, stops?, dest?, pin?, car?, heading? } */
    paint(ctx, v, data) {
      const { cx, cz, mpp, w, h } = v, X = x => (x - cx) / mpp + w / 2, Z = z => (z - cz) / mpp + h / 2;
      ctx.imageSmoothingEnabled = false;
      ctx.fillStyle = M ? '#b9b49c' : '#d9c99a'; ctx.fillRect(0, 0, w, h);
      const x0 = cx - w / 2 * mpp, x1 = cx + w / 2 * mpp, z0 = cz - h / 2 * mpp, z1 = cz + h / 2 * mpp;
      if (M) {
        // image x/z are world blocks; the road graph (N) is shifted by meta.minx/minz
        const mx = meta.minx ?? 0, mz = meta.minz ?? 0;
        if (mpp >= 10 && M.overview) { const o = M.overview, e = img(o.file); if (e.ok) ctx.drawImage(e.im, X(o.x - mx), Z(o.z - mz), e.im.width * o.scale / mpp, e.im.height * o.scale / mpp); }
        else for (const [file, wx, wz] of M.tiles) {
          const size = meta.pack ?? 512, tx = wx - mx, tz = wz - mz; if (tx > x1 || tz > z1 || tx + size < x0 || tz + size < z0) continue;
          const e = img(file); if (e.ok) ctx.drawImage(e.im, Math.round(X(tx)), Math.round(Z(tz)), Math.ceil(size / mpp), Math.ceil(size / mpp));
        }
        trim();
      } else {
        // no colour layer: parchment with a faint block grid every 64 blocks when close
        if (mpp < 4) { ctx.fillStyle = 'rgba(120,95,50,.12)'; const s = 64; for (let gx = Math.ceil(x0 / s) * s; gx < x1; gx += s) ctx.fillRect(Math.round(X(gx)), 0, 1, h); for (let gz = Math.ceil(z0 / s) * s; gz < z1; gz += s) ctx.fillRect(0, Math.round(Z(gz)), w, 1); }
      }
      // roads: which classes show depends on zoom; outlines first, then fills
      const maxCl = M ? (mpp > 24 ? 1 : mpp > 12 ? 3 : mpp > 5 ? 4 : -1) : mpp > 24 ? 1 : mpp > 12 ? 3 : mpp > 6 ? 5 : 7, seen = new Set(), list = [];
      for (let gx = Math.floor(x0 / CELL); gx <= Math.floor(x1 / CELL); gx++) for (let gz = Math.floor(z0 / CELL); gz <= Math.floor(z1 / CELL); gz++)
        for (const i of cells.get(gx * 65536 + gz) ?? []) { if (seen.has(i)) continue; seen.add(i); if (G.edges[i][3] <= maxCl) list.push(i); }
      const width = cl => M ? (cl < 2 ? 2 : 1) : Math.max(1, Math.round(ROAD[cl][2] * Math.min(1.6, 4 / mpp)));
      ctx.lineCap = 'square';
      for (const pass of [0, 1]) for (let cl = maxCl; cl >= 0; cl--) {
        const [fill, edge] = ROAD[cl]; if (pass === 0 && (!edge || M)) continue;
        ctx.strokeStyle = pass ? fill : edge; ctx.lineWidth = width(cl) + (pass ? 0 : 2); ctx.beginPath();
        for (const i of list) { const e = G.edges[i]; if (e[3] !== cl) continue; const A = N[e[0]], B = N[e[1]]; ctx.moveTo(X(A.x), Z(A.z)); ctx.lineTo(X(B.x), Z(B.z)); }
        ctx.stroke();
      }
      // live traffic: freeway speeds as green / yellow / red over the road, closed stretches dark red
      if (data.traffic?.segs.length) {
        ctx.lineCap = 'butt'; ctx.lineWidth = Math.max(2, Math.round(3 * Math.min(1.6, 4 / mpp)));
        const by = new Map(); for (const sg of data.traffic.segs) { const c = JAM(sg[4]); if (!by.has(c)) by.set(c, []); by.get(c).push(sg); }
        for (const [c, list] of by) { ctx.strokeStyle = c; ctx.beginPath(); for (const [ax, az, bx, bz] of list) { if (Math.max(ax, bx) < x0 || Math.min(ax, bx) > x1 || Math.max(az, bz) < z0 || Math.min(az, bz) > z1) continue; ctx.moveTo(X(ax), Z(az)); ctx.lineTo(X(bx), Z(bz)); } ctx.stroke(); }
      }
      const px = (x, y, c, s = 1) => { ctx.fillStyle = c; ctx.fillRect(Math.round(x) - (s >> 1), Math.round(y) - (s >> 1), s, s); };
      const sprite = (rows, x, y, ink, light) => { const ox = Math.round(x) - (rows[0].length >> 1), oy = Math.round(y) - rows.length + 1; rows.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== '.') px(ox + i, oy + j, ch === '#' ? ink : light); })); };
      if (data.route?.length) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        for (const [c, lw] of [['#0b2a66', 5], ['#4aa3ff', 3]]) { ctx.strokeStyle = c; ctx.lineWidth = lw; ctx.beginPath(); data.route.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, X(p.x), Z(p.z))); ctx.stroke(); }
      }
      for (const it of data.traffic?.incidents ?? []) { if (it.x < x0 - 20 || it.x > x1 + 20 || it.z < z0 - 20 || it.z > z1 + 20) continue; ctx.drawImage(incidentIcon(it.kind, 1), Math.round(X(it.x)) - 6, Math.round(Z(it.z)) - 6); }
      for (const s of data.stops ?? []) sprite(FLAG, X(s.x) + 3, Z(s.z), '#3b2a12', '#ffd84a');
      if (data.dest) sprite(FLAG, X(data.dest.x) + 3, Z(data.dest.z), '#14361a', '#5fd35b');
      if (data.pin) sprite(PIN, X(data.pin.x), Z(data.pin.z), '#8e1b1b', '#ff6b5e');
      if (data.car) {
        const x = X(data.car.x), y = Z(data.car.z), a = data.heading ?? 0, f = [-Math.sin(a), -Math.cos(a)], r = [-f[1], f[0]];
        for (let i = -4; i <= 4; i++) for (let j = -4; j <= 4; j++) {
          const u = i * f[0] + j * f[1], s = i * r[0] + j * r[1], half = (3.4 - u) * 0.6;
          if (u >= -2.6 && u <= 3.4 && Math.abs(s) <= half) px(x + i, y + j, Math.abs(s) > half - 1 || u < -1.6 ? '#000' : '#ffd84a');
        }
      }
    },
  };
  return painter;
}

export function createMapView({ stage, painter, labels, onTap }) {
  const PX = 3; // CSS pixels per map pixel: chunky on purpose
  const cv = document.createElement('canvas'); cv.id = 'mapCanvas'; const g = cv.getContext('2d');
  const names = document.createElement('div'); names.id = 'mapNames';
  const frame = document.createElement('div'); frame.id = 'mapFrame';
  const ctl = document.createElement('div'); ctl.id = 'mapCtl';
  const st = { on: false, mpp: 4, w: 1, h: 1, dirty: true };
  const center = { x: 0, z: 0, set(x, _y, z) { this.x = x; this.z = z; st.dirty = true; } };
  const mk = (txt, label, fn) => { const b = document.createElement('button'); b.className = 'mb'; b.textContent = txt; b.title = label; b.setAttribute('aria-label', label); b.onclick = e => { e.stopPropagation(); fn(); }; ctl.append(b); return b; };
  mk('+', 'Zoom in', () => zoomAt(1 / 1.6)); mk('−', 'Zoom out', () => zoomAt(1.6));
  mk('◎', 'Center on car', () => { if (api.car) center.set(api.car.x, 0, api.car.z); });
  mk('▣', 'Whole map', () => fitAll());
  stage.append(cv, names, frame, ctl);
  for (const e of [cv, names, frame, ctl]) e.style.display = 'none';
  const pool = Array.from({ length: 18 }, () => { const d = document.createElement('div'); d.className = 'maplabel'; names.append(d); return d; });
  const B = painter.bounds, MAXMPP = Math.max(8, Math.max(B.maxX - B.minX, B.maxZ - B.minZ) / 60);

  function fit() { st.w = stage.clientWidth; st.h = stage.clientHeight; cv.width = Math.ceil(st.w / PX); cv.height = Math.ceil(st.h / PX); st.dirty = true; }
  function fitAll() { st.mpp = Math.min(MAXMPP, Math.max((B.maxX - B.minX) / (st.w / PX - 10), (B.maxZ - B.minZ) / (st.h / PX - 30))); center.set((B.minX + B.maxX) / 2, 0, (B.minZ + B.maxZ) / 2); }
  // sx, sy in CSS pixels
  const toWorld = (sx, sy) => ({ x: center.x + (sx - st.w / 2) / PX * st.mpp, z: center.z + (sy - st.h / 2) / PX * st.mpp });
  const toScreen = (x, z) => [(x - center.x) / st.mpp * PX + st.w / 2, (z - center.z) / st.mpp * PX + st.h / 2];
  function zoomAt(f, sx = st.w / 2, sy = st.h / 2) {
    const before = toWorld(sx, sy); st.mpp = Math.max(0.5, Math.min(MAXMPP, st.mpp * f));
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
    const maxCl = st.mpp > 24 ? 1 : st.mpp > 12 ? 3 : st.mpp > 6 ? 5 : 7;
    const cands = labels.filter(l => (painter.nameClass.get(l.nm) ?? 9) <= maxCl).map(l => ({ l, s: toScreen(l.pos.x, l.pos.z) }))
      .filter(({ s }) => s[0] > 30 && s[0] < st.w - 30 && s[1] > 60 && s[1] < st.h - 40)
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
    center, car: null, heading: 0, pin: null, data: null, traffic: null,
    get active() { return st.on; },
    get mpp() { return st.mpp; },
    enter(at) { if (st.on) return; st.on = true; for (const e of [cv, names, frame, ctl]) e.style.display = ''; document.body.classList.add('mapmode'); fit(); center.set(at.x, 0, at.z); },
    exit() { if (!st.on) return; st.on = false; for (const e of [cv, names, frame, ctl]) e.style.display = 'none'; document.body.classList.remove('mapmode'); api.pin = null; },
    resize() { if (st.on) fit(); },
    markDirty() { st.dirty = true; },
    /** call every frame while open; repaints only when something changed */
    update() {
      if (!st.on || !st.dirty) return false; st.dirty = false;
      painter.paint(g, { cx: center.x, cz: center.z, mpp: st.mpp, w: cv.width, h: cv.height }, { ...api.data, traffic: api.traffic, pin: api.pin, car: api.car, heading: api.heading });
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
          const w = Math.ceil(el.clientWidth / 2), h = Math.ceil(el.clientHeight / 2);
          if (c.width !== w || c.height !== h) { c.width = w; c.height = h; dirty = true; }
          if (!dirty && now - last < 250) return; dirty = false; last = now;   // 4 times a second is plenty
          if (api.car) painter.paint(x, { cx: api.car.x, cz: api.car.z, mpp: 5, w, h }, { ...api.data, traffic: api.traffic, car: api.car, heading: api.heading });
        },
      };
      return mini;
    },
  };
  return api;
}
