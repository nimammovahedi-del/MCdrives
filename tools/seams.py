"""Chunk seams for build.py join(): one owner per column where neighbouring chunks overlap, the ground of the two
chunks blended into one another across the seam, and the faces a step there needs.

Neighbouring chunks of an area overlap by a few blocks (build.py lap()), and each bake fits the terrain to its own base
level, which join() evens out with one lift per chunk. That leaves three faults along every seam:
- Both copies of the overlap were drawn: equal surfaces fight (a flickering line), and the overlap holds each bake's
  last row or column, where Arnis leaves stray water on coastal chunks (a broken line of water across a road).
- The two bakes do not agree on the ground to the block. The step across a seam wanders a block or two either side of
  the lift on open ground (the bakes sample the laser heights on slightly different grids), and on a road it can stay
  off by three or more blocks for hundreds of metres (each bake levels the road on its own).
- A bake draws no face towards the outside of its own world, so the riser of any such step was missing: one to three
  blocks of sky showed through the ground along the seam, across roads and grass alike.

sew() fixes all three, and only rewrites the packs within BLEND blocks of a seam; the rest are linked as before:
1. Blend: per line across the seam, the local disagreement (median over the overlap, then a running median along the
   seam, so only a steady offset counts and the one-block noise does not) is split between the two chunks: each moves
   its columns up or down by its share at the seam, tapering to nothing BLEND blocks into the chunk. Faces, partial
   blocks and heights of a column move with it (greedy quads are cut where the shift changes).
2. Owner: a column covered by exactly the two chunks of a seam goes to the chunk on its side of a cut through the
   overlap. Per line the cut sits where the blended surfaces meet with the smallest step (ties go to the nominal
   boundary, the south or east chunk's origin), and it never gives the north or west chunk its own last line. Any
   other shared column (where three or four chunks meet) goes to the chunk whose grid slot holds it, or, if that chunk
   was not built, to the most south-easterly chunk covering it. Everything else of the other chunks is cut away there.
3. Risers: wherever two side-by-side columns end up with different ground (two owners, or one chunk whose shift
   changes between them), the higher column gets the side faces it lacks down to the lower one's ground, in the
   colour of its top block.
Decks: a column where a deck stands over a lower surface (a bridge over a road) has the lower surface as its ground,
which the other bake may lack there (its ground is then the deck): columns where only one bake has two surfaces are
left out of the blend's
disagreement (else the blend moved the deck up on one side and down on the other by the bridge's height, split), and
the cut through them goes where the decks meet. And each chunk grades a deck or ramp that crosses into its neighbour
on its own (tools/road_grade.py), so the two sides can still meet at different levels: lift_roads() follows every
raised road across a seam on the joined road graph and lifts its lower side to an even grade (only ever up, only
the carriageway, only the deck where a road passes under it).
"""
import gzip
from collections import OrderedDict
import numpy as np

BLEND = 16      # blocks over which a chunk's share of the step tapers to nothing
SMOOTH = 25     # lines in the running median of the step along a seam
MAXSHIFT = 24   # a larger disagreement than this is not blended (it would be a ramp, not a seam)
DIRT = 0        # material id for a riser whose top block is not found; set by build.py from the material table
# raised roads across a seam (lift_roads()): each chunk grades its decks and ramps alone, so where one crosses a seam
# the two sides can stand at different levels (or one side dips under the other's ramp); the lower side is lifted
GRADE = 1 / 12  # steepest fall of the lifted road away from the level it is lifted to (road_grade.py GRADE)
LIFT_SRC = 40   # blocks along the road either side of the seam whose levels the road is held to
LIFT_MAX = 10   # never lift a road surface by more than this
LIFT_REACH = LIFT_SRC + LIFT_MAX * 12   # how far along the road from the seam a lift can reach
DECK_GAP = 3    # a column whose highest surface stands this many blocks over its lowest is a deck over a lower surface
NOLIFT = -10 ** 6   # floor of a column that moves whole when lifted

QT = np.dtype([("x", "u1"), ("z", "u1"), ("y", "<u2"), ("w", "u1"), ("h", "u1"), ("d", "u1"), ("m", "u1")])
BT = np.dtype([("x", "u1"), ("z", "u1"), ("y", "<u2"), ("m", "u1"), ("x0", "u1"), ("y0", "u1"), ("z0", "u1"),
               ("x1", "u1"), ("y1", "u1"), ("z1", "u1"), ("pad", "u1")])
NONE = -1000


def decode(blob):
    """A tile blob -> (greedy quads, partial-block boxes), as structured arrays."""
    raw = gzip.decompress(blob)
    n = int.from_bytes(raw[:4], "little")
    q = np.frombuffer(raw, QT, n, 4).copy()
    b = np.frombuffer(raw, BT, (len(raw) - 4 - 8 * n) // 12, 4 + 8 * n).copy()
    return q, b


def encode(q, b):
    return gzip.compress(len(q).to_bytes(4, "little") + q.tobytes() + b.tobytes(), 6)


def footprint(q):
    """Column rectangle (x0, z0, x1, z1), tile-local, of each quad. Viewer layout: +-y faces span w along x and h along
    z; +-z faces span w along x (h is up); +-x faces span w along z."""
    x0, z0 = q["x"].astype(np.int32), q["z"].astype(np.int32)
    w, h, d = q["w"].astype(np.int32), q["h"].astype(np.int32), q["d"]
    x1 = x0 + np.where(d < 4, w, 1)
    z1 = z0 + np.where(d < 2, h, np.where(d < 4, 1, w))
    return x0, z0, x1, z1


def rects(mask):
    """Greedy cover of a small boolean mask by rectangles (z0, x0, z1, x1)."""
    m = mask.copy(); out = []
    Z, X = m.shape
    for z in range(Z):
        x = 0
        while x < X:
            if not m[z, x]: x += 1; continue
            w = 1
            while x + w < X and m[z, x + w]: w += 1
            h = 1
            while z + h < Z and m[z + h, x:x + w].all(): h += 1
            m[z:z + h, x:x + w] = False
            out.append((z, x, z + h, x + w)); x += w
    return out


def reshape(q, b, own, shift):
    """Quads and boxes kept on owned columns, each moved up by its column's shift (own, shift: [64, 64] tile-local
    [z, x]). A quad over columns of different shift, or partly unowned, is cut into pieces. Returns (q, b, cut)."""
    if not own.any(): return q[:0], b[:0], len(q)
    key = np.where(own, shift, -10 ** 6).astype(np.int64)
    x0, z0, x1, z1 = footprint(q)
    # a quad is uniform when the min and max key over its footprint agree (2D sparse tables are overkill at 64 x 64:
    # compare against the key at its first column with a prefix sum of "differs")
    k0 = key[z0, x0]
    out = []; uni = np.ones(len(q), bool)
    multi = np.nonzero(((x1 - x0) > 1) | ((z1 - z0) > 1))[0]
    for k in multi:
        if (key[z0[k]:z1[k], x0[k]:x1[k]] != k0[k]).any(): uni[k] = False
    keep = q[uni & (k0 > -10 ** 6)].copy()
    keep["y"] = np.maximum(0, keep["y"].astype(np.int32) + key[z0, x0][uni & (k0 > -10 ** 6)]).astype(np.uint16)
    for k in np.nonzero(~uni)[0]:
        sub = key[z0[k]:z1[k], x0[k]:x1[k]]; r = q[k]; d = int(r["d"])
        for v in np.unique(sub):
            if v <= -10 ** 6: continue
            for a0, c0, a1, c1 in rects(sub == v):
                s = r.copy(); s["x"] = x0[k] + c0; s["z"] = z0[k] + a0; s["y"] = max(0, int(r["y"]) + int(v))
                if d < 2: s["w"], s["h"] = c1 - c0, a1 - a0
                elif d < 4: s["w"] = c1 - c0
                else: s["w"] = a1 - a0
                out.append(s)
    if out: keep = np.concatenate([keep, np.array(out, dtype=QT)])
    if len(b):
        bk = b[own[b["z"], b["x"]]].copy()
        bk["y"] = np.maximum(0, bk["y"].astype(np.int32) + shift[bk["z"], bk["x"]]).astype(np.uint16)
    else: bk = b
    return keep, bk, int((~uni).sum() + (k0 <= -10 ** 6).sum())


def reshape_lift(q, b, own, shift, lift, floor):
    """reshape() plus a road lift: in a column with lift L, faces at or above its floor (tile-local y, NOLIFT for the
    whole column) move up L more; a side face reaching across the floor is stretched up by L instead, so a pier stays
    joined to the deck it carries. Quads over lifted columns are cut into one-column pieces."""
    lk = lift > 0
    if not lk.any(): return reshape(q, b, own, shift)
    x0, z0, x1, z1 = footprint(q)
    cs = np.pad(lk.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    touch = (cs[z1, x1] - cs[z0, x1] - cs[z1, x0] + cs[z0, x0]) > 0
    keep, bk, cut = reshape(q[~touch], b[:0], own, shift)
    out = []
    for k in np.nonzero(touch)[0]:
        r = q[k]; d = int(r["d"]); y = int(r["y"]); h = int(r["h"])
        for zz in range(z0[k], z1[k]):
            for xx in range(x0[k], x1[k]):
                if not own[zz, xx]: continue
                s = r.copy(); s["x"] = xx; s["z"] = zz
                L, F, sh = int(lift[zz, xx]), int(floor[zz, xx]), int(shift[zz, xx])
                if d < 2:
                    s["w"], s["h"] = 1, 1; s["y"] = max(0, y + sh + (L if y >= F else 0))
                else:
                    s["w"] = 1
                    if y >= F: s["y"] = max(0, y + sh + L)
                    elif y + h - 1 >= F and L:
                        s["y"] = max(0, y + sh); hh = h + L
                        while hh > 255:   # h is one byte: stack pieces
                            t = s.copy(); t["h"] = 255; out.append(t); s["y"] = int(s["y"]) + 255; hh -= 255
                        s["h"] = hh
                    else: s["y"] = max(0, y + sh)
                out.append(s)
        cut += 1
    if out: keep = np.concatenate([keep, np.array(out, dtype=QT)])
    if len(b):
        bk = b[own[b["z"], b["x"]]].copy()
        yy = bk["y"].astype(np.int32)
        L = lift[bk["z"], bk["x"]]; F = floor[bk["z"], bk["x"]]
        bk["y"] = np.maximum(0, yy + shift[bk["z"], bk["x"]] + np.where(yy >= F, L, 0)).astype(np.uint16)
    return keep, bk, cut


def runmed(v, n):
    """Running median of v (NaN = no data) over n samples; 0 where a window has no data."""
    out = np.zeros(len(v))
    h = n // 2
    for i in range(len(v)):
        w = v[max(0, i - h):i + h + 1]; w = w[~np.isnan(w)]
        if len(w): out[i] = np.median(w)
    return out


class Chunks:
    """The chunks of a join in the built world's frame. Each entry of info (build.py join()) must carry:
    sx, sz, sy (placement), ext (bx0, bz0, bx1, bz1: the built columns its Arnis world really covers),
    slot (bx0, bz0, bx1, bz1: its cell of the area grid), row, col, id, dir, meta."""

    def __init__(self, info, read_h):
        self.info = info; self.read_h = read_h
        self.cache = OrderedDict(); self.seams = []

    def tile(self, k, p):
        key = (k, p["h"])
        if key not in self.cache:
            P = self.info[k]["meta"]["pack"]
            self.cache[key] = self.read_h(self.info[k], p).reshape(2, P, P)
            if len(self.cache) > 24: self.cache.popitem(last=False)
        return self.cache[key]

    def height(self, k, X0, Z0, X1, Z1):
        """Chunk k's ground over built columns [Z0:Z1, X0:X1], in the joined frame (+ sy), before any blend; NONE where
        it has none. The ground is the lowest drivable surface (height layer 0), or where that is the world floor (sky
        reaches under the terrain shell, as it does under much of a hilly bake) the highest one (layer 1), as the
        viewer's groundY() reads it."""
        i = self.info[k]; P = i["meta"]["pack"]
        out = np.full((Z1 - Z0, X1 - X0), NONE, np.int32)
        for p in i["meta"]["packs"]:
            px, pz = i["sx"] + p["x"], i["sz"] + p["z"]
            ax, az, bx, bz = max(X0, px), max(Z0, pz), min(X1, px + P), min(Z1, pz + P)
            if bx <= ax or bz <= az: continue
            h = self.tile(k, p)[:, az - pz:bz - pz, ax - px:bx - px].astype(np.int32)
            t = np.where(h[0] > 1, h[0], h[1])
            out[az - Z0:bz - Z0, ax - X0:bx - X0] = np.where(t > 1, t + i["sy"], NONE)
        return out

    def shift(self, k, X0, Z0, X1, Z1):
        """How far chunk k's columns in [Z0:Z1, X0:X1] move up for the blend."""
        out = np.zeros((Z1 - Z0, X1 - X0), np.int32)
        for s in self.seams:
            if k not in (s["a"], s["b"]): continue
            lo, hi, first = s["lo"], s["hi"], s["first"]
            amt = s["da"] if k == s["a"] else s["db"]
            if s["ax"] == 0: L, A = np.arange(X0, X1), np.arange(Z0, Z1)   # lines along x, across along z
            else: L, A = np.arange(Z0, Z1), np.arange(X0, X1)
            ins = (L >= lo) & (L < hi)
            if not ins.any(): continue
            d = (first - 1 - A) if k == s["a"] else (A - first)            # distance into the chunk from the boundary
            wgt = np.clip(1 - np.maximum(d, 0) / BLEND, 0, 1)
            a = np.zeros(len(L)); a[ins] = amt[L[ins] - lo]
            v = np.rint(a[:, None] * wgt[None, :]).astype(np.int32)       # [line, across]
            out += v.T if s["ax"] == 0 else v
        return out

    def ground(self, k, X0, Z0, X1, Z1):
        """Chunk k's ground after the blend."""
        h = self.height(k, X0, Z0, X1, Z1)
        return np.where(h > NONE, h + self.shift(k, X0, Z0, X1, Z1), NONE)

    def deck(self, k, X0, Z0, X1, Z1):
        """Where chunk k has a deck over a lower surface (DECK_GAP or more between its lowest and highest surface)."""
        h = self.layers(k, X0, Z0, X1, Z1)
        return (h[0] > 1) & (h[1] - h[0] >= DECK_GAP)

    def top(self, k, X0, Z0, X1, Z1):
        """Chunk k's highest surface after the blend, in the joined frame (NONE where none)."""
        h = self.layers(k, X0, Z0, X1, Z1)[1]
        return np.where(h > 1, h + self.info[k]["sy"] + self.shift(k, X0, Z0, X1, Z1), NONE)

    def layers(self, k, X0, Z0, X1, Z1):
        """Chunk k's raw height layers [2, Z, X] over built columns (chunk-local y, -1 where none)."""
        i = self.info[k]; P = i["meta"]["pack"]
        out = np.full((2, Z1 - Z0, X1 - X0), -1, np.int32)
        for p in i["meta"]["packs"]:
            px, pz = i["sx"] + p["x"], i["sz"] + p["z"]
            ax, az, bx, bz = max(X0, px), max(Z0, pz), min(X1, px + P), min(Z1, pz + P)
            if bx <= ax or bz <= az: continue
            out[:, az - Z0:bz - Z0, ax - X0:bx - X0] = self.tile(k, p)[:, az - pz:bz - pz, ax - px:bx - px]
        return out

    def floor(self, k, X0, Z0, X1, Z1):
        """Chunk-local y from which a lifted column of chunk k moves: just over the lower surface where a deck stands
        over one (so the road under a deck stays where it is), else the whole column (NOLIFT)."""
        h = self.layers(k, X0, Z0, X1, Z1)
        two = (h[0] > 1) & (h[1] - h[0] >= DECK_GAP)
        return np.where(two, h[0] + DECK_GAP, NOLIFT)

    def lift(self, X0, Z0, X1, Z1):
        """Road lift (lift_roads()) of the built columns [Z0:Z1, X0:X1], whichever chunk owns them."""
        out = np.zeros((Z1 - Z0, X1 - X0), np.int32)
        for (fx, fz, f) in getattr(self, "lifts", ()):
            ax, az, bx, bz = max(X0, fx), max(Z0, fz), min(X1, fx + f.shape[1]), min(Z1, fz + f.shape[0])
            if bx <= ax or bz <= az: continue
            sub = out[az - Z0:bz - Z0, ax - X0:bx - X0]
            np.maximum(sub, f[az - fz:bz - fz, ax - fx:bx - fx], out=sub)
        return out

    def pairs(self):
        """Edge neighbours (a, b, axis): b south of a (axis 0, seam runs along x) or east of a (axis 1, along z)."""
        out = []
        pos = {(i["row"], i["col"]): k for k, i in enumerate(self.info)}
        for k, i in enumerate(self.info):
            s = pos.get((i["row"] - 1, i["col"]))   # higher row = further north, so row - 1 is the south neighbour
            e = pos.get((i["row"], i["col"] + 1))
            if s is not None: out.append((k, s, 0))
            if e is not None: out.append((k, e, 1))
        return out


def plan(C, log=print):
    """Per seam between a (north or west) and b (south or east): the blend shares and the cut. Fills C.seams."""
    for a, b, ax in C.pairs():
        A, B = C.info[a]["ext"], C.info[b]["ext"]
        if ax == 0:   # b south of a: overlap rows B.z0 .. A.z1-1, lines are x
            lo, hi = max(A[0], B[0]), min(A[2], B[2]); first, last = B[1], A[3] - 1
        else:         # b east of a: overlap columns B.x0 .. A.x1-1, lines are z
            lo, hi = max(A[1], B[1]), min(A[3], B[3]); first, last = B[0], A[2] - 1
        if hi <= lo: continue
        n = last - first + 1
        if n < 1:
            log(f"seams: {C.info[a]['id']} and {C.info[b]['id']} do not overlap ({1 - n} block gap); left as they are")
            continue
        def across(k, f, l):   # [line, across] heights of chunk k over the lines lo..hi and the across range f..l
            h = C.height(k, lo, f, hi, l + 1).T if ax == 0 else C.height(k, f, lo, l + 1, hi)
            return h
        ha, hb = across(a, first - 1, last), across(b, first, last)       # a: first-1 .. last, b: first .. last
        # 1. the blend: steady local disagreement over the overlap, split between the two chunks
        # the step itself, b's column across the boundary from a's, at each place the cut may go (comparing the two
        # bakes column for column would also count the block or two the bakes are offset sideways, which on a slope
        # is a height difference that no cut ever shows)
        # a column where one bake has a deck over a lower surface (a road under a bridge) and the other has not is left
        # out: the first one's ground is the lower surface, the other's the deck itself, and comparing the two would
        # move the deck up on one side and down on the other
        def deck(k, f, l):
            m = C.deck(k, lo, f, hi, l + 1).T if ax == 0 else C.deck(k, f, lo, l + 1, hi)
            return m
        ka, kb = deck(a, first - 1, last), deck(b, first, last)
        ov = np.where((ha[:, :n] > NONE) & (hb > NONE) & (ka[:, :n] == kb), (hb - ha[:, :n]).astype(float), np.nan)
        raw = np.array([np.median(r[~np.isnan(r)]) if (~np.isnan(r)).any() else np.nan for r in ov])
        r = np.rint(runmed(raw, SMOOTH)); r[(np.abs(r) < 2) | (np.abs(r) > MAXSHIFT)] = 0   # one block is noise
        # ramp in and out along the seam too, so where the blend starts the ground steps a block at a time
        r = np.convolve(np.pad(r, BLEND, mode="edge"), np.ones(2 * BLEND + 1) / (2 * BLEND + 1), "valid")
        da = r / 2; db = da - r                                           # a + da meets b + db where ha + r = hb
        s = dict(a=a, b=b, ax=ax, lo=lo, hi=hi, first=first, da=da, db=db)
        C.seams.append(s)
        # 2. the cut, on the blended ground: candidates k = first .. last (a owns the lines before k, so it never keeps
        # its own last line)
        if ax == 0:
            ga = C.ground(a, lo, first - 1, hi, last + 1).T; gb = C.ground(b, lo, first, hi, last + 1).T
        else:
            ga = C.ground(a, first - 1, lo, last + 1, hi); gb = C.ground(b, first, lo, last + 1, hi)
        ks = np.arange(first, last + 1)
        steps = np.stack([gb[:, k - first] - ga[:, k - first] for k in ks], 1)
        valid = np.stack([(gb[:, k - first] > NONE) & (ga[:, k - first] > NONE) for k in ks], 1)
        # where only one side has a deck over a lower surface, the cut goes where the decks (the highest surfaces) meet
        if ax == 0:
            ta = C.top(a, lo, first - 1, hi, last + 1).T; tb = C.top(b, lo, first, hi, last + 1).T
        else:
            ta = C.top(a, first - 1, lo, last + 1, hi); tb = C.top(b, first, lo, last + 1, hi)
        tsteps = np.stack([np.where((ka[:, k - first] != kb[:, k - first]) & (ta[:, k - first] > NONE) & (tb[:, k - first] > NONE),
                                    tb[:, k - first] - ta[:, k - first], gb[:, k - first] - ga[:, k - first]) for k in ks], 1)
        pick = np.where(valid, np.abs(tsteps), 10 ** 6).argmin(1)        # the first minimum: ties go to the boundary
        s["cut"] = ks[pick]
        L = np.arange(len(pick))
        ok = valid[:, 0] & (hb[:, 0] > NONE) & (ha[:, 0] > NONE)        # lines with ground on both sides of the boundary
        raw_nom = np.where(ok, hb[:, 0] - ha[:, 0], 0)                  # unblended step at the nominal boundary
        nominal = np.where(ok, steps[:, 0], 0)
        chosen = np.where(ok, steps[L, pick], 0)
        st = {"lines": int(hi - lo), "overlap": int(n), "lines_compared": int(ok.sum()), "lines_blended": int((np.abs(r) >= 0.5).sum()),
              "largest_blend": int(round(np.abs(r).max())) if len(r) else 0,
              "step_unblended_nonzero": int((raw_nom != 0).sum()), "step_unblended_ge2": int((np.abs(raw_nom) >= 2).sum()),
              "step_blended_nonzero": int((nominal != 0).sum()), "step_blended_ge2": int((np.abs(nominal) >= 2).sum()),
              "step_at_cut_nonzero": int((chosen != 0).sum()), "step_at_cut_ge2": int((np.abs(chosen) >= 2).sum()),
              "cut_moved": int((pick > 0).sum())}
        s["stats"] = st
        log(f"seams: {C.info[a]['id']}|{C.info[b]['id']}: {st['lines']} lines ({st['lines_compared']} with ground on both sides), overlap {n}; blended {st['lines_blended']} "
            f"lines (up to {st['largest_blend']} blocks); step at the boundary nonzero on {st['step_unblended_nonzero']} lines "
            f"({st['step_unblended_ge2']} by 2+), after the blend {st['step_blended_nonzero']} ({st['step_blended_ge2']}), at the "
            f"chosen cut {st['step_at_cut_nonzero']} ({st['step_at_cut_ge2']}); cut moved off the boundary on {st['cut_moved']} lines")


CLS_M = {0: 11.0, 1: 11.0, 2: 8.0, 3: 7.0, 4: 6.5}   # roads.json class -> carriageway metres (road_grade.py DEFAULT_M)


def lift_roads(C, roads, log=print):
    """Raised roads across the seams: a deck or ramp graded on each side of a seam by its own chunk can meet the
    other side's at a different level, or dip under the level the other side holds it to. For every road (roads: the
    joined graph, nodes in the built frame) crossing a seam with a deck along it within LIFT_SRC blocks, its surface
    is followed along the centre line on the blended ground of the columns' owners; where it falls more than one
    block under the levels held within LIFT_SRC of the seam, less GRADE per block of distance, its lower side is lifted
    to that line (never by more than LIFT_MAX). The lift covers the carriageway (columns within its half-width at
    the road's level) and moves only the surface over a lower one where a deck stands, so a road under the deck keeps
    its level and headroom. Only ever raises; roads at grade (no deck within LIFT_SRC) are left to the blend.
    Fills C.lifts with (X0, Z0, lift field) per lifted crossing. Returns stats."""
    from collections import defaultdict
    C.lifts = []
    st = dict(crossings=0, raised=0, lifted=0, lift_cols=0, largest_lift=0)
    if not roads or not C.seams: return st
    nodes = np.asarray(roads["nodes"], float); E = roads["edges"]; names = roads.get("names", [])
    inc = defaultdict(list)
    for ei, e in enumerate(E): inc[e[0]].append(ei); inc[e[1]].append(ei)

    def walk(a, b, ei, reach):
        """Nodes and edges from a through b onwards, straight on through junctions, for reach blocks."""
        pts, eds, length = [a, b], [ei], float(np.hypot(*(nodes[b] - nodes[a])))
        while length < reach:
            dv = nodes[b] - nodes[a]; dv = dv / max(1e-9, np.hypot(*dv)); best = None
            for ej in inc[b]:
                if ej == ei: continue
                c = E[ej][1] if E[ej][0] == b else E[ej][0]
                v = nodes[c] - nodes[b]; L = np.hypot(*v)
                if L == 0 or c in pts: continue
                cos = float(dv @ v) / L
                if cos < 0.7: continue
                sc = (E[ej][2] == E[ei][2], cos)
                if best is None or sc > best[0]: best = (sc, ej, c)
            if best is None: break
            ei, a, b = best[1], b, best[2]; pts.append(b); eds.append(ei); length += float(np.hypot(*(nodes[b] - nodes[a])))
        return pts, eds

    def hw(e):
        m = CLS_M.get(e[3], 5.0) if len(e) > 3 else 5.0
        if len(e) > 5 and e[5]: m = max(m, e[5] * 3.5)
        return int(min(8, max(1, round((m - 1) / 2))))

    EU = np.array([e[0] for e in E], int); EV = np.array([e[1] for e in E], int)
    for s in C.seams:
        ax, first, lo, hi = s["ax"], s["first"], s["lo"], s["hi"]
        bnd = first - 0.5          # a's last own line ends here, b's first begins
        A_, B_ = nodes[EU, 1 - ax] - bnd, nodes[EV, 1 - ax] - bnd
        cand = np.nonzero((A_ * B_ <= 0) & (A_ != B_) & (np.maximum(nodes[EU, ax], nodes[EV, ax]) >= lo)
                          & (np.minimum(nodes[EU, ax], nodes[EV, ax]) < hi))[0]
        for ei in cand.tolist():
            e = E[ei]; u, v = e[0], e[1]
            cu, cv = nodes[u][1 - ax], nodes[v][1 - ax]      # across-seam coordinate (z for ax 0)
            if (cu - bnd) * (cv - bnd) > 0 or cu == cv: continue
            if cu > bnd: u, v, cu, cv = v, u, cv, cu        # u on a's side
            t = (bnd - cu) / (cv - cu); xc = nodes[u] + t * (nodes[v] - nodes[u])
            if not lo + 2 <= xc[ax] < hi - 2: continue
            dv = nodes[v] - nodes[u]
            if abs(dv[1 - ax]) < 0.5 * np.hypot(*dv): continue   # a road running along the seam: no deck crosses it here
            st["crossings"] += 1
            back, beds = walk(v, u, ei, LIFT_REACH + 2)       # v, u, ... into a
            fwd, feds = walk(u, v, ei, LIFT_REACH + 2)        # u, v, ... into b
            poly = np.array([nodes[n] for n in back[::-1]] + [nodes[n] for n in fwd[2:]])
            peds = beds[::-1] + feds[1:]                      # edge of each polyline segment
            seg = np.hypot(*np.diff(poly, axis=0).T); cum = np.concatenate([[0], np.cumsum(seg)])
            s0 = cum[len(back) - 2] + t * seg[len(back) - 2]  # arc length of the crossing
            S = np.arange(np.ceil(cum[0] - s0), np.floor(cum[-1] - s0) + 1)
            S = S[np.abs(S + 0.5) <= LIFT_REACH]
            if not len(S): continue
            px = np.interp(S + s0, cum, poly[:, 0]); pz = np.interp(S + s0, cum, poly[:, 1])
            si = np.clip(np.searchsorted(cum, S + s0, side="right") - 1, 0, len(peds) - 1)
            eidx = np.array(peds)[si]
            W = np.array([hw(E[k]) for k in eidx]); deck = np.array([len(E[k]) > 6 and E[k][6] == 1 for k in eidx])
            near = np.abs(S + 0.5) <= LIFT_SRC
            if not (deck & near).any():   # cheap test first: a deck over a lower surface under the centre line nearby
                qx, qz = np.floor(px[near]).astype(int), np.floor(pz[near]).astype(int)
                a0, b0 = qx.min(), qz.min(); hit = np.zeros(len(qx), bool)
                for j in (s["a"], s["b"]):
                    hit |= C.deck(j, a0, b0, qx.max() + 1, qz.max() + 1)[qz - b0, qx - a0]
                if hit.sum() < 6: continue
            X0, Z0 = int(np.floor(px.min())) - 10, int(np.floor(pz.min())) - 10
            X1, Z1 = int(np.floor(px.max())) + 11, int(np.floor(pz.max())) + 11
            own = owners(C, X0, Z0, X1, Z1)
            UP = np.full(own.shape, NONE, np.int32); LO = np.full(own.shape, NONE, np.int32)
            for j in set(np.unique(own).tolist()) - {-1}:
                h = C.layers(j, X0, Z0, X1, Z1); sh = C.shift(j, X0, Z0, X1, Z1) + C.info[j]["sy"]
                UP = np.where(own == j, np.where(h[1] > 1, h[1] + sh, NONE), UP)
                LO = np.where(own == j, np.where(h[0] > 1, h[0] + sh, np.where(h[1] > 1, h[1] + sh, NONE)), LO)
            ix = np.floor(px).astype(int) - X0; iz = np.floor(pz).astype(int) - Z0
            up, lw = UP[iz, ix], LO[iz, ix]
            two = (up > NONE) & (lw > NONE) & (up - lw >= DECK_GAP)
            # the road's own surface: at the seam the deck if the road is a bridge there, else the lowest surface (a
            # road under a deck); then whichever surface is nearer the last one
            P = np.full(len(S), np.nan); onlow = np.zeros(len(S), bool)
            c0 = int(np.argmin(np.abs(S + 0.5)))
            for rng in (range(c0, len(S)), range(c0 - 1, -1, -1)):
                last = None
                for k in rng:
                    cands = [(y, y == lw[k] and two[k]) for y in {int(up[k]), int(lw[k])} if y > NONE]
                    if not cands: continue
                    if last is None: y, low = max(cands) if deck[k] else min(cands)
                    else: y, low = min(cands, key=lambda c: (abs(c[0] - last), -c[0]))
                    P[k], onlow[k], last = y, low, y
            if (two & ~onlow & near).sum() < 6 and not (deck & near).any(): continue
            st["raised"] += 1
            src = near & ~np.isnan(P)
            if not src.any(): continue
            N = (P[src][None, :] - np.abs(S[:, None] - S[src][None, :]) * GRADE).max(1)
            lift = np.where(np.isnan(P) | onlow, 0, np.clip(np.floor(N - np.nan_to_num(P) + 1e-6), 0, LIFT_MAX)).astype(int)
            mid = np.abs(S + 0.5) <= 4
            if lift[mid].max(initial=0) < 2: continue
            # only the run of lifted stations through the seam
            keep = np.zeros(len(S), bool)
            for k in np.nonzero(mid & (lift > 0))[0]:
                a_ = k
                while a_ >= 0 and lift[a_] > 0 and not keep[a_]: keep[a_] = True; a_ -= 1
                a_ = k + 1
                while a_ < len(S) and lift[a_] > 0 and not keep[a_]: keep[a_] = True; a_ += 1
            lift = np.where(keep, lift, 0)
            F = np.zeros(own.shape, np.int32)
            gz, gx = np.mgrid[0:own.shape[0], 0:own.shape[1]]
            for k in np.nonzero(lift)[0]:
                r = W[k] + 0.5; cx, cz = px[k] - X0, pz[k] - Z0
                a0, a1 = max(0, int(cz - r)), min(own.shape[0], int(cz + r) + 2)
                b0, b1 = max(0, int(cx - r)), min(own.shape[1], int(cx + r) + 2)
                d2 = (gx[a0:a1, b0:b1] + 0.5 - cx) ** 2 + (gz[a0:a1, b0:b1] + 0.5 - cz) ** 2
                at = (d2 <= r * r) & (np.abs(UP[a0:a1, b0:b1] - P[k]) <= 1) & (own[a0:a1, b0:b1] >= 0)
                sub = F[a0:a1, b0:b1]; np.maximum(sub, np.where(at, lift[k], 0), out=sub)
            if not F.any(): continue
            C.lifts.append((X0, Z0, F))
            st["lifted"] += 1; st["lift_cols"] += int((F > 0).sum()); st["largest_lift"] = max(st["largest_lift"], int(lift.max()))
            nm = names[e[2]] if e[2] < len(names) else ""
            log(f"seams: lifted {nm or 'a road'} across {C.info[s['a']]['id']}|{C.info[s['b']]['id']} at "
                f"({xc[0]:.0f}, {xc[1]:.0f}): step {int(P[c0] - P[c0 - 1]) if c0 > 0 and not np.isnan(P[c0 - 1] + P[c0]) else '?'} "
                f"at the seam, up to {int(lift.max())} blocks over {int(keep.sum())} blocks of road, {int((F > 0).sum())} columns")
    log("seams: raised roads across seams: " + ", ".join(f"{k} {v:,}" for k, v in st.items()))
    return st


def owners(C, X0, Z0, X1, Z1):
    """Owner chunk index of each built column in [Z0:Z1, X0:X1]; -1 where no chunk has it."""
    W, D = X1 - X0, Z1 - Z0
    own = np.full((D, W), -1, np.int32); cnt = np.zeros((D, W), np.int32)
    cov = {}
    order = sorted(range(len(C.info)), key=lambda k: (C.info[k]["ext"][1], C.info[k]["ext"][0]))
    for k in order:   # the most south-easterly covering chunk last, so it wins unless a rule below says otherwise
        e = C.info[k]["ext"]
        ax, az, bx, bz = max(X0, e[0]), max(Z0, e[1]), min(X1, e[2]), min(Z1, e[3])
        if bx <= ax or bz <= az: continue
        m = np.zeros((D, W), bool); m[az - Z0:bz - Z0, ax - X0:bx - X0] = True
        cov[k] = m; own[m] = k; cnt += m
    for k, m in cov.items():   # the chunk whose grid slot holds a column owns it, when it covers it
        s = C.info[k]["slot"]
        ax, az, bx, bz = max(X0, s[0]), max(Z0, s[1]), min(X1, s[2]), min(Z1, s[3])
        if bx <= ax or bz <= az: continue
        sm = np.zeros((D, W), bool); sm[az - Z0:bz - Z0, ax - X0:bx - X0] = True
        own[sm & m] = k
    for s in C.seams:   # the seam cut, where exactly its two chunks meet
        a, b, lo, hi, cut = s["a"], s["b"], s["lo"], s["hi"], s["cut"]
        if a not in cov or b not in cov: continue
        two = cov[a] & cov[b] & (cnt == 2)
        if not two.any(): continue
        if s["ax"] == 0:
            xs = np.arange(X0, X1); c = np.full(W, -10 ** 9); ins = (xs >= lo) & (xs < hi); c[ins] = cut[xs[ins] - lo]
            before = np.arange(Z0, Z1)[:, None] < c[None, :]; two &= ins[None, :]
        else:
            zs = np.arange(Z0, Z1); c = np.full(D, -10 ** 9); ins = (zs >= lo) & (zs < hi); c[ins] = cut[zs[ins] - lo]
            before = np.arange(X0, X1)[None, :] < c[:, None]; two &= ins[:, None]
        own[two & before] = a; own[two & ~before] = b
    return own


DIRS = ((1, 0, 2), (-1, 0, 3), (0, 1, 4), (0, -1, 5))   # (dz, dx, face dir towards that neighbour)


def sew(info, read_pack, read_h, log=print, roads=None):
    """The seam edits for a join. Returns ({(chunk index, pack id): edit}, stats); an edit is a dict with the new pack
    bytes ("blob"), tile table ("tiles"), quad count ("quads") and height tile bytes ("hbytes"). roads: the joined road
    graph with nodes in the built frame, for lift_roads() (None leaves raised roads as the chunks graded them)."""
    C = Chunks(info, read_h)
    plan(C, log)
    lst = lift_roads(C, roads, log)
    boxes = [(fx, fz, fx + f.shape[1], fz + f.shape[0]) for fx, fz, f in C.lifts]
    edits = {}
    tot = dict(packs=0, quads_cut=0, boxes_dropped=0, heights_cleared=0, columns_shifted=0, columns_lifted=0, risers=0, riser_blocks=0)
    T = info[0]["meta"]["tile"] if info else 64
    for k, i in enumerate(info):
        P = i["meta"]["pack"]
        others = [j for j in range(len(info)) if j != k]
        for p in i["meta"]["packs"]:
            X0, Z0 = i["sx"] + p["x"], i["sz"] + p["z"]
            m = BLEND + 1   # a pack needs work only if another chunk's world reaches within the blend band of it
            if not any(info[j]["ext"][0] <= X0 + P + m and info[j]["ext"][2] >= X0 - m and info[j]["ext"][1] <= Z0 + P + m
                       and info[j]["ext"][3] >= Z0 - m for j in others) \
                    and not any(b[0] <= X0 + P + 1 and b[2] >= X0 - 1 and b[1] <= Z0 + P + 1 and b[3] >= Z0 - 1 for b in boxes):
                continue
            R = (X0 - 1, Z0 - 1, X0 + P + 1, Z0 + P + 1)
            own = owners(C, *R)
            mine = own[1:-1, 1:-1] == k
            sh = C.shift(k, *R)
            L = C.lift(*R)
            # the ground each column shows once every chunk is cut to what it owns, blended and lifted (the ground moves
            # with a lift where the whole column does), and the highest surface likewise
            top = np.full(own.shape, NONE, np.int32); shall = np.zeros(own.shape, np.int32)
            upr = np.full(own.shape, NONE, np.int32); up0 = np.full(own.shape, NONE, np.int32)
            if L.any(): Fk, layk = C.floor(k, *R), C.layers(k, *R)
            for j in set(np.unique(own).tolist()) - {-1}:
                shj = sh if j == k else C.shift(j, *R)
                hj = C.height(j, *R)
                if L.any():
                    Fj = C.floor(j, *R); lay = C.layers(j, *R)
                    gsh = shj + np.where(hj - info[j]["sy"] >= Fj, L, 0)
                    u = np.where(lay[1] > 1, lay[1] + info[j]["sy"] + shj, NONE)
                    up0 = np.where(own == j, u, up0)
                    upr = np.where(own == j, np.where(u > NONE, u + np.where(lay[1] >= Fj, L, 0), NONE), upr)
                else: gsh = shj
                top = np.where(own == j, np.where(hj > NONE, hj + gsh, NONE), top)
                shall = np.where(own == j, gsh, shall)
            shk = sh[1:-1, 1:-1]; gk = shall[1:-1, 1:-1]; Lk = L[1:-1, 1:-1]
            # risers: an owned column above a neighbour owned by another chunk, or moved by a different shift, needs
            # side faces down to that neighbour's ground
            need = []   # (pack-local x, z, final y0, final y1 inclusive, dir, y of the column's top face before the move)
            t = top[1:-1, 1:-1]
            for dz, dx, d in DIRS:
                nb = top[1 + dz:P + 1 + dz, 1 + dx:P + 1 + dx]; no = own[1 + dz:P + 1 + dz, 1 + dx:P + 1 + dx]
                ns = shall[1 + dz:P + 1 + dz, 1 + dx:P + 1 + dx]
                diff = (no != k) | (ns != gk)
                sel = mine & (no >= 0) & diff & (t > NONE) & (nb > NONE) & (t > nb)
                for z, x in zip(*[a.tolist() for a in np.nonzero(sel)]):
                    need.append((x, z, int(nb[z, x]) + 1 - i["sy"], int(t[z, x]) - i["sy"], d, int(t[z, x]) - i["sy"] - int(gk[z, x])))
            if Lk.any():
                # a lifted deck (over a lower surface, so not covered by the ground's risers) above the same deck next
                # to it, lifted less: the faces of the step
                two = (Fk[1:-1, 1:-1] > NOLIFT) & mine
                u, u0 = upr[1:-1, 1:-1], up0[1:-1, 1:-1]
                for dz, dx, d in DIRS:
                    nu = upr[1 + dz:P + 1 + dz, 1 + dx:P + 1 + dx]; nu0 = up0[1 + dz:P + 1 + dz, 1 + dx:P + 1 + dx]
                    sel = two & (u > NONE) & (nu > NONE) & (np.abs(u0 - nu0) <= 2) & (u > nu)
                    for z, x in zip(*[a.tolist() for a in np.nonzero(sel)]):
                        need.append((x, z, int(nu[z, x]) + 1 - i["sy"], int(u[z, x]) - i["sy"], d, int(layk[1, z + 1, x + 1])))
                tot["columns_lifted"] += int((mine & (Lk > 0)).sum())
            if mine.all() and not shk.any() and not need and not Lk.any(): continue
            buf = read_pack(i, p)
            blob = bytearray(); tiles = []; nq = 0
            byt = {}
            for r in need: byt.setdefault(((p["x"] + r[0]) // T, (p["z"] + r[1]) // T), []).append(r)
            for tx, tz, _, off, ln in p["tiles"]:
                q, b = decode(bytes(buf[off:off + ln]))
                lx, lz = tx * T - p["x"], tz * T - p["z"]
                ownt, sht = mine[lz:lz + T, lx:lx + T], shk[lz:lz + T, lx:lx + T]
                rs = byt.pop((tx, tz), [])
                up = q[q["d"] == 0]; ux0, uz0, ux1, uz1 = footprint(up)   # top faces before the move, for colours
                if Lk.any():
                    q2, b2, ncut = reshape_lift(q, b, ownt, sht, Lk[lz:lz + T, lx:lx + T], Fk[1:-1, 1:-1][lz:lz + T, lx:lx + T])
                else: q2, b2, ncut = reshape(q, b, ownt, sht)
                tot["quads_cut"] += ncut; tot["boxes_dropped"] += len(b) - len(b2)
                tot["columns_shifted"] += int((ownt & (sht != 0)).sum())
                if rs:
                    side = q2[q2["d"] >= 2]; sx0, sz0, sx1, sz1 = footprint(side)
                    sy0 = side["y"].astype(np.int32); sy1 = sy0 + side["h"].astype(np.int32)
                    add = []
                    for x, z, y0, y1, d, top0 in rs:   # top0: the column's top before the move
                        cx, cz = x - lx, z - lz
                        hit = np.nonzero((ux0 <= cx) & (cx < ux1) & (uz0 <= cz) & (cz < uz1) & (up["y"] == top0))[0]
                        if not len(hit):
                            hit = np.nonzero((ux0 <= cx) & (cx < ux1) & (uz0 <= cz) & (cz < uz1) & (up["y"] <= top0))[0]
                            hit = hit[np.argsort(-up["y"][hit].astype(np.int32))][:1]
                        mat = int(up["m"][hit[0]]) if len(hit) else DIRT
                        cover = np.zeros(y1 - y0 + 1, bool)   # cells the column's own (moved) faces already draw
                        f = np.nonzero((side["d"] == d) & (sx0 <= cx) & (cx < sx1) & (sz0 <= cz) & (cz < sz1)
                                       & (sy1 > y0) & (sy0 <= y1))[0]
                        for e in f: cover[max(sy0[e], y0) - y0:min(sy1[e], y1 + 1) - y0] = True
                        y = 0
                        while y < len(cover):
                            if cover[y]: y += 1; continue
                            e = y
                            while e < len(cover) and not cover[e] and e - y < 255: e += 1
                            add.append((cx, cz, y0 + y, 1, e - y, d, mat))
                            tot["risers"] += 1; tot["riser_blocks"] += e - y; y = e
                    if add: q2 = np.concatenate([q2, np.array(add, dtype=QT)])
                if not len(q2) and not len(b2): continue
                tb = encode(q2, b2)
                tiles.append([tx, tz, int(len(q2)), len(blob), len(tb)]); blob += tb; nq += len(q2)
            h = read_h(i, p).reshape(2, P, P).astype(np.int32)
            h = np.where(h > 1, h + shk[None] + (np.where(h >= Fk[1:-1, 1:-1][None], Lk[None], 0) if Lk.any() else 0), h)
            gone = ~mine & (h >= 0).any(0)
            tot["heights_cleared"] += int(gone.sum())
            h[:, ~mine] = -1
            edits[(k, p["id"])] = {"blob": bytes(blob), "tiles": tiles, "quads": nq,
                                   "hbytes": gzip.compress(h.astype("<i2").tobytes(), 6)}
            tot["packs"] += 1
    log("seams: " + ", ".join(f"{k} {v:,}" for k, v in tot.items()))
    return edits, {"seams": [dict(a=info[s["a"]]["id"], b=info[s["b"]]["id"], **s["stats"]) for s in C.seams], **tot,
                   "raised_roads": lst}
