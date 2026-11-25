// Driving HUD: maneuver card, bottom bar and the demo-controls drawer.
// Everything here is DOM; the 3D map never draws text that competes with the card.

// ---------- pixel arrows: shapes are tested cell by cell on a 20x20 grid (thick stroke + triangular head),
// so every arrow is crisp hard pixels; a 1-cell black outline and a darker right/bottom edge give the blocky depth.
const GRID = 20;
const SHAPES = {               // polyline in grid units, the head points along the last segment
  straight: [[10, 19], [10, 4]],
  right: [[6, 19], [6, 9], [15, 9]],
  left: [[14, 19], [14, 9], [5, 9]],
  'slight right': [[7, 19], [7, 12], [14, 5]],
  'slight left': [[13, 19], [13, 12], [6, 5]],
  uturn: [[14, 19], [14, 8], [10, 4.5], [6, 8], [6, 13]],
};
function cells(kind) {
  const on = new Uint8Array(GRID * GRID);
  const set = (x, y) => { if (x >= 0 && y >= 0 && x < GRID && y < GRID) on[y * GRID + x] = 1; };
  if (kind === 'arrive' || kind === 'stop') { // chequered flag on a pole
    for (let y = 2; y < 19; y++) { set(5, y); set(6, y); }
    for (let y = 2; y < 10; y++) for (let x = 7; x < 16; x++) set(x, y);
    return { on, chk: (x, y) => x >= 7 && y < 10 && ((Math.floor((x - 7) / 2) + Math.floor((y - 2) / 2)) % 2 === 0) };
  }
  const pts = SHAPES[kind] ?? SHAPES.straight, HW = 1.9, HL = 6.5, HB = 5.6;
  const [ax, ay] = pts[pts.length - 2], [bx, by] = pts[pts.length - 1];
  const L = Math.hypot(bx - ax, by - ay), ux = (bx - ax) / L, uy = (by - ay) / L;
  const tip = [bx + ux * 1.5, by + uy * 1.5], base = [tip[0] - ux * HL, tip[1] - uy * HL];
  for (let y = 0; y < GRID; y++) for (let x = 0; x < GRID; x++) {
    const px = x + 0.5, py = y + 0.5;
    // stem (stop short of the head)
    for (let i = 0; i < pts.length - 1; i++) {
      let [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
      if (i === pts.length - 2) { x1 = base[0] + ux * 0.5; y1 = base[1] + uy * 0.5; }
      const dx = x1 - x0, dy = y1 - y0, l2 = dx * dx + dy * dy, t = Math.max(0, Math.min(1, ((px - x0) * dx + (py - y0) * dy) / l2));
      if (Math.hypot(px - x0 - t * dx, py - y0 - t * dy) <= HW) set(x, y);
    }
    // head: triangle tip / base +- HB along the normal
    const along = (px - base[0]) * ux + (py - base[1]) * uy, across = Math.abs(-(px - base[0]) * uy + (py - base[1]) * ux);
    if (along >= 0 && along <= HL && across <= HB * (1 - along / HL) + 0.2) set(x, y);
  }
  return { on };
}
function drawArrow(kind, color = '#fff') {
  const c = document.createElement('canvas'); c.width = c.height = GRID; const g = c.getContext('2d');
  const { on: a, chk } = cells(kind), on = (x, y) => x >= 0 && y >= 0 && x < GRID && y < GRID && a[y * GRID + x];
  for (let y = 0; y < GRID; y++) for (let x = 0; x < GRID; x++) {
    if (on(x, y)) {
      const edge = !on(x + 1, y) || !on(x, y + 1);
      g.fillStyle = chk?.(x, y) ? '#222' : edge ? '#c9c9c9' : color; g.fillRect(x, y, 1, 1);
    } else if (on(x - 1, y) || on(x + 1, y) || on(x, y - 1) || on(x, y + 1)) { g.fillStyle = '#000'; g.fillRect(x, y, 1, 1); }
  }
  return c;
}
const arrowCache = new Map();
function arrowInto(el, kind) {
  if (el.dataset.kind === kind) return;
  if (!arrowCache.has(kind)) arrowCache.set(kind, drawArrow(kind));
  const src = arrowCache.get(kind), c = document.createElement('canvas'); c.width = c.height = GRID; c.getContext('2d').drawImage(src, 0, 0);
  el.replaceChildren(c); el.dataset.kind = kind;
}

// ---------- formatting
export function fmtDist(m, units) {
  if (units === 'metric') {
    if (m < 950) { const r = m < 100 ? 10 : 50; return [String(Math.max(r, Math.round(m / r) * r)), 'm']; }
    return [(m / 1000).toFixed(m < 9500 ? 1 : 0), 'km'];
  }
  const ft = m * 3.28084;
  if (ft < 1000) { const r = ft < 300 ? 25 : 50; return [String(Math.max(r, Math.round(ft / r) * r)), 'ft']; }
  const mi = m / 1609.34; return [mi < 9.95 ? mi.toFixed(1) : String(Math.round(mi)), 'mi'];
}
export const roadName = n => (n || '').replace(/^(I|SR|US|CA)\s+(\d+)/, '$1-$2').replace(/^[a-z]/, c => c.toUpperCase());
const fmtClock = d => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const VERB = { left: 'Turn left', right: 'Turn right', 'slight left': 'Keep left', 'slight right': 'Keep right', straight: 'Continue', uturn: 'U-turn', arrive: 'Arrive', stop: 'Stop at' };

// ---------- build
export function createHud({ units = 'imperial', onMenu, onEnd } = {}) {
  const stage = document.getElementById('stage') ?? document.body;
  const card = document.createElement('div'); card.id = 'card'; card.className = 'hud';
  card.innerHTML = `<div class="main panel"><div class="arrow slot"></div><div class="txt"><div class="dist t"></div><div class="name t"></div></div></div>
    <div class="then panel"><span class="t">Then</span><div class="slot"></div><span class="tn t"></span></div>`;
  const bar = document.createElement('div'); bar.id = 'bar'; bar.className = 'hud panel';
  bar.innerHTML = `<div class="speed slot grey"><b class="t"></b><i class="t"></i></div>
    <div class="limit"><i>SPEED<br>LIMIT</i><b></b></div>
    <div class="mid"><div class="eta t"></div><div class="rest t"></div><div class="xp"><span></span></div></div>
    <div class="btn menu t">Menu</div><div class="btn end t">End</div>`;
  stage.append(card, bar);
  const q = (r, s) => r.querySelector(s);
  const el = {
    arrow: q(card, '.arrow'), dist: q(card, '.dist'), name: q(card, '.name'), then: q(card, '.then'), thenArrow: q(card, '.then .slot'), thenName: q(card, '.tn'),
    spd: q(bar, '.speed b'), spdU: q(bar, '.speed i'), limit: q(bar, '.limit'), limitV: q(bar, '.limit b'),
    eta: q(bar, '.eta'), rest: q(bar, '.rest'), xp: q(bar, '.xp span'),
  };
  q(bar, '.menu').onclick = () => onMenu?.(); q(bar, '.end').onclick = () => onEnd?.();
  el.spdU.textContent = units === 'metric' ? 'km/h' : 'mph';
  let last = '';
  return {
    el: { card, bar },
    show(v) { card.style.display = v ? '' : 'none'; bar.style.display = v ? '' : 'none'; },
    /** s: { kind, name, toNext, then?: {kind,name}, speed m/s, limit m/s|null, left m, leftSec, progress 0..1, arrived, rerouting } */
    update(s) {
      const key = [s.kind, s.name, Math.round(s.toNext), s.then?.kind, s.then?.name, Math.round(s.speed * 2), Math.round(s.leftSec / 10), Math.round(s.left / 20), s.arrived, s.rerouting, s.limit].join('|');
      if (key === last) return; last = key;
      card.classList.toggle('warn', !!s.rerouting);
      if (s.rerouting) {
        arrowInto(el.arrow, 'uturn'); el.dist.textContent = 'Rerouting'; el.name.textContent = 'Finding a new route'; el.then.style.display = 'none';
      } else if (s.arrived) {
        arrowInto(el.arrow, 'arrive'); el.dist.textContent = 'Arrived'; el.name.textContent = roadName(s.name); el.then.style.display = 'none';
      } else {
        arrowInto(el.arrow, s.kind);
        const [n, u] = fmtDist(s.toNext, units); el.dist.innerHTML = `${n}<small>${u}</small>`;
        card.classList.toggle('soon', s.toNext < 60 && s.kind !== 'straight');
        el.name.textContent = roadName(s.name);
        el.name.title = `${VERB[s.kind]} ${s.name}`;
        if (s.then) { el.then.style.display = ''; arrowInto(el.thenArrow, s.then.kind); el.thenName.textContent = roadName(s.then.name); }
        else el.then.style.display = 'none';
      }
      el.spd.textContent = Math.round(s.speed * (units === 'metric' ? 3.6 : 2.23694));
      if (s.limit) { el.limit.classList.add('on'); el.limitV.textContent = Math.round(s.limit * (units === 'metric' ? 3.6 : 2.23694) / 5) * 5; }
      else el.limit.classList.remove('on');
      const arrive = new Date(Date.now() + s.leftSec * 1000);
      el.eta.textContent = s.arrived ? 'You’re here' : fmtClock(arrive);
      const [dn, du] = fmtDist(s.left, units), mins = Math.max(1, Math.round(s.leftSec / 60));
      el.rest.textContent = s.arrived ? '' : `${mins >= 60 ? Math.floor(mins / 60) + ' h ' + (mins % 60) : mins} min · ${dn} ${du}`;
      el.xp.style.width = `${Math.round(Math.min(1, s.progress) * 100)}%`;
    },
  };
}

// ---------- layout picking
export function pickLayout(param) {
  if (param && ['phone', 'phoneland', 'desktop', 'car'].includes(param)) return param;
  const w = innerWidth, h = innerHeight, touch = matchMedia('(pointer: coarse)').matches;
  if (w < 700 && h >= w) return 'phone';
  if (h < 520 && w > h && (touch || w < 1000)) return 'phoneland';
  return 'desktop';
}
