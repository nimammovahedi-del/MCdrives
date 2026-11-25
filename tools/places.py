"""Named destinations from the OSM-style JSON, for search: [name, kind, x, z, node, aliases] per place.

Imported by roadgraph.py (which owns the projection and the road graph) and by build.py (to
re-snap and de-duplicate when chunks are joined). `node` is the road-graph node a car can route to.
"""
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
    """[(name, kind, x, z, aliases, richness)] for every named destination; ll maps node id to (lon, lat)."""
    out = []
    for e in elements:
        t = e.get("tags") or {}
        name = (t.get("name") or "").strip()
        if len(name) < 2 or e["type"] not in ("node", "way"): continue
        kind = classify(t)
        if not kind: continue
        if e["type"] == "node":
            if e["id"] not in ll: continue
            x, z = xz(*ll[e["id"]])
        else:
            pts = [xz(*ll[n]) for n in e.get("nodes", []) if n in ll]
            if not pts: continue
            x, z = centroid(pts)
        out.append((name, kind, round(x, 1), round(z, 1), aliases(t, name), len(t)))
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


def build(items, g):
    """Dedupe extract() output and snap it to the graph g: the rows of places.json."""
    snap = Snapper(g)
    rows = [[n, k, x, z, snap(x, z), al] for n, k, x, z, al, _ in dedupe(items, key=lambda it: (it[5], len(it[4])))]
    rows.sort(key=lambda r: (r[0].lower(), r[2], r[3]))
    return rows
