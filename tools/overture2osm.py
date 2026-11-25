"""Fetch Overture Maps vector tiles for a bbox and write Overpass-style JSON that Arnis reads with --file.
Used because the OSM hosts Arnis normally calls are blocked in this environment."""
import sys, json, math, requests
from collections import defaultdict
from pmtiles.reader import Reader
import mapbox_vector_tile as mvt
from shapely.geometry import shape, box, mapping
from shapely.ops import unary_union, linemerge

S, W, N, E = map(float, sys.argv[1].split(","))
OUT = sys.argv[2]
REL = "2026-11-18.0"
BASE = f"https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/tiles/{REL}/"
sess = requests.Session()
clip = box(W, S, E, N)

def reader(name):
    url = BASE + name + ".pmtiles"
    def get(off, ln):
        r = sess.get(url, headers={"Range": f"bytes={off}-{off+ln-1}"}, timeout=120); r.raise_for_status(); return r.content
    return Reader(get)

def lonlat2tile(lon, lat, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y

def feats(name, z, layers):
    r = reader(name)
    x0, y0 = lonlat2tile(W, N, z); x1, y1 = lonlat2tile(E, S, z)
    out = defaultdict(list)
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            data = r.get(z, tx, ty)
            if not data: continue
            import gzip
            if data[:2] == b"\x1f\x8b": data = gzip.decompress(data)
            dec = mvt.decode(data, default_options={"y_coord_down": True})
            for lname, layer in dec.items():
                if lname not in layers: continue
                ext = layer.get("extent", 4096)
                for f in layer["features"]:
                    g = f["geometry"]
                    def conv(c):
                        n = 2 ** z
                        lon = (tx + c[0] / ext) / n * 360 - 180
                        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + c[1] / ext) / n))))
                        return (lon, lat)
                    def walk(c):
                        if isinstance(c[0], (int, float)): return conv(c)
                        return [walk(x) for x in c]
                    geom = shape({"type": g["type"], "coordinates": walk(g["coordinates"])})
                    if not geom.is_valid: geom = geom.buffer(0)
                    p = f["properties"]
                    out[(lname, p.get("id", id(f)))].append((geom, p))
    return out

nodes, ways = {}, []
nid = [1]; wid = [1]
def node(lon, lat):
    k = (round(lon, 7), round(lat, 7))
    if k not in nodes:
        nodes[k] = nid[0]; nid[0] += 1
    return nodes[k]
def add_way(coords, tags):
    ids = [node(*c) for c in coords]
    if len(ids) < 2: return
    ways.append({"type": "way", "id": wid[0], "nodes": ids, "tags": tags}); wid[0] += 1
def add_geom(geom, tags, area):
    geom = geom.intersection(clip) if area else geom.intersection(clip.buffer(0.002))
    if geom.is_empty: return
    parts = getattr(geom, "geoms", [geom])
    for g in parts:
        if g.geom_type == "Polygon" and area:
            if g.area < 1e-11: continue
            add_way(list(g.exterior.coords), tags)
        elif g.geom_type == "LineString" and not area:
            add_way(list(g.coords), tags)

def merged(items, area):
    geoms = [g for g, _ in items]
    if area: return unary_union([g for g in geoms if g.geom_type in ("Polygon", "MultiPolygon")])
    lines = [g for g in geoms if g.geom_type in ("LineString", "MultiLineString")]
    if not lines: return None
    u = unary_union(lines)
    return linemerge(u) if u.geom_type == "MultiLineString" else u

def parse_json(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return None
    return v

stats = defaultdict(int)
# Roads and rail
ROADS = {"motorway","trunk","primary","secondary","tertiary","residential","service","unclassified","living_street",
         "pedestrian","footway","cycleway","path","steps","track","bridleway","sidewalk","crosswalk"}
for (ln, fid), items in feats("transportation", 14, {"segment"}).items():
    p = items[0][1]; g = merged(items, False)
    if g is None or g.is_empty: continue
    st, cl, sub = p.get("subtype"), p.get("class"), p.get("subclass")
    tags = {}
    if st == "road":
        if cl in ("sidewalk", "crosswalk"): tags = {"highway": "footway", "footway": cl}
        elif cl in ROADS: tags = {"highway": cl + ("_link" if sub == "link" and cl in ("motorway","trunk","primary","secondary","tertiary") else "")}
        elif cl == "unknown": tags = {"highway": "road"}
        else: continue
        if sub in ("parking_aisle", "driveway", "alley"): tags["service"] = sub
    elif st == "rail":
        tags = {"railway": "rail"}
    else: continue
    if p.get("@name"): tags["name"] = p["@name"]
    flags = json.dumps(p.get("road_flags") or "")
    if "is_bridge" in flags: tags["bridge"] = "yes"; tags["layer"] = "1"
    if "is_tunnel" in flags: tags["tunnel"] = "yes"; tags["layer"] = "-1"
    add_geom(g, tags, False); stats["road:" + tags.get("highway", tags.get("railway", "?"))] += 1

# Land use, land, water, infrastructure: Overture keeps the original OSM tags in source_tags
for (ln, fid), items in feats("base", 13, {"land_use", "land", "water", "infrastructure"}).items():
    p = items[0][1]
    st = parse_json(p.get("source_tags")) or {}
    tags = {k: v for k, v in st.items() if isinstance(v, str)} if isinstance(st, dict) else {}
    if not tags:
        cl = p.get("class"); sub = p.get("subtype")
        if ln == "water": tags = {"natural": "water"} if cl not in ("stream","river","canal","drain","ditch") else {"waterway": cl}
        elif ln == "land_use": tags = {"landuse": cl or sub}
        elif ln == "land": tags = {"natural": cl or sub}
        else: tags = {sub or "man_made": cl or "yes"}
    area = any(gm.geom_type in ("Polygon", "MultiPolygon") for gm, _ in items)
    g = merged(items, area)
    if g is None or g.is_empty: continue
    if p.get("@name") and "name" not in tags: tags["name"] = p["@name"]
    add_geom(g, tags, area); stats[ln] += 1

# Buildings, with heights where Overture has them
for (ln, fid), items in feats("buildings", 14, {"building"}).items():
    p = items[0][1]; g = merged(items, True)
    if g is None or g.is_empty: continue
    if p.get("is_underground") in (True, "true"): continue
    tags = {"building": p.get("class") or "yes"}
    for src, dst in [("height","height"),("num_floors","building:levels"),("min_height","min_height"),
                     ("roof_shape","roof:shape"),("roof_color","roof:colour"),("facade_color","building:colour"),
                     ("facade_material","building:material"),("roof_material","roof:material"),("@name","name")]:
        if p.get(src) not in (None, ""): tags[dst] = str(p[src])
    add_geom(g, tags, True); stats["building"] += 1
    if "height" in tags: stats["building_with_height"] += 1

elements = [{"type": "node", "id": i, "lat": k[1], "lon": k[0]} for k, i in nodes.items()] + ways
json.dump({"version": 0.6, "generator": "overture2osm", "elements": elements}, open(OUT, "w"))
print(json.dumps(dict(stats), indent=0)); print("nodes", len(nodes), "ways", len(ways))
