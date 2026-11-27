#!/usr/bin/env python3
"""Drive every road of a built site and report what a driver would hit: steps, gaps, broken bridge decks, and road
that is not paved.

    roadcheck.py SITE [--out FILE] [--spots N] [--no-surface] [--max-seconds S] [--quiet]

SITE is a built site directory (build/<area>/site of a join, or build/<area>/<chunk>/site of a single chunk): it reads
data/meta.json (format 1 or 2, tools/metafmt.py), the height tile of every pack (h_*.bin, fetched from meta.base when
the site's world data was moved elsewhere), the packs' top faces for the surface check, and roads.json. It writes
SITE/roadcheck.json (summary, per chunk, per road class, the defects) and SITE/roadcheck-spots.json (the worst --spots
defects, default 60, as the `spots` input of .github/workflows/audit.yml: [{x, z, label, yaw, cat, deck?}]), and prints
a short summary. It never exits non-zero for what it finds.

What is measured is what the viewer (web/index.html) gives a car. The road graph's edges are strung into strokes
(chains()): through a node where two edges meet, and at a junction on along the straightest edge of the same name. Each
stroke is walked along its geometry, one sample per block, with up to CONTEXT blocks of the road beyond its ends walked
too but not reported on (so a rule that looks around sees past a junction). At each sample the
ground is the viewer's smoothY(): the median of groundY() at the sample and 2 blocks either way along x and z (columns
with no surface ignored), on the deck layer (highest surface) on edges tagged as bridge and the ground layer (lowest
surface, the top one where sky leaked under the terrain) elsewhere, read from the packs in the viewer's order (where
joined chunks overlap the later pack wins). The defects, with the thresholds named as in the code:

  step       HIGH    the ground changes by 2 or more blocks between neighbouring samples (one defect per cluster of
                     such changes within 3 samples; size: the largest rise or drop over the cluster)
  bump       MEDIUM  a 1-block rise and the fall back (or a dip and the rise back) within BUMP samples, with no other
                     step QUIET samples before or after: a hump or a sunk strip on a level road
  seam_step  MEDIUM  a 1-block step within SEAM blocks of a boundary between two built chunks that runs straight along
                     the boundary across the road (SEAM_LINE of the 13 lines across the boundary within 6 blocks step at
                     the same column; the steps of a natural slope follow the contour and cross the boundary at a point)
  lone_step  LOW     a 1-block step with no other step within LONE samples, none the same way within LONE_SLOPE, and the
                     road undoing it within 2 * LONE_SLOPE: a ledge on a level road. On real data these are mostly gentle
                     undulations that a cube world cannot draw otherwise, hence LOW
  undecked   HIGH    a step of 2 or more where the top surface runs on evenly: a bridge or culvert that the road graph
                     does not tag as one, so the viewer puts the car on what lies under it (a channel, a road)
  gap        HIGH    no surface under the car at all (smoothY finds nothing on either layer: sky, water, a hole), per
                     run of samples
  floor_gap  LOW     no ground under the car but the top surface is there: sky leaked under the terrain shell over a
                     short stretch, which the viewer's trip profile bridges (the road is drawn)
  hole       MEDIUM  no surface in the centre column on either layer while the columns around have one (size: run length)
  deck_joint HIGH    a bridge deck that meets the road beyond either end with a step of 2 or more blocks, or ends in a gap
  deck_step  HIGH    a step of 2 or more blocks on a deck away from its ends
  deck_bump  MEDIUM  a bump or lone step on a deck
  spike      LOW     a rise of 3 or more blocks that comes back down: a structure over the road read as its ground,
                     which the viewer's trip profile drops (the car drives through it at road level)
  surface    LOW     a run of SURFACE or more samples whose top block is not a road material (asphalt, lane paint,
                     road concrete; tools/mca2web.py ROAD_NAMES)

Every defect has x, z in the roads.json frame (the area frame of a join, the frame audit.yml's spots use), the road's
name and class, its chunk, and the distance to the nearest chunk boundary between two built chunks.
"""
import argparse, base64, gzip, json, math, sys, time, urllib.request
from collections import OrderedDict, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metafmt

CLS = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service"]
BUMP = 60        # samples: a rise and a fall back within this are one bump (or dip)
CONTEXT = 60     # blocks of the road beyond a stroke's ends that are walked with it, for the rules that look around
QUIET = 20       # samples around a bump with no other step
LONE = 40        # samples either side of a lone step with no other step at all
LONE_SLOPE = 120 # ... and none the same way: a slope down to 0.8 % has its next rise closer than this
SEAM = 3         # blocks from a chunk boundary within which a 1-block step counts as the seam's
SEAM_LINE = 9    # of the 13 lines across a seam within 6 blocks of where the road crosses it, how many must have the
                 # same 1-block step at the same place for it to run straight along the seam (a slope's own steps
                 # follow the contour and cross the seam at one point)
SURFACE = 12     # samples of not-road top block in a row before it is reported
DEDUPE = 10      # blocks: defects of one severity on one stroke this close together are one
SEV = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
KIND_SEV = {"step": "HIGH", "gap": "HIGH", "deck_joint": "HIGH", "deck_step": "HIGH", "undecked": "HIGH", "bump": "MEDIUM",
            "lone_step": "LOW", "seam_step": "MEDIUM", "hole": "MEDIUM", "deck_bump": "MEDIUM", "spike": "LOW",
            "surface": "LOW", "floor_gap": "LOW"}
MAX_DEFECTS = 6000   # entries kept in roadcheck.json (the summary counts all), which keeps it well under 2 MB


def _lighten(c, t): return sum(int(((c >> s) & 255) * (1 - t) + 255 * t) << s for s in (16, 8, 0))
# tools/mca2web.py ROAD_NAMES and the colours colour() gives them (gray_concrete_powder is concrete lightened by 0.12)
ROAD_RGB = {f"#{c:06x}" for c in (_lighten(0x3A3D41, 0.12), 0x565B5B, 0x15171C, 0x3A3D41, 0x848685, 0xCFD5D6,
                                   0xF0AF15, 0x7D7D73)}


class Site:
    """The viewer's view of a site's heights: groundY() over whole 512-block cells, read from the height tiles."""

    def __init__(self, root, log=print, surface=True):
        self.root = Path(root); data = self.root / "data"
        self.meta = m = metafmt.read(data)
        self.PK = m["pack"]; self.T = m["tile"]; self.ext = m.get("ext", "bin")
        self.base = m.get("base") or ""
        self.packs = m["packs"]; self.log = log
        self.W, self.D = m["width"], m["depth"]
        self.CX = math.ceil(self.W / self.PK)
        # the viewer's cells: packs whose square touches a 512-block cell, later packs first (they win an overlap)
        self.cells = defaultdict(list)
        for k in range(len(self.packs) - 1, -1, -1):
            p = self.packs[k]
            for cz in range(math.floor(p["z"] / self.PK), math.floor((p["z"] + self.PK - 1) / self.PK) + 1):
                for cx in range(math.floor(p["x"] / self.PK), math.floor((p["x"] + self.PK - 1) / self.PK) + 1):
                    if 0 <= cx < self.CX: self.cells[(cx, cz)].append(k)
        self.whole = None
        if not all("h" in p for p in self.packs):   # an old site: one whole-area height.bin
            a = np.frombuffer(gzip.decompress(self._read("height")), "<i2")
            n = self.W * self.D
            self.whole = a[:2 * n].reshape(-1, self.D, self.W) if a.size >= 2 * n else a[:n].reshape(1, self.D, self.W)
        self.htiles = OrderedDict(); self.ctiles = OrderedDict(); self.rtiles = OrderedDict()
        mats = m.get("materials", [])
        self.road_lut = np.zeros(256, bool)
        for i, mt in enumerate(mats):
            if mt.get("kind") == "solid" and mt.get("rgb", "").lower() in ROAD_RGB: self.road_lut[i] = True
        self.surface = surface and self.road_lut.any() and self.whole is None
        self.fetched = 0; self.missing = set()

    def _read(self, name):
        f = self.root / "data" / f"{name}.{self.ext}"
        if f.exists(): raw = f.read_bytes()
        elif self.base:
            url = f"{self.base}data/{name}.{self.ext}"
            raw = None
            for _ in range(3):
                try: raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "roadcheck"}), timeout=60).read(); break
                except Exception as e: err = e
            if raw is None: raise IOError(f"{url}: {err}")
            self.fetched += 1
        else: raise IOError(f"{f} missing")
        if self.ext == "json": raw = base64.b64decode(json.loads(raw)["base64"])
        return raw

    def htile(self, k):
        """Pack k's height tile, int16 [2, PK, PK] (None when it cannot be read)."""
        t = self.htiles.get(k)
        if t is not None or k in self.missing:
            if t is not None: self.htiles.move_to_end(k)
            return t
        try:
            a = np.frombuffer(gzip.decompress(self._read(self.packs[k]["h"])), "<i2")
            t = a[:2 * self.PK * self.PK].reshape(2, self.PK, self.PK)
        except Exception as e:
            self.log(f"roadcheck: height tile {self.packs[k]['h']} unreadable ({e})"); self.missing.add(k); return None
        self.htiles[k] = t
        while len(self.htiles) > 40: self.htiles.popitem(last=False)
        return t

    def rtile(self, k):
        """Pack k's road map, bool [2, PK, PK]: the column's surface on that layer has a road-material top face."""
        r = self.rtiles.get(k)
        if r is not None: self.rtiles.move_to_end(k); return r
        P, T = self.PK, self.T
        r = np.zeros((2, P, P), bool)
        h = self.htile(k); p = self.packs[k]
        if h is not None and p.get("tiles"):
            try:
                blob = self._read(p["id"])
                xs, zs, ys = [], [], []
                for tx, tz, _, off, ln in p["tiles"]:
                    raw = gzip.decompress(blob[off:off + ln])
                    n = int.from_bytes(raw[:4], "little")
                    q = np.frombuffer(raw, QT, n, 4)
                    q = q[(q["d"] == 0) & self.road_lut[q["m"]]]
                    if not len(q): continue
                    # tiles are numbered in the chunk's own frame, where the pack's corner is (x - dx, z - dz)
                    ox = tx * T - (p["x"] - p.get("dx", 0)); oz = tz * T - (p["z"] - p.get("dz", 0))
                    w, hh = q["w"].astype(np.int64), q["h"].astype(np.int64)
                    cnt = w * hh; qi = np.repeat(np.arange(len(q)), cnt)
                    j = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)   # each column of each quad
                    xs.append(ox + q["x"].astype(np.int64)[qi] + j % w[qi]); zs.append(oz + q["z"].astype(np.int64)[qi] + j // w[qi])
                    ys.append(q["y"].astype(np.int64)[qi])
                if xs:
                    x, z, y = np.concatenate(xs), np.concatenate(zs), np.concatenate(ys)
                    inb = (x >= 0) & (x < P) & (z >= 0) & (z < P)
                    x, z, y = x[inb], z[inb], y[inb]
                    for l in (0, 1):
                        hit = h[l][z, x] == y
                        r[l][z[hit], x[hit]] = True
            except Exception as e:
                self.log(f"roadcheck: pack {p['id']} unreadable for the surface check ({e})")
        self.rtiles[k] = r
        while len(self.rtiles) > 96: self.rtiles.popitem(last=False)
        return r

    def cell(self, cx, cz, deck):
        """groundY() over the cell: (y int32 [PK, PK], 0 where none; pack index, -1; layer the value came from)."""
        key = (cx, cz, deck)
        c = self.ctiles.get(key)
        if c is not None: self.ctiles.move_to_end(key); return c
        P = self.PK
        y = np.zeros((P, P), np.int32); src = np.full((P, P), -1, np.int32); lay = np.zeros((P, P), np.int8)
        if self.whole is not None:
            L = self.whole[1 if deck and len(self.whole) > 1 else 0]
            sub = L[cz * P:(cz + 1) * P, cx * P:(cx + 1) * P].astype(np.int32)
            y[:sub.shape[0], :sub.shape[1]] = sub + 1
        else:
            X0, Z0 = cx * P, cz * P
            idx = np.arange(P)
            for k in self.cells.get((cx, cz), ()):
                p = self.packs[k]
                ax, az = max(X0, p["x"]), max(Z0, p["z"]); bx, bz = min(X0 + P, p["x"] + P), min(Z0 + P, p["z"] + P)
                if bx <= ax or bz <= az: continue
                h = self.htile(k)
                cy, cxs = slice(az - Z0, bz - Z0), slice(ax - X0, bx - X0)
                free = y[cy, cxs] == 0
                if h is None:
                    # the viewer returns 0 (unknown) for a column whose tile is not there: nothing below it counts
                    y[cy, cxs] = np.where(free, -1, y[cy, cxs]); continue
                py, px = slice(az - p["z"], bz - p["z"]), slice(ax - p["x"], bx - p["x"])
                if deck: v = h[1][py, px].astype(np.int32); l = np.ones(v.shape, np.int8)
                else:
                    h0 = h[0]
                    low = h0 <= 1
                    cl = lambda a: np.clip(a, 0, P - 1)
                    leak = (low[:, cl(idx - 40)] & low[:, cl(idx + 40)] & low[cl(idx - 40), :] & low[cl(idx + 40), :] & (h0 >= 0))
                    v = np.where(leak, h[1], h0)[py, px].astype(np.int32)
                    l = leak[py, px].astype(np.int8)
                    del leak, low
                take = free & (v > 1)
                y[cy, cxs] = np.where(take, v + (p.get("dy") or 0) + 1, y[cy, cxs])
                src[cy, cxs] = np.where(take, k, src[cy, cxs]); lay[cy, cxs] = np.where(take, l, lay[cy, cxs])
            y[y < 0] = 0
        c = (y, src, lay)
        self.ctiles[key] = c
        while len(self.ctiles) > 48: self.ctiles.popitem(last=False)
        return c

    def ground(self, xs, zs, deck, want_src=False):
        """groundY(x, z, deck) at the points (built frame); int32, 0 where there is no surface."""
        ix = np.floor(xs).astype(np.int64); iz = np.floor(zs).astype(np.int64)
        out = np.zeros(ix.shape, np.int32); src = np.full(ix.shape, -1, np.int32); lay = np.zeros(ix.shape, np.int8)
        ok = (ix >= 0) & (iz >= 0) & (ix < self.W) & (iz < self.D)
        P = self.PK
        cid = np.where(ok, (iz // P) * (self.CX + 1) + ix // P, -1)
        for c in np.unique(cid[ok]):
            sel = cid == c
            cz, cx = divmod(int(c), self.CX + 1)
            y, s, l = self.cell(cx, cz, deck)
            a, b = iz[sel] - cz * P, ix[sel] - cx * P
            out[sel] = y[a, b]
            if want_src: src[sel] = s[a, b]; lay[sel] = l[a, b]
        return (out, src, lay) if want_src else out

    def smooth(self, xs, zs, deck):
        """smoothY(): median of the five columns' groundY above 2 (the viewer's upper middle for an even count), NaN
        where all five are empty; deck: bool per point."""
        vals = []
        for dx, dz in ((0, 0), (2, 0), (-2, 0), (0, 2), (0, -2)):
            g = np.where(deck, self.ground(xs + dx, zs + dz, True), self.ground(xs + dx, zs + dz, False)) \
                if deck.any() and not deck.all() else self.ground(xs + dx, zs + dz, bool(deck.all()))
            vals.append(np.where(g > 2, g, np.iinfo(np.int32).max))
        v = np.sort(np.stack(vals), axis=0)
        n = (v < np.iinfo(np.int32).max).sum(axis=0)
        med = np.take_along_axis(v, (n >> 1)[None], axis=0)[0].astype(np.float64)
        med[n == 0] = np.nan
        return med

    def road_top(self, src, lay, xs, zs):
        """Whether each sample's centre column surface (from ground(want_src=True)) is a road material."""
        out = np.zeros(src.shape, bool)
        for k in np.unique(src[src >= 0]):
            sel = src == k
            r = self.rtile(int(k)); p = self.packs[int(k)]
            lx = np.floor(xs[sel]).astype(np.int64) - p["x"]; lz = np.floor(zs[sel]).astype(np.int64) - p["z"]
            out[sel] = r[lay[sel].astype(np.int64), lz, lx]
        return out


QT = np.dtype([("x", "u1"), ("z", "u1"), ("y", "<u2"), ("w", "u1"), ("h", "u1"), ("d", "u1"), ("m", "u1")])


class Chunks:
    """The built chunks' slots in the built frame (meta.chunks), which chunk a point is on and how far the nearest
    boundary between two built chunks is."""

    def __init__(self, meta, size=None):
        # a single chunk's site: its world is the size roads.json gives (meta's width and depth are whole packs)
        w, d = size or (meta["width"], meta["depth"])
        cs = meta.get("chunks") or [{"id": "all", "dx": 0, "dz": 0, "w": w - (meta.get("minx") or 0), "d": d - (meta.get("minz") or 0)}]
        def step(vs):
            u = sorted(set(vs)); d = [b - a for a, b in zip(u, u[1:])]
            return (u[-1] - u[0]) / round((u[-1] - u[0]) / min(d)) if d else 0
        SX = step([c["dx"] for c in cs]) or cs[0].get("w") or meta["width"]
        SZ = step([c["dz"] for c in cs]) or cs[0].get("d") or meta["depth"]
        self.ids = [c["id"] for c in cs]
        self.r = np.array([(c["dx"], c["dz"], c["dx"] + c.get("w", SX), c["dz"] + c.get("d", SZ)) for c in cs], float)
        # the sides shared with another built chunk: (x0, z0, x1, z1) segments
        segs = []
        for i, (x0, z0, x1, z1) in enumerate(self.r):
            for (ax, az, bx, bz, ox, oz) in ((x0, z0, x1, z0, 0, -3), (x0, z1, x1, z1, 0, 3), (x0, z0, x0, z1, -3, 0), (x1, z0, x1, z1, 3, 0)):
                mx, mz = (ax + bx) / 2 + ox, (az + bz) / 2 + oz
                if any(j != i and r[0] <= mx <= r[2] and r[1] <= mz <= r[3] for j, r in enumerate(self.r)):
                    segs.append((ax, az, bx, bz))
        self.segs = np.array(segs, float).reshape(-1, 4)

    def owner(self, xs, zs):
        """Index of the chunk each point is on (its slot holds it; the nearest slot centre among several), -1 for none."""
        x, z = xs[:, None], zs[:, None]
        r = self.r[None]
        inside = (x >= r[..., 0]) & (x <= r[..., 2]) & (z >= r[..., 1]) & (z <= r[..., 3])
        d = (x - (r[..., 0] + r[..., 2]) / 2) ** 2 + (z - (r[..., 1] + r[..., 3]) / 2) ** 2
        d = np.where(inside, d, np.inf)
        o = d.argmin(axis=1)
        return np.where(np.isfinite(d.min(axis=1)), o, -1)

    def inside(self, xs, zs, m=0):
        """Whether each point is on built land at least m blocks from its outer edge."""
        ok = np.zeros(np.shape(xs), bool)
        for dx, dz in ((m, 0), (-m, 0), (0, m), (0, -m), (0, 0)):
            x, z = np.asarray(xs)[:, None] + dx, np.asarray(zs)[:, None] + dz
            r = self.r[None]
            hit = ((x >= r[..., 0]) & (x <= r[..., 2]) & (z >= r[..., 1]) & (z <= r[..., 3])).any(axis=1)
            ok = hit if (dx, dz) == (m, 0) else ok & hit
        return ok

    def seam_line(self, S, x, z, deck):
        """How many of the 13 lines across the nearest chunk boundary, 6 either way of (x, z), step by one block at the
        same place within 4 blocks of the boundary (built frame)."""
        if not len(self.segs): return 0
        ax, az, bx, bz = self.segs.T
        dx = np.where(x < ax, ax - x, np.where(x > bx, x - bx, 0)); dz = np.where(z < az, az - z, np.where(z > bz, z - bz, 0))
        k = int(np.argmin(dx * dx + dz * dz)); vert = ax[k] == bx[k]
        c = ax[k] if vert else az[k]          # the boundary: x = c (vertical) or z = c
        t = np.arange(-6, 7) + (z if vert else x)
        o = np.arange(-4, 5) + math.floor(c) - 0.5   # columns across, centred on the boundary
        A, B = np.meshgrid(t, o, indexing="ij")    # [line, across]
        xs, zs = (B, A) if vert else (A, B)
        g = S.ground(xs.ravel() + 0.5, zs.ravel() + 0.5, deck).reshape(A.shape).astype(np.int64)
        dd = np.diff(g, axis=1)
        valid = (g > 2).all(axis=1)
        one = valid & ((dd != 0).sum(axis=1) == 1) & (np.abs(dd).sum(axis=1) == 1)
        if not one.any(): return 0
        where = np.argmax(dd != 0, axis=1); sgn = dd[np.arange(len(dd)), where]
        best = 0
        for w in set(where[one].tolist()):
            for sn in (-1, 1): best = max(best, int((one & (where == w) & (sgn == sn)).sum()))
        return best

    def seam_dist(self, xs, zs):
        """Distance in blocks to the nearest boundary between two built chunks (inf when there is none)."""
        if not len(self.segs): return np.full(np.shape(xs), np.inf)
        x, z = np.asarray(xs, float)[:, None], np.asarray(zs, float)[:, None]
        ax, az, bx, bz = (self.segs[:, i][None] for i in range(4))
        dx = np.where(x < ax, ax - x, np.where(x > bx, x - bx, 0)); dz = np.where(z < az, az - z, np.where(z > bz, z - bz, 0))
        return np.sqrt(dx * dx + dz * dz).min(axis=1)


def chains(G, nodes):
    """The road graph's edges strung into strokes, the way a driver goes on along a road: at each node the edges are
    paired off, straightest pair first (one of the same name, and the same deck flag, preferred), as long as they turn
    by less than 60 degrees; where exactly two edges meet they always pair. Returns [(nodes, edge indices)]; every edge
    is in exactly one stroke."""
    E = G["edges"]
    inc = defaultdict(list)
    for k, e in enumerate(E):
        inc[e[0]].append(k); inc[e[1]].append(k)
    pair = {}   # (node, edge) -> the edge the stroke goes on with there
    for n, ks in inc.items():
        if len(ks) < 2: continue
        def out(k):   # unit vector from n along edge k
            e = E[k]; m = e[1] if e[0] == n else e[0]
            v = nodes[m] - nodes[n]; l = float(np.hypot(*v)); return v / l if l > 0 else v
        cand = []
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = ks[i], ks[j]
                if a == b: continue
                cos = -float(np.dot(out(a), out(b)))      # 1: straight on
                if len(ks) > 2 and cos < 0.5: continue
                same = (E[a][2] == E[b][2]) + (len(E[a]) > 6 and len(E[b]) > 6 and E[a][6] == E[b][6]) * 0.5
                cand.append((same * 0.3 + cos, a, b))
        for _, a, b in sorted(cand, reverse=True):
            if (n, a) in pair or (n, b) in pair: continue
            pair[(n, a)] = b; pair[(n, b)] = a
    used = [False] * len(E); out_ = []
    for k0, e0 in enumerate(E):
        if used[k0]: continue
        used[k0] = True
        ns, es = [e0[0], e0[1]], [k0]
        for back in (False, True):   # extend from the last node, then from the first
            while True:
                n = ns[0] if back else ns[-1]; k = pair.get((n, es[0] if back else es[-1]))
                if k is None or used[k]: break
                used[k] = True; e = E[k]
                m = e[1] if e[0] == n else e[0]
                if back: ns.insert(0, m); es.insert(0, k)
                else: ns.append(m); es.append(k)
        out_.append((ns, es))
    return out_, pair, inc


def onward(G, nodes, pair, inc, n, k, reach):
    """Nodes and edges a driver goes on along past node n, arriving there on edge k, for about reach blocks: the
    edge paired with k at n, else the straightest one that turns by less than 60 degrees."""
    E = G["edges"]; ns, es, got = [], [], 0.0
    seen = {k}
    while got < reach:
        nxt = pair.get((n, k))
        if nxt is None:
            e = E[k]; prev = e[1] if e[0] == n else e[0]
            v = nodes[n] - nodes[prev]; v = v / (np.hypot(*v) or 1)
            best = None
            for k2 in inc[n]:
                if k2 == k: continue
                e2 = E[k2]; m = e2[1] if e2[0] == n else e2[0]
                w = nodes[m] - nodes[n]; c = float(np.dot(v, w / (np.hypot(*w) or 1)))
                if c > 0.5 and (best is None or c > best[0]): best = (c, k2)
            nxt = best and best[1]
        if nxt is None or nxt in seen: break
        seen.add(nxt); e = E[nxt]; m = e[1] if e[0] == n else e[0]
        got += float(np.hypot(*(nodes[m] - nodes[n]))); ns.append(m); es.append(nxt); n, k = m, nxt
    return ns, es


def runs(mask):
    """(start, end exclusive) of every run of True."""
    if not mask.any(): return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def check(site_dir, n_spots=60, surface=True, max_seconds=None, log=print, debug=False):
    t0 = time.time()
    S = Site(site_dir, log=log, surface=surface)
    meta = S.meta
    G = json.loads((Path(site_dir) / "roads.json").read_text())
    ox, oz = meta.get("minx", 0) or 0, meta.get("minz", 0) or 0
    nodes = np.array(G["nodes"], float).reshape(-1, 2) - (ox, oz)    # the built frame
    names = G["names"]; E = G["edges"]
    C = Chunks(meta, None if meta.get("chunks") else G.get("size"))
    nch = len(C.ids)
    ch, pair, inc = chains(G, nodes)
    # walk the chains in the order of the 512-block cell they start in, so the cells and tiles in memory get reused
    ch.sort(key=lambda c: (int(nodes[c[0][0]][1] // 1024), int(nodes[c[0][0]][0] // 1024)))
    by_node = defaultdict(list)
    for k, e in enumerate(E): by_node[e[0]].append(k); by_node[e[1]].append(k)

    defects = []
    km_chunk = np.zeros(nch + 1); km_class = np.zeros(len(CLS)); samples = 0; partial = False
    surf_samples = 0; surf_road = 0
    deck_km = 0.0

    cur = {}
    def add(kind, i, X, Z, eidx, size, chain_len, extra=None):
        if not cur["mine"][i]: return   # in the road beyond the stroke: its own stroke reports it
        e = E[eidx]
        d = {"kind": kind, "sev": (extra or {}).pop("sev", None) or KIND_SEV[kind], "x": round(float(X[i]) + ox, 1), "z": round(float(Z[i]) + oz, 1),
             "size": size, "road": names[e[2]] if e[2] < len(names) else "", "cls": CLS[e[3]] if e[3] < len(CLS) else str(e[3]),
             "edge": int(eidx), "deck": bool(e[6]) if len(e) > 6 else False}
        if extra: d.update(extra)
        if debug: d["prof"] = [None if np.isnan(v) else int(v) for v in h[max(0, i - 30):i + 31]]
        defects.append(d)

    def sample_edge_from(n, k, deck, dist=(1.0, 2.0, 3.0)):
        """smoothY a few blocks into edge k from its node n (for the joints of a deck at a junction)."""
        e = E[k]; a, b = (e[0], e[1]) if e[0] == n else (e[1], e[0])
        P, Q = nodes[a], nodes[b]; L = float(np.hypot(*(Q - P)))
        if L < 1: return np.array([np.nan])
        t = np.array([min(d, L) for d in dist]) / L
        xs, zs = P[0] + (Q[0] - P[0]) * t, P[1] + (Q[1] - P[1]) * t
        return S.smooth(xs, zs, np.full(len(t), deck))

    for ci, (cn, ce) in enumerate(ch):
        if max_seconds and time.time() - t0 > max_seconds: partial = True; break
        # the stroke with up to CONTEXT blocks of the road beyond either end, which is walked but not reported on
        bn, be = onward(G, nodes, pair, inc, cn[0], ce[0], CONTEXT)
        an, ae = onward(G, nodes, pair, inc, cn[-1], ce[-1], CONTEXT)
        xn = bn[::-1] + cn + an; xe = be[::-1] + ce + ae
        pts = nodes[xn]
        seg = np.hypot(*(pts[1:] - pts[:-1]).T)
        cum = np.concatenate([[0], np.cumsum(seg)])
        Lb = float(cum[len(bn)]); L = float(cum[len(bn) + len(cn) - 1]) - Lb; Lt = float(cum[-1])
        if L < 0.5: continue
        n = max(1, int(math.ceil(L)))
        s = np.concatenate([Lb - np.arange(min(CONTEXT, int(Lb)), 0, -1), Lb + np.linspace(0, L, n + 1),
                            Lb + L + np.arange(1, min(CONTEXT, int(Lt - Lb - L)) + 1)])
        mine = (s >= Lb - 1e-6) & (s <= Lb + L + 1e-6)
        j = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1)
        t = np.where(seg[j] > 0, (s - cum[j]) / np.where(seg[j] > 0, seg[j], 1), 0)
        X = pts[j, 0] + (pts[j + 1, 0] - pts[j, 0]) * t; Z = pts[j, 1] + (pts[j + 1, 1] - pts[j, 1]) * t
        ce = xe
        eidx = np.array(ce)[j]
        dk = np.array([bool(E[k][6]) if len(E[k]) > 6 else False for k in ce])[j]
        cls = np.array([E[k][3] for k in ce])[j]
        own = C.owner(X, Z)
        land = C.inside(X, Z, 4)
        cur["mine"] = mine
        samples += int((land & mine).sum())
        ds = np.diff(s, prepend=0) * land * mine
        np.add.at(km_chunk, own, ds / 1000); np.add.at(km_class, np.clip(cls, 0, len(CLS) - 1), ds / 1000)
        deck_km += float(ds[dk].sum()) / 1000
        h = S.smooth(X, Z, dk)
        # only on built land, 3 blocks in from its outer edge (smoothY reads 2 blocks around): a chunk's own graph
        # runs past its world, and a joined one may end at the edge of the built chunks
        h[~land] = np.nan
        # the centre column (holes, and the surface check)
        cg = np.zeros(len(s), np.int32); src = np.full(len(s), -1, np.int32); lay = np.zeros(len(s), np.int8)
        for flag in (False, True):
            sel = dk == flag
            if sel.any():
                g, sr, ly = S.ground(X[sel], Z[sel], flag, want_src=True)
                cg[sel], src[sel], lay[sel] = g, sr, ly
        ext = lambda i, **kw: dict(chain=ci, **kw)

        # the top surface (the deck layer) too: where the viewer's ground is missing or far below it while the top is
        # there and even, the road is drawn and only the height data under it differs
        htop = S.smooth(X, Z, np.ones(len(s), bool))
        tg = S.ground(X, Z, True)
        druns = runs(dk)
        def deck_end(i, r=3):   # within r samples of where a deck run starts or ends
            return any((abs(i - a) <= r and a > 0) or (abs(i + 1 - b) <= r and b < len(dk)) for a, b in druns)

        # ---- gaps and holes
        for a, b in runs(np.isnan(h) & land & mine):
            m = (a + b) // 2
            if np.mean(~np.isnan(htop[a:b])) >= 0.8: kind = "floor_gap"   # sky under the terrain shell, road drawn
            elif any(deck_end(i, 1) for i in (a - 1, b - 1)): kind = "deck_joint"   # a deck that ends in a hole
            else: kind = "gap"
            add(kind, m, X, Z, eidx[m], b - a, L, ext(m, length=b - a, **({"gap": True} if kind == "deck_joint" else {})))
        for a, b in runs((cg <= 2) & (tg <= 2) & ~np.isnan(h) & land & mine):
            m = (a + b) // 2
            # one column across is mostly a kerb, median or wall on the road's centre line, which the car's five-column
            # median rides over; three or more in a row along the road is a hole in it
            add("hole", m, X, Z, eidx[m], b - a, L, ext(m, length=b - a, **({} if b - a >= 3 else {"sev": "LOW"})))

        # ---- the viewer's spike rule (a rise of 3+ within 4 blocks that drops back): drop those spans like the trip
        # profile does, reporting them once
        hv = h.copy()
        i = 2
        while i < len(hv):
            for a in (i - 1, i - 2, i - 3, i - 4):
                if a < 0 or not (hv[i] - hv[a] >= 3) or dk[a] != dk[i]: continue
                jj, lim = i + 1, min(len(hv), i + 800)
                while jj < lim and dk[jj] == dk[a] and not (hv[jj] <= hv[a] + 2): jj += 1
                if jj < lim and dk[jj] == dk[a] and hv[jj] <= hv[a] + 2 and any(k > a and hv[k] - hv[jj] >= 3 for k in range(jj - 4, jj)):
                    m = (a + jj) // 2
                    add("spike", m, X, Z, eidx[m], int(np.nanmax(hv[a + 1:jj]) - hv[a]), L, ext(m, length=jj - a - 1))
                    hv[a + 1:jj] = np.nan; i = jj
                    break
            i += 1

        # ---- steps on the profile (between valid neighbours)
        ok = ~np.isnan(hv)
        d = np.where(ok[1:] & ok[:-1], hv[1:] - np.where(ok[:-1], hv[:-1], 0), 0).astype(np.int64)
        big = np.flatnonzero(np.abs(d) >= 2)
        k = 0
        while k < len(big):
            g = [big[k]]
            while k + 1 < len(big) and big[k + 1] - g[-1] <= 3: k += 1; g.append(big[k])
            a, b = g[0], g[-1] + 1
            seg_h = hv[a:b + 1][~np.isnan(hv[a:b + 1])]
            size = int(seg_h.max() - seg_h.min()) if len(seg_h) else int(abs(d[a]))
            i = g[int(np.argmax(np.abs(d[g])))]
            tt = htop[max(0, a - 2):b + 3]
            if dk[a:b + 1].any() and deck_end(i): kind = "deck_joint"
            elif dk[a:b + 1].all(): kind = "deck_step"
            # the top surface runs on evenly over it: a bridge or culvert the road graph does not tag as one, whose
            # floor the viewer reads as the road
            elif not dk[a:b + 1].any() and np.isfinite(tt).all() and np.abs(np.diff(tt)).max() <= 1 and abs(tt[0] - tt[-1]) <= 1: kind = "undecked"
            else: kind = "step"
            add(kind, i, X, Z, eidx[i], size, L, ext(i, up=bool(d[i] > 0)))
            k += 1
        # deck ends at the chain's own ends (a bridge that starts at a junction): the roads beyond, one block in
        for a, b in druns:
            for end, node in ((a, xn[0]) if a == 0 else (None, None), (b - 1, xn[-1]) if b == len(dk) else (None, None)):
                if end is None or np.isnan(hv[end]): continue
                others = [k2 for k2 in by_node[node] if k2 not in ce and not (len(E[k2]) > 6 and E[k2][6])]
                if not others: continue
                diffs = [np.abs(sample_edge_from(node, k2, False) - hv[end]) for k2 in others]
                best = min((float(np.nanmin(v)) for v in diffs if not np.all(np.isnan(v))), default=np.inf)
                if np.isfinite(best) and best >= 2:
                    add("deck_joint", end, X, Z, eidx[end], int(best), L, ext(end, at="junction"))

        # ---- 1-block steps: bumps, lone steps, seam steps
        one = np.flatnonzero(np.abs(d) == 1)
        if len(one):
            sg = np.sign(d[one])
            allst = np.flatnonzero(d != 0)
            sd = C.seam_dist(X[one], Z[one]) if len(C.segs) else np.full(len(one), np.inf)
            done = set()
            for q, i in enumerate(one.tolist()):
                if i in done: continue
                near = allst[(allst >= i - QUIET) & (allst <= i + QUIET) & (allst != i)]
                # a bump (or dip): the next step comes back within BUMP, with no other step QUIET before or after
                nb = allst[allst > i]
                if len(nb) and nb[0] - i <= BUMP and abs(d[nb[0]]) == 1 and np.sign(d[nb[0]]) == -sg[q]:
                    j2 = int(nb[0])
                    others = allst[((allst >= i - QUIET) & (allst < i)) | ((allst > j2) & (allst <= j2 + QUIET))]
                    if not len(others) and i >= QUIET and j2 + QUIET < len(d) and ok[i - QUIET:j2 + QUIET].mean() > 0.9:
                        m = (i + j2 + 1) // 2
                        add("deck_bump" if dk[i:j2 + 1].any() else "bump", m, X, Z, eidx[m], 1, L,
                            ext(m, length=j2 - i, up=bool(sg[q] > 0)))
                        done.add(j2); continue
                same = allst[(allst != i) & (np.sign(d[allst]) == sg[q])]
                gap_same = np.min(np.abs(same - i)) if len(same) else 10 ** 9
                if sd[q] <= SEAM:
                    along = C.seam_line(S, (X[i] + X[i + 1]) / 2, (Z[i] + Z[i + 1]) / 2, bool(dk[i]))
                    if along >= SEAM_LINE:
                        add("seam_step", i, X, Z, eidx[i], 1, L, ext(i, up=bool(sg[q] > 0), along=along)); continue
                # a lone step: nothing else within LONE, and the road undoes it: the steps before and after it (within
                # 2 * LONE_SLOPE) go the other way, so it is a ledge on a level road, not one rise of a slope
                if not len(near) and gap_same > LONE_SLOPE and i >= LONE and i + LONE < len(ok) and ok[i - LONE:i + LONE + 1].mean() > 0.9:
                    before, after = allst[allst < i], allst[allst > i]
                    pb = before[-1] if len(before) and i - before[-1] <= 2 * LONE_SLOPE else None
                    pa = after[0] if len(after) and after[0] - i <= 2 * LONE_SLOPE else None
                    if pb is not None or pa is not None:
                        add("deck_bump" if dk[i] else "lone_step", i, X, Z, eidx[i], 1, L,
                            ext(i, up=bool(sg[q] > 0), undone=int(min(abs(v - i) for v in (pb, pa) if v is not None))))

        # ---- the surface
        if S.surface:
            valid = (cg > 2) & (src >= 0) & mine & land
            rt = np.zeros(len(s), bool)
            if valid.any(): rt[valid] = S.road_top(src[valid], lay[valid], X[valid], Z[valid])
            surf_samples += int(valid.sum()); surf_road += int((rt & valid).sum())
            for a, b in runs(valid & ~rt):
                if b - a >= SURFACE:
                    m = (a + b) // 2
                    add("surface", m, X, Z, eidx[m], b - a, L, ext(m, length=b - a))

    # ---- one defect per place: a broken joint shows as a step, a joint and an undecked stretch on one stroke within a
    # few blocks; keep the most severe (then the first kind in rank)
    rank = {k: i for i, k in enumerate(["deck_joint", "undecked", "deck_step", "gap", "step", "seam_step", "deck_bump", "bump",
                                         "hole", "spike", "floor_gap", "surface", "lone_step"])}
    defects.sort(key=lambda d: (d["chain"], -SEV[d["sev"]], rank[d["kind"]]))
    kept, near = [], defaultdict(list)
    for d in defects:
        key = (d["chain"], int(d["x"] // 16), int(d["z"] // 16))
        if any(abs(o["x"] - d["x"]) <= DEDUPE and abs(o["z"] - d["z"]) <= DEDUPE and o["sev"] == d["sev"]
               for gx in (-1, 0, 1) for gz in (-1, 0, 1) for o in near[(d["chain"], key[1] + gx, key[2] + gz)]):
            continue
        near[key].append(d); kept.append(d)
    defects = kept
    # ---- where each defect is: its chunk and the nearest boundary
    if defects:
        dx = np.array([d["x"] - ox for d in defects]); dz = np.array([d["z"] - oz for d in defects])
        own = C.owner(dx, dz); sd = C.seam_dist(dx, dz)
        for d, o, v in zip(defects, own.tolist(), sd.tolist()):
            d["chunk"] = C.ids[o] if o >= 0 else None
            d["seam_dist"] = round(v, 1) if math.isfinite(v) else None
    road_km = float(km_class.sum())
    kinds = list(KIND_SEV)
    count = lambda f: {k: sum(1 for d in defects if d["kind"] == k and f(d)) for k in kinds}
    def sevs(f):
        c = {s_: sum(1 for d in defects if d["sev"] == s_ and f(d)) for s_ in SEV}
        return c
    total = count(lambda d: True)
    per_km = lambda c, km: {k: round(v / km, 3) for k, v in c.items()} if km > 0 else {}
    summary = {"road_km": round(road_km, 2), "deck_km": round(deck_km, 2), "samples": samples, "defects": len(defects),
               "by_severity": sevs(lambda d: True), "by_kind": total, "per_km": per_km(total, road_km),
               "per_km_severity": per_km(sevs(lambda d: True), road_km)}
    if S.surface and surf_samples:
        summary["road_surface_share"] = round(surf_road / surf_samples, 4)
    per_chunk = {}
    for i, cid in enumerate(C.ids):
        f = lambda d, cid=cid: d.get("chunk") == cid
        km_ = float(km_chunk[i])
        per_chunk[cid] = {"road_km": round(km_, 2), "by_severity": sevs(f), "by_kind": {k: v for k, v in count(f).items() if v},
                          "high_per_km": round(sevs(f)["HIGH"] / km_, 3) if km_ > 0 else None}
    per_class = {}
    for i, c in enumerate(CLS):
        f = lambda d, c=c: d["cls"] == c
        if km_class[i] <= 0 and not any(f(d) for d in defects): continue
        per_class[c] = {"road_km": round(float(km_class[i]), 2), "by_severity": sevs(f), "by_kind": {k: v for k, v in count(f).items() if v}}
    # seams: how many defects lie within 8 blocks of a chunk boundary
    summary["near_seam"] = {s_: sum(1 for d in defects if d["sev"] == s_ and d.get("seam_dist") is not None and d["seam_dist"] <= 8) for s_ in SEV}

    order = sorted(defects, key=lambda d: (-SEV[d["sev"]], -float(d["size"]), CLS.index(d["cls"]) if d["cls"] in CLS else 9))
    spots = []
    taken = []
    for d in order:
        if len(spots) >= n_spots: break
        if any((d["x"] - a) ** 2 + (d["z"] - b) ** 2 < 30 ** 2 for a, b in taken): continue   # one picture per place
        taken.append((d["x"], d["z"]))
        e = E[d["edge"]]; A, B = G["nodes"][e[0]], G["nodes"][e[1]]
        sp = {"x": round(d["x"]), "z": round(d["z"]), "label": f"{d['kind']} {d['size']} on {d['road'] or d['cls']} ({d.get('chunk')})",
              "yaw": round(math.atan2(B[0] - A[0], B[1] - A[1]), 4), "cat": "seam" if d["kind"] == "seam_step" else "road_" + d["kind"]}
        if d["deck"]: sp["deck"] = True
        spots.append(sp)
    truncated = max(0, len(order) - MAX_DEFECTS)
    out = {"v": 1, "site": meta.get("name"), "made": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "seconds": round(time.time() - t0, 1), "partial": partial, "surface_checked": bool(S.surface),
           "thresholds": {"BUMP": BUMP, "QUIET": QUIET, "LONE": LONE, "LONE_SLOPE": LONE_SLOPE, "SEAM": SEAM, "SEAM_LINE": SEAM_LINE, "SURFACE": SURFACE},
           "summary": summary, "per_chunk": per_chunk, "per_class": per_class, "spots": spots,
           "defects_truncated": truncated,
           "defects": [{k: v for k, v in d.items() if k not in ("chain",)} for d in order[:MAX_DEFECTS]]}
    return out


def text(r):
    s = r["summary"]; km = s["road_km"] or 1
    L = [f"roadcheck: {s['road_km']} km of road ({s['deck_km']} km on decks), {s['samples']:,} samples, "
         f"{r['seconds']} s{' (PARTIAL: time limit)' if r['partial'] else ''}",
         "  per km: " + ", ".join(f"{k} {v / km:.2f}" for k, v in s["by_severity"].items()) + f"  ({s['defects']} defects)",
         "  by kind: " + ", ".join(f"{k} {v}" for k, v in s["by_kind"].items() if v)]
    if "road_surface_share" in s: L.append(f"  road surface under the car: {100 * s['road_surface_share']:.1f}% of samples")
    worst = sorted(r["per_chunk"].items(), key=lambda kv: -(kv[1]["high_per_km"] or 0))[:5]
    if len(r["per_chunk"]) > 1:
        L.append("  worst chunks (HIGH per km): " + ", ".join(f"{k} {v['high_per_km']}" for k, v in worst))
        L.append(f"  within 8 blocks of a seam: " + ", ".join(f"{k} {v}" for k, v in s["near_seam"].items()))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site")
    ap.add_argument("--out", help="report file (default SITE/roadcheck.json; spots beside it as roadcheck-spots.json)")
    ap.add_argument("--spots", type=int, default=60, help="how many of the worst defects to list as audit spots")
    ap.add_argument("--no-surface", action="store_true", help="skip the road material check (reads every pack)")
    ap.add_argument("--max-seconds", type=float, help="stop walking after this long and write what was found")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--debug", action="store_true", help="add the height profile around each defect")
    a = ap.parse_args()
    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x, file=sys.stderr))
    r = check(a.site, a.spots, surface=not a.no_surface, max_seconds=a.max_seconds, log=log, debug=a.debug)
    out = Path(a.out) if a.out else Path(a.site) / "roadcheck.json"
    out.write_text(json.dumps(r, separators=(",", ":")))
    (out.parent / "roadcheck-spots.json").write_text("[\n" + ",\n".join(json.dumps(sp) for sp in r["spots"]) + "\n]\n")
    print(text(r))


if __name__ == "__main__":
    main()
