"""Build a compact drivable road graph in Arnis block coordinates from the OSM-style JSON used for the bake."""
import sys, json, math
from collections import defaultdict
S, W, N, E = map(float, sys.argv[1].split(","))
src, out = sys.argv[2], sys.argv[3]
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
    t = e.get("tags", {}); hw = t.get("highway", "")
    base = hw.replace("_link", "")
    if base not in CLS or t.get("service") in ("parking_aisle", "driveway") or t.get("access") in ("private", "no"): continue
    nm = t.get("name") or (t.get("ref") or ("ramp" if hw.endswith("_link") else ""))
    if nm not in nidx: nidx[nm] = len(names); names.append(nm)
    oneway = t.get("oneway") in ("yes", "1", "true") or base == "motorway" or t.get("junction") == "roundabout"
    lanes = int(t["lanes"]) if str(t.get("lanes", "")).isdigit() else 0
    if "geometry" in e and e.get("nodes"):
        for n, g in zip(e["nodes"], e["geometry"]):
            if g: ll.setdefault(n, (g["lon"], g["lat"]))
    for a, b in zip(e.get("nodes", []), e.get("nodes", [])[1:]):
        if a == b or a not in ll or b not in ll: continue
        edges.append((a, b, nidx[nm], CLS.index(base), 1 if oneway else 0, lanes))
        adj[a].append(b); adj[b].append(a)
# keep the largest connected component, inside the world
inside = lambda n: 4 < xz(*ll[n])[0] < sx - 4 and 4 < xz(*ll[n])[1] < sz - 4
seen = {}; comps = []
for s in adj:
    if s in seen or not inside(s): continue
    st = [s]; seen[s] = len(comps); c = [s]
    while st:
        u = st.pop()
        for v in adj[u]:
            if v not in seen and inside(v): seen[v] = len(comps); st.append(v); c.append(v)
    comps.append(c)
big = set(max(comps, key=len))
ids = {n: i for i, n in enumerate(sorted(big))}
nodes = [[round(c, 1) for c in xz(*ll[n])] for n in sorted(big)]
E2 = [[ids[a], ids[b], nm, cl, ow, ln] for a, b, nm, cl, ow, ln in edges if a in big and b in big]
json.dump({"size": [sx, sz], "names": names, "nodes": nodes, "edges": E2}, open(out, "w"), separators=(",", ":"))
print(f"{len(nodes)} nodes, {len(E2)} edges, {len(names)} names, {len(comps)} components; kept {len(big)}")
