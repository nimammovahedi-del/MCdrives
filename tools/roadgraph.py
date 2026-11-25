"""Build a compact drivable road graph in Arnis block coordinates from the OSM-style JSON used for the bake.

    roadgraph.py S,W,N,E src.json out.json [--overhang N] [--places places.json]

--overhang lets the graph run N blocks past the bbox on every side instead of stopping 4 blocks
inside it. That is for one chunk of a larger area: neighbouring chunks then hold the same OSM
nodes either side of their shared edge, and `build.py` joins the chunks on those shared nodes.
With --overhang the output also carries "osm", the OSM node id of each node, so the join can
match them by identity rather than by distance. Default 0 keeps the single-bake behaviour.
--places also writes the named destinations (see places.py), snapped to this graph's nodes.
"""
import sys, json, math, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import places
from collections import defaultdict
S, W, N, E = map(float, sys.argv[1].split(","))
src, out = sys.argv[2], sys.argv[3]
OVER = int(sys.argv[sys.argv.index("--overhang") + 1]) if "--overhang" in sys.argv else 0
R = 6371000.0
def hav(dlat, dlon, lat):
    a = math.sin(math.radians(dlat)/2)**2 + math.cos(math.radians(lat))**2 * math.sin(math.radians(dlon)/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
sz = math.floor(hav(N - S, 0, 0)); sx = math.floor(hav(0, E - W, (S + N) / 2))
def xz(lon, lat): return ((lon - W) / (E - W) * sx, (1 - (lat - S) / (N - S)) * sz)
CLS = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service"]
d = json.load(open(src))
ll = {e["id"]: (e["lon"], e["lat"]) for e in d["elements"] if e["type"] == "node"}
names = [""]; nidx = {"": 0}
adj = defaultdict(list); edges = []
for e in d["elements"]:
    if e["type"] != "way": continue
    if "geometry" in e and e.get("nodes"):
        for n, g in zip(e["nodes"], e["geometry"]):
            if g: ll.setdefault(n, (g["lon"], g["lat"]))
    t = e.get("tags", {}); hw = t.get("highway", "")
    base = hw.replace("_link", "")
    if base not in CLS or t.get("service") in ("parking_aisle", "driveway") or t.get("access") in ("private", "no"): continue
    nm = t.get("name") or (t.get("ref") or ("ramp" if hw.endswith("_link") else ""))
    if nm not in nidx: nidx[nm] = len(names); names.append(nm)
    oneway = t.get("oneway") in ("yes", "1", "true") or base == "motorway" or t.get("junction") == "roundabout"
    deck = 1 if t.get("bridge") not in (None, "no") else 0
    lanes = int(t["lanes"]) if str(t.get("lanes", "")).isdigit() else 0
    for a, b in zip(e.get("nodes", []), e.get("nodes", [])[1:]):
        if a == b or a not in ll or b not in ll: continue
        edges.append((a, b, nidx[nm], CLS.index(base), 1 if oneway else 0, lanes, deck))
        adj[a].append(b); adj[b].append(a)
# keep the largest connected component, inside the world (or inside the world plus the overhang)
marg = -OVER if OVER else 4
inside = lambda n: marg < xz(*ll[n])[0] < sx - marg and marg < xz(*ll[n])[1] < sz - marg
seen = {}; comps = []
for s in adj:
    if s in seen or not inside(s): continue
    st = [s]; seen[s] = len(comps); c = [s]
    while st:
        u = st.pop()
        for v in adj[u]:
            if v not in seen and inside(v): seen[v] = len(comps); st.append(v); c.append(v)
    comps.append(c)
# With an overhang this is one chunk of a bigger area: a road that looks like an island here may
# well be joined up by a neighbouring chunk, so every component is kept and build.py picks the
# largest once the chunks are sewn together.
big = set(seen) if OVER else set(max(comps, key=len))
ids = {n: i for i, n in enumerate(sorted(big))}
nodes = [[round(c, 1) for c in xz(*ll[n])] for n in sorted(big)]
E2 = [[ids[a], ids[b], nm, cl, ow, ln, dk] for a, b, nm, cl, ow, ln, dk in edges if a in big and b in big]
g = {"size": [sx, sz], "names": names, "nodes": nodes, "edges": E2}
if OVER: g["osm"] = [n for n in sorted(big)]   # OSM node ids, so chunks can be joined on shared nodes
json.dump(g, open(out, "w"), separators=(",", ":"))
if "--places" in sys.argv:
    pl = places.build(places.extract(d["elements"], ll, xz), g)
    json.dump(pl, open(sys.argv[sys.argv.index("--places") + 1], "w"), separators=(",", ":"), ensure_ascii=False)
    print(f"{len(pl)} places, {sum(1 for r in pl if r[4] is not None)} routable")
print(f"{len(nodes)} nodes, {len(E2)} edges, {len(names)} names, {len(comps)} components; kept {len(big)}")
