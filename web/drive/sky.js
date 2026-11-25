// Day and night skies in the blocky style: gradient dome, square sun or moon, square stars,
// and a flat layer of block clouds. Everything is one draw call per element and follows the camera.
import * as THREE from '../vendor/three.module.js';

const LOOKS = {
  day: { top: 0x5f97ff, horizon: 0xbfdcff, fog: 0xb3d3ff, hemiSky: 0xe8f2ff, hemiGround: 0x6a6f55, hemi: 1.7, sun: 1.5, sunColor: 0xfff4e0,
         cloud: 0xffffff, cloudOpacity: 0.86, stars: 0, glassGlow: 0x000000, ribbon: 0x2f8cff, headlights: false },
  night: { top: 0x050916, horizon: 0x1a2747, fog: 0x141f3a, hemiSky: 0x5a6fa8, hemiGround: 0x1b1d26, hemi: 0.8, sun: 0.4, sunColor: 0xa9bfff,
           cloud: 0x3a4566, cloudOpacity: 0.55, stars: 1, glassGlow: 0x6b4a12, ribbon: 0x39c6ff, headlights: true },
};

/** Sun altitude in degrees for a time and place (simple NOAA-style approximation, good to ~1 degree). */
export function sunAltitude(date, lat = 33.65, lon = -117.74) {
  const rad = Math.PI / 180, d = (date.getTime() / 86400000) + 2440587.5 - 2451545.0;
  const g = (357.529 + 0.98560028 * d) * rad, q = 280.459 + 0.98564736 * d;
  const L = (q + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * rad, e = (23.439 - 0.00000036 * d) * rad;
  const dec = Math.asin(Math.sin(e) * Math.sin(L)), ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L));
  const gmst = (18.697374558 + 24.06570982441908 * d) % 24, lst = (gmst * 15 + lon) * rad, ha = lst - ra;
  return Math.asin(Math.sin(lat * rad) * Math.sin(dec) + Math.cos(lat * rad) * Math.cos(dec) * Math.cos(ha)) / rad;
}

function pixelTexture(size, paint) {
  const c = document.createElement('canvas'); c.width = c.height = size; paint(c.getContext('2d'), size);
  const t = new THREE.CanvasTexture(c); t.magFilter = THREE.NearestFilter; t.minFilter = THREE.NearestFilter; t.colorSpace = THREE.SRGBColorSpace; return t;
}

export function createSky(scene, { hemi, sun, clearMat, ribbon, car, animateClouds = true } = {}) {
  const group = new THREE.Group(); group.renderOrder = -10; scene.add(group);
  // gradient dome (drawn first, no fog, no depth write)
  const domeMat = new THREE.ShaderMaterial({
    uniforms: { top: { value: new THREE.Color() }, horizon: { value: new THREE.Color() } },
    vertexShader: 'varying float h; void main(){ h = normalize(position).y; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
    fragmentShader: 'uniform vec3 top; uniform vec3 horizon; varying float h; void main(){ float t = clamp(h * 1.6, 0.0, 1.0); t = floor(t * 12.0) / 12.0; gl_FragColor = vec4(mix(horizon, top, t), 1.0); }',
    side: THREE.BackSide, depthWrite: false, depthTest: false, fog: false,
  });
  const dome = new THREE.Mesh(new THREE.SphereGeometry(3000, 24, 12), domeMat); dome.renderOrder = -10; dome.frustumCulled = false; group.add(dome);

  // square sun and moon, far away, always facing the camera
  const sunTex = pixelTexture(16, (g, n) => { g.fillStyle = '#fff9d6'; g.fillRect(0, 0, n, n); g.fillStyle = '#ffe98a'; g.fillRect(2, 2, n - 4, n - 4); g.fillStyle = '#fff6c2'; g.fillRect(4, 4, n - 8, n - 8); });
  const moonTex = pixelTexture(16, (g, n) => {
    g.fillStyle = '#d9dde8'; g.fillRect(0, 0, n, n); g.fillStyle = '#aab0c2';
    for (const [x, y, s] of [[3, 3, 3], [9, 5, 2], [5, 10, 3], [11, 11, 2], [12, 2, 1]]) g.fillRect(x, y, s, s);
  });
  const disc = (tex, size) => { const m = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshBasicMaterial({ map: tex, fog: false, depthWrite: false, transparent: true })); m.renderOrder = -9; m.frustumCulled = false; group.add(m); return m; };
  const sunDisc = disc(sunTex, 260), moonDisc = disc(moonTex, 170);

  // square stars
  const starGeo = new THREE.BufferGeometry(), sp = [];
  let s = 11; const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
  for (let i = 0; i < 900; i++) { const u = rnd() * 2 * Math.PI, v = 0.08 + rnd() * 0.92, r = 2600, c = Math.sqrt(1 - v * v); sp.push(Math.cos(u) * c * r, v * r, Math.sin(u) * c * r); }
  starGeo.setAttribute('position', new THREE.Float32BufferAttribute(sp, 3));
  const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0xffffff, size: 2.5, sizeAttenuation: false, fog: false, depthWrite: false, transparent: true }));
  stars.renderOrder = -9; stars.frustumCulled = false; group.add(stars);

  // block clouds: a 96x96 grid of 12 m cells; a smooth noise mask picks which cells are cloud.
  const CELL = 12, NC = 96, cloudGeo = new THREE.BufferGeometry();
  {
    const P = [], I = []; let n = 0;
    const val = (x, y) => { const h = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return h - Math.floor(h); };
    const noise = (x, y) => { const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi, sm = t => t * t * (3 - 2 * t);
      const a = val(xi, yi), b = val(xi + 1, yi), c = val(xi, yi + 1), d = val(xi + 1, yi + 1);
      return a + (b - a) * sm(xf) + (c - a) * sm(yf) + (a - b - c + d) * sm(xf) * sm(yf); };
    const on = (i, j) => { const ii = (i + NC) % NC, jj = (j + NC) % NC; return noise(ii / 7, jj / 7) * 0.65 + noise(ii / 3, jj / 3) * 0.35 > 0.62; };
    const quad = (a, b, c, d) => { P.push(...a, ...b, ...c, ...d); I.push(n, n + 1, n + 2, n, n + 2, n + 3); n += 4; };
    const H = 5;
    for (let j = 0; j < NC; j++) for (let i = 0; i < NC; i++) {
      if (!on(i, j)) continue;
      const x0 = i * CELL, x1 = x0 + CELL, z0 = j * CELL, z1 = z0 + CELL;
      quad([x0, H, z0], [x0, H, z1], [x1, H, z1], [x1, H, z0]); quad([x0, 0, z0], [x1, 0, z0], [x1, 0, z1], [x0, 0, z1]);
      if (!on(i - 1, j)) quad([x0, 0, z0], [x0, 0, z1], [x0, H, z1], [x0, H, z0]);
      if (!on(i + 1, j)) quad([x1, 0, z1], [x1, 0, z0], [x1, H, z0], [x1, H, z1]);
      if (!on(i, j - 1)) quad([x1, 0, z0], [x0, 0, z0], [x0, H, z0], [x1, H, z0]);
      if (!on(i, j + 1)) quad([x0, 0, z1], [x1, 0, z1], [x1, H, z1], [x0, H, z1]);
    }
    cloudGeo.setAttribute('position', new THREE.Float32BufferAttribute(P, 3)); cloudGeo.setIndex(I); cloudGeo.computeVertexNormals();
  }
  const cloudMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.85, fog: false, depthWrite: false, side: THREE.DoubleSide });
  const SPAN = CELL * NC, clouds = new THREE.Group();
  for (const [dx, dz] of [[-1, -1], [0, -1], [-1, 0], [0, 0]]) { const m = new THREE.Mesh(cloudGeo, cloudMat); m.position.set(dx * SPAN, 0, dz * SPAN); m.frustumCulled = false; clouds.add(m); }
  clouds.renderOrder = 2; scene.add(clouds);
  let drift = 0;

  // headlights: two small glowing blocks and a soft pool of light on the road (no real light source = cheap)
  const lights = new THREE.Group();
  if (car) {
    const glow = new THREE.MeshBasicMaterial({ color: 0xfff3b0 });
    for (const x of [-0.7, 0.7]) { const b = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.35, 0.1), glow); b.position.set(x, 0.95, -2.25); lights.add(b); }
    const poolTex = pixelTexture(32, (g, n) => { for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) { const dx = (x - n / 2) / (n / 2), dy = y / n; const a = Math.max(0, 1 - Math.hypot(dx * (1.3 - dy * 0.6), (dy - 0.25) * 1.2)) * 0.55; g.fillStyle = `rgba(255,236,160,${a.toFixed(3)})`; g.fillRect(x, y, 1, 1); } });
    const pool = new THREE.Mesh(new THREE.PlaneGeometry(9, 16), new THREE.MeshBasicMaterial({ map: poolTex, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    pool.rotation.x = -Math.PI / 2; pool.position.set(0, 0.45, -10.5); lights.add(pool);
    car.add(lights);
  }

  const look = { mode: 'auto', now: 'day' };
  const ca = new THREE.Vector3(), cb = new THREE.Vector3();
  function apply(name) {
    const L = LOOKS[name]; look.now = name;
    domeMat.uniforms.top.value.set(L.top); domeMat.uniforms.horizon.value.set(L.horizon);
    if (scene.fog) scene.fog.color.set(L.fog); scene.background = new THREE.Color(L.horizon);
    if (hemi) { hemi.color.set(L.hemiSky); hemi.groundColor.set(L.hemiGround); hemi.intensity = L.hemi; }
    if (sun) { sun.intensity = L.sun; sun.color.set(L.sunColor); }
    cloudMat.color.set(L.cloud); cloudMat.opacity = L.cloudOpacity;
    stars.visible = L.stars > 0; sunDisc.visible = name === 'day'; moonDisc.visible = name === 'night';
    if (clearMat) { clearMat.emissive?.set(L.glassGlow); }
    if (ribbon?.setNight) ribbon.setNight(name === 'night'); else if (ribbon) ribbon.material.color.set(L.ribbon);
    lights.visible = L.headlights;
    // the car glows a little at night so it stays the brightest thing in its neighbourhood
    car?.traverse(o => { if (o.material?.emissive && o !== car) o.material.emissive.copy(o.material.color).multiplyScalar(name === 'night' ? 0.45 : 0); });
    document.body.classList.toggle('night', name === 'night');
  }
  function resolve() { if (look.force) return look.force; return look.mode === 'auto' ? (sunAltitude(new Date()) > -3 ? 'day' : 'night') : look.mode; }
  apply(resolve());
  let check = 0;
  return {
    get mode() { return look.mode; }, get now() { return look.now; },
    setMode(m) { look.mode = m; apply(resolve()); },
    setAnimate(v) { animateClouds = v; },
    /** force a look regardless of the chosen mode (the top-down map is always drawn by day); null to release */
    force(name) { look.force = name; apply(resolve()); },
    setClouds(v) { clouds.visible = v; },
    /** call after swapping the car's model: re-applies night glow and moves the headlights to its nose */
    refreshCar() { if (car) lights.position.z = (car.userData.headlightZ ?? -2.25) + 2.25; apply(look.now); },
    get animating() { return animateClouds && look.now === 'day'; },
    update(dt, cam) {
      if ((check += dt) > 30) { check = 0; const r = resolve(); if (r !== look.now) apply(r); }
      group.position.copy(cam.position);
      // sun / moon placed toward the south-west so it is visible in most heading-up views
      const dir = ca.set(-0.55, 0.42, 0.72).normalize();
      for (const d of [sunDisc, moonDisc]) { d.position.copy(dir).multiplyScalar(2400); d.lookAt(cb.set(0, 0, 0)); }
      if (animateClouds) drift += dt * 2.2;
      const ox = Math.floor((cam.position.x - drift) / SPAN) * SPAN + drift, oz = Math.floor(cam.position.z / SPAN) * SPAN;
      // 210 m up, but always well above the camera: a high overview must not look down through the cloud deck
      clouds.position.set(ox + SPAN / 2, Math.max(210, cam.position.y + 140), oz + SPAN / 2);
    },
  };
}
