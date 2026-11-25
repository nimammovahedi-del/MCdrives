"""Where a car should stop for an address or a place: the arrival rule, shared by the measurement
(tools/measure_arrival.py), the build-time precompute (tools/snap_addresses.py) and places.py.

Rule (tuned: tools/tune_arrival.py):
  1. An address knows its street. Snap it to the closest point ON an edge of that street (projection
     onto the segment, not the nearest node), but only if that point is within REACH (150 m) AND no more
     than MARGIN (60 m) farther than the nearest drivable road: a driver left 200 m away on the right
     street is worse off than one left 45 m away on the next road. (The 150-300 m second look, far=True,
     is off by default.)
  2. Names are compared after normalisation: case, county abbreviations (DR, AVE, TRL, VW...),
     directional prefixes (E/East), spaces ("SUMMER HOUSE" = "Summerhouse"), a dropped street type, and
     as a last resort a one-letter typo for long names.
  3. Otherwise fall back to the nearest drivable road, never a motorway or a ramp, named roads preferred
     by FALLBACK_BONUS; when the own street is known but too far, roads within FACE_SLACK of the nearest
     that lie on the own street's side of the address win (the side of the block facing its street).
  3b. A lot: only when the own-street point is more than LOT_MIN (35 m) from the address, an unnamed
     service road (an apartment complex's or a plaza's own drive) that is closer, not behind the address
     (as seen from the street), and leads out to that same street along service roads ends the trip.
  4. The result is (edge index, t along the edge from a to b, point, side): side is +1 when the address
     is on the right of a driver going a -> b, -1 on the left (flip it when the route drives b -> a).
"""
import math
from collections import defaultdict

REACH = 150.0
FAR_REACH = 300.0         # second look for the address's own street before falling back (big lots, long drives)
FALLBACK_BONUS = 15.0
ENDPOINT_SNAP = 3.0       # closer than this to an edge end: use the node itself
CELL = 50.0
LOT_GAIN = 5.0            # a lot's drive must be this much closer than the street to be used
LOT_COS = -0.3            # ...and not behind the address as seen from the street (alleys)
LOT_LINK = 300.0          # ...and lead to the address's own street within this many blocks of service road
LOT_MIN = 35.0            # ...and only when the street point itself is more than this far from the address
MARGIN = 60.0             # own street wins if no more than this much farther than the nearest drivable road
DIR_REACH = 500.0         # how far to look for the own street to know which side of the block faces it
FACE_SLACK = 30.0         # fallback: roads up to this much farther than the nearest, on the street's side, win
FACE_COS = -0.3

# county / USPS abbreviations (the address side) and the viewer's search abbreviations, both to full words
ABBR = {"st": "street", "ave": "avenue", "av": "avenue", "pkwy": "parkway", "pky": "parkway", "dr": "drive", "blvd": "boulevard",
        "rd": "road", "ln": "lane", "ctr": "center", "cntr": "center", "centre": "center", "pl": "place", "ct": "court", "hwy": "highway",
        "fwy": "freeway", "n": "north", "s": "south", "e": "east", "w": "west", "cir": "circle", "ter": "terrace", "trl": "trail",
        "sq": "square", "mt": "mount", "plz": "plaza", "crk": "creek", "gln": "glen", "vw": "view", "grv": "grove", "rdg": "ridge",
        "spgs": "springs", "sprgs": "springs", "pne": "pine", "vly": "valley", "cyn": "canyon", "hl": "hill", "mdw": "meadow",
        "mdws": "meadows", "xing": "crossing", "lp": "loop", "aly": "alley", "hts": "heights", "pt": "point", "pk": "park", "lk": "lake",
        "est": "estates", "cv": "cove", "wy": "way", "trce": "trace", "pass": "pass", "rnch": "ranch", "hbr": "harbor", "is": "island",
        "lndg": "landing", "rte": "route", "sta": "station", "smt": "summit", "vis": "vista", "vlg": "village", "wl": "well",
        "wls": "wells", "bnd": "bend", "brk": "brook", "byp": "bypass", "cswy": "causeway", "frst": "forest", "gdn": "garden",
        "gdns": "gardens", "hvn": "haven", "knl": "knoll", "lgt": "light", "mnr": "manor", "orch": "orchard", "psge": "passage",
        "shr": "shore", "spg": "spring", "strm": "stream", "tr": "trail", "tunl": "tunnel", "vl": "ville", "cres": "crescent",
        "crst": "crest", "ests": "estates", "expy": "expressway", "fls": "falls", "flt": "flat", "ft": "fort", "jct": "junction",
        "mtn": "mountain", "rdgs": "ridges", "sq": "square", "stn": "station", "trak": "track", "un": "union", "wk": "walk",
        "ste": "suite", "saint": "saint"}
DIRS = {"north", "south", "east", "west"}
TYPES = {"street", "avenue", "drive", "road", "lane", "court", "place", "circle", "terrace", "trail", "way", "parkway", "boulevard",
         "highway", "loop", "plaza", "square", "alley", "walk", "row", "path", "crossing", "center"}


def words(name):
    s = name.lower().replace("&", " and ").replace("'", "").replace("’", "")
    out, cur = [], []
    for ch in s:
        if ch.isalnum(): cur.append(ch)
        else:
            if cur: out.append("".join(cur)); cur = []
    if cur: out.append("".join(cur))
    return [ABBR.get(w, w) for w in out]


def keys(name):
    """(tight, loose): tight ignores case, abbreviations and spaces; loose also drops a directional
    prefix/suffix and a trailing street type ("E Coastal Peak" ~ "Coastal Peak", "Reserve Dr" ~ "Reserve")."""
    w = words(name)
    tight = "".join(w)
    l = list(w)
    if len(l) > 1 and l[0] in DIRS: l = l[1:]
    if len(l) > 1 and l[-1] in DIRS: l = l[:-1]
    if len(l) > 1 and l[-1] in TYPES: l = l[:-1]
    return tight, "".join(l)


def lev(a, b, cap=3):
    if abs(len(a) - len(b)) > cap: return cap + 1
    p = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        c = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1): c[j] = min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (ca != cb))
        p = c
    return p[-1]


def same_street(ka, kb):
    """0 tight match, 1 loose match, 2 one-letter typo (names of 7+ letters), None different."""
    if ka[0] == kb[0]: return 0
    if ka[1] == kb[1] and ka[1]: return 1
    if len(ka[0]) >= 7 and len(kb[0]) >= 7 and lev(ka[0], kb[0], 1) <= 1: return 2
    return None


class Graph:
    """roads.json with a grid of edges for nearest-segment queries."""

    def __init__(self, g):
        self.g = g; self.nodes = g["nodes"]; self.names = g["names"]; self.edges = g["edges"]
        self.nkeys = [keys(n) if n else ("", "") for n in self.names]
        self.by_tight = defaultdict(set); self.by_loose = defaultdict(set)
        for i, k in enumerate(self.nkeys):
            if k[0]: self.by_tight[k[0]].add(i); self.by_loose[k[1]].add(i)
        self.grid = defaultdict(list)
        for i, e in enumerate(self.edges):
            (x0, z0), (x1, z1) = self.nodes[e[0]], self.nodes[e[1]]
            for gx in range(int(min(x0, x1) // CELL), int(max(x0, x1) // CELL) + 1):
                for gz in range(int(min(z0, z1) // CELL), int(max(z0, z1) // CELL) + 1):
                    self.grid[(gx, gz)].append(i)
        # node -> edges, for corner detection
        self.node_edges = defaultdict(list)
        for i, e in enumerate(self.edges): self.node_edges[e[0]].append(i); self.node_edges[e[1]].append(i)

    def drivable(self, i):
        e = self.edges[i]
        return e[3] != 0 and self.names[e[2]] != "ramp"

    def near(self, x, z, r):
        seen = set(); r0 = int(r // CELL) + 1; gx, gz = int(x // CELL), int(z // CELL)
        for a in range(-r0, r0 + 1):
            for b in range(-r0, r0 + 1):
                for i in self.grid.get((gx + a, gz + b), ()):
                    if i not in seen: seen.add(i); yield i

    def project(self, i, x, z):
        a, b = self.edges[i][0], self.edges[i][1]
        (ax, az), (bx, bz) = self.nodes[a], self.nodes[b]
        dx, dz = bx - ax, bz - az; l2 = dx * dx + dz * dz or 1.0
        t = max(0.0, min(1.0, ((x - ax) * dx + (z - az) * dz) / l2))
        px, pz = ax + t * dx, az + t * dz
        return math.hypot(x - px, z - pz), t, px, pz

    def side(self, i, x, z):
        """+1: (x, z) is on the right of a driver going a -> b; -1 left (x east, z south, as in the viewer)."""
        a, b = self.edges[i][0], self.edges[i][1]
        (ax, az), (bx, bz) = self.nodes[a], self.nodes[b]
        c = (bx - ax) * (z - az) - (bz - az) * (x - ax)
        return 1 if c > 0 else -1

    def street_names(self, street):
        """name indices that are this street, with their match tier"""
        k = keys(street); out = {}
        for i in self.by_tight.get(k[0], ()): out[i] = 0
        for i in self.by_loose.get(k[1], ()): out.setdefault(i, 1)
        return out, k

    # ---- the rules
    def old_rule(self, x, z):
        """The viewer's nearestRoad(): every edge (motorways and ramps included), named non-ramp roads
        get a 15 m head start, then the edge END nearer the projection is the destination node."""
        r = 200.0
        while True:
            best = None
            for i in self.near(x, z, r):
                d, t, px, pz = self.project(i, x, z)
                nm = self.names[self.edges[i][2]]
                s = d - (15 if nm and nm != "ramp" else 0)
                if best is None or s < best[0]: best = (s, i, t, d)
            if best is not None and best[3] <= r - CELL or r > 5000: break
            r *= 2
        s, i, t, d = best
        node = self.edges[i][0] if t < 0.5 else self.edges[i][1]
        return {"edge": i, "node": node, "proj_d": d}

    def _on_street(self, x, z, want, k, reach, fuzzy):
        best = None
        for i in self.near(x, z, reach):
            if not self.drivable(i): continue
            nm = self.edges[i][2]
            tier = want.get(nm)
            if tier is None and fuzzy and self.nkeys[nm][0] and len(k[0]) >= 7:
                tier = same_street(k, self.nkeys[nm])
            if tier is None: continue
            d, t, px, pz = self.project(i, x, z)
            if d <= reach and (best is None or d < best[0]): best = (d, i, t, px, pz, "street" if tier == 0 else "street~")
        return best

    def _lot(self, x, z, k, best):
        """An unnamed service road (a lot's or complex's own drive) closer to the address than its street, on
        the same side as the street (not an alley behind), and leading out to that street within LOT_LINK
        blocks along unnamed service roads: arrive on it, in the lot."""
        cands = []
        for i in self.near(x, z, best[0]):
            e = self.edges[i]
            if e[3] != 7 or self.names[e[2]] or not self.drivable(i): continue
            d, t, px, pz = self.project(i, x, z)
            if d > best[0] - LOT_GAIN: continue
            v1 = (px - x, pz - z); v2 = (best[3] - x, best[4] - z)
            l = (math.hypot(*v1) or 1) * (math.hypot(*v2) or 1)
            if (v1[0] * v2[0] + v1[1] * v2[1]) / l < LOT_COS: continue
            cands.append((d, i, t, px, pz))
        for d, i, t, px, pz in sorted(cands):
            if self.reaches(i, k): return (d, i, t, px, pz, "lot")
        return None

    def reaches(self, i, k, limit=None):
        """does edge i lead, along unnamed service roads within limit blocks, to an edge of the street with keys k"""
        limit = LOT_LINK if limit is None else limit
        e = self.edges[i]; seen = {e[0]: 0.0, e[1]: 0.0}; todo = [e[0], e[1]]
        while todo:
            u = todo.pop()
            for j in self.node_edges[u]:
                f = self.edges[j]
                if self.nkeys[f[2]][0] and same_street(k, self.nkeys[f[2]]) is not None and self.drivable(j): return True
                if f[3] != 7 or self.names[f[2]]: continue
                v = f[1] if f[0] == u else f[0]; dd = seen[u] + math.dist(self.nodes[u], self.nodes[v])
                if dd <= limit and (v not in seen or dd < seen[v]): seen[v] = dd; todo.append(v)
        return False

    def nearest(self, x, z, toward=None, bonus=FALLBACK_BONUS):
        """The fallback road: drivable (no motorway, no ramp), named roads preferred by `bonus`. With `toward`
        (a point on the address's own street), roads within FACE_SLACK of the nearest that lie on the street's
        side of the address (cosine >= FACE_COS) win: the side of the block that faces the address's street.
        Returns ((d, i, t, px, pz, how), plain nearest distance)."""
        r = 200.0
        while True:
            cands = []
            for i in self.near(x, z, r):
                if not self.drivable(i): continue
                d, t, px, pz = self.project(i, x, z)
                cands.append((d, i, t, px, pz))
            dmin = min((c[0] for c in cands), default=None)
            if (dmin is not None and dmin + FACE_SLACK <= r - CELL) or r > 5000: break
            r *= 2
        if not cands: return None, None
        score = lambda c: c[0] - (bonus if self.names[self.edges[c[1]][2]] else 0)
        pool, how = cands, "fallback"
        if toward is not None:
            vx, vz = toward[0] - x, toward[1] - z; lv = math.hypot(vx, vz) or 1
            face = [c for c in cands if c[0] <= dmin + FACE_SLACK and
                    ((c[3] - x) * vx + (c[4] - z) * vz) / ((math.hypot(c[3] - x, c[4] - z) or 1) * lv) >= FACE_COS]
            if face: pool, how = face, "fallback-facing"
        b = min(pool, key=score)
        return b + (how,), dmin

    def snap(self, x, z, street=None, reach=REACH, fuzzy=True, lots=True, margin=MARGIN, far=False, lot_min=LOT_MIN):
        """New rule. Returns dict(edge, t, x, z, d, side, how) with how in 'street' (tier 0), 'street~' (loose or
        typo match), 'street-far' (150 to 300 m, only with far=True), 'lot', 'fallback', 'fallback-facing'.
        The own street wins when its point is within `reach` and no more than `margin` farther than the
        nearest drivable road; its lot only when the street point is more than `lot_min` from the address."""
        best = None; toward = None
        if street:
            want, k = self.street_names(street)
            s = self._on_street(x, z, want, k, reach, fuzzy)
            if s is None and far:
                s = self._on_street(x, z, want, k, FAR_REACH, fuzzy)
                if s is not None: s = s[:5] + ("street-far",)
            _, dmin = self.nearest(x, z, None, 0.0) if margin is not None else (None, None)
            if s is not None and (margin is None or s[0] <= dmin + margin):
                best = s
                if lots and s[0] > lot_min:
                    lot = self._lot(x, z, k, best)
                    if lot is not None: best = lot
            else:   # too far or missing: which way is the street, to keep to its side of the block
                d = s or self._on_street(x, z, want, k, DIR_REACH, fuzzy)
                if d is not None: toward = (d[3], d[4])
        if best is None:
            best, _ = self.nearest(x, z, toward)
        d, i, t, px, pz, how = best
        a, b = self.edges[i][0], self.edges[i][1]
        L = math.dist(self.nodes[a], self.nodes[b])
        node = None
        if t * L < ENDPOINT_SNAP: node = a
        elif (1 - t) * L < ENDPOINT_SNAP: node = b
        return {"edge": i, "t": t, "x": px, "z": pz, "d": d, "side": self.side(i, x, z), "how": how, "node": node}
