"""Re-grade the road surfaces of an Arnis world along the OSM roads it was built from (used by mca2web.py --osm).

Why: Arnis lays a bridge deck to clear what passes under it at 1 block per 3 cells (HARD_RUN), over water at 1 per 3
(WATER_RUN), and its approach ramps at most 1 per 8 and only as far as the next junction. OSM bridge ways are short
(often 50 to 130 m, the span alone), so on flat ground a deck and its ramps come out as a staircase of 1-block risers
every 2 to 6 blocks. A deck that starts close to the road it crosses is still low where it passes over it, 4 or 5
blocks above the lower road with 2 rows of deck structure and a pier under that, which from the lower road reads as a
wall with a 1-2 block slot under it. Side-by-side carriageways (and ways overlapping at junctions) each get a level of
their own, so one road surface steps by a block along its lanes.

What: this works on the decoded world (the converter's per-region caches, before any face is drawn) with the OSM
ways of the bake, and only ever raises road surfaces or opens space under an upper road:
  1. Every car road is followed along its OSM centre line through nodes no other road uses (a "chain": a bridge and
     its approaches are one chain), reading the level of its own surface at each block of length, and a reference
     ground: the top of Arnis's terrain shell, closed over dips such as a channel or a road in a cut.
  2. Where a lower road is covered within 3 blocks of its surface by a road crossing above, the upper road is
     required to stand CLEAR blocks above the lower one there.
  3. Each chain's height above the reference ground is graded: where it stands SOURCE or more above it (a deck or a
     ramp), its surface may fall away no faster than GRADE either way, so ramps lengthen; junctions and the world
     edge keep their level, so between a junction and a deck the climb is as even as the room there allows.
  4. Every cross-section of a chain is levelled to one height. Overlapping or side-by-side carriageways on one surface
     are levelled to the higher one: a chain that another lifts is required to reach that level there and re-graded,
     so it ramps up to it instead of being left a step below. Carriageway that no chain's kerbs cover (deck widening)
     follows the chain whose margin it is in; a surface with another road surface close over it is not lifted.
  5. Surface columns are raised (what stands on the surface - markings, railings, posts - moves up with it, and the
     gap is filled with stone bricks like Arnis's own ramp fill); under an upper road over a lower one everything
     between the lower surface and the upper road's last row is cleared and the lower surface repaved.
Roads on hills keep following the terrain: grading is relative to the ground, and only away from raised structures.
"""
import json, math, os, time
import numpy as np
from scipy import ndimage

GRADE = 1 / 12      # steepest a raised road may fall away from a deck, in blocks per block, where there is room
SOURCE = 3          # a road this many blocks above the reference ground is a deck or ramp to grade from
SOURCE_RUN = 4      # ... over at least this many blocks of its length
CLEAR = 5           # an upper road over a lower one: 3 blocks of headroom, one of deck, then its surface
MAX_RAISE = 12      # never lift a surface by more than this
CLOSE_BINS = 81     # reference ground: closing of the ground along a chain over this many blocks
NLEV = 4            # surface levels kept per column
KEY = 4096          # pixel * KEY + level, as one int64 key
NDIR = 12           # road directions are compared in sectors of 15 degrees; within one sector either way is parallel

CAR = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street", "road",
       "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
DEFAULT_M = {"motorway": (11.0, 3.5), "trunk": (11.0, 3.5), "motorway_link": (5.0, 3.5), "trunk_link": (5.0, 3.5),
             "primary": (8.0, 3.25), "secondary": (7.0, 3.25), "tertiary": (6.5, 3.25),
             "secondary_link": (3.0, 3.25), "tertiary_link": (3.0, 3.25)}


def half_width(t):
    """Arnis's kerb half-width in blocks (road_width.rs, leaving out parking and cycle lanes)."""
    hw = t.get("highway", "")
    base, lane = DEFAULT_M.get(hw, (5.0, 3.25))
    m = base
    try: m = max(m, int(str(t.get("lanes", "")).split(";")[0]) * lane)
    except ValueError: pass
    if hw in ("residential", "unclassified") and "lanes" not in t and t.get("oneway") not in ("yes", "1", "true", "-1") \
            and any(t.get(k) not in (None, "no", "none") for k in ("sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right")):
        m = max(m, 2 * lane)
    for k in ("width:carriageway", "width"):
        try:
            m = max(m, float(str(t[k]).split(";")[0].replace(",", ".").rstrip(" m"))); break
        except (KeyError, ValueError): pass
    return int(min(8, max(1, round((m - 1) / 2))))


def projection(world):
    """lon/lat -> block (x, z) of the bake, from the world's metadata.json (as build.py projection())."""
    md = json.load(open(os.path.join(world, "metadata.json")))
    s, n, w, e = md["minGeoLat"], md["maxGeoLat"], md["minGeoLon"], md["maxGeoLon"]
    R = 6371000.0
    def hav(dlat, dlon, lat):
        a = math.sin(math.radians(dlat) / 2) ** 2 + math.cos(math.radians(lat)) ** 2 * math.sin(math.radians(dlon) / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    sx = math.floor(hav(0, e - w, (s + n) / 2)); sz = math.floor(hav(n - s, 0, 0))
    k = md.get("scale", 1) or 1
    if k != 1: sx *= k; sz *= k
    return lambda lon, lat: ((lon - w) / (e - w) * sx, (1 - (lat - s) / (n - s)) * sz)


def load_ways(osm_path, xz):
    d = json.load(open(osm_path))
    ll = {e["id"]: (e["lon"], e["lat"]) for e in d["elements"] if e["type"] == "node"}
    ways = []
    for e in d["elements"]:
        if e["type"] != "way": continue
        if "geometry" in e and e.get("nodes"):
            for nd, g in zip(e["nodes"], e["geometry"]):
                if g: ll.setdefault(nd, (g["lon"], g["lat"]))
        t = e.get("tags", {})
        if t.get("highway") not in CAR or t.get("area") == "yes": continue
        if t.get("tunnel") not in (None, "no") or t.get("covered") not in (None, "no"): continue
        nodes = [nd for nd in e.get("nodes", []) if nd in ll]
        if len(nodes) < 2: continue
        ways.append({"id": e["id"], "tags": t, "nodes": nodes, "hw": half_width(t), "bridge": t.get("bridge") not in (None, "no")})
    pos = {nd: xz(*ll[nd]) for w in ways for nd in w["nodes"]}
    return ways, pos


def chains_of(ways):
    """Ways joined end to end through nodes only those two ways use, as lists of (way index, reversed); and the
    junction nodes (three or more way ends, or a way passing through where another meets it)."""
    use, ends = {}, {}
    for i, w in enumerate(ways):
        ns = w["nodes"]
        for k, nd in enumerate(ns):
            use[nd] = use.get(nd, 0) + (1 if k in (0, len(ns) - 1) else 2)
        ends.setdefault(ns[0], []).append((i, 0)); ends.setdefault(ns[-1], []).append((i, 1))
    junction = {nd for nd, u in use.items() if u >= 3}
    def onward(nd, cur, seen):
        nxt = [(j, e) for j, e in ends.get(nd, []) if j != cur]
        if nd in junction or len(nxt) != 1 or nxt[0][0] in seen: return None
        return nxt[0]
    done, chains = set(), []
    for i in range(len(ways)):
        if i in done: continue
        cur, rev, seen = i, False, {i}
        while True:   # back to the chain's first way; (cur, rev) is traversed from nodes[-1] when rev
            nx = onward(ways[cur]["nodes"][-1 if rev else 0], cur, seen)
            if nx is None: break
            cur, rev = nx[0], nx[1] == 0; seen.add(cur)
        chain, seen = [], set()
        while True:
            chain.append((cur, rev)); done.add(cur); seen.add(cur)
            nx = onward(ways[cur]["nodes"][0 if rev else -1], cur, seen | done)
            if nx is None: break
            cur, rev = nx[0], nx[1] == 1
        chains.append(chain)
    return chains, junction


def _segmax(keys, vals):
    """Unique keys and the largest value of each."""
    o = np.argsort(keys, kind="stable"); k, v = keys[o], vals[o]
    first = np.concatenate([[True], k[1:] != k[:-1]]) if len(k) else np.zeros(0, bool)
    starts = np.flatnonzero(first)
    return k[first], (np.maximum.reduceat(v, starts) if len(v) else v)


class Grader:
    def __init__(self, W, D, H, log=print):
        self.W, self.D, self.H, self.log = W, D, H, log
        self.lev = np.full((NLEV, D, W), -1, dtype=np.int16)     # surface levels per column, highest first
        self.carr = np.zeros((NLEV, D, W), dtype=bool)          # ... whose block is carriageway (asphalt, markings)
        self.ground = np.full((D, W), -1, dtype=np.int16)       # top of Arnis's terrain shell
        self.occ = np.zeros(((H + 7) // 8, D, W), dtype=np.uint8)   # packed: a block (not leaves or water) at y

    # ---- pass 1: per-column summaries of one region's grid
    def scan_region(self, ox, oz, g, dm, solid_lut, carr_lut, block_lut):
        """g, dm: [H, P, P] cube and partial material ids of a region whose column (0, 0) is world (ox, oz)."""
        H, P = g.shape[0], g.shape[1]
        sl = (slice(oz, oz + P), slice(ox, ox + P))
        self.occ[(slice(None),) + sl] = np.packbits(block_lut[g] | (dm != 0), axis=0)
        ground = np.full((P, P), -1, dtype=np.int16)
        lev = np.full((NLEV, P, P), -1, dtype=np.int16); carr = np.zeros((NLEV, P, P), dtype=bool)
        cnt = np.zeros((P, P), dtype=np.int8)
        above = np.zeros((P, P), dtype=bool)
        for y in range(H - 1, 0, -1):          # top down: surfaces are solid blocks with no solid block on them
            gy = g[y]
            sol = solid_lut[gy]
            new = sol & ~above & (cnt < NLEV)
            if new.any():
                zz, xx = np.nonzero(new); k = cnt[zz, xx]
                lev[k, zz, xx] = y; carr[k, zz, xx] = carr_lut[gy[zz, xx]]
                cnt[zz, xx] += 1
            above = sol
            ground = np.where(gy != 0, y + 2, ground)   # ends as the lowest block above layer 0, plus 2
        self.ground[sl] = ground; self.lev[(slice(None),) + sl] = lev; self.carr[(slice(None),) + sl] = carr

    def occupied(self, idx, lo, hi):
        """Whether each column idx (flat z*W+x) has a block anywhere in [lo, hi]."""
        z, x = np.divmod(idx, self.W)
        out = np.zeros(len(idx), dtype=bool)
        if not len(idx): return out
        for d in range(int((hi - lo).max()) + 1):
            y = lo + d
            ok = (y <= hi) & (y >= 0) & (y < self.H)
            yc = np.clip(y, 0, self.H - 1)
            out |= ok & (((self.occ[yc >> 3, z, x] >> (7 - (yc & 7))) & 1) == 1)
        return out

    # ---- the road network
    def build(self, ways, pos):
        W, D = self.W, self.D
        chains, junction = chains_of(ways)
        self.junction = junction
        C = []
        for ch in chains:
            pts, hws, nodes, br = [], [], [], []
            for wi, rev in ch:
                w = ways[wi]; ns = w["nodes"][::-1] if rev else w["nodes"]
                for k, nd in enumerate(ns):
                    if pts and k == 0: continue
                    pts.append(pos[nd]); nodes.append(nd); hws.append(w["hw"]); br.append(w["bridge"])
            pts = np.array(pts, dtype=np.float64)
            keep = np.concatenate([[True], np.hypot(*(pts[1:] - pts[:-1]).T) > 1e-6])
            pts = pts[keep]
            hws, nodes, br = ([v for v, k in zip(a, keep) if k] for a in (hws, nodes, br))
            if len(pts) < 2: continue
            s = np.concatenate([[0.0], np.cumsum(np.hypot(*(pts[1:] - pts[:-1]).T))])
            if s[-1] < 2: continue
            C.append({"pts": pts, "s": s, "hw": np.array(hws), "nodes": nodes, "bridge": np.array(br),
                      "ways": [ways[wi]["id"] for wi, _ in ch]})
        # the pixels within reach of each chain: flat index, s, signed lateral offset, half width there
        for c in C:
            P, S, L, HW = [], [], [], []
            pts, s = c["pts"], c["s"]
            for k in range(len(pts) - 1):
                a, b = pts[k], pts[k + 1]
                d = b - a; ln = math.hypot(*d)
                hw = float(max(c["hw"][k], c["hw"][k + 1]))
                # a deck is stamped as a square around each centre line cell: wider across on a diagonal
                if c["bridge"][k + 1]: hw *= (abs(d[0]) + abs(d[1])) / ln
                R = hw + 2.5
                x0, x1 = int(max(0, math.floor(min(a[0], b[0]) - R))), int(min(W - 1, math.ceil(max(a[0], b[0]) + R)))
                z0, z1 = int(max(0, math.floor(min(a[1], b[1]) - R))), int(min(D - 1, math.ceil(max(a[1], b[1]) + R)))
                if x1 < x0 or z1 < z0: continue
                zz, xx = np.mgrid[z0:z1 + 1, x0:x1 + 1]
                zz, xx = zz.ravel(), xx.ravel()
                px, pz = xx + 0.5, zz + 0.5
                t = np.clip(((px - a[0]) * d[0] + (pz - a[1]) * d[1]) / (ln * ln), 0, 1)
                dist = np.hypot(a[0] + t * d[0] - px, a[1] + t * d[1] - pz)
                side = np.sign(d[0] * (pz - a[1]) - d[1] * (px - a[0]))
                m = dist <= R
                P.append((zz * W + xx)[m]); S.append((s[k] + t * ln)[m]); L.append((side * dist)[m])
                HW.append(np.full(int(m.sum()), hw))
            if not P: continue
            P, S, L, HW = map(np.concatenate, (P, S, L, HW))
            if not len(P): continue
            o = np.lexsort((np.abs(L), P))
            P, S, L, HW = P[o], S[o], L[o], HW[o]
            first = np.concatenate([[True], P[1:] != P[:-1]])
            c["pix"], c["ps"], c["plat"], c["phw"] = P[first], S[first], L[first], HW[first].astype(float)
            c["nb"] = int(math.ceil(s[-1])) + 1
            c["b"] = np.clip(c["ps"].astype(int), 0, c["nb"] - 1)
        self.C = [c for c in C if "pix" in c]
        self.log(f"road grade: {len(ways)} car ways in {len(self.C)} chains, {len(junction)} junctions")

    def levels_at(self, pix):
        z, x = np.divmod(pix, self.W)
        return self.lev[:, z, x], self.carr[:, z, x]

    def ground_of(self, c):
        """Median top of the terrain shell under the carriageway, per block of length (interpolated where unknown)."""
        nb, b = c["nb"], c["b"]
        z, x = np.divmod(c["pix"], self.W)
        g = self.ground[z, x].astype(float)
        m = (np.abs(c["plat"]) <= c["phw"]) & (g >= 0)
        gb = np.full(nb, np.nan)
        if m.any():
            o = np.argsort(b[m], kind="stable"); bb, gg = b[m][o], g[m][o]
            cuts = np.flatnonzero(np.diff(bb)) + 1
            for sb, sg in zip(np.split(bb, cuts), np.split(gg, cuts)): gb[sb[0]] = np.median(sg)
        return gb

    def track(self, c, gb):
        """The chain's own surface level per block of length, followed from a stretch that is plainly at grade."""
        nb, b = c["nb"], c["b"]
        near = np.abs(c["plat"]) <= 1.5
        lev, car = self.levels_at(c["pix"])
        cand = [dict() for _ in range(nb)]
        for k in range(NLEV):
            m = near & car[k] & (lev[k] >= 0)
            for bi, y in zip(b[m].tolist(), lev[k][m].tolist()):
                cand[bi][y] = cand[bi].get(y, 0) + 1
        h = np.full(nb, np.nan)
        single = [i for i in range(nb) if len(cand[i]) == 1]
        grade = [i for i in single if not np.isnan(gb[i]) and abs(next(iter(cand[i])) - gb[i]) <= 2]
        pool = grade or single or [i for i in range(nb) if cand[i]]
        if not pool: return h
        start = min(pool, key=lambda i: abs(i - nb / 2))
        h[start] = max(cand[start], key=cand[start].get)
        for rng in (range(start + 1, nb), range(start - 1, -1, -1)):
            last, gap = h[start], 0
            for i in rng:
                tol = min(2, 1 + gap // 8)
                ok = [y for y in cand[i] if abs(y - last) <= tol]
                if ok:
                    y = min(ok, key=lambda y: (abs(y - last), -cand[i][y]))
                    h[i] = y; last = y; gap = 0
                else:
                    gap += 1
        ok = ~np.isnan(h)
        if (~ok).any():
            idx = np.arange(nb)
            h[~ok] = np.interp(idx[~ok], idx[ok], h[ok])
        return h

    def run(self, osm_path, world, origin=(0, 0)):
        """origin: the world block that is column (0, 0) of the grids."""
        pj = projection(world)
        xz = lambda lon, lat: tuple(v - o for v, o in zip(pj(lon, lat), origin))
        t0 = time.time()
        ways, pos = load_ways(osm_path, xz)
        self.build(ways, pos)
        tick = lambda what: self.log(f"road grade: {what} at {time.time() - t0:.1f}s")
        tick("chains")
        W = self.W
        st = dict(chains=len(self.C), tracked=0, crossings=0, chains_raised=0, raised_cols=0, raised_blocks=0, max_raise=0,
                  levelled_bins=0, carved_cols=0)
        for c in self.C:
            gb = self.ground_of(c)
            h = self.track(c, gb)
            c["ok"] = not np.isnan(h).all()
            if not c["ok"]: continue
            st["tracked"] += 1
            okg = ~np.isnan(gb)
            c["gok"] = okg.copy()
            if not okg.any(): gb = h.copy()
            elif (~okg).any(): gb[~okg] = np.interp(np.flatnonzero(~okg), np.flatnonzero(okg), gb[okg])
            nb = c["nb"]
            gref = ndimage.grey_closing(gb, size=min(CLOSE_BINS, 2 * ((nb - 1) // 2) + 1), mode="nearest")
            c["h"], c["gref"] = h, ndimage.uniform_filter1d(gref, 9, mode="nearest")
            c["req"] = np.full(nb, -np.inf)
            c["hb"] = np.round(h[c["b"]]).astype(np.int64)
        self.C = [c for c in self.C if c["ok"]]
        tick("tracked")
        # ---- crossings: a lower road covered within 3 blocks of its surface by an upper road
        reach_pix, reach_ci, reach_lev = [], [], []
        for ci, c in enumerate(self.C):
            r = np.abs(c["plat"]) <= c["phw"] + 2
            reach_pix.append(c["pix"][r]); reach_ci.append(np.full(int(r.sum()), ci)); reach_lev.append(c["hb"][r])
        reach_pix, reach_ci, reach_lev = map(np.concatenate, (reach_pix, reach_ci, reach_lev))
        o = np.argsort(reach_pix, kind="stable"); reach_pix, reach_ci, reach_lev = reach_pix[o], reach_ci[o], reach_lev[o]
        nodesets = [set(c["nodes"]) for c in self.C]
        crossings = []   # (pixel, lower level, upper chain, its pixel row)
        for ai, a in enumerate(self.C):
            inner = np.abs(a["plat"]) <= a["phw"]
            pix, hb = a["pix"][inner], a["hb"][inner]
            blocked = self.occupied(pix, hb + 1, hb + 3)
            for p, y in zip(pix[blocked].tolist(), hb[blocked].tolist()):
                lo, hi = np.searchsorted(reach_pix, p), np.searchsorted(reach_pix, p, side="right")
                above = [(int(ci), int(u)) for ci, u in zip(reach_ci[lo:hi], reach_lev[lo:hi])
                         if ci != ai and u >= y + 2 and not (nodesets[ci] & nodesets[ai])]
                if not above: continue
                ci, u = min(above, key=lambda t: t[1])
                bc = self.C[ci]
                k = int(np.searchsorted(bc["pix"], p))
                if k >= len(bc["pix"]) or bc["pix"][k] != p: continue
                bi = bc["b"][k]
                bc["req"][bi] = max(bc["req"][bi], y + CLEAR)
                crossings.append((p, y, ci, k))
        st["crossings"] = len(crossings)
        tick("crossings")
        # ---- grade each chain
        for c in self.C:
            self._grade(c)
            st["chains_raised"] += bool((c["t"] > c["h"]).any())
        tick("graded")
        self._own_and_level(st)
        tick("levelled")
        return self._finish(st, crossings)

    def _grade(self, c):
        """Target level per bin of a chain from its height above reference ground and its requirements."""
        # requirements (a level to clear a road below, or a neighbour's level) are absolute and spread at GRADE in
        # absolute terms; a deck's own height is relative to the reference ground and spreads relative to it
        h, gref, reqa, nb = c["h"], c["gref"], c["req"], c["nb"]
        e = h - gref
        req = reqa - gref
        need = np.maximum(e, req)
        # a deck or ramp: SOURCE or more above measured ground over at least SOURCE_RUN blocks of length (not an
        # edge of the world's data, where the ground is unknown, nor one odd column), that also stands out of the
        # chain's own profile: a level deck over a river or a dip carries on level, it is not a hump to ramp up to
        if "prom" not in c:
            c["prom"] = h - ndimage.grey_opening(h, size=min(CLOSE_BINS, 2 * ((nb - 1) // 2) + 1), mode="nearest")
        src = ndimage.binary_opening((e >= SOURCE) & (c["prom"] >= SOURCE) & c["gok"], structure=np.ones(SOURCE_RUN, bool))
        dil = np.where(src, e, -np.inf)
        dila = reqa.copy()
        if np.isfinite(dil).any() or np.isfinite(dila).any():
            for a in (dil, dila):
                for i in range(1, nb): a[i] = max(a[i], a[i - 1] - GRADE)
                for i in range(nb - 2, -1, -1): a[i] = max(a[i], a[i + 1] - GRADE)
            dil = np.maximum(dil, dila - gref)
            # junctions, and where the chain leaves the world, keep their level
            pins = {int(min(nb - 1, round(sv))) for nd, sv in zip(c["nodes"], c["s"]) if nd in self.junction}
            inw = np.unique(c["b"])
            if inw[0] > 0: pins.add(int(inw[0]))
            if inw[-1] < nb - 1: pins.add(int(inw[-1]))
            for k in (0, -1):
                x, z = c["pts"][k]
                if not (1 <= x < self.W - 1 and 1 <= z < self.D - 1): pins.add(int(inw[0] if k == 0 else inw[-1]))
            cap = np.full(nb, np.inf); idx = np.arange(nb)
            for pb in pins:
                d = np.abs(idx - pb).astype(float); d[pb] = np.inf
                gs = max(GRADE, float(np.max((need - e[pb]) / d)))
                cap = np.minimum(cap, e[pb] + gs * np.abs(idx - pb))
            p = np.maximum(need, np.minimum(dil, cap))
        else:
            p = need
        t = np.maximum(np.round(gref + p), h)
        if "t" in c: t = np.maximum(t, c["t"])
        c["t"] = np.minimum(t, h + MAX_RAISE)

    def _own_and_level(self, st):
        # ---- own surface pixels: the highest surface within a block of the chain's level; carriageway inside the
        # kerbs, anything at the 2-block margin (deck edges, railings, verges)
        for c in self.C:
            lev, car = self.levels_at(c["pix"])
            inner = np.abs(c["plat"]) <= c["phw"]
            own = np.full(len(c["pix"]), -1, dtype=np.int64)
            for k in range(NLEV):
                m = (lev[k] >= 0) & (np.abs(lev[k] - c["hb"]) <= 1) & (car[k] | ~inner)
                own = np.where(m, np.maximum(own, lev[k]), own)
            c["own"] = own
            # a margin pixel that is carriageway belongs to another road: it only compares levels with it
            c["mcar"] = ~inner & (own >= 0) & (car & (lev == own[None])).any(axis=0)
            # direction of the chain per bin, as one of NDIR sectors of the half circle
            bins = np.arange(c["nb"], dtype=float)
            x = np.interp(bins, c["s"], c["pts"][:, 0]); z = np.interp(bins, c["s"], c["pts"][:, 1])
            ang = np.arctan2(np.gradient(z), np.gradient(x)) % np.pi
            c["dir"] = (np.round(ang / (np.pi / NDIR)).astype(np.int64) % NDIR)
        # overlapping and side-by-side carriageways on one surface: a chain whose carriageway another chain running
        # the same way lifts (inside its kerbs or at its 2-block margin) is required to reach that level there, and
        # re-graded, so it ramps up to it at GRADE instead of being left a step below. A road crossing at an angle
        # only levels a 1-block difference (overlaps at junctions), so a raised ramp never drags up the roads it meets.
        for _ in range(6):
            keys, tv = self._claims(final=False); changed = 0
            dkeys, dtv = self._claims(final=False, by_dir=True)
            for c in self.C:
                inner = (c["own"] >= 0) & (np.abs(c["plat"]) <= c["phw"])
                if not inner.any(): continue
                kk = c["pix"][inner] * KEY + c["own"][inner]
                got = tv[np.searchsorted(keys, kk)]
                bb = c["b"][inner]; cur = c["t"][bb]
                par = np.full(len(kk), -np.inf)
                for dd in (-1, 0, 1):
                    q = kk * NDIR + (c["dir"][bb] + dd) % NDIR
                    j = np.minimum(np.searchsorted(dkeys, q), len(dkeys) - 1)
                    par = np.where(dkeys[j] == q, np.maximum(par, dtv[j]), par)
                got = np.where((got > cur) & (got - cur <= 1), got, np.minimum(got, par))
                up = got > cur
                if not up.any(): continue
                best = np.full(c["nb"], -np.inf); np.maximum.at(best, bb[up], got[up])
                r = best > c["t"]
                if not r.any(): continue
                c["req"] = np.where(r, np.maximum(c["req"], best), c["req"])
                before = c["t"].copy()
                self._grade(c)
                changed += int((c["t"] != before).sum())
            st["levelled_bins"] += changed
            if not changed: break

    def _claims(self, final=True, by_dir=False):
        """Level each chain claims per (pixel, surface level): the highest. With final, a margin pixel that is
        carriageway takes the level of the chain(s) whose kerbs it lies inside, and only carriageway no chain's
        kerbs cover (deck widening, square deck stamps) takes the margin claim."""
        ks, vs, own = [], [], []
        for c in self.C:
            m = c["own"] >= 0
            k = c["pix"][m] * KEY + c["own"][m]
            if by_dir: k = k * NDIR + c["dir"][c["b"][m]]
            ks.append(k); vs.append(c["t"][c["b"][m]]); own.append(~c["mcar"][m])
        ks, vs, own = map(np.concatenate, (ks, vs, own))
        if not final: return _segmax(ks, vs)
        ki, vi = _segmax(ks[own], vs[own])
        km, vm = _segmax(ks[~own], vs[~own])
        keep = ~np.isin(km, ki)
        k = np.concatenate([ki, km[keep]]); v = np.concatenate([vi, vm[keep]]); o = np.argsort(k)
        return k[o], v[o]

    def _finish(self, st, crossings):
        keys, tv = self._claims()
        pix, y = np.divmod(keys, KEY)
        d = tv.astype(np.int64) - y
        m = (d > 0) & (d <= MAX_RAISE)
        # a surface with another road surface standing over it closely (a deck over the ground beside a lower
        # road) is left alone: lifting it would lift the deck too, as part of the stack standing on it
        above = np.zeros(len(keys), bool)
        for j in range(1, NLEV):
            same = np.zeros(len(keys), bool); same[:-j] = pix[j:] == pix[:-j]
            gap = np.zeros(len(keys), np.int64); gap[:-j] = y[j:] - y[:-j]
            above |= same & (gap > 0) & (gap <= 13)
        m &= ~above
        self.raises = {}
        for p, y0, dd in zip(pix[m].tolist(), y[m].tolist(), d[m].tolist()):
            self.raises.setdefault(p, []).append((y0, dd))
        # ---- carving under an upper road over a lower one, to the upper road's final level
        self.carves = {}
        for p, y, ci, k in crossings:
            u = int(self.C[ci]["t"][self.C[ci]["b"][k]])
            if u < y + 4: continue
            hi = u - 2 if u >= y + 5 else u - 1
            self.carves[p] = (y, max(hi, self.carves.get(p, (y, hi))[1]))
        st["raised_cols"] = len(self.raises)
        st["raised_blocks"] = int(d[m].sum())
        st["max_raise"] = int(d[m].max()) if m.any() else 0
        st["carved_cols"] = len(self.carves)
        self.stats = st
        return st

    # ---- pass 2: apply to one region's grids in place (y in the same frame as scan_region)
    def apply_region(self, ox, oz, g, dm, ds, fill_id, pave_id, carr_lut, block_lut):
        P = g.shape[1]
        if not hasattr(self, "_ops"):
            ops = [(p // self.W, p % self.W, y0, d) for p, v in self.raises.items() for y0, d in v]
            ops.sort(key=lambda o: (o[0], o[1], -o[2]))     # per column, the highest surface first
            self._ops = np.array(ops, dtype=np.int32).reshape(-1, 4)
            cv = [(p // self.W, p % self.W, y, hi) for p, (y, hi) in self.carves.items()]
            self._cv = np.array(cv, dtype=np.int32).reshape(-1, 4)
        def mine(a):
            m = (a[:, 0] >= oz) & (a[:, 0] < oz + P) & (a[:, 1] >= ox) & (a[:, 1] < ox + P)
            r = a[m].copy(); r[:, 0] -= oz; r[:, 1] -= ox
            return r
        nr = _raise(g, dm, ds, mine(self._ops), block_lut, fill_id)
        nc = _carve(g, dm, ds, mine(self._cv), block_lut, carr_lut, pave_id)
        return nr, nc


from numba import njit


@njit(cache=True)
def _raise(g, dm, ds, ops, block_lut, fill):
    """Lifts the surface at (z, x, y0) by d: the stack standing on it (up to the first 3 free blocks, at most 10 high)
    moves up with it and the gap is filled; never into a block, and leaving 3 free blocks over the moved stack."""
    H = g.shape[0]; n = 0
    for i in range(ops.shape[0]):
        z, x, y0, d = ops[i, 0], ops[i, 1], ops[i, 2], ops[i, 3]
        a = y0 + 1
        while a + 2 < H and a - y0 <= 10:
            if (block_lut[g[a, z, x]] or dm[a, z, x] != 0 or block_lut[g[a + 1, z, x]] or dm[a + 1, z, x] != 0
                    or block_lut[g[a + 2, z, x]] or dm[a + 2, z, x] != 0):
                a += 1
            else:
                break
        if a - y0 > 10 or a + 2 >= H: continue
        room = 0
        while a + room < H and room < d + 3 and not (block_lut[g[a + room, z, x]] or dm[a + room, z, x] != 0): room += 1
        dd = min(d, room - 3)
        if dd <= 0: continue
        for y in range(a - 1, y0 - 1, -1):
            g[y + dd, z, x] = g[y, z, x]; dm[y + dd, z, x] = dm[y, z, x]; ds[y + dd, z, x] = ds[y, z, x]
        for y in range(y0, y0 + dd):
            g[y, z, x] = fill; dm[y, z, x] = 0; ds[y, z, x] = 0
        n += dd
    return n


@njit(cache=True)
def _carve(g, dm, ds, cv, block_lut, carr_lut, pave):
    """Clears (y, hi] over a lower road at (z, x, y) and repaves its surface."""
    n = 0
    for i in range(cv.shape[0]):
        z, x, y, hi = cv[i, 0], cv[i, 1], cv[i, 2], cv[i, 3]
        for yy in range(y + 1, hi + 1):
            if block_lut[g[yy, z, x]] or dm[yy, z, x] != 0: n += 1
            g[yy, z, x] = 0; dm[yy, z, x] = 0; ds[yy, z, x] = 0
        if not carr_lut[g[y, z, x]]:
            g[y, z, x] = pave; dm[y, z, x] = 0; ds[y, z, x] = 0
    return n
