#!/usr/bin/env python3
"""One-command build: real map data -> Arnis block world -> web tiles -> drivable viewer.

    python build.py areas/spectrum.json            # bake, convert, road graph, pack
    python build.py areas/spectrum.json --serve    # ...then open the viewer locally
    python build.py areas/irvine.json --plan       # only show how a big area splits into chunks
    python build.py areas/irvine.json --join-only  # join chunk outputs that are already there

An area larger than max_chunk_km2 is baked as a grid of chunks, each its own Arnis world with its
own origin. The join step then writes ONE site for the whole area at build/<slug>/site: every
chunk's packs and height tiles placed at its offset in one pack list, and one road graph sewn
together across the chunk seams, so the viewer drives across the whole area without a break.

Each step is skipped when its output already exists, so a failed or interrupted run
resumes where it stopped. Use --force to redo everything, or --from STEP to redo from
bake | convert | roads | site.
"""
import argparse, base64, gzip, json, math, os, shutil, subprocess, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import places

ROOT = Path(__file__).resolve().parent
STEPS = ["bake", "convert", "roads", "site"]
ARNIS_WINDOWS_URL = "https://github.com/louis-e/arnis/releases/latest/download/arnis-windows.exe"
EARTH = 6371000.0
# How far past its own bbox a chunk's road graph reaches, so neighbours share the OSM nodes on
# their seam and the join can sew the graphs together there. Blocks.
OVERHANG = 32


def km(bbox):
    s, w, n, e = bbox
    return (n - s) * 111.32, (e - w) * 111.32 * math.cos(math.radians((s + n) / 2))


def split(bbox, max_km2):
    """Cut a bbox into a grid of near-square chunks no larger than max_km2 each."""
    s, w, n, e = bbox
    h, wd = km(bbox)
    rows = max(1, math.ceil(h / math.sqrt(max_km2)))
    cols = max(1, math.ceil(wd / (max_km2 / (h / rows))))
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append({
                "id": f"r{r}c{c}" if rows * cols > 1 else "all", "row": r, "col": c,
                # rounded to the six decimals the bake is given, so the join projects the same bbox
                "bbox": [round(v, 6) for v in (s + (n - s) * r / rows, w + (e - w) * c / cols,
                                               s + (n - s) * (r + 1) / rows, w + (e - w) * (c + 1) / cols)],
            })
    return out, rows, cols


def keep_chunks(area, chunks):
    """Drop the chunks an area does not want. "keep" in the area file lists chunk ids outright;
    "within" names an administrative boundary (e.g. {"name": "Irvine", "admin_level": "8"}) and keeps
    the chunks it covers at least min_share of (default a tenth). MC_KEEP (a JSON list) overrides both, so every job of one CI run uses
    the list its plan job worked out instead of asking the map server again."""
    ids = json.loads(os.environ["MC_KEEP"]) if os.environ.get("MC_KEEP") else area.get("keep")
    if ids is None and area.get("within") and len(chunks) > 1:
        s, w, n, e = area["bbox"]; wi = area["within"]
        q = (f'[out:json][timeout:90];rel["boundary"="administrative"]["name"="{wi["name"]}"]'
             f'["admin_level"="{wi.get("admin_level", "8")}"]({s},{w},{n},{e});out geom;')
        data = None
        for host in ("overpass-api.de", "overpass.private.coffee", "overpass.kumi.systems"):
            try:
                req = urllib.request.Request(f"https://{host}/api/interpreter", data=("data=" + urllib.parse.quote(q)).encode(),
                                             headers={"User-Agent": "mcdrives-build"})
                data = json.loads(urllib.request.urlopen(req, timeout=120).read())
                if data.get("elements"): break
            except Exception as ex: print(f"boundary: {host} failed: {ex}", file=sys.stderr)
        if not data or not data.get("elements"): sys.exit(f"boundary: no administrative boundary named {wi['name']} found")
        # Even-odd ray casting over every boundary segment: no need to stitch the ways into rings.
        segs = [(a["lon"], a["lat"], b["lon"], b["lat"]) for el in data["elements"] for m in el.get("members", [])
                if m.get("type") == "way" and m.get("geometry") for a, b in zip(m["geometry"], m["geometry"][1:])]
        def inside(lon, lat):
            return sum(1 for x0, y0, x1, y1 in segs if (y0 > lat) != (y1 > lat) and lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0)) % 2 == 1
        ids = []
        for c in chunks:
            cs, cw, cn, ce = c["bbox"]; N = 12
            share = sum(inside(cw + (ce - cw) * (i + .5) / N, cs + (cn - cs) * (j + .5) / N) for i in range(N) for j in range(N)) / N / N
            if share >= wi.get("min_share", 0.1): ids.append(c["id"])
        print(f"boundary: {wi['name']} covers enough of {len(ids)} of {len(chunks)} chunks", file=sys.stderr)
    if ids is None: return chunks
    kept = [c for c in chunks if c["id"] in ids]
    if not kept: sys.exit("no chunk left after the keep/within filter")
    return kept


NO_CARS = ("boat,crane,excavator,fountain,helicopter,jetbridge,landmark,lighthouse,plane,playground,"
           "starship,tombstone,tractor,windturbine")   # every Arnis prop family except car

# ---------------------------------------------------------------- joining chunks into one world
def hav(dlat, dlon, lat):
    a = math.sin(math.radians(dlat) / 2) ** 2 + math.cos(math.radians(lat)) ** 2 * math.sin(math.radians(dlon) / 2) ** 2
    return EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def projection(bbox):
    """Arnis's own projection for a bbox: the world's block size, and lon/lat -> block coordinates.

    It is linear inside one bbox, so a chunk's blocks differ from the parent area's by a
    translation - but the metres per degree of longitude depend on the bbox's mean latitude, so the
    scale of a chunk row is not exactly the parent's. chunk_offset() measures what that costs."""
    s, w, n, e = bbox
    sx = math.floor(hav(0, e - w, (s + n) / 2)); sz = math.floor(hav(n - s, 0, 0))
    return sx, sz, lambda lon, lat: ((lon - w) / (e - w) * sx, (1 - (lat - s) / (n - s)) * sz)


def chunk_offset(parent_bbox, bbox):
    """Where a chunk's block (0, 0) sits in the parent area's blocks, and the worst corner error.

    Returns (dx, dz, error in blocks). The error includes rounding the offset to whole blocks."""
    _, _, pxz = projection(parent_bbox)
    cx1, cz1, _ = projection(bbox)
    s, w, n, e = bbox
    dx, dz = (round(v) for v in pxz(w, n))
    err = 0.0
    for lon, lat, x, z in ((w, n, 0, 0), (e, n, cx1, 0), (w, s, 0, cz1), (e, s, cx1, cz1)):
        px, pz = pxz(lon, lat)
        err = max(err, abs(px - (dx + x)), abs(pz - (dz + z)))
    return dx, dz, err


def read_gz_array(path_bin, path_json):
    """A gzipped int16 file (a height tile, or the whole-area height.bin of older converters), or its --b64 .json."""
    import numpy as np
    if path_bin.exists():
        raw = gzip.decompress(path_bin.read_bytes())
    else:
        raw = gzip.decompress(base64.b64decode(json.loads(path_json.read_text())["base64"]))
    return np.frombuffer(raw, dtype="<i2")


def join_roads(parts, area_size, seam_tol, log=print):
    """One road graph for the whole area out of one graph per chunk.

    parts: (id, dx, dz, graph) per chunk, dx/dz being chunk_offset(). Returns the graph, the number
    of seam merges and node_map(chunk position) -> function from a chunk's node index to the joined one. Node coordinates are
    translated, the name tables merged, and the nodes that two chunks share on their seam are
    merged into one junction - by OSM id where the chunk graphs carry one, otherwise by distance.
    Then nodes outside the area are dropped and the largest connected component is kept."""
    names = [""]; nidx = {"": 0}
    nodes = []; owner = []; osm = []; edges = []; bases = []
    for ci, (cid, dx, dz, g) in enumerate(parts):
        remap = []; bases.append(len(nodes))
        for nm in g["names"]:
            if nm not in nidx: nidx[nm] = len(names); names.append(nm)
            remap.append(nidx[nm])
        base = len(nodes)
        ids = g.get("osm") or [None] * len(g["nodes"])
        for (x, z), oid in zip(g["nodes"], ids):
            nodes.append([x + dx, z + dz]); owner.append(ci); osm.append(oid)
        for a, b, nm, *rest in g["edges"]:   # rest: class, oneway, lanes, deck[, private]
            edges.append((base + a, base + b, remap[nm], *rest))

    par = list(range(len(nodes)))

    def find(i):
        while par[i] != i: par[i] = par[par[i]]; i = par[i]
        return i

    def union(a, b):
        a, b = find(a), find(b)
        if a == b: return False
        par[b] = a; return True

    by_osm = defaultdict(list)
    for i, oid in enumerate(osm):
        if oid is not None: by_osm[oid].append(i)
    by_id = sum(union(g[0], j) for g in by_osm.values() for j in g[1:])

    # Nodes that sit near the edge of their own chunk are the only candidates for a distance match.
    band = OVERHANG + seam_tol + 4
    edge_of = []
    for ci, (cid, dx, dz, g) in enumerate(parts):
        sx, sz = g["size"]
        edge_of.append((dx, dz, dx + sx, dz + sz))
    cand = [i for i, (x, z) in enumerate(nodes)
            if min(x - edge_of[owner[i]][0], edge_of[owner[i]][2] - x,
                   z - edge_of[owner[i]][1], edge_of[owner[i]][3] - z) <= band]
    cell = max(1.0, float(seam_tol))
    grid = defaultdict(list)
    for i in cand: grid[(int(nodes[i][0] // cell), int(nodes[i][1] // cell))].append(i)
    by_dist = 0
    for i in cand:
        gx, gz = int(nodes[i][0] // cell), int(nodes[i][1] // cell)
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for j in grid.get((gx + a, gz + b), ()):
                    if j <= i or owner[j] == owner[i] or osm[i] is not None and osm[i] == osm[j]: continue
                    if (nodes[i][0] - nodes[j][0]) ** 2 + (nodes[i][1] - nodes[j][1]) ** 2 <= cell * cell:
                        by_dist += union(i, j)
    log(f"  seams: merged {by_id} nodes on shared OSM ids, {by_dist} more within {seam_tol} blocks")

    # one node per group, at the mean of its members
    group = defaultdict(list)
    for i in range(len(nodes)): group[find(i)].append(i)
    pos = {}
    for r, members in group.items():
        pos[r] = [sum(nodes[i][0] for i in members) / len(members), sum(nodes[i][1] for i in members) / len(members)]
    # drop anything outside the area (the overhang, and the 4-block margin kept by roadgraph.py)
    asx, asz = area_size
    # ... and anything past the built world: where a kept chunk borders one that was not built, its overhang
    # roads would run out over empty space
    def built(p): return any(x0 - 1 <= p[0] <= x1 + 1 and z0 - 1 <= p[1] <= z1 + 1 for x0, z0, x1, z1 in edge_of)
    keep = {r for r, p in pos.items() if 4 < p[0] < asx - 4 and 4 < p[1] < asz - 4 and built(p)}
    link = defaultdict(set)
    kept_edges = set()
    for a, b, *rest in edges:
        ra, rb = find(a), find(b)
        if ra == rb or ra not in keep or rb not in keep: continue
        kept_edges.add((ra, rb, *rest))
        link[ra].add(rb); link[rb].add(ra)
    seen = set(); best = []
    for s in link:
        if s in seen: continue
        comp = [s]; seen.add(s); st = [s]
        while st:
            u = st.pop()
            for v in link[u]:
                if v not in seen: seen.add(v); st.append(v); comp.append(v)
        if len(comp) > len(best): best = comp
    big = set(best)
    order = sorted(big, key=lambda r: pos[r])
    ids = {r: i for i, r in enumerate(order)}
    out_nodes = [[round(pos[r][0], 1), round(pos[r][1], 1)] for r in order]
    out_edges = sorted({(ids[a], ids[b], *rest) for a, b, *rest in kept_edges if a in big and b in big})
    log(f"  roads: {len(nodes)} chunk nodes -> {len(group)} after merging -> {len(out_nodes)} in the "
        f"largest component, {len(out_edges)} edges, {len(names)} names")
    def node_map(ci):
        """chunk ci's node index -> joined node index (None where the node was dropped)"""
        return lambda i: ids.get(find(bases[ci] + i))
    return ({"size": [asx, asz], "names": names, "nodes": out_nodes, "edges": [list(e) for e in out_edges]},
            by_id + by_dist, node_map)


def join_places(info, node_map, roads, area_size, log=print):
    """One places.json for the area: each chunk's places moved by its offset, nodes remapped to the
    joined graph (re-snapped when the node was dropped), the overlap between chunks de-duplicated.
    A chunk without places.json counts as empty."""
    items = []; asx, asz = area_size
    for ci, i in enumerate(info):
        pp = i["dir"] / "places.json"
        if not pp.exists(): log(f"  places: chunk {i['id']} has no places.json, treating as empty"); continue
        m = node_map(ci)
        for name, kind, x, z, nd, al, *at in json.loads(pp.read_text()):
            x, z = round(x + i["dx"], 1), round(z + i["dz"], 1)
            # at: the arrival point (places.py), shifted like the place; older chunks have none
            at = [[round(at[0][0] + i["dx"], 1), round(at[0][1] + i["dz"], 1)]] if at and at[0] else []
            if 0 <= x <= asx and 0 <= z <= asz: items.append([name, kind, x, z, None if nd is None else m(nd), al, *at])
    n0 = len(items)
    snap = places.Snapper(roads)
    import arrival
    A = arrival.Graph(roads)
    for it in items:
        if len(it) > 6:   # the node at the arrival point's edge in the joined graph (the arrival point is on a road)
            r = A.snap(it[6][0], it[6][1], None); e = roads["edges"][r["edge"]]
            it[4] = r["node"] if r["node"] is not None else (e[0] if r["t"] < 0.5 else e[1])
        if it[4] is None: it[4] = snap(it[2], it[3])
    rows = places.dedupe(items, key=lambda it: (it[4] is not None, len(it[5])))
    rows.sort(key=lambda r: (r[0].lower(), r[2], r[3]))
    log(f"  places: {n0} in the chunks -> {len(rows)} after overlap de-duplication, {sum(r[4] is not None for r in rows)} routable")
    return rows


def join_addresses(parts, area_size, log=print):
    """One address list for the area: per-chunk data/addresses.json (tools/addresses.py, in the chunk's
    roads.json frame) shifted by the chunk offset like the roads, clipped to the area, overlap de-duplicated."""
    asx, asz = area_size
    streets, sidx, seen, out = [], {}, set(), []
    for cid, dx, dz, d in parts:
        a = d["a"]
        for k in range(0, len(a), 4):
            st, num, x, z = d["streets"][a[k]], a[k + 1], a[k + 2] + dx, a[k + 3] + dz
            if not (0 <= x < asx and 0 <= z < asz) or (num, st) in seen: continue
            seen.add((num, st))
            if st not in sidx: sidx[st] = len(streets); streets.append(st)
            out += [sidx[st], num, round(x), round(z)]
    log(f"  addresses: {len(out) // 4:,} on {len(streets):,} streets")
    return {"v": 1, "streets": streets, "a": out}


def copy_web(site):
    """Put the viewer and the homepage into a built site. The map app is served at /map (map.html, which
    Cloudflare Pages also answers as /map); the homepage pages from web/home sit at the root, with their
    css, js and images under /assets. Both share /vendor (three.js, fonts)."""
    shutil.copy(ROOT / "web" / "index.html", site / "map.html")
    shutil.copytree(ROOT / "web" / "vendor", site / "vendor", dirs_exist_ok=True)
    shutil.copytree(ROOT / "web" / "drive", site / "drive", dirs_exist_ok=True)
    home = ROOT / "web" / "home"
    for f in home.glob("*.html"):
        shutil.copy(f, site / f.name)
    shutil.copytree(home / "assets", site / "assets", dirs_exist_ok=True)

def join(area, chunks, out, seam_tol=6, log=print):
    """Write one site for the whole area from the per-chunk sites already in out/<id>/site.

    Packs and their height tiles are linked (never re-encoded) and given unique ids plus the offset
    of their chunk, so nothing holds more than one chunk at a time. Where chunks overlap, the viewer
    takes a column's height from the last pack in the list that has a surface there, as the merged
    height field of earlier joins did."""
    import numpy as np
    parent = area["bbox"]
    psx, psz, _ = projection(parent)
    site = out / "site"; data = site / "data"
    missing = [c["id"] for c in chunks if not (out / c["id"] / "site" / "data" / "meta.json").exists()]
    if missing: sys.exit(f"join: no converted chunk at {out}/<id>/site/data for: {', '.join(missing)}")

    info = []
    for c in chunks:
        cs = out / c["id"] / "site"
        cm = json.loads((cs / "data" / "meta.json").read_text())
        dx, dz, err = chunk_offset(parent, c["bbox"])
        info.append({"id": c["id"], "bbox": c["bbox"], "dir": cs, "meta": cm, "dx": dx, "dz": dz, "err": round(err, 2)})
    worst = max(i["err"] for i in info)
    log(f"join: {len(info)} chunks, projection error at chunk corners at most {worst:.2f} blocks")
    if worst > 8: log(f"join: WARNING projection error {worst:.1f} blocks is large; chunk seams will not line up")

    ref = json.dumps(info[0]["meta"]["materials"])
    for i in info[1:]:
        if json.dumps(i["meta"]["materials"]) != ref:
            sys.exit(f"join: chunk {i['id']} has a different material table than {info[0]['id']}. "
                     "Re-run convert for every chunk with the current tools/mca2web.py.")
    ext = info[0]["meta"]["ext"]
    if ext != "bin": sys.exit("join: --b64 chunks cannot be joined; re-convert without --b64")
    minx = min(i["dx"] + i["meta"]["minx"] for i in info)
    minz = min(i["dz"] + i["meta"]["minz"] for i in info)
    for i in info:
        i["sx"] = i["dx"] + i["meta"]["minx"] - minx
        i["sz"] = i["dz"] + i["meta"]["minz"] - minz
    # Arnis fits the terrain of each bake to its own base level, so chunks do not share a ground
    # height. Neighbours overlap by a strip of blocks: the median height difference there is the
    # step between them, and least squares turns the pairwise steps into one lift per chunk.
    def rect(i): return i["sx"], i["sz"], i["sx"] + i["meta"]["width"], i["sz"] + i["meta"]["depth"]

    def ground(i, x0, z0, x1, z1):
        """Layer 0 of chunk i over its own columns [z0:z1, x0:x1], from its height tiles (or an older height.bin)."""
        cm = i["meta"]; P = cm["pack"]
        if not all("h" in p for p in cm["packs"]):
            h0 = read_gz_array(i["dir"] / "data" / "height.bin", i["dir"] / "data" / "height.json")
            h0 = np.pad(h0[:cm["depth"] * cm["width"]].reshape(cm["depth"], cm["width"]), 16, constant_values=-1)
            return h0[max(0, z0 + 16):z1 + 16, max(0, x0 + 16):x1 + 16].copy()
        out = np.full((z1 - z0, x1 - x0), -1, dtype="<i2")
        for p in cm["packs"]:
            ax, az, bx, bz = max(x0, p["x"]), max(z0, p["z"]), min(x1, p["x"] + P), min(z1, p["z"] + P)
            if bx <= ax or bz <= az: continue
            t = read_gz_array(i["dir"] / "data" / f"{p['h']}.bin", i["dir"] / "data" / f"{p['h']}.json")[:P * P].reshape(P, P)
            out[az - z0:bz - z0, ax - x0:bx - x0] = t[az - p["z"]:bz - p["z"], ax - p["x"]:bx - p["x"]]
        return out
    strips = {}
    for a, i in enumerate(info):
        for b, j in enumerate(info):
            if a == b: continue
            x0, z0 = (max(p, q) for p, q in zip(rect(i)[:2], rect(j)[:2])); x1 = min(rect(i)[2], rect(j)[2]); z1 = min(rect(i)[3], rect(j)[3])
            if min(x1 - x0, z1 - z0) < 8 or max(x1 - x0, z1 - z0) < 400: continue   # corner neighbours share too little
            x0 -= 8; z0 -= 8; x1 += 8; z1 += 8   # bakes may stop just short of each other
            strips[a, b] = ground(i, x0 - i["sx"], z0 - i["sz"], x1 - i["sx"], z1 - i["sz"]).astype(np.int32)
    rows, rhs = [], []
    for (a, b), sa in strips.items():
        if a > b: continue
        sb = strips[b, a]; ok = (sa > 2) & (sb > 2)
        d = (sb - sa)[ok]
        if d.size < 200:
            # The bakes barely overlap: compare each chunk's last surface before the seam with the
            # other's first one after it, line by line across the seam.
            if sa.shape[0] < sa.shape[1]: sa, sb = sa.T, sb.T
            va, vb = sa > 2, sb > 2; n = sa.shape[1]; col = np.arange(n); d = []
            for p, vp, q, vq in ((sa, va, sb, vb), (sb, vb, sa, va)):   # p ends where q begins
                last = np.where(vp, col, -1).max(axis=1); first = np.where(vq, col, n).min(axis=1)
                r = np.nonzero((last >= 0) & (first < n) & (np.abs(first - last) <= 6))[0]
                d.append((q[r, first[r]] - p[r, last[r]]) * (1 if p is sa else -1))
            d = max(d, key=len)
        if d.size < 50: continue
        step = float(np.median(d))
        r = np.zeros(len(info)); r[a], r[b] = 1, -1; rows.append(r); rhs.append(step)   # lift[a] - lift[b] = h_b - h_a
        log(f"join: ground step {info[a]['id']} to {info[b]['id']}: {step:+.0f} blocks "
            f"(middle half of the overlap within {np.percentile(d, 25):+.0f}..{np.percentile(d, 75):+.0f})")
    lift = np.zeros(len(info))
    if rows:
        A = np.vstack(rows + [np.eye(len(info))[0]]); lift = np.linalg.lstsq(A, np.array(rhs + [0.0]), rcond=None)[0]
        res = np.abs(A[:-1] @ lift - np.array(rhs)).max()
        log(f"join: ground lifts {[int(round(v)) for v in lift]}, worst disagreement between neighbours {res:.1f} blocks")
        if res > 2: log("join: WARNING chunks disagree about ground height by more than 2 blocks; seams will show a step")
    for i, v in zip(info, lift): i["lift"] = int(round(v))
    miny = min(i["meta"]["miny"] + i["lift"] for i in info)
    width = max(i["dx"] + i["meta"]["minx"] + i["meta"]["width"] for i in info) - minx
    depth = max(i["dz"] + i["meta"]["minz"] + i["meta"]["depth"] for i in info) - minz
    height = max(i["meta"]["miny"] + i["lift"] + i["meta"]["height"] for i in info) - miny
    for i in info: i["sy"] = i["meta"]["miny"] + i["lift"] - miny
    log(f"join: {width} x {depth} x {height} blocks, origin ({minx}, {minz}, {miny})")

    shutil.rmtree(site, ignore_errors=True); data.mkdir(parents=True)
    # ---- packs and height tiles: link the bytes, translate the ids and the placement
    packs = []; used = set(); total = 0; hbytes = 0; quads = 0

    def put(src, dst):
        try: os.link(src, dst)
        except OSError: shutil.copy(src, dst)
        return dst.stat().st_size
    for i in info:
        cm, P = i["meta"], i["meta"]["pack"]
        old = None   # a chunk converted before height tiles existed: cut its height.bin into tiles here
        if not all("h" in p for p in cm["packs"]):
            old = read_gz_array(i["dir"] / "data" / "height.bin", i["dir"] / "data" / "height.json")
            old = old.reshape(-1, cm["depth"], cm["width"])[[0, -1]]
        for p in cm["packs"]:
            pid = p["id"] if p["id"] not in used else f"{i['id']}_{p['id']}"
            while pid in used: pid += "_"
            hid = "h" + pid[1:] if pid.startswith("p_") else "h_" + pid
            used.add(pid)
            total += put(i["dir"] / "data" / f"{p['id']}.{ext}", data / f"{pid}.{ext}"); quads += p["quads"]
            if old is None: hbytes += put(i["dir"] / "data" / f"{p['h']}.{ext}", data / f"{hid}.{ext}")
            else:
                t = np.full((2, P, P), -1, dtype="<i2")
                src = old[:, p["z"]:p["z"] + P, p["x"]:p["x"] + P]
                t[:, :src.shape[1], :src.shape[2]] = src
                (data / f"{hid}.{ext}").write_bytes(gzip.compress(t.tobytes(), 6)); hbytes += (data / f"{hid}.{ext}").stat().st_size
            packs.append({"id": pid, "h": hid, "x": p["x"] + i["sx"], "z": p["z"] + i["sz"], "quads": p["quads"],
                          "dx": i["sx"], "dz": i["sz"], "dy": i["sy"], "tiles": p["tiles"]})
        del old
    log(f"join: {len(packs)} packs, {total/1e6:.1f} MB, {quads:,} quads; height tiles {hbytes/1e6:.1f} MB")

    # ---- roads
    parts = []
    for i in info:
        rp = i["dir"] / "roads.json"
        if not rp.exists(): sys.exit(f"join: chunk {i['id']} has no roads.json")
        parts.append((i["id"], i["dx"], i["dz"], json.loads(rp.read_text())))
    roads, merged, node_map = join_roads(parts, (psx, psz), seam_tol, log)
    (site / "roads.json").write_text(json.dumps(roads, separators=(",", ":")))
    # ---- addresses for search (optional: a chunk without them is skipped)
    aparts = [(i["id"], i["dx"], i["dz"], json.loads((i["dir"] / "data" / "addresses.json").read_text()))
              for i in info if (i["dir"] / "data" / "addresses.json").exists()]
    if aparts: (data / "addresses.json").write_text(json.dumps(join_addresses(aparts, (psx, psz), log), separators=(",", ":")))
    pl = join_places(info, node_map, roads, (psx, psz), log)
    (site / "places.json").write_text(json.dumps(pl, separators=(",", ":"), ensure_ascii=False))

    meta = {"tile": info[0]["meta"]["tile"], "pack": info[0]["meta"]["pack"], "ext": ext,
            "width": width, "depth": depth, "height": height, "minx": minx, "minz": minz, "miny": miny,
            "quads": quads, "bytes": total, "packs": packs, "materials": info[0]["meta"]["materials"],
            "name": area["name"], "bbox": parent,
            "chunks": [{"id": i["id"], "dx": i["sx"], "dz": i["sz"], "dy": i["sy"], "err": i["err"]} for i in info]}
    (data / "meta.json").write_text(json.dumps(meta))
    # overhead colour layer for the 2D map (optional: the map falls back to roads on parchment)
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "maptiles.py"), str(site)], capture_output=True, text=True)
    log("join: " + (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else f"join: map colour layer skipped ({r.stderr.strip()[-300:]})")
    copy_web(site)
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
    return {"chunks": len(info), "blocks": [width, depth, height], "packs": len(packs), "quads": quads,
            "nodes": len(roads["nodes"]), "places": len(pl), "edges": len(roads["edges"]), "seam_nodes_merged": merged,
            "projection_error_blocks": round(worst, 2), "site_mb": round(size / 1e6, 2)}


def find_arnis(arg):
    for cand in [arg, os.environ.get("ARNIS_BIN"), ROOT / "bin" / "arnis-windows.exe", ROOT / "bin" / "arnis",
                 shutil.which("arnis")]:
        if cand and Path(cand).is_file():
            return str(cand)
    if os.name == "nt":
        dest = ROOT / "bin" / "arnis-windows.exe"
        dest.parent.mkdir(exist_ok=True)
        print(f"downloading Arnis to {dest}")
        urllib.request.urlretrieve(ARNIS_WINDOWS_URL, dest)
        return str(dest)
    sys.exit("Arnis not found. Pass --arnis PATH, set ARNIS_BIN, or put the binary in bin/ "
             "(build it with: cargo build --release --no-default-features).")


def run(cmd, log, env=None):
    print("  $", " ".join(str(c) for c in cmd))
    with open(log, "ab") as f:
        r = subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT, env=env)
    if r.returncode:
        tail = Path(log).read_text(errors="replace")[-3000:]
        sys.exit(f"step failed (exit {r.returncode}); see {log}\n{tail}")


def token_env(area, chunk_km2):
    """Mapillary street photos are optional. The token is read from a file outside the repo or
    from the environment, handed to Arnis for that one run, and never printed or written."""
    env = os.environ.copy()
    if not area.get("mapillary") or chunk_km2 > 0.10:  # Arnis only fetches facades for tiny areas
        env.pop("MAPILLARY_TOKEN", None)
        return env
    path = os.environ.get("MAPILLARY_TOKEN_FILE")
    if path and Path(path).is_file():
        env["MAPILLARY_TOKEN"] = Path(path).read_text().strip()
    return env


def build_chunk(area, chunk, args, work, overhang=0):
    bbox = chunk["bbox"]
    bstr = ",".join(f"{v:.6f}" for v in bbox)
    h, w = km(bbox)
    work.mkdir(parents=True, exist_ok=True)
    site, log = work / "site", work / "build.log"
    world_parent, osm = work / "world", work / "osm.json"
    if args.world: world_parent = Path(args.world)
    if args.osm: osm = Path(args.osm)
    redo = STEPS.index(args.from_step) if args.from_step else (0 if args.force else len(STEPS))
    times = {}

    def step(name, done):
        if STEPS.index(name) < redo and done():
            print(f"[{chunk['id']}] {name}: up to date"); return False
        print(f"[{chunk['id']}] {name}"); times[name] = time.time(); return True

    def world_dir():
        hits = [p.parent for p in world_parent.rglob("region") if any(p.glob("*.mca"))] if world_parent.exists() else []
        return hits[0] if hits else None

    if step("bake", lambda: world_dir() and osm.exists()):
        if args.world: sys.exit(f"--world {args.world} has no region files")
        shutil.rmtree(world_parent, ignore_errors=True); world_parent.mkdir(parents=True)
        cmd = [find_arnis(args.arnis), f"--bbox={bstr}", f"--output-dir={world_parent}",
               f"--save-json-file={osm}", "--no-update-check"]
        if area.get("lidar", True) and h * w <= 16: cmd.append("--lidar")
        # Parked cars are left out: a blocky car on every other parking space reads as clutter from the
        # driver's seat. An area can set "props" to "all" or its own list to change that.
        cmd.append("--props=" + area.get("props", NO_CARS))
        cmd += area.get("arnis_args", [])
        run(cmd, log, token_env(area, h * w))
        if not world_dir(): sys.exit(f"Arnis finished but wrote no world; see {log}")
        times["bake"] = time.time() - times["bake"]

    data = site / "data"
    if step("convert", lambda: (data / "meta.json").exists()):
        shutil.rmtree(site, ignore_errors=True); data.mkdir(parents=True)
        run([sys.executable, ROOT / "tools" / "mca2web.py", world_dir(), data] + (["--b64"] if args.b64 else []), log)
        times["convert"] = time.time() - times["convert"]

    # street addresses for search, in the same frame as roads.json; optional, so a network hiccup never fails a build.
    # Fetched before the roads step, which reads them to learn a place's street from its house number.
    if not (data / "addresses.json").exists() or (STEPS.index("roads") >= redo):
        try: run([sys.executable, ROOT / "tools" / "addresses.py", bstr, data / "addresses.json"], log)
        except BaseException as e: print(f"[{chunk['id']}] addresses skipped: {e}")
    if step("roads", lambda: (site / "roads.json").exists() and (site / "places.json").exists()):
        cmd = [sys.executable, ROOT / "tools" / "roadgraph.py", bstr, osm, site / "roads.json", "--places", site / "places.json"]
        if (data / "addresses.json").exists(): cmd += ["--addresses", data / "addresses.json"]
        if area.get("private_roads"): cmd.append("--keep-private")   # gated streets, used only to start or end a trip
        if overhang: cmd += ["--overhang", overhang]   # reach past the bbox so the join can sew seams
        run(cmd, log)
        times["roads"] = time.time() - times["roads"]

    if step("site", lambda: (site / "map.html").exists()):
        meta = json.loads((data / "meta.json").read_text())
        meta.update(name=area["name"] + ("" if chunk["id"] == "all" else f" ({chunk['id']})"), bbox=bbox)
        (data / "meta.json").write_text(json.dumps(meta))
        copy_web(site)
        times["site"] = time.time() - times["site"]

    meta = json.loads((data / "meta.json").read_text())
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
    return {"id": chunk["id"], "row": chunk["row"], "col": chunk["col"], "bbox": bbox, "km2": round(h * w, 3),
            "blocks": [meta["width"], meta["depth"], meta["height"]], "quads": meta["quads"], "packs": len(meta["packs"]),
            "site_mb": round(size / 1e6, 2), "seconds": {k: round(v, 1) for k, v in times.items() if v < 1e6}}


def chunk_summary(area, chunk, work):
    """The report entry for a chunk that is already built, without running or checking any step.
    Used by --join-only, where the chunk outputs were built elsewhere (one CI job each)."""
    site = work / "site"
    if not (site / "data" / "meta.json").exists(): return None
    meta = json.loads((site / "data" / "meta.json").read_text())
    h, w = km(chunk["bbox"])
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
    return {"id": chunk["id"], "row": chunk["row"], "col": chunk["col"], "bbox": chunk["bbox"], "km2": round(h * w, 3),
            "blocks": [meta["width"], meta["depth"], meta["height"]], "quads": meta["quads"],
            "packs": len(meta["packs"]), "site_mb": round(size / 1e6, 2), "seconds": {}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("area", help="area config, e.g. areas/spectrum.json")
    ap.add_argument("--arnis", help="path to the Arnis binary (else ARNIS_BIN, bin/, PATH, or auto-download on Windows)")
    ap.add_argument("--world", help="reuse an existing Arnis world folder instead of baking (single-chunk areas)")
    ap.add_argument("--osm", help="OSM JSON saved by that bake (--save-json-file)")
    ap.add_argument("--out", default=str(ROOT / "build"), help="output root (default: build/)")
    ap.add_argument("--b64", action="store_true", help="write packs as base64 JSON, for hosts that refuse .bin files")
    ap.add_argument("--plan", action="store_true", help="print the chunk plan and exit")
    ap.add_argument("--plan-ids", action="store_true", help="print the chunk ids as a JSON list and exit (for CI matrices)")
    ap.add_argument("--only", help="build just these chunk ids, comma-separated")
    ap.add_argument("--force", action="store_true", help="redo every step")
    ap.add_argument("--from", dest="from_step", choices=STEPS, help="redo from this step onward")
    ap.add_argument("--join-only", action="store_true",
                    help="skip the chunk builds and only join the chunk outputs already in the output folder")
    ap.add_argument("--no-join", action="store_true", help="build the chunks but do not join them")
    ap.add_argument("--seam-tol", type=int, default=6, metavar="BLOCKS",
                    help="how far apart two chunks' road nodes may be and still be the same junction (default 6)")
    ap.add_argument("--serve", action="store_true", help="serve the result on http://localhost:8765 when done")
    args = ap.parse_args()

    area = json.loads(Path(args.area).read_text())
    chunks, rows, cols = split(area["bbox"], area.get("max_chunk_km2", 4))
    chunks = keep_chunks(area, chunks)
    h, w = km(area["bbox"])
    if args.plan_ids:
        print(json.dumps([c["id"] for c in chunks])); return
    print(f"{area['name']}: {h:.2f} x {w:.2f} km = {h*w:.2f} km2 in {len(chunks)} chunk(s) ({rows} x {cols})")
    if args.plan:
        for c in chunks:
            dx, dz, err = chunk_offset(area["bbox"], c["bbox"])
            print(" ", c["id"], ",".join(f"{v:.6f}" for v in c["bbox"]), f"at block ({dx}, {dz}), error {err:.2f}")
        return
    overhang = OVERHANG if len(chunks) > 1 else 0
    out = Path(args.out) / area["slug"]
    if args.only: chunks = [c for c in chunks if c["id"] in args.only.split(",")]
    if not chunks: sys.exit(f"--only {args.only} matched no chunk")
    if (args.world or args.osm) and len(chunks) > 1:
        sys.exit("--world/--osm cover one bake, so use them with --only <chunk id> on a multi-chunk area")

    # Chunks may be built one at a time, or on separate machines, so the report keeps what is
    # already there and replaces only the chunks this run rebuilt.
    report = {"name": area["name"], "bbox": area["bbox"], "rows": rows, "cols": cols, "chunks": []}
    if (out / "report.json").exists():
        try: report.update(json.loads((out / "report.json").read_text()))
        except ValueError: pass
    t0 = time.time()
    done = []
    if not args.join_only:
        for c in chunks:
            done.append(build_chunk(area, c, args, out / c["id"], overhang))
            report["chunks"] = [r for r in report["chunks"] if r["id"] not in {d["id"] for d in done}] + done
            report["chunks"].sort(key=lambda r: (r["row"], r["col"]))
            (out / "report.json").write_text(json.dumps(report, indent=1))
        print(f"\nchunks done in {time.time()-t0:.0f}s: {sum(c['km2'] for c in done):.2f} km2, "
              f"{sum(c['quads'] for c in done):,} quads, {sum(c['site_mb'] for c in done):.1f} MB")

    # Chunks built elsewhere (one CI job each) have no report entry here, so fill them in.
    full, _, _ = split(area["bbox"], area.get("max_chunk_km2", 4))
    full = keep_chunks(area, full)
    have = {r["id"] for r in report["chunks"]}
    report["chunks"] += [s for s in (chunk_summary(area, c, out / c["id"]) for c in full if c["id"] not in have) if s]
    report["chunks"].sort(key=lambda r: (r["row"], r["col"]))

    # One site for the whole area. A single-chunk area is already one world, so it is left alone.
    site = out / chunks[0]["id"] / "site" if rows * cols == 1 else out / "site"
    missing = [c["id"] for c in full if not (out / c["id"] / "site" / "data" / "meta.json").exists()]
    if rows * cols > 1 and not args.no_join and not missing:
        tj = time.time()
        report["join"] = join(area, full, out, args.seam_tol)
        report["join"]["seconds"] = round(time.time() - tj, 1)
    elif rows * cols > 1:
        why = "--no-join" if args.no_join else f"{len(missing)} chunk(s) not built yet: {', '.join(missing[:6])}"
        print(f"not joining ({why}); run `python build.py {args.area} --join-only` when every chunk is there")
        site = out / chunks[0]["id"] / "site"
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(f"\ndone in {time.time()-t0:.0f}s. Site: {site}  Report: {out/'report.json'}")
    if args.serve:
        print(f"serving {site} at http://localhost:8765  (Ctrl+C to stop)")
        subprocess.run([sys.executable, "-m", "http.server", "8765", "--directory", str(site)])


if __name__ == "__main__":
    main()
