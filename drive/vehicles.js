// Original block-style vehicles for the driving map. 1 unit = 1 m; front points to -Z; wheels touch y = 0.
// Every vehicle is described once as a list of parts (boxes, a few rotated boxes and side-profile prisms).
// buildVehicle() merges the parts into one mesh per colour (few draw calls), and vehicleIcon() paints the
// same parts as a crisp side-view pixel icon, so the 3D model and the picker icon never drift apart.
// All materials are MeshLambertMaterial (sky.js drives their emissive at night).

export const VEHICLES = {
  sedan: { label: 'Sedan' },
  suv: { label: 'SUV' },
  pickup: { label: 'Pickup' },
  wedge: { label: 'Wedge EV' },
  police: { label: 'Police' },
  taxi: { label: 'Taxi' },
  sports: { label: 'Sports' },
  icecream: { label: 'Ice cream' },
};

const C = {
  glass: 0x3a4a5a, tire: 0x1d1d1d, hub: 0x9aa0a6, dark: 0x2b2d30, trim: 0x55595e,
  head: 0xfff2c0, tail: 0xd3201c, amber: 0xf59a23, white: 0xf2f2ee,
};

// ---------- part recorder
function parts() {
  const list = [];
  const P = {
    list,
    // box: w (x) x h (y) x d (z), centred at x/z, standing on y0
    box(color, w, h, d, x, y0, z, o = {}) { list.push({ t: 'box', color, w, h, d, x, y: y0 + h / 2, z, rx: o.rx || 0, anim: o.anim, icon: o.icon }); },
    // the same box on both sides (x and -x)
    pair(color, w, h, d, x, y0, z, o) { P.box(color, w, h, d, x, y0, z, o); P.box(color, w, h, d, -x, y0, z, o); },
    // side profile polygon [[z, y], ...] (convex, any winding) extruded across x by `width`, centred at x
    prism(color, width, pts, x = 0) { list.push({ t: 'prism', color, w: width, x, pts }); },
    // four wheels with hub caps: zs = axle positions, x = wheel centre offset
    wheels(zs, { x = 1.0, w = 0.4, r = 0.4 } = {}) {
      for (const z of zs) {
        P.pair(C.tire, w, 2 * r, 2 * r, x, 0, z);
        P.pair(C.hub, 0.04, r * 0.8, r * 0.8, x + w / 2 + 0.01, r * 0.6, z);
      }
    },
    // glass greenhouse with a body-colour roof slab and pillars
    cabin(color, { w, y0, y1, z0, z1, roofInset = 0.15, roof = 0.14, pillars = [] }) {
      P.box(C.glass, w, y1 - y0, z1 - z0, 0, y0, (z0 + z1) / 2);
      P.box(color, w + 0.04, roof, z1 - z0 - 2 * roofInset, 0, y1, (z0 + z1) / 2);
      for (const z of pillars) P.box(color, w + 0.04, y1 - y0, 0.14, 0, y0, z);
    },
    lights({ w, front, rear, y = 0.8, h = 0.3, x = 0.7, tailY, tailH = 0.22 }) {
      P.pair(C.head, 0.5, h, 0.06, x, y, front - 0.03);
      P.pair(C.tail, 0.45, tailH, 0.06, x + 0.05, tailY ?? y + 0.12, rear + 0.03);
    },
    bumpers({ w, front, rear, y = 0.35, h = 0.22, color = C.dark }) {
      P.box(color, w + 0.04, h, 0.14, 0, y, front - 0.02);
      P.box(color, w + 0.04, h, 0.14, 0, y, rear + 0.02);
    },
  };
  return P;
}

// ---------- vehicle descriptions
const SPECS = {
  sedan() {
    const P = parts(), red = 0xd8322e, W = 2.2, L = 4.4;
    P.box(red, W, 0.95, L, 0, 0.4, 0);
    P.cabin(red, { w: 2.0, y0: 1.35, y1: 2.06, z0: -1.0, z1: 1.25, pillars: [0.15] });
    P.box(C.dark, 0.9, 0.22, 0.05, 0, 0.82, -L / 2 - 0.02); // grille
    P.lights({ w: W, front: -L / 2, rear: L / 2, y: 0.82, h: 0.26 });
    P.bumpers({ w: W, front: -L / 2, rear: L / 2 });
    P.wheels([-1.4, 1.4]);
    return { P, W, L };
  },

  suv() {
    const P = parts(), col = 0x2f6d58, W = 2.3, L = 4.8;
    P.box(col, W, 1.05, L, 0, 0.5, 0);
    P.cabin(col, { w: 2.12, y0: 1.55, y1: 2.28, z0: -0.85, z1: 2.3, roofInset: 0.1, pillars: [0.45, 1.45] });
    P.pair(C.dark, 0.08, 0.08, 2.9, 0.85, 2.42, 0.75); // roof rails
    for (const z of [-0.45, 0.75, 1.95]) P.box(C.dark, 1.78, 0.1, 0.1, 0, 2.5, z); // cross bars
    P.box(C.dark, 0.7, 0.7, 0.2, 0, 0.75, L / 2 + 0.1); // spare wheel
    P.box(C.hub, 0.36, 0.36, 0.04, 0, 0.92, L / 2 + 0.21);
    P.box(C.dark, 1.2, 0.3, 0.05, 0, 1.0, -L / 2 - 0.02); // grille
    P.pair(C.trim, 0.06, 0.14, 4.2, W / 2 + 0.02, 0.62, 0); // side cladding
    P.lights({ w: W, front: -L / 2, rear: L / 2, y: 1.05, h: 0.28, x: 0.78, tailY: 1.2 });
    P.bumpers({ w: W, front: -L / 2, rear: L / 2, y: 0.4, h: 0.3 });
    P.wheels([-1.55, 1.55], { x: 1.05, w: 0.45, r: 0.47 });
    return { P, W, L };
  },

  pickup() {
    const P = parts(), col = 0x2f62b0, W = 2.3, L = 5.6;
    P.box(col, W, 0.95, 3.4, 0, 0.5, -1.1); // hood + cab base, z -2.8..0.6
    P.cabin(col, { w: 2.12, y0: 1.45, y1: 2.2, z0: -0.95, z1: 0.6, roofInset: 0.08 });
    P.box(col, W, 0.55, 2.2, 0, 0.5, 1.7); // bed floor, z 0.6..2.8
    P.box(C.dark, 1.96, 0.04, 2.0, 0, 1.05, 1.75); // bed liner
    P.pair(col, 0.17, 0.55, 2.2, W / 2 - 0.085, 1.05, 1.7); // bed sides
    P.box(col, W, 0.55, 0.15, 0, 1.05, 0.68); // bed front wall
    P.box(col, W, 0.55, 0.15, 0, 1.05, L / 2 - 0.075); // tailgate
    P.pair(C.dark, 0.06, 0.06, 2.1, W / 2 - 0.085, 1.6, 1.7); // bed rail caps
    P.box(C.dark, 1.3, 0.34, 0.05, 0, 0.95, -L / 2 - 0.02); // grille
    P.lights({ w: W, front: -L / 2, rear: L / 2, y: 1.0, h: 0.3, x: 0.78, tailY: 1.1, tailH: 0.4 });
    P.bumpers({ w: W, front: -L / 2, rear: L / 2, y: 0.4, h: 0.3, color: C.hub });
    P.wheels([-1.75, 1.75], { x: 1.05, w: 0.45, r: 0.47 });
    return { P, W, L };
  },

  wedge() {
    const P = parts(), steel = 0xb9bec3, W = 2.3, L = 5.8, f = -L / 2, r = L / 2;
    // one-piece angular body: flat nose, long rising windscreen line to the peak, long falling bed line
    const peakZ = -0.35, peakY = 2.05, noseY = 1.15, tailY = 1.5;
    const front = z => noseY + (z - f) * (peakY - noseY) / (peakZ - f);
    const back = z => peakY - (z - peakZ) * (peakY - tailY) / (r - peakZ);
    P.prism(steel, W, [[f, 0.55], [f, noseY], [peakZ, peakY], [r, tailY], [r, 0.55]]);
    // glass band: windscreen, roof glass and side windows, a hair outside the steel
    const g0 = -1.8, g1 = 0.85, gy = 1.42;
    P.prism(C.glass, W + 0.02, [[g0, gy], [g0, front(g0) + 0.015], [peakZ, peakY + 0.015], [g1, back(g1) + 0.015], [g1, gy]]);
    P.prism(steel, W + 0.04, [[-0.55, gy], [-0.55, front(-0.55) + 0.02], [-0.4, front(-0.4) + 0.02], [-0.4, gy]]); // B pillar
    P.prism(steel, W + 0.03, [[0.25, back(0.25) + 0.03], [r - 0.12, back(r - 0.12) + 0.03], [r - 0.12, back(r - 0.12) - 0.02], [0.25, back(0.25) - 0.02]]); // bed cover seam
    P.box(C.white, W - 0.1, 0.07, 0.05, 0, noseY - 0.1, f - 0.02); // full-width light strip
    P.box(C.tail, W - 0.1, 0.07, 0.05, 0, tailY - 0.12, r + 0.02);
    P.box(C.head, 0.5, 0.2, 0.05, 0.7, 0.85, f - 0.02); P.box(C.head, 0.5, 0.2, 0.05, -0.7, 0.85, f - 0.02);
    P.pair(C.dark, 0.08, 0.22, 1.3, W / 2 + 0.02, 0.98, -1.85); // wheel arch cladding
    P.pair(C.dark, 0.08, 0.22, 1.3, W / 2 + 0.02, 0.98, 1.85);
    P.pair(C.dark, 0.06, 0.16, 2.3, W / 2 + 0.02, 0.56, 0); // sill
    P.box(C.dark, W + 0.04, 0.2, 0.14, 0, 0.45, f - 0.02);
    P.box(C.dark, W + 0.04, 0.2, 0.14, 0, 0.45, r + 0.02);
    P.wheels([-1.85, 1.85], { x: 1.06, w: 0.45, r: 0.48 });
    return { P, W, L };
  },

  police() {
    const P = parts(), blk = 0x1b1c1f, wht = 0xf4f4f0, W = 2.2, L = 4.6;
    P.box(blk, W, 0.95, L, 0, 0.4, 0);
    P.pair(wht, 0.04, 0.6, 2.3, W / 2 + 0.01, 0.68, 0.15); // white doors
    P.cabin(wht, { w: 2.0, y0: 1.35, y1: 2.06, z0: -1.0, z1: 1.3, pillars: [0.15] });
    P.box(C.dark, 1.5, 0.1, 0.42, 0, 2.2, 0.05); // light bar base
    P.box(0xe0242a, 0.66, 0.2, 0.36, -0.38, 2.3, 0.05, { anim: 'red' });
    P.box(0x2563eb, 0.66, 0.2, 0.36, 0.38, 2.3, 0.05, { anim: 'blue' });
    P.box(0xe0242a, 0.1, 0.2, 0.18, 0.75, 2.3, -0.04, { icon: 'only' }); // the side icon shows both colours
    P.box(C.dark, 1.5, 0.45, 0.12, 0, 0.35, -L / 2 - 0.12); // push bar
    P.box(C.dark, 0.9, 0.22, 0.05, 0, 0.82, -L / 2 - 0.02);
    P.lights({ w: W, front: -L / 2, rear: L / 2, y: 0.84, h: 0.24 });
    P.box(C.dark, W + 0.04, 0.22, 0.14, 0, 0.35, L / 2 + 0.02);
    P.wheels([-1.45, 1.45]);
    return { P, W, L };
  },

  taxi() {
    const P = parts(), yel = 0xf5c21b, W = 2.2, L = 4.5;
    P.box(yel, W, 0.95, L, 0, 0.4, 0);
    P.cabin(yel, { w: 2.0, y0: 1.35, y1: 2.06, z0: -1.0, z1: 1.3, pillars: [0.15] });
    for (let i = 0; i < 6; i++) P.pair(C.tire, 0.04, 0.13, 0.32, W / 2 + 0.01, 1.07 - (i % 2) * 0.13, -1.3 + i * 0.32 + 0.16 + 0.32); // checker band
    P.box(C.white, 0.95, 0.3, 0.4, 0, 2.2, 0.15); // roof sign
    P.box(C.tire, 0.97, 0.08, 0.42, 0, 2.2, 0.15);
    P.box(C.dark, 0.9, 0.22, 0.05, 0, 0.82, -L / 2 - 0.02);
    P.lights({ w: W, front: -L / 2, rear: L / 2, y: 0.82, h: 0.26 });
    P.bumpers({ w: W, front: -L / 2, rear: L / 2 });
    P.wheels([-1.45, 1.45]);
    return { P, W, L };
  },

  sports() {
    const P = parts(), col = 0xff6a13, W = 2.1, L = 4.4;
    P.box(col, W, 0.62, L, 0, 0.28, 0); // low wide body, top 0.9
    P.box(col, 1.9, 0.12, 1.5, 0, 0.9, -1.35); // raised hood
    P.cabin(col, { w: 1.8, y0: 0.9, y1: 1.32, z0: -0.6, z1: 1.0, roofInset: 0.22, roof: 0.1 });
    P.pair(C.white, 0.22, 0.01, 1.5, 0.2, 1.02, -1.35); // twin stripes: hood, roof, rear deck
    P.pair(C.white, 0.22, 0.01, 1.16, 0.2, 1.42, 0.2);
    P.pair(C.white, 0.22, 0.01, 1.2, 0.2, 0.9, 1.6);
    P.pair(C.dark, 0.05, 0.22, 0.7, W / 2 + 0.01, 0.48, 0.55); // side intakes
    P.pair(C.dark, 0.1, 0.32, 0.12, 0.65, 0.9, 1.95); // spoiler posts
    P.box(C.dark, 2.0, 0.08, 0.45, 0, 1.22, 2.0); // wing
    P.box(C.dark, W + 0.1, 0.08, 0.3, 0, 0.22, -L / 2 + 0.05); // splitter
    P.box(C.dark, 1.2, 0.16, 0.05, 0, 0.42, -L / 2 - 0.02);
    P.lights({ w: W, front: -L / 2, rear: L / 2, y: 0.62, h: 0.16, x: 0.68, tailY: 0.66, tailH: 0.14 });
    P.box(C.dark, W + 0.04, 0.18, 0.12, 0, 0.26, L / 2 + 0.02);
    P.wheels([-1.4, 1.4], { x: 0.95, w: 0.48, r: 0.36 });
    return { P, W, L };
  },

  icecream() {
    const P = parts(), wht = 0xf5f1e8, pink = 0xf27fae, mint = 0x7fd8c0, W = 2.3, L = 5.2;
    P.box(wht, W, 0.9, 1.7, 0, 0.5, -1.75); // hood + cab base, z -2.6..-0.9
    P.cabin(wht, { w: 2.1, y0: 1.4, y1: 2.2, z0: -1.75, z1: -0.75, roofInset: 0.05 });
    P.box(wht, W, 2.4, 3.5, 0, 0.5, 0.85); // serving box, z -0.9..2.6, top 2.9
    P.pair(pink, 0.04, 0.3, 3.5, W / 2 + 0.01, 1.0, 0.85); // stripes
    P.pair(mint, 0.04, 0.12, 3.5, W / 2 + 0.01, 1.35, 0.85);
    P.box(pink, W + 0.04, 0.3, 0.04, 0, 1.0, L / 2 + 0.01);
    P.box(C.glass, 0.04, 0.7, 1.8, W / 2 + 0.01, 1.65, 0.9); // serving window (kerb side)
    P.box(pink, 0.5, 0.08, 2.0, W / 2 + 0.25, 2.42, 0.9); // awning
    P.box(mint, 0.5, 0.06, 2.0, W / 2 + 0.25, 2.38, 0.9);
    P.box(pink, W + 0.04, 0.12, 3.54, 0, 2.9, 0.85); // roof trim
    P.box(0xd9a35b, 0.5, 0.6, 0.5, 0, 3.02, 0.9); // big cone sign
    P.box(pink, 0.84, 0.42, 0.84, 0, 3.62, 0.9); // scoop
    P.box(0xfbe8f0, 0.5, 0.22, 0.5, 0, 4.04, 0.9);
    P.box(0xd3201c, 0.16, 0.16, 0.16, 0, 4.26, 0.9); // cherry
    P.box(C.dark, 1.0, 0.25, 0.05, 0, 0.9, -L / 2 - 0.02);
    P.lights({ w: W, front: -L / 2, rear: L / 2, y: 0.9, h: 0.26, x: 0.78, tailY: 0.75 });
    P.bumpers({ w: W, front: -L / 2, rear: L / 2, y: 0.4, h: 0.28, color: C.hub });
    P.wheels([-1.75, 1.6], { x: 1.05, w: 0.45, r: 0.45 });
    return { P, W, L };
  },
};

const cache = {};
const spec = kind => cache[kind] ??= (SPECS[kind] ?? SPECS.sedan)();

// ---------- 3D build
function boxGeo(THREE, p) {
  const g = new THREE.BoxGeometry(p.w, p.h, p.d).toNonIndexed();
  const m = new THREE.Matrix4().makeRotationX(p.rx).setPosition(p.x, p.y, p.z);
  return g.applyMatrix4(m);
}
function prismGeo(THREE, p) {
  // convex side profile in (z, y), extruded along x; flat-shaded, non-indexed
  let pts = p.pts;
  let area = 0; for (let i = 0; i < pts.length; i++) { const a = pts[i], b = pts[(i + 1) % pts.length]; area += a[0] * b[1] - b[0] * a[1]; }
  if (area < 0) pts = [...pts].reverse(); // counter-clockwise in (z, y)
  const pos = [], nor = [], uv = [], x0 = p.x - p.w / 2, x1 = p.x + p.w / 2;
  const zs = pts.map(q => q[0]), ys = pts.map(q => q[1]);
  const zmin = Math.min(...zs), zr = Math.max(...zs) - zmin || 1, ymin = Math.min(...ys), yr = Math.max(...ys) - ymin || 1;
  const tri = (a, b, c, n, uvs) => { for (const v of [a, b, c]) pos.push(...v); for (let i = 0; i < 3; i++) nor.push(...n); uv.push(...uvs); };
  // caps: the +x cap faces +x. With (z, y) counter-clockwise seen from +x looking -x... (z right = -x view) -> wind per normal
  for (let i = 1; i < pts.length - 1; i++) {
    const [a, b, c] = [pts[0], pts[i], pts[i + 1]];
    const u = q => [(q[0] - zmin) / zr, (q[1] - ymin) / yr];
    // +x cap: viewed from +x, screen right is -z, so CCW in (z,y) is CW on screen: reverse
    tri([x1, a[1], a[0]], [x1, c[1], c[0]], [x1, b[1], b[0]], [1, 0, 0], [...u(a), ...u(c), ...u(b)]);
    tri([x0, a[1], a[0]], [x0, b[1], b[0]], [x0, c[1], c[0]], [-1, 0, 0], [...u(a), ...u(b), ...u(c)]);
  }
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    const dz = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dz, dy) || 1;
    const n = [0, -dz / len, dy / len]; // outward for CCW in (z, y): (dy, -dz) in (z, y)
    const A0 = [x0, a[1], a[0]], A1 = [x1, a[1], a[0]], B0 = [x0, b[1], b[0]], B1 = [x1, b[1], b[0]];
    // choose winding so the face normal matches n
    const e1 = [B0[0] - A0[0], B0[1] - A0[1], B0[2] - A0[2]], e2 = [A1[0] - A0[0], A1[1] - A0[1], A1[2] - A0[2]];
    const cx = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]];
    const ok = cx[0] * n[0] + cx[1] * n[1] + cx[2] * n[2] > 0;
    if (ok) { tri(A0, B0, A1, n, [0, 0, 1, 0, 0, 1]); tri(A1, B0, B1, n, [0, 1, 1, 0, 1, 1]); }
    else { tri(A0, A1, B0, n, [0, 0, 0, 1, 1, 0]); tri(A1, B1, B0, n, [0, 1, 1, 1, 1, 0]); }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3));
  g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  return g;
}
function merge(THREE, geos) {
  const out = new THREE.BufferGeometry();
  for (const [name, size] of [['position', 3], ['normal', 3], ['uv', 2]]) {
    const n = geos.reduce((s, g) => s + g.attributes[name].array.length, 0), arr = new Float32Array(n);
    let o = 0; for (const g of geos) { arr.set(g.attributes[name].array, o); o += g.attributes[name].array.length; }
    out.setAttribute(name, new THREE.BufferAttribute(arr, size));
  }
  for (const g of geos) g.dispose();
  out.computeBoundingSphere();
  return out;
}

export function buildVehicle(kind, THREE, tex) {
  const { P, W, L } = spec(kind);
  const group = new THREE.Group(); group.name = 'vehicle:' + kind;
  const byKey = new Map();
  for (const p of P.list) {
    if (p.icon === 'only') continue;
    const key = p.anim ?? p.color;
    if (!byKey.has(key)) byKey.set(key, { color: p.color, anim: p.anim, geos: [] });
    byKey.get(key).geos.push(p.t === 'box' ? boxGeo(THREE, p) : prismGeo(THREE, p));
  }
  const anim = {};
  for (const { color, anim: a, geos } of byKey.values()) {
    const mat = new THREE.MeshLambertMaterial({ color, map: tex || null });
    const mesh = new THREE.Mesh(merge(THREE, geos), mat);
    group.add(mesh);
    if (a) anim[a] = { mat, base: new THREE.Color(color), dim: new THREE.Color(color).multiplyScalar(0.35), on: -1 };
  }
  let top = 0; for (const p of P.list) top = Math.max(top, p.t === 'box' ? p.y + p.h / 2 : Math.max(...p.pts.map(q => q[1])));
  group.userData = { kind, width: W, length: L, height: top, front: -L / 2, headlightZ: -L / 2 - 0.05, anim: Object.keys(anim).length ? anim : null };
  return group;
}

// Police light bar: red and blue alternate, each with a quick double flash. Cheap: touches 2 materials only on change.
export function animateVehicle(group, t) {
  const a = group?.userData?.anim; if (!a) return;
  const p = (t * 1.6) % 1, f = q => (((q * 8) | 0) % 2 === 0 ? 1 : 0);
  const st = { red: p < 0.5 ? f(p) : 0, blue: p >= 0.5 ? f(p - 0.5) : 0 };
  for (const k in a) {
    const s = a[k], on = st[k] ?? 0; if (s.on === on) continue; s.on = on;
    s.mat.color.copy(on ? s.base : s.dim);
    s.mat.emissive.copy(s.base).multiplyScalar(on ? 0.9 : 0);
  }
}

// ---------- side-view pixel icon (front to the right), 24 x 16, painted with fillRect only
const hex = c => '#' + c.toString(16).padStart(6, '0');
function sidePoly(p) {
  if (p.t === 'prism') return p.pts;
  const hz = p.d / 2, hy = p.h / 2, c = Math.cos(p.rx), s = Math.sin(p.rx);
  // rotation about x: (y, z) -> (y c - z s, y s + z c)
  return [[-hz, -hy], [hz, -hy], [hz, hy], [-hz, hy]].map(([z, y]) => [p.z + y * s + z * c, p.y + y * c - z * s]);
}
function inPoly(pts, z, y) {
  let ins = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [zi, yi] = pts[i], [zj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && z < (zj - zi) * (y - yi) / (yj - yi) + zi) ins = !ins;
  }
  return ins;
}
export function vehicleIcon(kind, w = 24, h = 16) {
  const { P, L } = spec(kind);
  const cv = document.createElement('canvas'); cv.width = w; cv.height = h;
  const g = cv.getContext('2d');
  let top = 0; const polys = P.list.map(p => { const pts = sidePoly(p); for (const q of pts) top = Math.max(top, q[1]); return { p, pts, near: p.x + p.w / 2 }; });
  const s = Math.min(4, (w - 2) / L, (h - 1) / top);
  const cx = w / 2, base = h; // pixel row h is the ground
  polys.sort((a, b) => a.near - b.near); // far side first, near side (+x) painted last
  for (const { p, pts } of polys) {
    if (p.x + p.w / 2 < -0.05 && p.x < 0) continue; // fully on the hidden far side
    g.fillStyle = hex(p.color);
    let z0 = Infinity, z1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const [z, y] of pts) { z0 = Math.min(z0, z); z1 = Math.max(z1, z); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
    const px0 = Math.max(0, Math.floor(cx - z1 * s)), px1 = Math.min(w, Math.ceil(cx - z0 * s));
    const py0 = Math.max(0, Math.floor(base - y1 * s)), py1 = Math.min(h, Math.ceil(base - y0 * s));
    for (let py = py0; py < py1; py++) {
      let run = -1;
      for (let px = px0; px <= px1; px++) {
        const hit = px < px1 && inPoly(pts, (cx - (px + 0.5)) / s, (base - (py + 0.5)) / s);
        if (hit && run < 0) run = px;
        if (!hit && run >= 0) { g.fillRect(run, py, px - run, 1); run = -1; }
      }
    }
  }
  return cv;
}
