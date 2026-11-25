// Driving camera presets. All share one rule: motion is damped (springs, no snapping) so the map
// never jumps while the driver glances at it.
import * as THREE from '../vendor/three.module.js';

export const CAMERAS = {
  // the original demo view: fixed 40 m tilted heading-up, drops near a turn
  classic: { label: 'Classic', h: [42, 42], back: [52, 52], ahead: [30, 30], turnH: 30, turnBack: 34, peek: 0, lead: 0, carY: 0.5 },
  // speed-aware: low and close in town, higher and farther on the freeway; previews the turn
  smart: { label: 'Smart (speed + turns)', h: [34, 70], back: [40, 70], ahead: [26, 70], turnH: 62, turnBack: 40, peek: 0.6, lead: 0.3, carY: 0.68 },
  // nearly top-down 2.5D: easiest to read, least 3D
  high: { label: 'High (2.5D)', h: [110, 170], back: [24, 40], ahead: [10, 24], turnH: 95, turnBack: 20, peek: 0.3, lead: 0.2, carY: 0.62 },
  // low chase cam: most immersive, least overview
  chase: { label: 'Chase (low)', h: [9, 14], back: [18, 26], ahead: [30, 50], turnH: 22, turnBack: 22, peek: 0.25, lead: 0.4, carY: 0.72 },
};

const lerp = (a, b, t) => a + (b - a) * t;
const ease = (_, k, dt) => 1 - Math.exp(-dt * k); // blend factor for exponential smoothing
const wrap = a => Math.atan2(Math.sin(a), Math.cos(a));

export class DriveCamera {
  constructor(camera) {
    this.cam = camera; this.preset = 'smart';
    this.h = 40; this.back = 50; this.ahead = 30; this.heading = 0; this.hv = 0; this.turnW = 0;
    this.pos = new THREE.Vector3(); this.look = new THREE.Vector3(); this._t = new THREE.Vector3(); this._f = new THREE.Vector3();
    this.snap = true; this.viewport = { w: 1, h: 1, padTop: 0, padBottom: 0, padLeft: 0 };
    // look-around: the driver's own rotation, tilt and zoom on top of the automatic camera. It eases back
    // to the automatic view 8 s after the last touch, or at once on recenter().
    this.user = { yaw: 0, tilt: 0, zoom: 1, idle: 0, back: false };
  }
  userDrag(dx, dy) { const u = this.user; u.yaw -= dx * 0.006; u.tilt = Math.max(-0.55, Math.min(0.55, u.tilt + dy * 0.004)); u.idle = 0; u.back = false; }
  userTwist(a) { const u = this.user; u.yaw += a; u.idle = 0; u.back = false; }
  userZoom(f) { const u = this.user; u.zoom = Math.max(0.35, Math.min(3, u.zoom * f)); u.idle = 0; u.back = false; }
  recenter() { this.user.back = true; }
  get lookingAround() { const u = this.user; return Math.abs(u.yaw) > 0.03 || Math.abs(u.tilt) > 0.03 || Math.abs(u.zoom - 1) > 0.03; }
  setPreset(name) { if (CAMERAS[name]) { this.preset = name; } }
  /** pad* are screen fractions hidden by the HUD; the car is placed in the visible part */
  setViewport(v) { Object.assign(this.viewport, v); this.snap = true; }
  /**
   * s: { p: car position, dir: unit travel direction (Vector3), speed m/s, toNext m, nextKind,
   *      turnPoint: Vector3|null, outDir: Vector3|null (direction after the next turn) }
   */
  update(dt, s) {
    const P = CAMERAS[this.preset];
    const sp = Math.min(1, Math.max(0, (s.speed - 8) / 22));            // 0 in town, 1 at ~30 m/s
    // turn weight: time-based, so the camera starts preparing ~8 s before a turn at any speed
    const tt = s.toNext / Math.max(s.speed, 6);
    const wantTurn = s.nextKind && s.nextKind !== 'straight' && s.nextKind !== 'arrive' && s.toNext < 360 ? Math.min(1, Math.max(0, (16 - tt) / 10)) : 0;
    this.turnW += (wantTurn - this.turnW) * ease(0, 1.6, dt);
    const tw = this.turnW;
    const wantH = lerp(lerp(P.h[0], P.h[1], sp), P.turnH, tw), wantB = lerp(lerp(P.back[0], P.back[1], sp), P.turnBack, tw);
    const wantA = lerp(P.ahead[0], P.ahead[1], sp);
    const k = this.snap ? 1 : ease(0, 1.2, dt);
    this.h += (wantH - this.h) * k; this.back += (wantB - this.back) * k; this.ahead += (wantA - this.ahead) * k;
    const u = this.user;
    if ((u.idle += dt) > 8 || u.back) {
      const r = ease(0, u.back ? 4 : 1.5, dt); u.yaw = wrap(u.yaw) * (1 - r); u.tilt *= 1 - r; u.zoom += (1 - u.zoom) * r;
      if (!this.lookingAround) { u.yaw = 0; u.tilt = 0; u.zoom = 1; u.back = false; }
    }
    // free = 1 while the driver is looking around: the automatic turn preview then stays out of the way
    const free = Math.min(1, Math.abs(wrap(u.yaw)) * 3 + Math.abs(u.tilt) * 3);
    const R = Math.hypot(this.h, this.back) * u.zoom, el = Math.max(0.22, Math.min(1.45, Math.atan2(this.h, this.back) + u.tilt));
    const camH = R * Math.sin(el), camB = R * Math.cos(el);
    // heading: critically damped spring toward the travel direction, pre-rotated toward the exit road
    let target = Math.atan2(-s.dir.x, -s.dir.z);
    if (s.outDir && P.lead > 0 && tw > 0) target += wrap(Math.atan2(-s.outDir.x, -s.outDir.z) - target) * P.lead * tw;
    if (this.snap) { this.heading = target; this.hv = 0; }
    else {
      const w = 2.6, err = wrap(target - this.heading);
      this.hv += (w * w * err - 2 * w * this.hv) * dt; this.heading = wrap(this.heading + this.hv * dt);
    }
    const hd = this.heading + u.yaw, f = this._f.set(-Math.sin(hd), 0, -Math.cos(hd));
    const camPos = this._t.copy(s.p).addScaledVector(f, -camB); camPos.y = s.p.y + camH;
    if (this.snap || u.idle < 0.3) this.pos.copy(camPos); else this.pos.lerp(camPos, ease(0, 6, dt));  // follow fingers directly
    // look point: ahead of the car, pulled toward the turn so its exit road is on screen
    const ah = (this.ahead + (this.extraAhead ?? 0) * (1 - free)) * u.zoom, lx = s.p.x + f.x * ah, lz = s.p.z + f.z * ah;
    let wx = lx, wz = lz;
    if (s.turnPoint && P.peek > 0 && tw > 0) { const pk = P.peek * tw * (1 - free); wx = lerp(lx, s.turnPoint.x, pk); wz = lerp(lz, s.turnPoint.z, pk); }
    const wantLook = this._t.set(wx, s.p.y, wz);
    if (this.snap || u.idle < 0.3) this.look.copy(wantLook); else this.look.lerp(wantLook, ease(0, 5, dt));
    this.cam.position.copy(this.pos); this.cam.lookAt(this.look);
    // place the car lower on screen so more road ahead is visible (pitch the view by a screen offset)
    const { w: vw, h: vh, padTop, padBottom, padLeft } = this.viewport;
    const sc = this.screenOf(s.p);
    const wantY = (lerp(padTop, 1 - padBottom, P.carY) - sc.y) * vh, wantX = (lerp(padLeft, 1, 0.5) - sc.x) * vw;
    const kk = this.snap ? 1 : ease(0, 3, dt);
    this.offY = (this.offY ?? wantY) + (wantY - (this.offY ?? wantY)) * kk;
    this.offX = (this.offX ?? wantX) + (wantX - (this.offX ?? wantX)) * kk;
    this.cam.setViewOffset(vw, vh, -this.offX, -this.offY, vw, vh); this.cam.updateProjectionMatrix();
    this.carScreen = { x: sc.x + this.offX / vw, y: sc.y + this.offY / vh };
    // keep the junction below the card: measure where the turn point lands and push the look point toward it
    let want2 = 0;
    if (s.turnPoint && tw > 0.05) {
      const v = this._t.copy(s.turnPoint).setY(s.p.y).project(this.cam), ty = (1 - v.y) / 2;
      const top = padTop + 0.08;
      want2 = v.z < 1 ? Math.max(0, Math.min(220, ((this.extraAhead ?? 0) + (top - ty) * 500))) : (this.extraAhead ?? 0) + 40;
      want2 = Math.min(220, Math.max(0, want2)) * tw;
    }
    this.extraAhead = (this.extraAhead ?? 0) + (want2 - (this.extraAhead ?? 0)) * (this.snap ? 1 : ease(0, 1.5, dt));
    this.snap = false;
  }
  screenOf(p) {
    // screen position (0..1, y down) of a world point with no view offset applied
    this.cam.clearViewOffset(); this.cam.updateMatrixWorld(); this.cam.updateProjectionMatrix();
    const v = this._t.copy(p).project(this.cam); return { x: (v.x + 1) / 2, y: (1 - v.y) / 2 };
  }
}
