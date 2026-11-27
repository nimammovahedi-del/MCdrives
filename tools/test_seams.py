"""Synthetic checks of seams.py hold(), on small arrays instead of converted chunks.
    python3 tools/test_seams.py          (or: python3 -m pytest tools/test_seams.py)
SEAMS=path/to/seams.py runs the same checks against another copy of the module (to show what an older one did)."""
import os, sys, importlib.util
import numpy as np

_p = os.environ.get("SEAMS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "seams.py"))
_s = importlib.util.spec_from_file_location("seams_under_test", _p); S = importlib.util.module_from_spec(_s); _s.loader.exec_module(S)

# the frame: chunk a (north) has rows 0..LAST, chunk b (south) rows FIRST..ROWS-1; lines are x (seam axis 0)
ROWS, COLS, FIRST, N = 400, 200, 200, 4
LAST = FIRST + N - 1


class Fake(S.Chunks):
    """Chunks over whole-frame arrays: building mask, drawn top and taper per chunk ([Z, X], NONE / False outside)."""
    def __init__(self, mask, top, taper):
        super().__init__([{"id": "a"}, {"id": "b"}], None, read_b=lambda i, p: b"")
        self.M, self.T, self.P = mask, top, taper

    def _cut(self, arr, fill, X0, Z0, X1, Z1):
        out = np.full((Z1 - Z0, X1 - X0), fill, arr.dtype)
        az, ax_, bz, bx = max(Z0, 0), max(X0, 0), min(Z1, ROWS), min(X1, COLS)
        if bz > az and bx > ax_: out[az - Z0:bz - Z0, ax_ - X0:bx - X0] = arr[az:bz, ax_:bx]
        return out

    def bmask(self, k, X0, Z0, X1, Z1): return self._cut(self.M[k], False, X0, Z0, X1, Z1)
    def drawn(self, k, X0, Z0, X1, Z1): return self._cut(self.T[k], S.NONE, X0, Z0, X1, Z1)
    def taper(self, k, X0, Z0, X1, Z1): return self._cut(self.P[k], 0, X0, Z0, X1, Z1)
    def height(self, k, X0, Z0, X1, Z1):   # flat ground at 64 wherever the chunk has its world
        return np.where(self._cut(self.T[k], S.NONE, X0, Z0, X1, Z1) > S.NONE, 64, S.NONE)
    def road(self, k, X0, Z0, X1, Z1): return np.zeros((Z1 - Z0, X1 - X0), bool)
    def deck(self, k, X0, Z0, X1, Z1): return np.zeros((Z1 - Z0, X1 - X0), bool)


def world():
    """Empty frame: masks, drawn tops (ground 64 wherever the chunk has its world) and a taper of +2 / -2 at the seam."""
    M = [np.zeros((ROWS, COLS), bool) for _ in range(2)]
    T = [np.full((ROWS, COLS), S.NONE, np.int32) for _ in range(2)]
    T[0][:LAST + 1] = 64; T[1][FIRST:] = 64
    P = [np.zeros((ROWS, COLS), np.int32) for _ in range(2)]
    z = np.arange(ROWS)
    P[0][:LAST + 1] = np.rint(2 * np.clip(1 - np.maximum(FIRST - 1 - z[:LAST + 1], 0) / S.BLEND, 0, 1))[:, None]
    P[1][FIRST:] = np.rint(-2 * np.clip(1 - np.maximum(z[FIRST:] - FIRST, 0) / S.BLEND, 0, 1))[:, None]
    return M, T, P


def building(M, T, k, z0, z1, x0, x1, roof):
    """Chunk k draws a building over rows z0..z1-1, columns x0..x1-1 (only where it has its world)."""
    lo, hi = (0, LAST + 1) if k == 0 else (FIRST, ROWS)
    a, b = max(z0, lo), min(z1, hi)
    if b > a: M[k][a:b, x0:x1] = True; T[k][a:b, x0:x1] = roof


def run(M, T, P):
    C = Fake(M, T, P)
    s = dict(a=0, b=1, ax=0, lo=0, hi=COLS, first=FIRST, n=N, da=np.zeros(COLS), db=np.zeros(COLS))
    C.seams.append(s)
    S.hold(C, s, log=lambda m: None)
    return C, s


def moved(C, k):
    """[Z, X] of chunk k: where the hold moves a column off the taper."""
    return C.shift(k, 0, 0, COLS, ROWS) != C.taper(k, 0, 0, COLS, ROWS)


def depth(k):
    z = np.arange(ROWS)[:, None] * np.ones((1, COLS), int)
    return (FIRST - 1 - z) if k == 0 else (z - FIRST)


def test_small_building_across_the_seam_meets_at_one_height():
    M, T, P = world()
    building(M, T, 0, 180, 212, 40, 80, 80)      # a: 20 rows of its own plus the lap
    building(M, T, 1, 180, 230, 40, 80, 83)      # b's roof is 3 higher
    C, s = run(M, T, P)
    assert s["hstats"]["matched"] == 1 and s["hstats"]["held_parts"] == 2
    sa = C.shift(0, 40, 190, 80, 191)[0, 0]; sb = C.shift(1, 40, 220, 80, 221)[0, 0]
    assert (80 + sa) == (83 + sb), (sa, sb)       # the two roofs meet at one height
    for k in (0, 1):                              # everything moved belongs to the building and lies near the seam
        assert (moved(C, k) <= M[k]).all() and (depth(k)[moved(C, k)] < S.HOLD).all()


def test_house_far_from_the_seam_never_moves():
    """s0501: a building across the seam, joined in b's mask by a row of touching houses to a house ~124 blocks into b,
    whose rear half is a separate group of the mask. Nothing of b deeper than HOLD may move; the house stays whole."""
    M, T, P = world()
    building(M, T, 0, 60, 204, 20, 160, 90)       # a big building of a, crossing the seam
    building(M, T, 1, 196, 215, 20, 160, 93)      # b's half of it, 3 higher
    building(M, T, 1, 215, 318, 100, 108, 70)     # touching garages and houses running south
    building(M, T, 1, 318, 324, 90, 130, 72)      # the house 118..124 blocks from the seam: front half ...
    building(M, T, 1, 325, 332, 90, 130, 72)      # ... and rear half, a separate group of the mask
    C, s = run(M, T, P)
    far = depth(1) >= 100
    assert not (moved(C, 1) & far).any(), "a column of b 100+ blocks from the seam was moved"
    assert not (moved(C, 1) & (depth(1) >= S.HOLD)).any()
    house = np.zeros((ROWS, COLS), bool); house[318:332, 90:130] = True
    sh = C.shift(1, 0, 0, COLS, ROWS)
    assert len(np.unique(sh[house])) == 1, "the house came out at two heights"


def test_deep_building_keeps_the_taper_and_the_other_half_meets_it():
    M, T, P = world()
    building(M, T, 0, 186, 204, 40, 80, 80)      # a: a small part
    building(M, T, 1, 196, 330, 40, 80, 82)      # b: reaches far past HOLD
    C, s = run(M, T, P)
    assert s["hstats"].get("too_deep", 0) >= 1
    assert not moved(C, 1).any()                  # b keeps the taper
    assert (moved(C, 0) <= M[0]).all()            # a's part alone is held, to meet b at the seam


def test_roof_rim_outside_the_mask_moves_with_the_building():
    M, T, P = world()
    building(M, T, 0, 180, 212, 40, 80, 80); building(M, T, 1, 180, 230, 40, 80, 83)
    T[1][230:232, 40:80] = 81                     # b's eaves: roof columns the mask's opening left out
    C, s = run(M, T, P)
    sh = C.shift(1, 0, 0, COLS, ROWS)
    assert len(np.unique(sh[200:232, 40:80])) == 1, "the eaves stayed behind"
    assert (sh[232:, :] == C.taper(1, 0, 0, COLS, ROWS)[232:, :]).all()   # and nothing past them moved


def test_without_building_tiles_nothing_is_held():
    M, T, P = world()
    building(M, T, 0, 180, 212, 40, 80, 80); building(M, T, 1, 180, 230, 40, 80, 83)
    C = Fake(M, T, P); C.read_b = None
    s = dict(a=0, b=1, ax=0, lo=0, hi=COLS, first=FIRST, n=N, da=np.zeros(COLS), db=np.zeros(COLS)); C.seams.append(s)
    S.hold(C, s, log=lambda m: None)
    assert C.holds == {} and s["bcut"] == []


if __name__ == "__main__":
    bad = 0
    for name, f in list(globals().items()):
        if name.startswith("test_") and callable(f):
            try: f(); print("ok  ", name)
            except AssertionError as e: bad += 1; print("FAIL", name, e)
    sys.exit(1 if bad else 0)
