// Top-down block map: the world seen straight down (north up), drawn at no more pixels than there are
// blocks so it stays crisp and blocky, framed like a paper map. The route, the car, the destination and a
// dropped pin are painted on a chunky pixel overlay; street names are DOM text. Drag to pan, pinch or
// scroll to zoom, tap to drop a pin.
import * as THREE from '../vendor/three.module.js';

const OV = 3; // CSS px per overlay pixel
const PIN = ['..####..', '.#++++#.', '#++##++#', '#++##++#', '.#++++#.', '..#++#..', '...##...', '...##...'];

export function createMapView({ renderer, scene, stage, sky, labels, onTap }) {
  const cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 1, 2000); cam.up.set(0, 0, -1);
  const center = new THREE.Vector3(), st = { on: false, mpp: 2.2, w: 1, h: 1, pr: 1, dirty: true };
  const frame = document.createElement('div'); frame.id = 'mapFrame';
  const ov = document.createElement('canvas'); ov.id = 'mapOverlay'; const g = ov.getContext('2d');
  const names = document.createElement('div'); names.id = 'mapNames';
  const ctl = document.createElement('div'); ctl.id = 'mapCtl';
  const mk = (txt, label, fn) => { const b = document.createElement('button'); b.className = 'mb'; b.textContent = txt; b.title = label; b.setAttribute('aria-label', label); b.onclick = e => { e.stopPropagation(); fn(); }; ctl.append(b); return b; };
  mk('+', 'Zoom in', () => zoomAt(1 / 1.5)); mk('−', 'Zoom out', () => zoomAt(1.5));
  const meBtn = mk('◎', 'Center on car', () => { if (api.car) { center.set(api.car.x, 0, api.car.z); st.dirty = true; } });
  stage.append(ov, names, frame, ctl);
  for (const e of [ov, names, frame, ctl]) e.style.display = 'none';
  const pool = Array.from({ length: 16 }, () => { const d = document.createElement('div'); d.className = 'maplabel'; names.append(d); return d; });
  let saved = null;

  function fit() {
    st.w = stage.clientWidth; st.h = stage.clientHeight;
    const hw = st.w / 2 * st.mpp, hh = st.h / 2 * st.mpp;
    cam.left = -hw; cam.right = hw; cam.top = hh; cam.bottom = -hh; cam.updateProjectionMatrix();
    // one render pixel per CSS pixel at most: a CSS pixel already spans more than a block at normal zoom,
    // so this keeps the blocky map look and costs well under the 3D view (which runs at 1.5 to 2)
    const pr = Math.min(saved?.pr ?? 1, 1);
    if (Math.abs(pr - st.pr) > 0.02 || renderer.getPixelRatio() !== pr) { st.pr = pr; renderer.setPixelRatio(pr); renderer.setSize(st.w, st.h); }
    ov.width = Math.ceil(st.w / OV); ov.height = Math.ceil(st.h / OV);
    st.dirty = true;
  }
  function zoomAt(f, sx = st.w / 2, sy = st.h / 2) {
    const before = toWorld(sx, sy), m = Math.max(0.5, Math.min(4, st.mpp * f));
    if (m === st.mpp) return; st.mpp = m; fit();
    const after = toWorld(sx, sy); center.x += before.x - after.x; center.z += before.z - after.z; st.dirty = true;
  }
  const toWorld = (sx, sy) => ({ x: center.x + (sx - st.w / 2) * st.mpp, z: center.z + (sy - st.h / 2) * st.mpp });
  const toScreen = (x, z) => [(x - center.x) / st.mpp + st.w / 2, (z - center.z) / st.mpp + st.h / 2];

  // gestures
  const ptr = new Map(); let tap = null, pinch = null;
  ov.addEventListener('pointerdown', e => {
    ov.setPointerCapture(e.pointerId); ptr.set(e.pointerId, { x: e.clientX, y: e.clientY });
    tap = ptr.size === 1 ? { x: e.clientX, y: e.clientY, t: performance.now() } : null; pinch = null;
  });
  ov.addEventListener('pointermove', e => {
    const p = ptr.get(e.pointerId); if (!p) return;
    if (ptr.size === 1) { center.x -= (e.clientX - p.x) * st.mpp; center.z -= (e.clientY - p.y) * st.mpp; st.dirty = true; }
    p.x = e.clientX; p.y = e.clientY;
    if (ptr.size === 2) {
      const [a, b] = [...ptr.values()], d = Math.hypot(a.x - b.x, a.y - b.y), r = stage.getBoundingClientRect();
      if (pinch) zoomAt(pinch / d, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
      pinch = d;
    }
    if (tap && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > 8) tap = null;
  });
  const up = e => {
    ptr.delete(e.pointerId); pinch = null;
    if (tap && performance.now() - tap.t < 500) { const r = stage.getBoundingClientRect(), w = toWorld(e.clientX - r.left, e.clientY - r.top); onTap?.(w.x, w.z); }
    tap = null;
  };
  ov.addEventListener('pointerup', up); ov.addEventListener('pointercancel', e => { ptr.delete(e.pointerId); tap = null; pinch = null; });
  ov.addEventListener('wheel', e => { e.preventDefault(); const r = stage.getBoundingClientRect(); zoomAt(Math.exp(e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top); }, { passive: false });

  // overlay painting, in overlay pixels
  const px = (x, y, c, s = 1) => { g.fillStyle = c; g.fillRect(Math.round(x) - (s >> 1), Math.round(y) - (s >> 1), s, s); };
  function line(pts, c, s) { for (let i = 1; i < pts.length; i++) { const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], n = Math.max(1, Math.ceil(Math.hypot(x1 - x0, y1 - y0))); for (let k = 0; k <= n; k++) px(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n, c, s); } }
  function sprite(rows, x, y, ink, light) { const ox = Math.round(x) - (rows[0].length >> 1), oy = Math.round(y) - rows.length + 1; rows.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== '.') px(ox + i, oy + j, ch === '#' ? ink : light); })); }
  function paint() {
    g.clearRect(0, 0, ov.width, ov.height);
    const S = (x, z) => { const [a, b] = toScreen(x, z); return [a / OV, b / OV]; };
    const t = api.trip;
    if (t) {
      const pts = []; for (let d = Math.max(0, t.dist); d <= t.L; d += Math.max(2, st.mpp * 2)) { const p = t.along(d).p; pts.push(S(p.x, p.z)); }
      pts.push(S(t.end.x, t.end.z)); line(pts, '#0b2a66', 4); line(pts, '#4aa3ff', 2);
      const [ex, ey] = S(t.end.x, t.end.z); sprite(PIN, ex, ey, '#1b5e20', '#7cd96b');
    }
    if (api.pin) { const [x, y] = S(api.pin.x, api.pin.z); sprite(PIN, x, y, '#8e1b1b', '#ff6b5e'); }
    if (api.car) {
      // the car: a 7-pixel arrow turned to its heading
      const [x, y] = S(api.car.x, api.car.z), a = api.heading ?? 0, f = [-Math.sin(a), -Math.cos(a)], r = [f[1] * -1, f[0]];
      for (let i = -3; i <= 3; i++) for (let j = -3; j <= 3; j++) {
        const along = -(i * f[0] + j * f[1]) * 0, u = i * f[0] + j * f[1], v = i * r[0] + j * r[1];
        if (u >= -2.6 && u <= 3.2 && Math.abs(v) <= (3.2 - u) * 0.62) px(x + i, y + j, Math.abs(v) > (3.2 - u) * 0.62 - 1 || u < -1.6 ? '#000' : '#ffd84a');
      }
    }
    // street names: nearest the centre first, no overlaps
    const used = [], seen = new Set(); let n = 0;
    const cands = labels.map(l => ({ l, s: toScreen(l.pos.x, l.pos.z) })).filter(({ s }) => s[0] > 30 && s[0] < st.w - 30 && s[1] > 60 && s[1] < st.h - 40)
      .sort((a, b) => Math.hypot(a.s[0] - st.w / 2, a.s[1] - st.h / 2) - Math.hypot(b.s[0] - st.w / 2, b.s[1] - st.h / 2));
    for (const { l, s } of cands) {
      if (n >= pool.length || seen.has(l.name)) continue;
      const w = l.name.length * 7 + 10, r = [s[0] - w / 2, s[1] - 9, s[0] + w / 2, s[1] + 9];
      if (used.some(q => r[0] < q[2] && r[2] > q[0] && r[1] < q[3] && r[3] > q[1])) continue;
      used.push(r); seen.add(l.name); const d = pool[n++]; d.textContent = l.name; d.style.transform = `translate(${Math.round(r[0])}px, ${Math.round(r[1])}px)`; d.style.display = '';
    }
    for (; n < pool.length; n++) pool[n].style.display = 'none';
  }

  const api = {
    cam, center, trip: null, car: null, heading: 0, pin: null,
    get active() { return st.on; },
    get extent() { return Math.hypot(st.w, st.h) / 2 * st.mpp; },
    enter(at) {
      if (st.on) return; st.on = true;
      saved = { pr: renderer.getPixelRatio(), near: scene.fog.near, far: scene.fog.far, bg: null };
      center.set(at.x, 0, at.z); sky.force('day'); sky.setClouds(false);
      saved.bg = scene.background; scene.background = new THREE.Color(0xd9c99a); // unexplored parchment
      scene.fog.near = 1e6; scene.fog.far = 2e6;
      for (const e of [ov, names, frame, ctl]) e.style.display = '';
      document.body.classList.add('mapmode'); fit();
    },
    exit() {
      if (!st.on) return; st.on = false;
      scene.fog.near = saved.near; scene.fog.far = saved.far; sky.setClouds(true); sky.force(null); scene.background = sky.now ? scene.background : saved.bg;
      renderer.setPixelRatio(saved.pr); st.pr = saved.pr;
      for (const e of [ov, names, frame, ctl]) e.style.display = 'none';
      document.body.classList.remove('mapmode'); api.pin = null;
    },
    resize() { if (st.on) fit(); },
    markDirty() { st.dirty = true; },
    /** call every frame in map mode; returns true when the map needs a redraw */
    update() {
      if (!st.on) return false;
      cam.position.set(center.x, 1200, center.z); cam.lookAt(center.x, 0, center.z); cam.updateMatrixWorld();
      meBtn.classList.toggle('on', !!api.car && Math.hypot(api.car.x - center.x, api.car.z - center.z) < 5);
      const was = st.dirty; paint(); st.dirty = false; return was;
    },
  };
  return api;
}
