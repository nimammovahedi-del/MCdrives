"""Grade every car road to one smooth longitudinal profile along its OSM centre line (used by mca2web.py --road-profile).

Why: Arnis lays a road on its terrain, and the terrain is quantised to whole blocks. Where the ground rises under a
road, its contour lines cross the road wherever they happen to fall: diagonally across the lanes, along a lane for a
hundred blocks, round a closed "plate" on a freeway, through a bridge deck, a block up and back down on a level road.
A cube world cannot avoid steps on a slope, but they can be few, evenly spaced, straight across the full road width,
and absent on a level road, inside a junction and on a deck.

How this differs from road_grade.py (the parked re-grade): that pass only ever raised surfaces (decks to clear what
they cross, ramps lengthened at 1 in 12, carriageways levelled up to the higher one, lifts handed on between chains),
by up to 12 blocks, per carriageway, without blending the ground beside; where a lift stopped (the next junction, the
edge of a carriageway, a chunk boundary worked out without the neighbour) it left a wall, a ledge or kerb teeth. Here
every road gets exactly ONE level per block of its length, chosen to fit the road as baked, and every column of its
cross-section is moved up or down by at most MAXD blocks to it:
  1. Chains: OSM car ways joined end to end through nodes no other way uses (road_grade.chains_of; tunnels and
     covered ways are not read, nor service roads). The road's own surface level per block of length is followed
     from the blocks along the centre line (road_grade.Grader.track), so a deck is followed on the deck and a road
     under a bridge below it, and a running median over DESPIKE blocks drops one-block bumps and pits from it.
  2. Columns: the columns of the road's cross-section (within its half width of the centre line, two more on a deck)
     at the followed level, each with what may be done to it: how far it can rise under whatever is over it (HEAD
     free blocks stay), how far it can sink into the ground under it, and whether too much stands on it to move.
  3. Fit: for every block of length and every level within MAXD of the followed one, the mean misfit of the
     cross-section's columns at that level (blocks they would have to move; a column that cannot follow costs most).
     Side-by-side carriageways (parallel chains within PARALLEL_GAP blocks of each other, at about the same height)
     add each other's misfit, so they choose the same levels and their steps fall on one line across both.
  4. Junctions: all chains through a node they share (a junction, or where two chains just meet) take one level
     there, the one with the least summed misfit; held over the junction's width plus JUNCTION_PAD where the roads
     meet level, at the node only where they meet on a slope.
  5. Profile: per chain, the levels with the least misfit plus STEP_COST per one-block step (a dynamic programme), with
     no step back the other way within MINRUN blocks unless keeping the hump or dip saves more than BUMP_COST (so the
     profile is monotone between its extrema), two-block steps only at a high price, and a hair of preference for the
     followed level where two fit equally well (coupled carriageways a block apart keep their own levels; the
     coupling only puts their steps in one place). On a slope the steps fall where half the
     cross-section has stepped: evenly spaced, one per block of rise, straight across the road.
  6. Bridges: a deck is never lowered (its clearance stays) and one whose followed level varies by at most one block
     takes no step anywhere on its span (it is laid at its higher level; the joints take the step).
  7. Moving: every road column is moved to the level of its station: the surface block (with its lane paint) and what
     stands on it move together; raising stretches the column (the block under the surface fills the gap), lowering
     compresses it, so the bottom of the column never moves and the column stays closed to the ground below it. A
     lowering that would open the hollow under a neighbour's ground to the sky is dropped (judged on the world as
     baked, so the outcome does not depend on the order of the moves or on region edges), and so is any move without
     room (counted as refused).
  8. Verges: columns within VERGE blocks of a moved road column follow it partly (the move scaled down with the
     distance, rounded), when they lie within 2 blocks of the road's old height and carry no more than STUFF_VERGE
     blocks (grass, fences; no buildings, trees or walls), so the step is eased into the terrain beside the road.
Chunk boundaries: each chunk's bake reaches 128 m past its own edge (bake_pad_m) on a height grid it shares with its
neighbours (elevation_base_m), and mca2web.py crops only after this pass, so both neighbours grade the same roads over
the same blocks there; every rule here looks at most MINRUN (60) blocks along a road, inside that margin.
"""
import math, os, time
import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
import road_grade as RG

DESPIKE = 7        # blocks: running median of the followed level (drops one-block bumps and pits)
MINRUN = 60        # blocks: a step back the other way sooner than this after a step is a hump or a dip (costs BUMP_COST)
MINSTAIR = 2       # blocks: two steps the same way closer than this cost a second STEP_COST
STEP_COST = 3.0    # a step must save this much misfit (blocks x bins, per column) to be taken
BUMP_COST = 30.0   # ... so a hump or dip is levelled unless keeping it saves more misfit than that
MAXD = 2           # never move a road surface by more than this
JUNCTION_PAD = 2   # blocks past the widest road's half width that a junction is held level
HEAD = 4           # free blocks to leave over a raised surface
VERGE = 3          # blocks beside the road that are blended
STUFF_ROAD = 8     # a road column may carry this many blocks standing on it (posts, lamps, railings) and still move
STUFF_VERGE = 3    # ... a verge column
PARALLEL_COS = 0.94   # carriageways this close to parallel are one road for the profile
PARALLEL_GAP = 12  # ... and at most this many blocks apart between their kerbs (the two carriageways of a freeway)


def _runs(q):
    """Start, end (exclusive) and level of each run of equal values."""
    cut = np.flatnonzero(np.diff(q)) + 1
    st = np.concatenate([[0], cut]); en = np.concatenate([cut, [len(q)]])
    return st, en, q[st]


class Profiler:
    def __init__(self, W, D, H, log=print):
        self.W, self.D, self.H, self.log = W, D, H, log
        self.G = RG.Grader(W, D, H, log=lambda *a: None)

    def scan_region(self, ox, oz, g, dm, solid_lut, carr_lut, block_lut):
        self.G.scan_region(ox, oz, g, dm, solid_lut, carr_lut, block_lut)

    # ---- per column: what stands on a surface, what is over it, how thick the ground under it is
    def _columns(self, pix, y0):
        G = self.G
        occ = np.stack([G.occupied(pix, y0 + k, y0 + k) for k in range(1, 15)])   # [14, n]
        n = len(pix)
        stuff = np.zeros(n, np.int64)          # blocks standing on the surface without a gap
        alive = np.ones(n, bool)
        for k in range(14):
            alive &= occ[k]; stuff += alive
        ceil = np.full(n, 10 ** 6, np.int64)   # first block over a gap above the stuff
        seen_gap = np.zeros(n, bool)
        for k in range(14):
            seen_gap |= ~occ[k]
            hit = seen_gap & occ[k] & (ceil == 10 ** 6)
            ceil[hit] = y0[hit] + k + 1
        room = np.minimum(ceil - y0 - 1 - HEAD, ceil - (y0 + stuff) - 1)
        room = np.where(ceil == 10 ** 6, MAXD, np.clip(room, 0, MAXD))   # never forced down by what is over it
        thick = np.zeros(n, np.int64); alive = np.ones(n, bool)
        for k in range(1, MAXD + 1):
            alive &= G.occupied(pix, y0 - k, y0 - k); thick += alive
        return stuff, room, thick

    def _sealed(self, ops, st):
        """Drops the lowerings that would open the hollow under a neighbour's ground to the sky: every block of the
        column that turns to air must have beside it a block, or a neighbour with nothing over that height (open
        air). Read from the world as it was (so the answer does not depend on the order of the moves, and holds across
        region edges); a lowered neighbour's new air is road air, open like this one's."""
        G, W, D = self.G, self.W, self.D
        if not hasattr(self, "topocc"):
            lut = np.zeros(256, np.int64)
            for v in range(1, 256): lut[v] = 7 - ((v & -v).bit_length() - 1)   # the highest y of a byte (MSB = y0)
            top = np.full((D, W), -1, np.int64)
            for L in range(G.occ.shape[0] - 1, -1, -1):
                b = G.occ[L]; hit = (b != 0) & (top < 0)
                top[hit] = L * 8 + lut[b[hit]]
            self.topocc = top
        low = np.flatnonzero(ops[:, 2] < 0)
        if not len(low): return ops
        p, y0, e = ops[low, 0], ops[low, 1], -ops[low, 2]
        z, x = np.divmod(p, W)
        ok = np.ones(len(low), bool)
        for k in range(MAXD):
            act = k < e
            y = y0 - k                                       # turns to air unless what stood on it comes down there
            filled = G.occupied(p, y + e, y + e)
            need = act & ~filled
            for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nz, nx = np.clip(z + dz, 0, D - 1), np.clip(x + dx, 0, W - 1)
                npix = nz * W + nx
                fine = G.occupied(npix, y, y) | (self.topocc[nz, nx] <= y)
                ok &= ~need | fine
        st["refused_hollow"] = int((~ok).sum())
        return np.delete(ops, low[~ok], axis=0)

    def run(self, osm_path, world, origin=(0, 0)):
        G, W = self.G, self.W
        t0 = time.time()
        if os.environ.get("ROAD_PROFILE_SCAN"):   # the scan, to run the profile again without the world (debugging)
            np.savez_compressed(os.environ["ROAD_PROFILE_SCAN"], lev=G.lev, carr=G.carr, ground=G.ground, occ=G.occ,
                                dims=np.array([self.W, self.D, self.H, origin[0], origin[1]]))
        pj = RG.projection(world)
        xz = lambda lon, lat: tuple(v - o for v, o in zip(pj(lon, lat), origin))
        ways, pos, _ = RG.load_ways(osm_path, xz)
        G.build(ways, pos)
        C = G.C
        st = dict(chains=len(C), tracked=0, junctions=0, partners=0, cols_up=0, cols_down=0, cols_level=0,
                  cols_blocked=0, verge_cols=0, deck_spans=0, decks_levelled=0, junctions_sloped=0)
        # ---- 1. per chain: its level per block of length, its columns, and what each candidate level costs
        NO = 2 * MAXD + 1                        # candidate levels per bin: the baked level -MAXD .. +MAXD
        for c in C:
            gb = G.ground_of(c)
            h = G.track(c, gb)
            c["ok"] = not np.isnan(h).all()
            if not c["ok"]: continue
            st["tracked"] += 1
            nb, b = c["nb"], c["b"]
            hr = np.floor(ndimage.median_filter(h, size=min(DESPIKE, nb), mode="nearest") + 0.5).astype(np.int64)
            br = np.asarray(c["bridge"], bool)
            brb = br[np.minimum(len(br) - 1, np.searchsorted(c["s"], np.arange(nb) + 0.5))]
            # the chain's columns at its own level (the surface nearest the followed level)
            pix, plat, phw = c["pix"], c["plat"], c["phw"]
            lv, _ = G.levels_at(pix)
            dist = np.where(lv >= 0, np.abs(lv - h[b][None]), 99)
            k = dist.argmin(0); idx = np.arange(len(pix))
            y0 = lv[k, idx].astype(np.int64)
            road = (dist[k, idx] <= MAXD) & ((np.abs(plat) <= phw + 0.5) | (brb[b] & (np.abs(plat) <= phw + 2.5)))
            pix, y0, bb, plat, phw = pix[road], y0[road], b[road], plat[road], phw[road]
            stuff, room, thick = self._columns(pix, y0)
            ok = stuff <= STUFF_ROAD
            # cost of each candidate level: the mean misfit of the bin's columns; a column further than MAXD off, or
            # one that cannot follow (no room over it to rise, too little ground under it to sink, too much on it)
            # costs most
            cost = np.zeros((nb, NO)); cnt = np.zeros(nb)
            np.add.at(cnt, bb, 1)
            for o in range(NO):
                dl = hr[bb] + o - MAXD - y0
                can = ok & (dl <= room) & (-dl <= thick)
                np.add.at(cost[:, o], bb, np.where(can | (dl == 0), np.minimum(np.abs(dl), MAXD + 1), MAXD + 1))
            cost /= np.maximum(cnt, 1)[:, None]
            lo = hr - MAXD
            nostep = np.zeros(nb, bool)
            for s0, e0, v in zip(*_runs(brb.astype(np.int64))):
                if not v: continue
                st["deck_spans"] += 1
                lo[s0:e0] = np.maximum(lo[s0:e0], hr[s0:e0])       # a deck is never lowered
                if hr[s0:e0].max() - hr[s0:e0].min() <= 1:          # a level deck stays level
                    nostep[s0 + 1:e0] = True; st["decks_levelled"] += 1
            Ls = hr[:, None] + np.arange(NO)[None] - MAXD
            bad = Ls < lo[:, None]
            c.update(h=h, hr=hr, brb=brb, cols=(pix, y0, bb, plat, phw, ok), cost=cost, bad=bad, nostep=nostep)
        C = [c for c in C if c["ok"]]
        self.C = C
        # ---- 2. side-by-side carriageways: each bin also weighs the columns of its partner's bin
        pts, cid, bix, dirs, hws, lev, br = [], [], [], [], [], [], []
        for ci, c in enumerate(C):
            nb = c["nb"]; arc = np.arange(nb) + 0.5
            P = c["pts"]; s = c["s"]
            x = np.interp(arc, s, P[:, 0]); z = np.interp(arc, s, P[:, 1])
            seg = np.clip(np.searchsorted(s, arc) - 1, 0, len(P) - 2)
            d = P[seg + 1] - P[seg]; d /= np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
            pts.append(np.stack([x, z], 1)); cid.append(np.full(nb, ci)); bix.append(np.arange(nb)); dirs.append(d)
            hws.append(np.maximum(c["hw"][np.minimum(seg + 1, len(c["hw"]) - 1)], c["hw"][seg]).astype(float))
            lev.append(c["hr"]); br.append(c["brb"])
        if C:
            pts, cid, bix, dirs, hws, lev, br = map(np.concatenate, (pts, cid, bix, dirs, hws, lev, br))
            cost_all = np.concatenate([c["cost"] for c in C])
            tree = cKDTree(pts)
            pr = tree.query_pairs(2 * 8 + PARALLEL_GAP + 1, output_type="ndarray")   # 8: road_grade's widest half
            if len(pr):
                pr = np.concatenate([pr, pr[:, ::-1]])
                a, b = pr[:, 0], pr[:, 1]
                dd = pts[b] - pts[a]
                cosd = np.abs((dirs[a] * dirs[b]).sum(1))
                lat = np.abs(dd[:, 0] * dirs[a][:, 1] - dd[:, 1] * dirs[a][:, 0])
                along = np.abs(dd[:, 0] * dirs[a][:, 0] + dd[:, 1] * dirs[a][:, 1])
                ok = ((cid[a] != cid[b]) & (cosd >= PARALLEL_COS) & (lat <= hws[a] + hws[b] + PARALLEL_GAP) & (along <= 1)
                      & (np.abs(lev[a] - lev[b]) <= 1) & (br[a] == br[b]))
                a, b, along = a[ok], b[ok], along[ok]
                o = np.lexsort((along, cid[b], a)); a, b = a[o], b[o]
                first = np.concatenate([[True], (a[1:] != a[:-1]) | (cid[b][1:] != cid[b][:-1])]) if len(a) else np.zeros(0, bool)
                a, b = a[first], b[first]
                st["partners"] = int(len(np.unique(a)))
                tot = cost_all.copy(); n = np.ones(len(cost_all))
                sh = lev[b] - lev[a]                      # partner's level offset index o_b = o_a - sh
                for o in range(NO):
                    ob = o - sh
                    v = np.where((ob >= 0) & (ob < NO), cost_all[b, np.clip(ob, 0, NO - 1)], MAXD + 1)
                    np.add.at(tot[:, o], a, v)
                np.add.at(n, a, 1)
                cost_all = tot / n[:, None]
                off = np.cumsum([0] + [c["nb"] for c in C])
                for ci, c in enumerate(C): c["cost"] = cost_all[off[ci]:off[ci + 1]]
        # ---- 3. junctions: one level for every chain through a junction node, held over its width
        at, seen = {}, {}
        for ci, c in enumerate(C):
            for nd in set(c["nodes"]) - c.get("cut", set()): seen[nd] = seen.get(nd, 0) + 1
        shared = {nd for nd, n in seen.items() if n > 1} | set(G.junction)   # also where two chains just meet
        for ci, c in enumerate(C):
            for k, nd in enumerate(c["nodes"]):
                if nd in shared and nd not in c.get("cut", set()):
                    at.setdefault(nd, []).append((ci, float(c["s"][k]), float(c["hw"][k])))
        for c in C: c["J"] = np.full(c["nb"], -10 ** 6, np.int64)
        for nd, lst in at.items():
            r = max(hw for _, _, hw in lst) + JUNCTION_PAD
            spans = []
            for ci, a, _ in lst:
                c = C[ci]; bins = np.flatnonzero(np.abs(np.arange(c["nb"]) + 0.5 - a) <= r)
                if len(bins): spans.append((ci, bins, a))
            if not spans: continue
            # held level over its width where the roads meet level (a junction on a slope, or ramps that merge on a
            # climbing deck, only share the height at the node itself)
            flat = all(np.ptp(C[ci]["hr"][bins]) <= 1 for ci, bins, _ in spans)
            st["junctions_sloped"] += not flat
            if not flat:
                spans = [(ci, bins[np.abs(bins + 0.5 - a) <= 1], a) for ci, bins, a in spans]
                spans = [t for t in spans if len(t[1])]
                if not spans: continue
            levels = np.unique(np.concatenate([C[ci]["hr"][bins] for ci, bins, _ in spans]))
            levels = np.arange(levels.min() - MAXD, levels.max() + MAXD + 1)
            tot = np.zeros(len(levels))
            for ci, bins, _ in spans:
                c = C[ci]
                for o in range(NO):
                    L = c["hr"][bins] + o - MAXD
                    v = np.where(c["bad"][bins, o], 50.0, c["cost"][bins, o])
                    np.add.at(tot, L - levels[0], v)
                    # a level outside a bin's candidates is as bad as a forbidden one
                miss = np.ones((len(bins), len(levels)), bool)
                miss[np.arange(len(bins))[:, None], (c["hr"][bins][:, None] + np.arange(NO)[None] - MAXD) - levels[0]] = False
                tot += 50.0 * miss.sum(0)
            J = int(levels[tot.argmin()])
            st["junctions"] += 1
            for ci, bins, a in spans:
                c = C[ci]
                inb = np.abs(J - c["hr"][bins]) <= MAXD
                c["J"][bins[inb]] = J
        # ---- 4./5. the profile of each chain: the levels that fit its columns best, with few steps
        claims = []   # (pix, y0, target, priority, bridge, movable)
        for ci, c in enumerate(C):
            nb, hr = c["nb"], c["hr"]
            # (a hair more for each block away from the followed level: where two levels fit as well, as for two
            # coupled carriageways a block apart, each keeps its own, so neither is moved to the other's)
            cost = np.where(c["bad"], np.inf, c["cost"] + 1e-3 * np.abs(np.arange(NO) - MAXD)[None])
            fixed = c["J"] > -10 ** 6
            if fixed.any():
                fo = c["J"][fixed] - hr[fixed] + MAXD
                cf = np.full((int(fixed.sum()), NO), np.inf); cf[np.arange(len(fo)), fo] = 0.0
                cost[fixed] = cf
            q = hr + _profile(cost, hr, c["nostep"] | np.concatenate([[False], fixed[1:] & fixed[:-1]]), MINRUN, MINSTAIR, STEP_COST) - MAXD
            c["q"] = q
            pix, y0, bb, plat, phw, ok = c["cols"]
            pri = np.abs(plat) / np.maximum(phw, 0.5)
            claims.append(np.stack([pix, y0, q[bb], np.round(pri * 1000).astype(np.int64), c["brb"][bb].astype(np.int64),
                                    ok.astype(np.int64)], 1))
        if os.environ.get("ROAD_PROFILE_DEBUG"):   # the chains' profiles, for looking into a result
            import pickle
            pickle.dump([dict(ways=c["ways"], pts=c["pts"], s=c["s"], h=c["h"], hr=c["hr"], q=c["q"], J=c["J"], brb=c["brb"],
                              hw=c["hw"], cost=c["cost"], bad=c["bad"], nostep=c["nostep"]) for c in C],
                        open(os.environ["ROAD_PROFILE_DEBUG"], "wb"))
        claims = np.concatenate(claims) if claims else np.zeros((0, 6), np.int64)
        # one claim per (column, level): the chain whose centre line is nearest (relative to its width)
        o = np.lexsort((claims[:, 3], claims[:, 1], claims[:, 0])); claims = claims[o]
        first = np.concatenate([[True], (claims[1:, 0] != claims[:-1, 0]) | (claims[1:, 1] != claims[:-1, 1])]) if len(claims) else np.zeros(0, bool)
        claims = claims[first]
        d = claims[:, 2] - claims[:, 1]
        d = np.clip(d, -MAXD, MAXD)
        blocked = claims[:, 5] == 0
        st["cols_blocked"] = int((blocked & (d != 0)).sum())
        d[blocked] = 0
        st["cols_up"] = int((d > 0).sum()); st["cols_down"] = int((d < 0).sum()); st["cols_level"] = int((d == 0).sum())
        ops = [np.stack([claims[:, 0], claims[:, 1], d, np.full(len(d), STUFF_ROAD)], 1)[d != 0]]
        # ---- 7. verges
        D = self.D
        roadm = np.zeros(D * W, bool); roadm[claims[:, 0]] = True
        dmap = np.zeros(D * W, np.int64); ymap = np.full(D * W, -1, np.int64)
        at_grade = claims[:, 4] == 0
        dmap[claims[at_grade, 0]] = d[at_grade]; ymap[claims[at_grade, 0]] = claims[at_grade, 1]
        roadm = roadm.reshape(D, W)
        if roadm.any() and (dmap != 0).any():
            dist, (iz, ix) = ndimage.distance_transform_edt(~roadm, return_indices=True)
            near = (dist > 0) & (dist <= VERGE + 0.5)
            vz, vx = np.nonzero(near)
            src = iz[vz, vx] * W + ix[vz, vx]
            ds_ = dmap[src]; dv = np.floor(ds_ * (VERGE + 1 - dist[vz, vx]) / (VERGE + 1) + 0.5).astype(np.int64)
            sel = (dv != 0) & (ymap[src] >= 0)
            vp = vz[sel] * W + vx[sel]; dv = dv[sel]; yref = ymap[src][sel]
            lv, _ = G.levels_at(vp)
            dist2 = np.where(lv >= 0, np.abs(lv - yref[None]), 99)
            k = dist2.argmin(0)
            vy = lv[k, np.arange(len(vp))].astype(np.int64)
            ok = dist2[k, np.arange(len(vp))] <= 2
            vp, vy, dv = vp[ok], vy[ok], dv[ok]
            if len(vp):
                stuff, room, thick = self._columns(vp, vy)
                ok = (stuff <= STUFF_VERGE) & np.where(dv > 0, dv <= room, -dv <= thick)
                vp, vy, dv = vp[ok], vy[ok], dv[ok]
            st["verge_cols"] = int(len(vp))
            ops.append(np.stack([vp, vy, dv, np.full(len(vp), STUFF_VERGE)], 1))
        ops = np.concatenate(ops).astype(np.int64)
        ops = self._sealed(ops, st)
        # per column, the highest surface first
        o = np.lexsort((-ops[:, 1], ops[:, 0])); ops = ops[o]
        z, x = np.divmod(ops[:, 0], W)
        self.ops = np.stack([z, x, ops[:, 1], ops[:, 2], ops[:, 3]], 1).astype(np.int32)
        st["seconds"] = round(time.time() - t0, 1)
        self.st = st
        return st

    def apply_region(self, ox, oz, g, dm, ds, soft):
        """soft: materials (leaves, water) that count as air over a surface and are overwritten by what moves there."""
        self.soft = soft
        P = g.shape[1]; a = self.ops
        m = (a[:, 0] >= oz) & (a[:, 0] < oz + P) & (a[:, 1] >= ox) & (a[:, 1] < ox + P)
        r = a[m].copy(); r[:, 0] -= oz; r[:, 1] -= ox
        if not len(r): return 0, 0
        res = _move(g, dm, ds, r, HEAD, self.soft)
        self.refused = getattr(self, "refused", 0) + int((res == 0).sum())
        return int((res > 0).sum()), int((res < 0).sum())


from numba import njit


@njit(cache=True)
def _move(g, dm, ds, ops, head, soft):
    """Moves the surface at (z, x, y0) by d (ops rows: z, x, y0, d, most blocks standing on it): +d stretches the column
    (the stack on the surface moves up, the block under the surface fills the gap), -d compresses it (the stack moves
    down into the ground, whose bottom stays). Returns per op: d when done, 0 when refused (not a surface, too much on
    it, no room over it, too little ground under it)."""
    H = g.shape[0]
    out = np.zeros(ops.shape[0], np.int32)
    for i in range(ops.shape[0]):
        z, x, y0, d, smax = ops[i, 0], ops[i, 1], ops[i, 2], ops[i, 3], ops[i, 4]
        if y0 <= 1 or y0 >= H - 1 or g[y0, z, x] == 0: continue
        t = y0
        while t + 1 < H and ((g[t + 1, z, x] != 0 and not soft[g[t + 1, z, x]]) or dm[t + 1, z, x] != 0) and t - y0 < smax + 1: t += 1
        if t - y0 > smax: continue
        if d > 0:
            top = max(t + d, y0 + d + head)
            if top >= H: continue
            free = True
            for y in range(t + 1, top + 1):
                if (g[y, z, x] != 0 and not soft[g[y, z, x]]) or dm[y, z, x] != 0: free = False; break
            if not free: continue
            f = g[y0 - 1, z, x] if g[y0 - 1, z, x] != 0 else g[y0, z, x]
            for y in range(t, y0, -1):
                g[y + d, z, x] = g[y, z, x]; dm[y + d, z, x] = dm[y, z, x]; ds[y + d, z, x] = ds[y, z, x]
            g[y0 + d, z, x] = g[y0, z, x]; dm[y0 + d, z, x] = 0; ds[y0 + d, z, x] = 0
            for y in range(y0, y0 + d):
                g[y, z, x] = f; dm[y, z, x] = 0; ds[y, z, x] = 0
            out[i] = d
        elif d < 0:
            e = -d
            ok = y0 - e >= 1
            for k in range(1, e + 1):
                if not ok: break
                if g[y0 - k, z, x] == 0: ok = False
            if not ok: continue
            for y in range(y0, t + 1):
                g[y - e, z, x] = g[y, z, x]; dm[y - e, z, x] = dm[y, z, x]; ds[y - e, z, x] = ds[y, z, x]
            for y in range(t - e + 1, t + 1):
                g[y, z, x] = 0; dm[y, z, x] = 0; ds[y, z, x] = 0
            out[i] = d
    return out


INF = 1e18


@njit(cache=True)
def _profile(cost, hr, nostep, minrun, minstair, step_cost):
    """The profile of one chain: per bin an offset o (level hr + o - MAXD) minimising the summed cost plus step_cost per
    step of one block (six times that for two blocks at once), never inside a nostep bin; a step back the other way
    sooner than minrun bins after the last step (a hump or a dip) costs BUMP_COST more, and one the same way sooner
    than minstair bins a step_cost more. Dynamic programme over (offset, bins since the last step, its direction)."""
    nb, NO = cost.shape
    R = minrun + 1
    dp = np.full((NO, R, 3), INF); nd = np.full((NO, R, 3), INF)
    back = np.zeros((nb, NO, R, 3), np.int32)
    for o in range(NO):
        dp[o, R - 1, 0] = cost[0, o] if cost[0, o] < INF else 1e6
    for b in range(1, nb):
        nd[:] = INF
        sh = hr[b] - hr[b - 1]
        for o in range(NO):
            for r in range(R):
                for d in range(3):
                    v = dp[o, r, d]
                    if v >= INF: continue
                    L = o - sh                        # the same level, as an offset of bin b
                    for dl in (-2, -1, 0, 1, 2):
                        o2 = L + dl
                        if o2 < 0 or o2 >= NO: continue
                        c = cost[b, o2]
                        if c >= INF: continue
                        if dl == 0:
                            r2 = min(r + 1, R - 1); d2 = d; add = 0.0
                        else:
                            if nostep[b]: continue
                            d2 = 1 if dl > 0 else 2
                            add = step_cost * (1 if abs(dl) == 1 else 6)
                            if d != 0 and d != d2 and r < minrun: add += BUMP_COST
                            if d == d2 and r < minstair: add += step_cost
                            r2 = 0
                        t = v + c + add
                        if t < nd[o2, r2, d2]:
                            nd[o2, r2, d2] = t; back[b, o2, r2, d2] = (o * R + r) * 3 + d
        # nothing reachable (the followed level jumps by more than two blocks, or every level is ruled out): start
        # over from the cheapest level, as at the first bin
        if nd.min() >= INF:
            best = 0
            bv = INF
            for o in range(NO):
                for r in range(R):
                    for d in range(3):
                        if dp[o, r, d] < bv: bv = dp[o, r, d]; best = (o * R + r) * 3 + d
            if bv >= INF: bv = 0.0
            for o in range(NO):
                c = cost[b, o]
                if c >= INF: c = 1e6
                nd[o, R - 1, 0] = bv + c; back[b, o, R - 1, 0] = best
        dp[:] = nd
    out = np.zeros(nb, np.int64)
    bv = INF; cur = 0
    for o in range(NO):
        for r in range(R):
            for d in range(3):
                if dp[o, r, d] < bv: bv = dp[o, r, d]; cur = (o * R + r) * 3 + d
    for b in range(nb - 1, -1, -1):
        o = cur // (3 * R)
        out[b] = o
        if b > 0: cur = back[b, o, (cur // 3) % R, cur % 3]
    return out
