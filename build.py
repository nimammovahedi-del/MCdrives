#!/usr/bin/env python3
"""One-command build: real map data -> Arnis block world -> web tiles -> drivable viewer.

    python build.py areas/spectrum.json            # bake, convert, road graph, pack
    python build.py areas/spectrum.json --serve    # ...then open the viewer locally
    python build.py areas/irvine.json --plan       # only show how a big area splits into chunks

Each step is skipped when its output already exists, so a failed or interrupted run
resumes where it stopped. Use --force to redo everything, or --from STEP to redo from
bake | convert | roads | site.
"""
import argparse, json, math, os, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STEPS = ["bake", "convert", "roads", "site"]
ARNIS_WINDOWS_URL = "https://github.com/louis-e/arnis/releases/latest/download/arnis-windows.exe"


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
                "bbox": [s + (n - s) * r / rows, w + (e - w) * c / cols,
                         s + (n - s) * (r + 1) / rows, w + (e - w) * (c + 1) / cols],
            })
    return out, rows, cols


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


def build_chunk(area, chunk, args, work):
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
        run([sys.executable, ROOT / "tools" / "roadgraph.py", bstr, osm, site / "roads.json"], log)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("area", help="area config, e.g. areas/spectrum.json")
    ap.add_argument("--arnis", help="path to the Arnis binary (else ARNIS_BIN, bin/, PATH, or auto-download on Windows)")
    ap.add_argument("--world", help="reuse an existing Arnis world folder instead of baking (single-chunk areas)")
    ap.add_argument("--osm", help="OSM JSON saved by that bake (--save-json-file)")
    ap.add_argument("--out", default=str(ROOT / "build"), help="output root (default: build/)")
    ap.add_argument("--b64", action="store_true", help="write packs as base64 JSON, for hosts that refuse .bin files")
    ap.add_argument("--plan", action="store_true", help="print the chunk plan and exit")
    ap.add_argument("--only", help="build just these chunk ids, comma-separated")
    ap.add_argument("--force", action="store_true", help="redo every step")
    ap.add_argument("--from", dest="from_step", choices=STEPS, help="redo from this step onward")
    ap.add_argument("--serve", action="store_true", help="serve the result on http://localhost:8765 when done")
    args = ap.parse_args()

    area = json.loads(Path(args.area).read_text())
    chunks, rows, cols = split(area["bbox"], area.get("max_chunk_km2", 4))
    h, w = km(area["bbox"])
    print(f"{area['name']}: {h:.2f} x {w:.2f} km = {h*w:.2f} km2 in {len(chunks)} chunk(s) ({rows} x {cols})")
    if args.plan:
        for c in chunks: print(" ", c["id"], ",".join(f"{v:.6f}" for v in c["bbox"]))
        return
    if (args.world or args.osm) and len(chunks) > 1: sys.exit("--world/--osm only work for single-chunk areas")
    if args.only: chunks = [c for c in chunks if c["id"] in args.only.split(",")]

    out = Path(args.out) / area["slug"]
    report = {"name": area["name"], "bbox": area["bbox"], "rows": rows, "cols": cols, "chunks": []}
    t0 = time.time()
    for c in chunks:
        report["chunks"].append(build_chunk(area, c, args, out / c["id"]))
        (out / "report.json").write_text(json.dumps(report, indent=1))
    done = report["chunks"]
    print(f"\ndone in {time.time()-t0:.0f}s: {sum(c['km2'] for c in done):.2f} km2, "
          f"{sum(c['quads'] for c in done):,} quads, {sum(c['site_mb'] for c in done):.1f} MB. Report: {out/'report.json'}")
    if args.serve:
        site = out / done[0]["id"] / "site"
        print(f"serving {site} at http://localhost:8765  (Ctrl+C to stop)")
        subprocess.run([sys.executable, "-m", "http.server", "8765", "--directory", str(site)])


if __name__ == "__main__":
    main()
