"""Named destinations from the OSM-style JSON, for search: [name, kind, x, z, node, aliases] per place.

Imported by roadgraph.py (which owns the projection and the road graph) and by build.py (to
re-snap and de-duplicate when chunks are joined). `node` is the road-graph node a car can route to.
"""
import math
from collections import defaultdict

AMENITY_KIND = {"townhall": "civic", "courthouse": "civic", "police": "civic", "fire_station": "civic", "post_office": "civic",
    "community_centre": "civic", "library": "civic", "public_building": "civic", "school": "school", "kindergarten": "school",
    "college": "university", "university": "university", "hospital": "hospital", "clinic": "hospital", "doctors": "hospital",
    "pharmacy": "shop", "marketplace": "shop", "restaurant": "food", "cafe": "food", "fast_food": "food", "bar": "food", "pub": "food",
    "food_court": "food", "ice_cream": "food", "biergarten": "food", "place_of_worship": "worship", "fuel": "fuel",
    "charging_station": "fuel", "bus_station": "station", "ferry_terminal": "station"}
AMENITY_SKIP = {"parking", "parking_space", "parking_entrance", "bicycle_parking", "motorcycle_parking", "bench", "waste_basket",
    "waste_disposal", "toilets", "shelter", "vending_machine", "atm", "telephone", "post_box", "recycling", "drinking_water",
    "bicycle_rental", "fountain", "clock", "lounger", "table", "bbq", "dressing_room", "give_box", "water_point", "compressed_air"}
TOURISM_KIND = {"hotel": "hotel", "motel": "hotel", "hostel": "hotel", "guest_house": "hotel", "resort": "hotel", "apartment": "hotel"}
TOURISM_SKIP = {"information", "picnic_site", "artwork", "viewpoint", "camp_pitch"}
LEISURE_KIND = {"park": "park", "garden": "park", "playground": "park", "dog_park": "park", "nature_reserve": "park",
    "recreation_ground": "park", "stadium": "other", "sports_centre": "other", "golf_course": "other", "fitness_centre": "other",
    "marina": "other", "water_park": "other", "ice_rink": "other", "track": "other"}
BUILDING_KIND = {"retail": "shop", "supermarket": "shop", "kiosk": "shop", "commercial": "office", "office": "office",
    "church": "worship", "cathedral": "worship", "chapel": "worship", "mosque": "worship", "synagogue": "worship", "temple": "worship",
    "hospital": "hospital", "school": "school", "university": "university", "college": "university", "civic": "civic",
    "government": "civic", "public": "civic", "fire_station": "civic", "hotel": "hotel", "train_station": "station"}
BUILDING_SKIP = {"parking", "garage", "garages", "carport", "roof", "shed", "hut", "cabin", "service", "transformer_tower", "toilets"}
PLACE_KIND = {"neighbourhood": "neighbourhood", "suburb": "neighbourhood", "quarter": "neighbourhood", "village": "neighbourhood",
    "town": "neighbourhood", "city": "neighbourhood", "hamlet": "neighbourhood", "city_block": "neighbourhood"}
ALIAS_TAGS = ("alt_name", "short_name", "official_name", "old_name", "loc_name")


def classify(t):
    a = t.get("amenity")
    if a in AMENITY_SKIP: return None
    if a: return AMENITY_KIND.get(a, "other")
    if t.get("shop") and t["shop"] != "no": return "mall" if t["shop"] == "mall" else "shop"
    if t.get("tourism"):
        return None if t["tourism"] in TOURISM_SKIP else TOURISM_KIND.get(t["tourism"], "other")
    if t.get("leisure") in LEISURE_KIND: return LEISURE_KIND[t["leisure"]]
    if t.get("office"): return "office"
    if t.get("railway") in ("station", "halt", "tram_stop") or t.get("public_transport") == "station": return "station"
    if t.get("place") in PLACE_KIND: return "neighbourhood"
    b = t.get("building")
    if b and b != "no":
        return None if b in BUILDING_SKIP else BUILDING_KIND.get(b, "other")
    return {"retail": "mall", "commercial": "office"}.get(t.get("landuse"))


def aliases(t, name):
    out = []
    for k in ALIAS_TAGS:
        for v in t.get(k, "").split(";"):
            v = v.strip()
            if v and v != name and v not in out: out.append(v)
    b = t.get("brand", "").strip()
    if b and b != name and b not in out: out.append(b)
    return out


def centroid(pts):
    if len(pts) > 1 and pts[0] == pts[-1]: pts = pts[:-1]
    if len(pts) < 3: return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    a = cx = cz = 0.0
    for (x0, z0), (x1, z1) in zip(pts, pts[1:] + pts[:1]):
        c = x0 * z1 - x1 * z0; a += c; cx += (x0 + x1) * c; cz += (z0 + z1) * c
    if abs(a) < 1e-9: return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return cx / (3 * a), cz / (3 * a)


def extract(elements, ll, xz):
    """[(name, kind, x, z, aliases, richness, extra)] for every named destination; ll maps node id to (lon, lat).
    extra: {"pts": outline in blocks (ways only), "street": addr:street, "num": addr:housenumber,
    "ent": [(x, z, entrance value)] entrance=* nodes on the outline, or the node itself when it is one}."""
    ent = {e["id"]: e["tags"]["entrance"] for e in elements if e["type"] == "node" and (e.get("tags") or {}).get("entrance")}
    out = []
    for e in elements:
        t = e.get("tags") or {}
        name = (t.get("name") or "").strip()
        if len(name) < 2 or e["type"] not in ("node", "way"): continue
        kind = classify(t)
        if not kind: continue
        ex = {"street": (t.get("addr:street") or "").strip() or None, "num": (t.get("addr:housenumber") or "").strip() or None, "pts": None, "ent": []}
        if e["type"] == "node":
            if e["id"] not in ll: continue
            x, z = xz(*ll[e["id"]])
            if e["id"] in ent: ex["ent"].append((x, z, ent[e["id"]]))
        else:
            pts = [xz(*ll[n]) for n in e.get("nodes", []) if n in ll]
            if not pts: continue
            x, z = centroid(pts)
            if len(pts) >= 4: ex["pts"] = pts
            ex["ent"] = [(*xz(*ll[n]), ent[n]) for n in e.get("nodes", []) if n in ent and n in ll]
        out.append((name, kind, round(x, 1), round(z, 1), aliases(t, name), len(t), ex))
    return out


def dedupe(items, key, dist=150):
    """Keep the best (highest key) of the entries sharing a name (case-insensitive) within dist blocks.
    items are sequences with name at [0], x at [2], z at [3]."""
    grid = defaultdict(list); kept = []
    for it in sorted(items, key=key, reverse=True):
        nm = it[0].lower(); gx, gz = int(it[2] // dist), int(it[3] // dist)
        if any(o[0].lower() == nm and (o[2] - it[2]) ** 2 + (o[3] - it[3]) ** 2 <= dist * dist
               for a in (-1, 0, 1) for b in (-1, 0, 1) for o in grid[(gx + a, gz + b)]): continue
        grid[(gx, gz)].append(it); kept.append(it)
    return kept


class Snapper:
    """Nearest road node a car can route to: on a named road that is not a motorway or a ramp."""
    CELL = 100

    def __init__(self, g, names_skip=("ramp",)):
        ok = set()
        for a, b, nm, cl, *_ in g["edges"]:
            if nm and cl != 0 and g["names"][nm] not in names_skip: ok.add(a); ok.add(b)
        self.nodes = g["nodes"]; self.grid = defaultdict(list)
        for i in ok: self.grid[(int(self.nodes[i][0] // self.CELL), int(self.nodes[i][1] // self.CELL))].append(i)

    def __call__(self, x, z, maxd=300):
        r = int(maxd // self.CELL) + 1; gx, gz = int(x // self.CELL), int(z // self.CELL)
        best, bd = None, maxd * maxd
        for a in range(-r, r + 1):
            for b in range(-r, r + 1):
                for i in self.grid.get((gx + a, gz + b), ()):
                    d = (self.nodes[i][0] - x) ** 2 + (self.nodes[i][1] - z) ** 2
                    if d <= bd: best, bd = i, d
        return best


def build(items, g, addresses=None, parking=(), why=None):
    """Dedupe extract() output and give each place an arrival point on the graph g: the rows of places.json,
    [name, kind, x, z, node, aliases, [ax, az]]. node (the old field, kept for older viewers) is the graph node
    at the arrival edge's nearer end; [ax, az] is the arrival point itself, on that edge, in the same frame as
    x, z (so build.py can shift it with the chunk). addresses: addresses.json data in the same frame, used to
    learn a place's street from its house number. parking: outlines of amenity=parking areas.
    why: a list that gets the rule used for each row (for measurement)."""
    import arrival
    A = arrival.Graph(g); finder = Placer(A, addresses, parking)
    rows = []
    for n, k, x, z, al, _, ex in dedupe(items, key=lambda it: (it[5], len(it[4]))):
        tgt, how = finder(x, z, k, ex)
        if tgt["d"] > MAXD:   # as before: no road within 300 blocks, not routable
            rows.append([n, k, x, z, None, al, None])
            if why is not None: why.append((n, "unroutable", tgt))
            continue
        e = g["edges"][tgt["edge"]]
        node = tgt["node"] if tgt["node"] is not None else (e[0] if tgt["t"] < 0.5 else e[1])
        rows.append([n, k, x, z, node, al, [round(tgt["x"], 1), round(tgt["z"], 1)]])
        if why is not None: why.append((n, how, tgt))
    order = sorted(range(len(rows)), key=lambda i: (rows[i][0].lower(), rows[i][2], rows[i][3]))
    if why is not None: why[:] = [why[i] for i in order]
    return [rows[i] for i in order]


MAXD = 300
CAMPUS = {"school", "university", "hospital", "park", "mall", "civic"}


class Placer:
    """Where a car arrives for a place without its own address point. In order:
      1. an entrance=* node on the outline (main first), snapped to the nearest drivable road;
      2. a service road or parking area access: a drivable service edge inside the place's outline or a
         parking area touching it, at the point nearest the place's centre (a shop in a plaza: its lot);
      3. the place's street, from addr:street or from its addr:housenumber matched to an address point
         nearby, with the address rule (tools/arrival.py);
      4. a large campus (school, park...) with no service road: the nearest named road that borders its
         outline, at the point nearest the centre (a stand-in for the main entrance, which the data lacks);
      5. the nearest drivable road (never a motorway or ramp)."""

    def __init__(self, A, addresses, parking):
        self.A = A; self.parking = [p for p in parking if len(p) >= 4]
        self.byn = defaultdict(list)
        if addresses:
            st, a = addresses["streets"], addresses["a"]
            for k in range(0, len(a), 4): self.byn[str(a[k + 1]).upper()].append((st[a[k]], a[k + 2], a[k + 3]))

    def __call__(self, x, z, kind, ex):
        from shapely.geometry import Polygon, Point
        A = self.A
        if ex.get("ent"):
            best = sorted(ex["ent"], key=lambda e: (e[2] != "main", (e[0] - x) ** 2 + (e[1] - z) ** 2))[0]
            return A.snap(best[0], best[1], ex.get("street")), "entrance"
        street = ex.get("street"); how_st = "addr:street"
        if not street and ex.get("num"):
            c = [r for r in self.byn.get(ex["num"].upper(), ()) if (r[1] - x) ** 2 + (r[2] - z) ** 2 < 250 ** 2]
            if c: street = min(c, key=lambda r: (r[1] - x) ** 2 + (r[2] - z) ** 2)[0]; how_st = "housenumber->address"
        poly = None
        if ex.get("pts"):
            try:
                poly = Polygon(ex["pts"]).buffer(0)
            except Exception: poly = None
        region = poly.buffer(8) if poly is not None else Point(x, z).buffer(40)
        lots = [Polygon(p).buffer(0) for p in self.parking]
        lots = [l for l in lots if l.is_valid and l.distance(region) < 15]
        area = region
        for l in lots: area = area.union(l.buffer(5))
        best = None
        for i in A.near(x, z, 400):
            e = A.edges[i]
            if e[3] != 7 or not A.drivable(i) or A.names[e[2]]: continue   # unnamed service roads: lot and campus driveways
            if street and not self._reaches(i, street): continue           # ...that lead out to the place's own street
            from shapely.geometry import LineString
            seg = LineString([A.nodes[e[0]], A.nodes[e[1]]])
            if not seg.intersects(area): continue
            part = seg.intersection(area)
            if part.is_empty: continue
            from shapely.ops import nearest_points
            q = nearest_points(part, Point(x, z))[0]   # the overlap may be a point or a mix, which project() refuses
            d, t, px, pz = A.project(i, q.x, q.y)
            dd = (px - x) ** 2 + (pz - z) ** 2
            if best is None or dd < best[0]: best = (dd, i, t, px, pz)
        # the street option, and the plain nearest road: a lot access much farther than either is a road
        # crossing a big outline, not this place's driveway
        r_st = A.snap(x, z, street) if street else None
        if r_st is not None and r_st["how"].startswith("fallback"): r_st = None
        near = r_st or A.snap(x, z, None)
        if best is not None and math.sqrt(best[0]) <= max(60.0, near["d"] + 30.0):
            _, i, t, px, pz = best
            return self._mk(i, t, px, pz, x, z), "service/parking access" + (f" ({how_st}: {street})" if street else "")
        if r_st is not None: return r_st, how_st
        if poly is not None and kind in CAMPUS and poly.area > 20000:
            ring = poly.exterior; best = None
            for i in A.near(x, z, int(math.sqrt(poly.area)) + 100):
                e = A.edges[i]
                if not A.drivable(i) or not A.names[e[2]]: continue
                from shapely.geometry import LineString
                seg = LineString([A.nodes[e[0]], A.nodes[e[1]]])
                if seg.distance(ring) > 30: continue
                d, t, px, pz = A.project(i, x, z)
                key = d   # the nearest road that borders the outline
                if best is None or key < best[0]: best = (key, i, t, px, pz)
            if best is not None:
                _, i, t, px, pz = best
                return self._mk(i, t, px, pz, x, z), "campus frontage (nearest bordering road)"
        return A.snap(x, z, None), "nearest road"

    def _reaches(self, i, street, limit=300.0):
        """does unnamed service edge i lead, along unnamed service edges within limit blocks, to the named street"""
        A = self.A; want, _ = A.street_names(street)
        e = A.edges[i]; seen = {e[0]: 0.0, e[1]: 0.0}; todo = [e[0], e[1]]
        while todo:
            u = todo.pop()
            for j in A.node_edges[u]:
                f = A.edges[j]
                if f[2] in want: return True
                if f[3] != 7 or A.names[f[2]]: continue
                v = f[1] if f[0] == u else f[0]; d = seen[u] + math.dist(A.nodes[u], A.nodes[v])
                if d <= limit and (v not in seen or d < seen[v]): seen[v] = d; todo.append(v)
        return False

    def _mk(self, i, t, px, pz, x, z):
        A = self.A; e = A.edges[i]; L = math.dist(A.nodes[e[0]], A.nodes[e[1]])
        node = e[0] if t * L < 3 else e[1] if (1 - t) * L < 3 else None
        return {"edge": i, "t": t, "x": px, "z": pz, "d": math.hypot(px - x, pz - z), "side": A.side(i, x, z), "how": "place", "node": node}
