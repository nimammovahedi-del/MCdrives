// Live 3D garage on How It Works: the real car models from the app, drag to turn, pick one to drive.
import * as THREE from '../vendor/three.module.js';
import { VEHICLES, buildVehicle, animateVehicle, vehicleIcon } from '../drive/vehicles.js';

const root = document.querySelector('[data-garage]');
if (root) {
  const box = root.querySelector('.garage'), cv = box.querySelector('canvas'), list = root.querySelector('.cars');
  const nameEl = root.querySelector('.nm b'), noteEl = root.querySelector('.nm span'), go = root.querySelector('[data-go]');
  const NOTES = { sedan: 'The everyday pick.', suv: 'Roof rack included.', pickup: 'Room in the back.', wedge: 'Sharp, flat and electric.',
    police: 'Lights on. Siren off.', taxi: 'Irvine Spectrum, please.', sports: 'Stripes make it faster.', icecream: 'Because why not.' };

  const renderer = new THREE.WebGLRenderer({ canvas: cv, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, devicePixelRatio));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(30, 1, 0.1, 100);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a877f, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.5); sun.position.set(4, 8, 5); scene.add(sun);

  // grain texture, same recipe as the map
  const g = document.createElement('canvas'); g.width = g.height = 16; const x = g.getContext('2d'); let s = 7;
  const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
  for (let j = 0; j < 16; j++) for (let i = 0; i < 16; i++) { let v = 0.88 + rnd() * 0.12; if (!i || !j) v *= 0.95; const k = Math.round(v * 255); x.fillStyle = `rgb(${k},${k},${k})`; x.fillRect(i, j, 1, 1); }
  const tex = new THREE.CanvasTexture(g); tex.magFilter = THREE.NearestFilter; tex.colorSpace = THREE.SRGBColorSpace; tex.wrapS = tex.wrapT = THREE.RepeatWrapping;

  // a slab of road under the car, with lane dashes
  const pad = new THREE.Group(); scene.add(pad);
  const slab = new THREE.Mesh(new THREE.BoxGeometry(9, 0.5, 9), new THREE.MeshLambertMaterial({ color: 0x55585c, map: tex })); slab.position.y = -0.25; pad.add(slab);
  const curb = new THREE.MeshLambertMaterial({ color: 0xbdb9b0, map: tex });
  for (const sx of [-1, 1]) { const c = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.62, 9), curb); c.position.set(sx * 4.75, -0.19, 0); pad.add(c); }
  const dash = new THREE.MeshLambertMaterial({ color: 0xe8b400, map: tex });
  for (const z of [-3.5, -0.5, 2.5]) { const d = new THREE.Mesh(new THREE.BoxGeometry(0.25, 0.02, 1.5), dash); d.position.set(0, 0.01, z); pad.add(d); }

  let car = null, kind = (() => { try { return localStorage.getItem('mc.car'); } catch { return null; } })();
  if (!VEHICLES[kind]) kind = 'wedge';
  let yaw = -0.6, vel = 0, dragging = false, lastX = 0, idle = 0;

  const fit = () => { const w = box.clientWidth, h = box.clientHeight; renderer.setSize(w, h, false); cam.aspect = w / h; cam.updateProjectionMatrix(); };
  const pick = k => {
    kind = k; if (car) { pad.remove(car); car.traverse(o => { o.geometry?.dispose(); o.material?.dispose(); }); }
    car = buildVehicle(k, THREE, tex); pad.add(car);
    const L = car.userData.length, H = car.userData.height, d = Math.max(L * 3.1, 13);
    cam.position.set(0, H + d * 0.36, d); cam.lookAt(0, H * 0.45, 0);
    nameEl.textContent = VEHICLES[k].label; noteEl.textContent = NOTES[k] || '';
    list.querySelectorAll('.car').forEach(b => b.setAttribute('aria-pressed', b.dataset.k === k));
    if (go) go.href = 'map?car=' + k;
    try { localStorage.setItem('mc.car', k); } catch {}
  };
  for (const k of Object.keys(VEHICLES)) {
    const b = document.createElement('button'); b.className = 'car'; b.dataset.k = k; b.title = VEHICLES[k].label; b.setAttribute('aria-label', VEHICLES[k].label);
    b.append(vehicleIcon(k, 24, 16)); b.onclick = () => pick(k); list.append(b);
  }
  box.addEventListener('pointerdown', e => { dragging = true; lastX = e.clientX; box.setPointerCapture(e.pointerId); });
  box.addEventListener('pointermove', e => { if (!dragging) return; const dx = e.clientX - lastX; lastX = e.clientX; yaw += dx * 0.012; vel = dx * 0.012; idle = 0; });
  box.addEventListener('pointerup', () => { dragging = false; });
  addEventListener('resize', fit);
  fit(); pick(kind);
  let t0 = performance.now();
  const loop = now => {
    const dt = Math.min(0.05, (now - t0) / 1000); t0 = now;
    if (!dragging) { idle += dt; vel *= 0.92; yaw += vel + (idle > 1.5 ? dt * 0.35 : 0); }
    pad.rotation.y = yaw; animateVehicle(car, now / 1000);
    renderer.render(scene, cam); requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}
