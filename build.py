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
import metafmt

ROOT = Path(__file__).resolve().parent
STEPS = ["bake", "convert", "roads", "site"]
# blocks per metre of height in every bake of an area with "elevation_base_m" (token_env())
ELEVATION_SCALE = 1.0
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


LAP_M = 3.0
def lap(area, chunks):
    """In a conformal area each chunk is baked LAP_M metres past its south and east sides. Arnis cuts a
    world to whole blocks, so a chunk can end a block short of its neighbour, which shows as a line of
    sky along the seam; with the lap, neighbours always overlap by a block or more and the later one
    covers the seam. Everything made for a chunk uses this wider box; its north-west corner, which
    places it, does not move."""
    if frame(area) is None: return chunks
    for c in chunks:
        s, w, n, e = c["bbox"]
        c["bbox"] = [round(s - LAP_M / 111320, 6), w, n, round(e + LAP_M / (111320 * math.cos(math.radians((s + n) / 2))), 6)]
        if area.get("bake_pad_m"):
            # A padded bake (pad_frame()) keeps the chunk's blocks where an unpadded bake would put them only if
            # the bbox's width and height are less than half a metre past a whole metre (see pad_frame()), so
            # the south and the east lap each grow by up to 1.5 m, in the 6-decimal steps the bbox is given in, until they are.
            b = c["bbox"]
            for i in range(16):
                sb = round(s - (LAP_M + i * 0.1) / 111320, 6)
                if math.modf(hav(n - sb, 0, 0))[0] < 0.5: b[0] = sb; break
            for i in range(16):
                eb = round(e + (LAP_M + i * 0.1) / (111320 * math.cos(math.radians((s + n) / 2))), 6)
                if math.modf(hav(0, eb - w, (b[0] + n) / 2))[0] < 0.5: b[3] = eb; break
    return chunks


def pad_frame(bbox, scale, pad_m):
    """The bbox a chunk is baked with when its area sets "bake_pad_m": `bbox` grown by about pad_m metres on
    every side, so that what Arnis works out over the whole bbox at once has settled by the chunk's own edge.

    Arnis fits and smooths a bake's terrain over its own bbox: the built-up Gaussian (sigma 30 m, 91 cells
    each way) is renormalised over the cells inside the bbox, so at a bbox edge it averages one side only
    and two neighbours see the same slope from opposite sides; the coastal pull (25 m), the anomaly repair
    and the road profiles stop at the edge too. With the pad all of that happens out in the pad, which
    mca2web.py --crop then drops.

    Returns (bake bbox, (x0, z0, x1, z1)): the block window of the padded world that is the chunk, end
    exclusive. Its corner (x0, z0) is a whole block and the padded bake has the same blocks per degree as
    an unpadded one (the padded width is (M + N) * scale blocks for an unpadded M * scale, and Arnis's
    floor() of the metres gives M + N back), so the window is the chunk exactly as projection(bbox, scale)
    places it, and nothing downstream (roads, addresses, the join) knows the bake was padded. lap() keeps
    the bbox's metres under half a metre past a whole one so such an N exists."""
    s, w, n, e = bbox
    sx, sz, _ = projection(bbox, scale)
    mx, mz = round(sx / scale), round(sz / scale)
    gx, gz = (e - w) / sx, (n - s) / sz            # degrees per block, which the padded bake must keep
    P = int(math.ceil(pad_m * scale))             # whole blocks of pad on the west and north sides
    best = None
    for nz in range(math.ceil(2 * P / scale) + 1, math.ceil(2 * P / scale) + 60):
        nb = n + P * gz; sb = nb - (mz + nz) * scale * gz
        if math.floor(hav(nb - sb, 0, 0)) != mz + nz: continue
        for nx in range(math.ceil(2 * P / scale) + 1, math.ceil(2 * P / scale) + 60):
            wb = w - P * gx; eb = wb + (mx + nx) * scale * gx
            b = [round(v, 9) for v in (sb, wb, nb, eb)]
            bx, bz, pj = projection(b, scale)
            x0, z0 = pj(w, n); x1, z1 = pj(e, s)
            err = max(abs(x0 - P), abs(z0 - P), abs(x1 - P - sx), abs(z1 - P - sz))
            if best is None or err < best[0]: best = (err, b)
            if err < 0.01: break
        if best and best[0] < 0.01: break
    if best is None or best[0] > 0.05:
        sys.exit(f"pad_frame: no padded bbox for {bbox} keeps the chunk's blocks (best error {best and best[0]:.3f} blocks)")
    return best[1], (P, P, P + int(sx) + 1, P + int(sz) + 1)


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


def projection(bbox, scale=1.0):
    """Arnis's own projection for a bbox: the world's block size, and lon/lat -> block coordinates.

    It is linear inside one bbox, so a chunk's blocks differ from the parent area's by a
    translation - but the metres per degree of longitude depend on the bbox's mean latitude, so the
    scale of a chunk row is not exactly the parent's. chunk_offset() measures what that costs.
    scale is Arnis's --scale, which multiplies the whole metres of each side (so the size may be a
    fraction; the world's last block is its integer part). tools/roadgraph.py, tools/addresses.py and
    web/drive/traffic.js hold copies of this."""
    s, w, n, e = bbox
    sx = math.floor(hav(0, e - w, (s + n) / 2)); sz = math.floor(hav(n - s, 0, 0))
    if scale != 1: sx *= scale; sz *= scale
    return sx, sz, lambda lon, lat: ((lon - w) / (e - w) * sx, (1 - (lat - s) / (n - s)) * sz)


def frame(area):
    """The latitude lat0 of a "conformal" area's frame, or None for the plain frame of projection().

    A tall area cannot use projection() for its chunks: a row far from the middle latitude is
    several blocks wider or narrower than the slot that frame gives it, so its seams gap or overlap.
    With "conformal": true in the area file the frame is Mercator, 1 block = 1 metre at lat0, and
    every chunk is baked at chunk_scale() so that its blocks fit its slot in both directions.
    lat0 is the middle of the chunk row nearest the equator, which makes every scale 1 or a little
    more: below 1 Arnis narrows each road by a lane and rounds each building a block down."""
    s, _, n, _ = area["bbox"]
    rows = split(area["bbox"], area.get("max_chunk_km2", 4))[1]
    if not area.get("conformal") or rows == 1: return None
    return min((s + (n - s) * (r + .5) / rows for r in range(rows)), key=abs)


def chunk_scale(lat0, bbox):
    """Arnis's --scale for a chunk in the frame of lat0: 1 without one, and for the row at lat0."""
    if lat0 is None: return 1.0
    return round(math.cos(math.radians(lat0)) / math.cos(math.radians((bbox[0] + bbox[2]) / 2)), 6)


def area_projection(bbox, lat0=None):
    """The frame the chunks of an area are joined in: projection(), or Mercator true to scale at lat0."""
    if lat0 is None: return projection(bbox)
    s, w, n, e = bbox
    r = EARTH * math.cos(math.radians(lat0)); y = lambda lat: math.asinh(math.tan(math.radians(lat)))
    return (math.floor(r * math.radians(e - w)), math.floor(r * (y(n) - y(s))),
            lambda lon, lat: (r * math.radians(lon - w), r * (y(n) - y(lat))))


def chunk_offset(parent_bbox, bbox, lat0=None):
    """Where a chunk's block (0, 0) sits in the parent area's blocks, and the worst corner error.

    Returns (dx, dz, error in blocks). The error includes rounding the offset to whole blocks."""
    _, _, pxz = area_projection(parent_bbox, lat0)
    cx1, cz1, _ = projection(bbox, chunk_scale(lat0, bbox))
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


class Built:
    """The built land of a joined area: the union of its chunks' rectangles, in the area frame.

    rects: (x0, z0, x1, z1) per built chunk, each its slot in the area (chunk_offset() and the size of
    projection() at the chunk's scale, the same box join_roads() keeps road nodes in). A point counts as
    built when it lies in one of them, give or take tol blocks. A whole-rectangle area is one big union
    of every chunk; a keep-list or "within" area has holes and ragged edges, and nothing past them exists
    in the world, so search must not send a driver there."""
    CELL = 1024

    def __init__(self, rects):
        self.rects = list(rects); self.grid = defaultdict(list)
        for r in self.rects:
            for gx in range(int(r[0] // self.CELL) - 1, int(r[2] // self.CELL) + 2):
                for gz in range(int(r[1] // self.CELL) - 1, int(r[3] // self.CELL) + 2):
                    self.grid[(gx, gz)].append(r)

    def __call__(self, x, z, tol=0.0):
        return any(x0 - tol <= x <= x1 + tol and z0 - tol <= z <= z1 + tol
                   for x0, z0, x1, z1 in self.grid.get((int(x // self.CELL), int(z // self.CELL)), ()))


def chunk_rect(i):
    """A joined chunk's slot in the area frame (info entry from join())."""
    w, d, _ = projection(i["bbox"], i["meta"].get("scale", 1))
    return (i["dx"], i["dz"], i["dx"] + w, i["dz"] + d)


def join_places(info, node_map, roads, area_size, log=print, built=None):
    """One places.json for the area: each chunk's places moved by its offset, nodes remapped to the
    joined graph (re-snapped when the node was dropped), the overlap between chunks de-duplicated.
    A chunk without places.json counts as empty.

    Only places whose centre lies on built land (built: a Built) are kept. A chunk's places.json holds
    every named element of its OSM extract, and that extract has whole ways that only touch the chunk
    (a park, a campus, a building on the edge), so their centres can lie hundreds of blocks past it; inside
    the area that is the neighbour's land, but next to a chunk that was not built there is no world.
    An arrival point on a road that the join dropped (the chunk graphs reach OVERHANG blocks past their
    chunk, the joined graph stops at the built land) is moved to the nearest road of the joined graph."""
    items = []; asx, asz = area_size; off = 0
    for ci, i in enumerate(info):
        pp = i["dir"] / "places.json"
        if not pp.exists(): log(f"  places: chunk {i['id']} has no places.json, treating as empty"); continue
        m = node_map(ci)
        for name, kind, x, z, nd, al, *at in json.loads(pp.read_text()):
            x, z = round(x + i["dx"], 1), round(z + i["dz"], 1)
            # at: the arrival point (places.py), shifted like the place; older chunks have none
            at = [[round(at[0][0] + i["dx"], 1), round(at[0][1] + i["dz"], 1)]] if at and at[0] else []
            if not (0 <= x <= asx and 0 <= z <= asz): continue
            if built is not None and not built(x, z): off += 1; continue
            items.append([name, kind, x, z, None if nd is None else m(nd), al, *at])
    n0 = len(items)
    snap = places.Snapper(roads)
    import arrival
    A = arrival.Graph(roads)
    moved = 0
    for it in items:
        if len(it) > 6:   # the node at the arrival point's edge in the joined graph (the arrival point is on a road)
            r = A.snap(it[6][0], it[6][1], None)
            if built is not None and not built(it[6][0], it[6][1]):
                # its road ran past the built land, where the join dropped it: arrive on the nearest road the
                # joined graph has, as places.py does when nothing better is known
                r = A.snap(it[2], it[3], None)
                if r is not None: it[6] = [round(r["x"], 1), round(r["z"], 1)]; moved += 1
            if r is not None:   # None: no road in range (a test area of scattered chunks)
                e = roads["edges"][r["edge"]]
                it[4] = r["node"] if r["node"] is not None else (e[0] if r["t"] < 0.5 else e[1])
        if it[4] is None: it[4] = snap(it[2], it[3])
    rows = places.dedupe(items, key=lambda it: (it[4] is not None, len(it[5])))
    rows.sort(key=lambda r: (r[0].lower(), r[2], r[3]))
    log(f"  places: {n0} in the chunks ({off} more outside the built chunks dropped) -> {len(rows)} after overlap "
        f"de-duplication, {sum(r[4] is not None for r in rows)} routable, {moved} arrival points moved onto the joined roads")
    return rows


def join_addresses(parts, area_size, log=print, built=None):
    """One address list for the area: per-chunk data/addresses.json (tools/addresses.py, in the chunk's
    roads.json frame) shifted by the chunk offset like the roads, clipped to the area and to the built
    land (built: a Built), overlap de-duplicated."""
    asx, asz = area_size
    streets, sidx, seen, out = [], {}, set(), []; off = 0
    for cid, dx, dz, d in parts:
        a = d["a"]
        for k in range(0, len(a), 4):
            st, num, x, z = d["streets"][a[k]], a[k + 1], a[k + 2] + dx, a[k + 3] + dz
            if not (0 <= x < asx and 0 <= z < asz) or (num, st) in seen: continue
            if built is not None and not built(x, z): off += 1; continue
            seen.add((num, st))
            if st not in sidx: sidx[st] = len(streets); streets.append(st)
            out += [sidx[st], num, round(x), round(z)]
    log(f"  addresses: {len(out) // 4:,} on {len(streets):,} streets ({off} outside the built chunks dropped)")
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

def join(area, chunks, out, seam_tol=6, log=print, sew_seams=True):
    """Write one site for the whole area from the per-chunk sites already in out/<id>/site.

    Packs and their height tiles are linked (never re-encoded) and given unique ids plus the offset
    of their chunk, so nothing holds more than one chunk at a time. Where chunks overlap, tools/seams.py
    gives each shared column to one chunk and rewrites the packs near the seams accordingly (sew_seams=False
    leaves both copies drawn, as earlier joins did)."""
    import numpy as np
    parent = area["bbox"]; lat0 = frame(area)
    psx, psz, _ = area_projection(parent, lat0)
    site = out / "site"; data = site / "data"
    missing = [c["id"] for c in chunks if not (out / c["id"] / "site" / "data" / "meta.json").exists()]
    if missing: sys.exit(f"join: no converted chunk at {out}/<id>/site/data for: {', '.join(missing)}")

    info = []
    for c in chunks:
        cs = out / c["id"] / "site"
        cm = metafmt.read(cs / "data")
        dx, dz, err = chunk_offset(parent, c["bbox"], lat0)
        if "bbox" in cm and max(abs(p - q) for p, q in zip(cm["bbox"], c["bbox"])) > 2e-6:
            # e.g. chunks of an earlier run reused across a change of "bake_pad_m", which also moves the lap (lap())
            sys.exit(f"join: chunk {c['id']} was built for the bbox {cm['bbox']}, but this area's plan has {c['bbox']}. "
                     f"Re-bake it with --only {c['id']} --force.")
        if cm.get("scale", 1) != chunk_scale(lat0, c["bbox"]):
            sys.exit(f"join: chunk {c['id']} was baked at scale {cm.get('scale', 1)}, but this area's frame wants "
                     f"{chunk_scale(lat0, c['bbox'])}. Re-bake it with --only {c['id']} --force.")
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
        """The ground of chunk i over its own columns [z0:z1, x0:x1], from its height tiles (or an older height.bin)."""
        cm = i["meta"]; P = cm["pack"]
        if not all("h" in p for p in cm["packs"]):
            h0 = read_gz_array(i["dir"] / "data" / "height.bin", i["dir"] / "data" / "height.json")
            h0 = np.pad(h0[:cm["depth"] * cm["width"]].reshape(cm["depth"], cm["width"]), 16, constant_values=-1)
            return h0[max(0, z0 + 16):z1 + 16, max(0, x0 + 16):x1 + 16].copy()
        out = np.full((z1 - z0, x1 - x0), -1, dtype="<i2")
        for p in cm["packs"]:
            ax, az, bx, bz = max(x0, p["x"]), max(z0, p["z"]), min(x1, p["x"] + P), min(z1, p["z"] + P)
            if bx <= ax or bz <= az: continue
            t = read_gz_array(i["dir"] / "data" / f"{p['h']}.bin", i["dir"] / "data" / f"{p['h']}.json")[:2 * P * P].reshape(-1, P, P)
            # where the lowest surface is the world floor (sky under the terrain shell, common on hills) the
            # ground is the top surface, as the viewer's groundY() reads it
            t = np.where(t[0] > 1, t[0], t[-1])
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

    # ---- seams (tools/seams.py): one owner per shared column, the two chunks' ground blended into each other across
    # the seam, and the riser faces a remaining step needs; only the packs near a seam are rewritten
    edits, seam_stats = {}, None
    if sew_seams and all("h" in p for i in info for p in i["meta"]["packs"]):
        import seams
        full = split(parent, area.get("max_chunk_km2", 4))[0]
        gx = {c["col"]: chunk_offset(parent, c["bbox"], lat0)[0] - minx for c in full if c["row"] == 0}
        gz = {c["row"]: chunk_offset(parent, c["bbox"], lat0)[1] - minz for c in full if c["col"] == 0}
        big = 10 ** 9
        for c, i in zip(chunks, info):
            ex, ez, _ = projection(c["bbox"], chunk_scale(lat0, c["bbox"]))
            x0, z0 = i["dx"] - minx, i["dz"] - minz   # built column of the Arnis world's block (0, 0)
            i.update(row=c["row"], col=c["col"], ext=(x0, z0, x0 + math.floor(ex) + 1, z0 + math.floor(ez) + 1),
                     slot=(gx[c["col"]], gz[c["row"]], gx.get(c["col"] + 1, big), gz.get(c["row"] - 1, big)))
        names = [m["rgb"] + m["kind"] for m in info[0]["meta"]["materials"]]
        seams.DIRT = names.index("#86603fsolid") if "#86603fsolid" in names else 0
        seams.ROAD = frozenset(names.index(c + "solid") for c in getattr(seams, "ROAD_RGB", ()) if c + "solid" in names)
        def read_pack(i, p): return (i["dir"] / "data" / f"{p['id']}.{ext}").read_bytes()
        def read_h(i, p): return read_gz_array(i["dir"] / "data" / f"{p['h']}.bin", i["dir"] / "data" / f"{p['h']}.json")
        # the joined road graph in the built frame, so seams.py can follow a deck or ramp across a seam (lift_roads())
        rparts = [(i["id"], i["dx"], i["dz"], json.loads((i["dir"] / "roads.json").read_text()))
                  for i in info if (i["dir"] / "roads.json").exists()]
        sroads = None
        if len(rparts) == len(info) and not area.get("roads_as_baked"):
            g, _, _ = join_roads(rparts, (psx, psz), seam_tol, log=lambda *a: None)
            sroads = dict(g, nodes=[[x - minx, z - minz] for x, z in g["nodes"]])
        edits, seam_stats = seams.sew(info, read_pack, read_h, log, roads=sroads)

    shutil.rmtree(site, ignore_errors=True); data.mkdir(parents=True)
    # ---- packs and height tiles: link the bytes, translate the ids and the placement
    packs = []; used = set(); total = 0; hbytes = 0; quads = 0

    def put(src, dst):
        try: os.link(src, dst)
        except OSError: shutil.copy(src, dst)
        return dst.stat().st_size
    for k, i in enumerate(info):
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
            e = edits.get((k, p["id"]))
            if e is not None:   # a pack on a seam, cut to the columns this chunk owns (see tools/seams.py)
                if not e["tiles"]: continue
                (data / f"{pid}.{ext}").write_bytes(e["blob"]); (data / f"{hid}.{ext}").write_bytes(e["hbytes"])
                total += len(e["blob"]); hbytes += len(e["hbytes"]); quads += e["quads"]
                packs.append({"id": pid, "h": hid, "x": p["x"] + i["sx"], "z": p["z"] + i["sz"], "quads": e["quads"],
                              "dx": i["sx"], "dz": i["sz"], "dy": i["sy"], "tiles": e["tiles"]})
                continue
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
    # search sends drivers only to built land (the chunks of this join), never into a hole of a keep-list
    built = Built(chunk_rect(i) for i in info)
    # ---- addresses for search (optional: a chunk without them is skipped)
    aparts = [(i["id"], i["dx"], i["dz"], json.loads((i["dir"] / "data" / "addresses.json").read_text()))
              for i in info if (i["dir"] / "data" / "addresses.json").exists()]
    if aparts: (data / "addresses.json").write_text(json.dumps(join_addresses(aparts, (psx, psz), log, built), separators=(",", ":")))
    pl = join_places(info, node_map, roads, (psx, psz), log, built)
    (site / "places.json").write_text(json.dumps(pl, separators=(",", ":"), ensure_ascii=False))

    meta = {"tile": info[0]["meta"]["tile"], "pack": info[0]["meta"]["pack"], "ext": ext,
            "width": width, "depth": depth, "height": height, "minx": minx, "minz": minz, "miny": miny,
            "quads": quads, "bytes": total, "packs": packs, "materials": info[0]["meta"]["materials"],
            "name": area["name"], "bbox": parent, **({} if lat0 is None else {"lat0": lat0}),
            # w, d: the chunk's slot (chunk_rect()), so tools can tell built land from holes without the area file
            "chunks": [{"id": i["id"], "dx": i["sx"], "dz": i["sz"], "dy": i["sy"], "err": i["err"],
                        "w": round(chunk_rect(i)[2] - minx - i["sx"], 1), "d": round(chunk_rect(i)[3] - minz - i["sz"], 1)}
                       for i in info]}
    metafmt.write(data, meta, log)   # format 2: a small meta.json, the tile tables in data/tiles/ (tools/metafmt.py)
    # overhead colour layer for the 2D map (optional: the map falls back to roads on parchment)
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "maptiles.py"), str(site)], capture_output=True, text=True)
    log("join: " + (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else f"join: map colour layer skipped ({r.stderr.strip()[-300:]})")
    copy_web(site)
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
    return {"chunks": len(info), "blocks": [width, depth, height], "packs": len(packs), "quads": quads,
            "nodes": len(roads["nodes"]), "places": len(pl), "edges": len(roads["edges"]), "seam_nodes_merged": merged,
            "projection_error_blocks": round(worst, 2), "site_mb": round(size / 1e6, 2),
            **({"seams": seam_stats} if seam_stats else {})}


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
    from the environment, handed to Arnis for that one run, and never printed or written.

    Also the elevation base grid: with "elevation_base_m" in the area file every bake maps metres to blocks
    at one vertical scale, ELEVATION_SCALE blocks per metre (the frame's 1 block = 1 metre at lat0), from a
    base a whole number of blocks above elevation_base_m (tools/arnis-fixed-base.patch, built into the CI's
    Arnis). Arnis's own fit starts each bake at its own lowest cell, so neighbouring bakes differ by a
    fraction of a block that the join's whole-block lift cannot take out; on the grid they differ by whole
    blocks only. Without the key the variables are cleared, so Arnis fits as it always did."""
    env = os.environ.copy()
    env.pop("ARNIS_ELEVATION_BASE_M", None); env.pop("ARNIS_ELEVATION_SCALE", None)
    if area.get("elevation_base_m") is not None:
        env["ARNIS_ELEVATION_BASE_M"] = str(float(area["elevation_base_m"]))
        env["ARNIS_ELEVATION_SCALE"] = str(ELEVATION_SCALE)
    if not area.get("mapillary") or chunk_km2 > 0.10:  # Arnis only fetches facades for tiny areas
        env.pop("MAPILLARY_TOKEN", None)
        return env
    path = os.environ.get("MAPILLARY_TOKEN_FILE")
    if path and Path(path).is_file():
        env["MAPILLARY_TOKEN"] = Path(path).read_text().strip()
    return env


def enrich_osm(arnis, bstr, opts, osm, work, log, env, cid, what=True):
    """Area option "enrich": more building tags before the bake (tools/enrich_osm.py): true for all, or a
    list of "roof_colour" (from NAIP aerial photos) and "heights" (storeys and heights from Overture).
    Returns the file to bake from with --file, or None to bake as usual; nothing here can fail the build.

    The OSM data has to exist before the bake, so Arnis fetches it first in a --prewarm run with the
    bake's own --bbox and options: the same data source (tile archive, Overpass as fallback) writes the
    same osm.json --save-json-file always wrote, and every other input the bake reads (terrain, laser,
    aerial photos, Overture, canopy, land cover) lands in Arnis's cache on the way, so the bake that
    follows downloads nothing twice. osm.json stays the unenriched file: roadgraph.py and places.py
    read it afterwards, and enrichment changes no road, name or place tag."""
    osm.unlink(missing_ok=True)
    pre = [arnis, f"--bbox={bstr}", f"--output-dir={work / 'world'}", f"--save-json-file={osm}",
           "--no-update-check", "--prewarm"] + opts
    print("  $", " ".join(str(c) for c in pre))
    with open(log, "ab") as f:
        r = subprocess.run([str(c) for c in pre], stdout=f, stderr=subprocess.STDOUT, env=env)
    # A prewarm exits 1 when anything could not be cached (the laser index, say), after osm.json is written.
    try:
        ok = osm.exists() and json.loads(osm.read_text()).get("elements") is not None
    except ValueError:
        ok = False
    if not ok:
        print(f"[{cid}] enrich: skipped, the prewarm run wrote no OSM data (exit {r.returncode}); baking as usual")
        osm.unlink(missing_ok=True)
        return None
    out, stats = work / "osm_enriched.json", work / "enrich.json"
    cmd = [sys.executable, ROOT / "tools" / "enrich_osm.py", osm, bstr, out, "--stats", stats, "--cache", work / "enrich_cache"]
    if what is not True:
        if "roof_colour" not in what: cmd.append("--no-naip")
        if "heights" not in what: cmd.append("--no-overture")
    print("  $", " ".join(str(c) for c in cmd))
    try:
        with open(log, "ab") as f:
            r = subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT, timeout=1800)
        if r.returncode or not out.exists(): raise RuntimeError(f"exit {r.returncode}")
        st = json.loads(stats.read_text())
        print(f"[{cid}] enrich: {st.get('buildings', 0)} buildings, {st.get('roof_colours_written', 0)} roof colours, "
              f"{st.get('levels_filled', 0)} storey counts and {st.get('heights_filled', 0)} heights filled; sources "
              + ", ".join(f"{k} {'ok' if v.get('ok') else 'failed'}" for k, v in st.get("sources", {}).items()))
        return out
    except Exception as e:
        print(f"[{cid}] enrich: skipped ({e}); baking from the unenriched osm.json")
        return osm


def build_chunk(area, chunk, args, work, overhang=0):
    bbox = chunk["bbox"]
    bstr = ",".join(f"{v:.6f}" for v in bbox)
    h, w = km(bbox)
    scale = chunk_scale(frame(area), bbox)   # not 1 only in a conformal area
    # "bake_pad_m": Arnis bakes a bbox that much wider on every side and the convert crops it back (pad_frame());
    # only the bake (and its OSM prewarm) sees the wider bbox, everything after it the chunk's own
    # (conformal areas only: lap() keeps their chunk bboxes such that pad_frame() can place the padded bake exactly)
    pad = area.get("bake_pad_m", 0) if frame(area) is not None else 0
    bake_bbox, crop = pad_frame(bbox, scale, pad) if pad else (bbox, None)
    bake_bstr = ",".join(f"{v:.9f}" for v in bake_bbox) if pad else bstr
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

    if redo and world_dir() and (world_dir() / "metadata.json").exists():
        md = json.loads((world_dir() / "metadata.json").read_text())
        baked = md.get("scale", 1)
        if baked != scale: sys.exit(f"[{chunk['id']}] the world in {world_parent} was baked at scale {baked}, "
                                    f"this area's frame wants {scale}: re-bake it with --force")
        if (pad or not args.world) and "minGeoLat" in md and max(abs(md[k] - v) for k, v in zip(("minGeoLat", "minGeoLon", "maxGeoLat", "maxGeoLon"), bake_bbox)) > 1e-6:
            sys.exit(f"[{chunk['id']}] the world in {world_parent} was baked for another bbox than "
                     f"{bake_bstr}{' (bake_pad_m ' + str(pad) + ')' if pad else ''}: re-bake it with --force")
    if step("bake", lambda: world_dir() and osm.exists()):
        if args.world: sys.exit(f"--world {args.world} has no region files")
        shutil.rmtree(world_parent, ignore_errors=True); world_parent.mkdir(parents=True)
        arnis = find_arnis(args.arnis)
        opts = []
        if area.get("lidar", True) and h * w <= 16: opts.append("--lidar")
        # Parked cars are left out: a blocky car on every other parking space reads as clutter from the
        # driver's seat. An area can set "props" to "all" or its own list to change that.
        opts.append("--props=" + area.get("props", NO_CARS))
        opts += area.get("arnis_args", [])
        if scale != 1: opts.append(f"--scale={scale}")
        source = [f"--save-json-file={osm}"]
        if area.get("enrich") and not args.osm:
            enriched = enrich_osm(arnis, bake_bstr, opts, osm, work, log, token_env(area, h * w), chunk["id"], area["enrich"])
            if enriched: source = [f"--file={enriched}"]
        cmd = [arnis, f"--bbox={bake_bstr}", f"--output-dir={world_parent}"] + source + ["--no-update-check"] + opts
        seen = log.stat().st_size if log.exists() else 0
        run(cmd, log, token_env(area, h * w))
        if area.get("elevation_base_m") is not None:
            # an Arnis without tools/arnis-fixed-base.patch ignores the variables and fits as before
            with open(log, "rb") as f: f.seek(seen); said = f.read().decode(errors="replace")
            grid = [l[l.index("Elevation base grid:"):] for l in said.splitlines() if "Elevation base grid:" in l]
            if not grid and ("Realistic elevation" in said or "Elevation compressed" in said):
                sys.exit(f"[{chunk['id']}] this Arnis has no elevation base grid (\"elevation_base_m\"): "
                         "build it with tools/arnis-fixed-base.patch")
            for l in grid: print(f"[{chunk['id']}] {l}")
        if not world_dir(): sys.exit(f"Arnis finished but wrote no world; see {log}")
        times["bake"] = time.time() - times["bake"]

    data = site / "data"
    if step("convert", lambda: (data / "meta.json").exists()):
        shutil.rmtree(site, ignore_errors=True); data.mkdir(parents=True)
        # with the OSM data of the bake, road surfaces are re-graded along the roads (tools/road_grade.py)
        # (the enriched copy when the bake used one, as its ways are the ones Arnis built)
        baked_osm = work / "osm_enriched.json" if area.get("enrich") and not args.osm and (work / "osm_enriched.json").exists() else osm
        grade = ["--osm", baked_osm] if baked_osm.exists() and not area.get("no_road_grade") else []
        # the chunk's own bbox (without the lap), where roads crossing into a neighbour are held to levels both work out
        own = next((c["bbox"] for c in split(area["bbox"], area.get("max_chunk_km2", 4))[0] if c["id"] == chunk["id"]), None)
        if grade and own and chunk["id"] != "all": grade += ["--bounds", ",".join(f"{v:.6f}" for v in own)]
        # "roads_as_baked": road surfaces stay as Arnis laid them (no re-grade here, no lift at seams in join())
        if grade and area.get("roads_as_baked"): grade += ["--no-regrade"]
        if crop: grade += ["--crop", ",".join(map(str, crop))]   # a padded bake: keep the chunk's own blocks
        run([sys.executable, ROOT / "tools" / "mca2web.py", world_dir(), data] + (["--b64"] if args.b64 else []) + grade, log)
        times["convert"] = time.time() - times["convert"]

    # street addresses for search, in the same frame as roads.json; optional, so a network hiccup never fails a build.
    # Fetched before the roads step, which reads them to learn a place's street from its house number.
    if not (data / "addresses.json").exists() or (STEPS.index("roads") >= redo):
        try: run([sys.executable, ROOT / "tools" / "addresses.py", bstr, data / "addresses.json"] + (["--scale", scale] if scale != 1 else []), log)
        except BaseException as e: print(f"[{chunk['id']}] addresses skipped: {e}")
    if step("roads", lambda: (site / "roads.json").exists() and (site / "places.json").exists()):
        cmd = [sys.executable, ROOT / "tools" / "roadgraph.py", bstr, osm, site / "roads.json", "--places", site / "places.json"]
        if (data / "addresses.json").exists(): cmd += ["--addresses", data / "addresses.json"]
        if area.get("private_roads"): cmd.append("--keep-private")   # gated streets, used only to start or end a trip
        if overhang: cmd += ["--overhang", overhang]   # reach past the bbox so the join can sew seams
        if scale != 1: cmd += ["--scale", scale]
        run(cmd, log)
        times["roads"] = time.time() - times["roads"]

    if step("site", lambda: (site / "map.html").exists()):
        meta = json.loads((data / "meta.json").read_text())
        meta.update(name=area["name"] + ("" if chunk["id"] == "all" else f" ({chunk['id']})"), bbox=bbox)
        if scale != 1: meta["scale"] = scale
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
    ap.add_argument("--no-seams", action="store_true",
                    help="join without tools/seams.py: every chunk's overlap strip drawn as baked (for comparisons)")
    ap.add_argument("--serve", action="store_true", help="serve the result on http://localhost:8765 when done")
    args = ap.parse_args()

    area = json.loads(Path(args.area).read_text())
    chunks, rows, cols = split(area["bbox"], area.get("max_chunk_km2", 4))
    chunks = keep_chunks(area, lap(area, chunks))
    h, w = km(area["bbox"])
    if args.plan_ids:
        print(json.dumps([c["id"] for c in chunks])); return
    print(f"{area['name']}: {h:.2f} x {w:.2f} km = {h*w:.2f} km2 in {len(chunks)} chunk(s) ({rows} x {cols})")
    if args.plan:
        lat0 = frame(area)
        if lat0 is not None: print(f"conformal frame: 1 block = 1 metre at latitude {lat0:.6f}")
        if area.get("elevation_base_m") is not None:
            print(f"elevation base grid: heights at {ELEVATION_SCALE} blocks per metre from a whole block above "
                  f"{area['elevation_base_m']} m (needs Arnis with tools/arnis-fixed-base.patch)")
        for c in chunks:
            dx, dz, err = chunk_offset(area["bbox"], c["bbox"], lat0)
            k = chunk_scale(lat0, c["bbox"])
            print(" ", c["id"], ",".join(f"{v:.6f}" for v in c["bbox"]), f"at block ({dx}, {dz}),",
                  *([f"scale {k},"] if k != 1 else []), f"error {err:.2f}",
                  *([f"baked as {','.join(f'{v:.9f}' for v in pad_frame(c['bbox'], k, area['bake_pad_m'])[0])}"]
                    if area.get("bake_pad_m") and lat0 is not None else []))
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
    full = keep_chunks(area, lap(area, full))
    have = {r["id"] for r in report["chunks"]}
    report["chunks"] += [s for s in (chunk_summary(area, c, out / c["id"]) for c in full if c["id"] not in have) if s]
    report["chunks"].sort(key=lambda r: (r["row"], r["col"]))

    # One site for the whole area. A single-chunk area is already one world, so it is left alone.
    site = out / chunks[0]["id"] / "site" if rows * cols == 1 else out / "site"
    missing = [c["id"] for c in full if not (out / c["id"] / "site" / "data" / "meta.json").exists()]
    if rows * cols > 1 and not args.no_join and not missing:
        tj = time.time()
        report["join"] = join(area, full, out, args.seam_tol, sew_seams=not args.no_seams)
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
