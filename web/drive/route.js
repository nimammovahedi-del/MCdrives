// The route you follow, in three looks to compare. All are thinner than the old 5 m ribbon and
// drawn in hard pixels or blocks so they sit in the block world instead of on top of it.
//   strip:  a narrow lane-centre strip with pixel chevrons that creep forward
//   paint:  a faint tint over the lane with a block-pixel arrow painted every 12 m, like a road marking
//   blocks: small floating chevrons made of cubes, every 7 m along the lane
import * as THREE from '../vendor/three.module.js';

export const ROUTE_STYLES = { strip: 'Thin strip + chevrons', paint: 'Painted lane arrows', blocks: 'Floating block arrows' };

// pixel textures: drawn cell by cell so they stay crisp (nearest filtering)
function pixTex(w, h, draw) {
  const c = document.createElement('canvas'); c.width = w; c.height = h; const g = c.getContext('2d'); draw(g, w, h);
  const t = new THREE.CanvasTexture(c); t.magFilter = THREE.NearestFilter; t.minFilter = THREE.LinearMipmapLinearFilter;
  t.wrapS = THREE.ClampToEdgeWrapping; t.wrapT = THREE.RepeatWrapping; t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4; return t;
}
// texture v runs along the route: row 0 is the far (forward) end of one tile
const STYLES = {
  strip: {
    hw: 0.9, lift: 0.3, tile: 5, scroll: 3, ghost: 0.22,
    tex: () => pixTex(7, 12, (g, w, h) => {
      g.fillStyle = 'rgba(47,140,255,0.66)'; g.fillRect(0, 0, w, h);
      g.fillStyle = 'rgba(18,64,150,0.9)'; g.fillRect(0, 0, 1, h); g.fillRect(w - 1, 0, 1, h);
      // a chevron pointing forward that fills the strip's width, two pixels thick
      g.fillStyle = 'rgba(235,248,255,1)';
      for (let i = 0; i < 4; i++) for (const t of [0, 1]) { const y = 2 + i + t; g.fillRect(3 - i, y, 1, 1); g.fillRect(3 + i, y, 1, 1); }
    }),
  },
  paint: {
    hw: 1.5, lift: 0.25, tile: 12, scroll: 0, ghost: 0.16,
    tex: () => pixTex(12, 48, (g, w, h) => {
      g.fillStyle = 'rgba(70,150,255,0.30)'; g.fillRect(0, 0, w, h);
      // a road-marking arrow, 0.25 m pixels: head then shaft
      g.fillStyle = 'rgba(240,248,255,0.95)';
      for (let i = 0; i < 5; i++) g.fillRect(5 - i, 8 + i, 2 + 2 * i, 1);
      g.fillRect(4, 13, 4, 1); g.fillRect(5, 14, 2, 12);
      g.fillStyle = 'rgba(30,80,170,0.55)';  // a one-pixel shadow so it reads on light concrete
      for (let i = 0; i < 5; i++) g.fillRect(7 + i, 9 + i, 1, 1);
      g.fillRect(7, 15, 1, 11);
    }),
  },
  blocks: {
    hw: 0.3, lift: 0.25, tile: 4, scroll: 0, ghost: 0.12, gap: 8, float: 1.1,
    // a faint thread on the road ties the floating arrows together
    tex: () => pixTex(2, 4, (g, w, h) => { g.fillStyle = 'rgba(47,140,255,0.45)'; g.fillRect(0, 0, w, h); g.clearRect(0, 3, w, 1); }),
  },
};

// a chevron of cubes pointing to -z, as one merged geometry with darker sides
function cubeGeometry(cells, s, topC, sideC) {
  const P = [], N = [], C = [], top = new THREE.Color(topC), side = new THREE.Color(sideC);
  for (const [x, y, z] of cells) {
    const g = new THREE.BoxGeometry(s, s, s).toNonIndexed(); g.translate(x * s, y * s, z * s);
    const p = g.getAttribute('position'), n = g.getAttribute('normal');
    for (let i = 0; i < p.count; i++) { P.push(p.getX(i), p.getY(i), p.getZ(i)); N.push(n.getX(i), n.getY(i), n.getZ(i)); const c = n.getY(i) > 0.5 ? top : side; C.push(c.r, c.g, c.b); }
    g.dispose();
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(P, 3)); g.setAttribute('normal', new THREE.Float32BufferAttribute(N, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(C, 3));
  return g;
}
const CHEVRON = [[0, 0, -2], [-1, 0, -1], [1, 0, -1], [0, 0, -1], [-2, 0, 0], [2, 0, 0], [-1, 0, 0], [1, 0, 0], [-2, 0, 1], [2, 0, 1]];

/** the turn marker: a block arrow pointing to -z, about 10 m long, made of 1.2 m cubes */
export function voxelArrow() {
  const cells = [];
  for (let z = 0; z < 5; z++) cells.push([0, 0, z], [-1 + 1, 0, z]);           // shaft, 2 wide (x 0..1)
  for (let r = 0; r < 4; r++) for (let x = -r; x <= r + 1; x++) cells.push([x, 0, -1 - (3 - r)]); // head, widest at its base
  const uniq = [...new Map(cells.map(c => [c.join(), c])).values()];
  const g = cubeGeometry(uniq, 1.2, 0xffd84a, 0xc58f14); g.translate(-0.6, 0, -1);
  const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ vertexColors: true, depthTest: false, transparent: true, opacity: 0.95 }));
  const grp = new THREE.Group(); grp.add(m); m.renderOrder = 4; return grp;
}

export function createRoute(scene, { style = 'strip' } = {}) {
  const strip = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 }));
  strip.frustumCulled = false; strip.renderOrder = 2; scene.add(strip);
  // faint copy drawn through everything for the next 120 m, so the route stays readable under trees and bridges
  const ghost = new THREE.Mesh(strip.geometry, new THREE.MeshBasicMaterial({ color: 0x2f8cff, transparent: true, depthTest: false, depthWrite: false, side: THREE.DoubleSide }));
  ghost.frustumCulled = false; ghost.renderOrder = 3; scene.add(ghost);
  const chevGeo = cubeGeometry(CHEVRON, 0.6, 0x9ad8ff, 0x1f6fd6);
  const chevMat = new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.95 });
  let blocks = null, tex = null, S = STYLES[style] ? style : 'strip', src = null, scroll = 0, night = false, nInst = 0, gap = 7;
  const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0), one = new THREE.Vector3(1, 1, 1), pos = new THREE.Vector3();

  function build() {
    if (!src) return;
    const { L, along, yAt } = src, st = STYLES[S];
    strip.geometry.dispose(); strip.geometry = new THREE.BufferGeometry(); ghost.geometry = strip.geometry;
    if (blocks) { scene.remove(blocks); blocks.dispose(); blocks = null; }
    tex?.dispose(); tex = null;
    if (S === 'blocks') {
      // instance i sits at L - i*gap, so hiding the ones already passed is just lowering the count
      gap = st.gap; nInst = Math.floor(L / gap);
      blocks = new THREE.InstancedMesh(chevGeo, chevMat, nInst); blocks.frustumCulled = false; blocks.renderOrder = 2;
      for (let i = 0; i < nInst; i++) {
        const d = L - (i + 0.5) * gap, a = along(d), b = along(d + 2);
        q.setFromAxisAngle(up, Math.atan2(-(b.p.x - a.p.x), -(b.p.z - a.p.z)));
        pos.set(a.p.x, yAt(d) + st.float, a.p.z); blocks.setMatrixAt(i, m4.compose(pos, q, one));
      }
      blocks.instanceMatrix.needsUpdate = true; scene.add(blocks);
    }
    tex = st.tex(); strip.material.map = tex; strip.material.needsUpdate = true;
    ghost.material.opacity = st.ghost; strip.visible = ghost.visible = true;
    const P = [], U = [], I = []; let n = 0;
    for (let d = 0; d <= L; d += 2) {
      const { p, dir } = along(d), nx = -dir.z, nz = dir.x, y = yAt(d) + st.lift;
      P.push(p.x + nx * st.hw, y, p.z + nz * st.hw, p.x - nx * st.hw, y, p.z - nz * st.hw);
      U.push(0, d / st.tile, 1, d / st.tile);
      if (n) I.push(n * 2 - 2, n * 2 - 1, n * 2, n * 2 - 1, n * 2 + 1, n * 2); n++;
    }
    strip.geometry.setAttribute('position', new THREE.Float32BufferAttribute(P, 3));
    strip.geometry.setAttribute('uv', new THREE.Float32BufferAttribute(U, 2)); strip.geometry.setIndex(I);
    applyNight();
  }
  function applyNight() {
    // unlit, so it already stands out at night; just shift it a touch lighter and cooler
    strip.material.color.set(night ? 0xd8f0ff : 0xffffff); ghost.material.color.set(night ? 0x39c6ff : 0x2f8cff);
    chevMat.color.set(night ? 0xffffff : 0xe6f3ff);
  }
  return {
    get style() { return S; },
    /** L: route length, along(d) -> {p, dir}, yAt(d) -> road height */
    build(L, along, yAt) { src = { L, along, yAt }; build(); },
    setStyle(s) { if (STYLES[s] && s !== S) { S = s; build(); } },
    setNight(v) { night = v; applyNight(); },
    clear() { src = null; strip.visible = ghost.visible = false; if (blocks) blocks.visible = false; },
    /** trims what is already driven; returns true when it changed something that needs a redraw */
    update(dt, dist) {
      if (!src) return false;
      if (blocks) blocks.count = Math.max(0, Math.min(nInst, Math.floor((src.L - dist - 4) / gap)));
      const i0 = Math.max(0, Math.floor((dist - 4) / 2)) * 6;
      strip.geometry.setDrawRange(i0, Infinity); ghost.geometry.setDrawRange(i0, 60 * 6);
      const st = STYLES[S];
      if (st.scroll && tex) { scroll = (scroll + dt * st.scroll / st.tile) % 1; tex.offset.y = -scroll; return true; }
      return false;
    },
  };
}
