#!/usr/bin/env python3
"""One-command build: real map data -> Arnis block world -> web tiles -> drivable viewer.

    python build.py areas/spectrum.json            # bake, convert, road graph, pack
    python build.py areas/spectrum.json --serve    # ...then open the viewer locally
    python build.py areas/irvine.json --plan       # only show how a big area splits into chunks
    python build.py areas/irvine.json --join-only  # join chunk outputs that are already there

An area larger than max_chunk_km2 is baked as a grid of chunks, each its own Arnis world with its
own origin. The join step then writes ONE site for the whole area at build/<slug>/site: every
chunk's packs placed at its offset in one pack list, one height field, and one road graph sewn
together across the chunk seams, so the viewer drives across the whole area without a break.

Each step is skipped when its output already exists, so a failed or interrupted run
resumes where it stopped. Use --force to redo everything, or --from STEP to redo from
bake | convert | roads | site.
"""
import argparse, base64, gzip, json, math, os, shutil, subprocess, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path

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
    """height.bin (gzipped int16) or, for --b64 builds, height.json."""
    import numpy as np
    if path_bin.exists():
        raw = gzip.decompress(path_bin.read_bytes())
    else:
        raw = gzip.decompress(base64.b64decode(json.loads(path_json.read_text())["base64"]))
    return np.frombuffer(raw, dtype="<i2")


def join_roads(parts, area_size, seam_tol, log=print):
    """One road graph for the whole area out of one graph per chunk.

    parts: (id, dx, dz, graph) per chunk, dx/dz being chunk_offset(). Node coordinates are
    translated, the name tables merged, and the nodes that two chunks share on their seam are
    merged into one junction - by OSM id where the chunk graphs carry one, otherwise by distance.
    Then nodes outside the area are dropped and the largest connected component is kept."""
    names = [""]; nidx = {"": 0}
    nodes = []; owner = []; osm = []; edges = []
    for ci, (cid, dx, dz, g) in enumerate(parts):
        remap = []
        for nm in g["names"]:
            if nm not in nidx: nidx[nm] = len(names); names.append(nm)
            remap.append(nidx[nm])
        base = len(nodes)
        ids = g.get("osm") or [None] * len(g["nodes"])
        for (x, z), oid in zip(g["nodes"], ids):
            nodes.append([x + dx, z + dz]); owner.append(ci); osm.append(oid)
        for a, b, nm, cl, ow, ln, dk in g["edges"]:
            edges.append((base + a, base + b, remap[nm], cl, ow, ln, dk))

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
    keep = {r for r, p in pos.items() if 4 < p[0] < asx - 4 and 4 < p[1] < asz - 4}
    link = defaultdict(set)
    kept_edges = set()
    for a, b, nm, cl, ow, ln, dk in edges:
        ra, rb = find(a), find(b)
        if ra == rb or ra not in keep or rb not in keep: continue
        kept_edges.add((ra, rb, nm, cl, ow, ln, dk))
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
    out_edges = sorted({(ids[a], ids[b], nm, cl, ow, ln, dk) for a, b, nm, cl, ow, ln, dk in kept_edges
                        if a in big and b in big})
    log(f"  roads: {len(nodes)} chunk nodes -> {len(group)} after merging -> {len(out_nodes)} in the "
        f"largest component, {len(out_edges)} edges, {len(names)} names")
    return {"size": [asx, asz], "names": names, "nodes": out_nodes, "edges": [list(e) for e in out_edges]}, by_id + by_dist


def join(area, chunks, out, seam_tol=6, log=print):
    """Write one site for the whole area from the per-chunk sites already in out/<id>/site.

    Packs are copied (never re-encoded) and given unique ids plus the offset of their chunk; the
    height field is pasted together through a memory-mapped file, so neither step holds more than
    one chunk at a time."""
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
    miny = min(i["meta"]["miny"] for i in info)
    width = max(i["dx"] + i["meta"]["minx"] + i["meta"]["width"] for i in info) - minx
    depth = max(i["dz"] + i["meta"]["minz"] + i["meta"]["depth"] for i in info) - minz
    height = max(i["meta"]["miny"] + i["meta"]["height"] for i in info) - miny
    for i in info:
        i["sx"] = i["dx"] + i["meta"]["minx"] - minx
        i["sz"] = i["dz"] + i["meta"]["minz"] - minz
        i["sy"] = i["meta"]["miny"] - miny
    log(f"join: {width} x {depth} x {height} blocks, origin ({minx}, {minz}, {miny})")

    shutil.rmtree(site, ignore_errors=True); data.mkdir(parents=True)
    # ---- packs: copy the bytes, translate the ids and the placement
    packs = []; used = set(); total = 0; quads = 0
    for i in info:
        for p in i["meta"]["packs"]:
            pid = p["id"] if p["id"] not in used else f"{i['id']}_{p['id']}"
            while pid in used: pid += "_"
            used.add(pid)
            src = i["dir"] / "data" / f"{p['id']}.{ext}"
            dst = data / f"{pid}.{ext}"
            try: os.link(src, dst)
            except OSError: shutil.copy(src, dst)
            total += dst.stat().st_size; quads += p["quads"]
            packs.append({"id": pid, "x": p["x"] + i["sx"], "z": p["z"] + i["sz"], "quads": p["quads"],
                          "dx": i["sx"], "dz": i["sz"], "dy": i["sy"], "tiles": p["tiles"]})
    log(f"join: {len(packs)} packs, {total/1e6:.1f} MB, {quads:,} quads")

    # ---- height: one [2, depth, width] field, -1 where no chunk covers
    tmp = data / "height.memmap"
    h = np.memmap(tmp, dtype="<i2", mode="w+", shape=(2, depth, width))
    for z in range(0, depth, 2048): h[:, z:z+2048, :] = -1
    for i in info:
        cm = i["meta"]
        a = read_gz_array(i["dir"] / "data" / "height.bin", i["dir"] / "data" / "height.json")
        a = a.reshape(2, cm["depth"], cm["width"]) if a.size == 2 * cm["depth"] * cm["width"] else \
            np.repeat(a.reshape(1, cm["depth"], cm["width"]), 2, axis=0)
        a = np.where(a >= 0, a + i["sy"], -1).astype("<i2")
        x0, z0 = i["sx"], i["sz"]
        sx0, sz0 = max(0, -x0), max(0, -z0)
        x0, z0 = max(0, x0), max(0, z0)
        ex = min(width, x0 + a.shape[2] - sx0); ez = min(depth, z0 + a.shape[1] - sz0)
        if ex <= x0 or ez <= z0: continue
        src = a[:, sz0:sz0 + (ez - z0), sx0:sx0 + (ex - x0)]
        np.copyto(h[:, z0:ez, x0:ex], src, where=src >= 0)
        del a, src
    h.flush()
    with gzip.open(data / "height.bin", "wb", compresslevel=6) as f:
        for layer in range(2):
            for z in range(0, depth, 1024):
                f.write(np.ascontiguousarray(h[layer, z:z+1024, :]).tobytes())
    del h
    tmp.unlink()
    log(f"join: height.bin {(data/'height.bin').stat().st_size/1e6:.1f} MB for {width}x{depth} columns")

    # ---- roads
    parts = []
    for i in info:
        rp = i["dir"] / "roads.json"
        if not rp.exists(): sys.exit(f"join: chunk {i['id']} has no roads.json")
        parts.append((i["id"], i["dx"], i["dz"], json.loads(rp.read_text())))
    roads, merged = join_roads(parts, (psx, psz), seam_tol, log)
    (site / "roads.json").write_text(json.dumps(roads, separators=(",", ":")))

    meta = {"tile": info[0]["meta"]["tile"], "pack": info[0]["meta"]["pack"], "ext": ext,
            "width": width, "depth": depth, "height": height, "minx": minx, "minz": minz, "miny": miny,
            "quads": quads, "bytes": total, "packs": packs, "materials": info[0]["meta"]["materials"],
            "name": area["name"], "bbox": parent,
            "chunks": [{"id": i["id"], "dx": i["sx"], "dz": i["sz"], "dy": i["sy"], "err": i["err"]} for i in info]}
    (data / "meta.json").write_text(json.dumps(meta))
    shutil.copy(ROOT / "web" / "index.html", site / "index.html")
    shutil.copytree(ROOT / "web" / "vendor", site / "vendor", dirs_exist_ok=True)
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
    return {"chunks": len(info), "blocks": [width, depth, height], "packs": len(packs), "quads": quads,
            "nodes": len(roads["nodes"]), "edges": len(roads["edges"]), "seam_nodes_merged": merged,
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
        cmd += area.get("arnis_args", [])
        run(cmd, log, token_env(area, h * w))
        if not world_dir(): sys.exit(f"Arnis finished but wrote no world; see {log}")
        times["bake"] = time.time() - times["bake"]

    data = site / "data"
    if step("convert", lambda: (data / "meta.json").exists()):
        shutil.rmtree(site, ignore_errors=True); data.mkdir(parents=True)
        run([sys.executable, ROOT / "tools" / "mca2web.py", world_dir(), data] + (["--b64"] if args.b64 else []), log)
        times["convert"] = time.time() - times["convert"]

    if step("roads", lambda: (site / "roads.json").exists()):
        cmd = [sys.executable, ROOT / "tools" / "roadgraph.py", bstr, osm, site / "roads.json"]
        if overhang: cmd += ["--overhang", overhang]   # reach past the bbox so the join can sew seams
        run(cmd, log)
        times["roads"] = time.time() - times["roads"]

    if step("site", lambda: (site / "index.html").exists()):
        meta = json.loads((data / "meta.json").read_text())
        meta.update(name=area["name"] + ("" if chunk["id"] == "all" else f" ({chunk['id']})"), bbox=bbox)
        (data / "meta.json").write_text(json.dumps(meta))
        shutil.copy(ROOT / "web" / "index.html", site / "index.html")
        shutil.copytree(ROOT / "web" / "vendor", site / "vendor", dirs_exist_ok=True)
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
