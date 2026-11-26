"""Which chunks of an area have drivable roads, for areas too large to bake whole (a county).

    roadcover.py areas/NAME.json [--county "Orange County"] [--min-km 0.3] [--out cover.json] [--write]

Reads Overture Maps transportation tiles (zoom 13, HTTP range requests) and, with --county, the
Overture divisions tiles for the county outline. For each chunk of the area's grid it reports the
kilometres of drivable road and the share of the chunk inside the county. A chunk is kept when it
has at least --min-km of road and its centre or a tenth of its area is inside the county.
--write stores the kept ids as "keep" in the area file, the list build.py reads.
"""
import sys, json, math, gzip, argparse
from pathlib import Path
import requests
from pmtiles.reader import Reader
import mapbox_vector_tile as mvt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
REL = "2026-11-18.0"
BASE = f"https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/tiles/{REL}/"
DRIVE = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "living_street", "unclassified", "service"}
sess = requests.Session()


def reader(name):
    def get(off, ln):
        for _ in range(4):
            try:
                r = sess.get(BASE + name, headers={"Range": f"bytes={off}-{off + ln - 1}"}, timeout=120); r.raise_for_status(); return r.content
            except requests.RequestException as e: err = e
        raise err
    return Reader(get)


def tiles(rd, z, bbox):
    s, w, n, e = bbox; k = 2 ** z
    def t(lon, lat): return (lon + 180) / 360 * k, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * k
    x0, y0 = t(w, n); x1, y1 = t(e, s)
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            d = rd.get(z, tx, ty)
            if not d: continue
            if d[:2] == b"\x1f\x8b": d = gzip.decompress(d)
            def ll(gx, gy, ext, tx=tx, ty=ty):
                return ((tx + gx / ext) / k * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + gy / ext) / k)))))
            yield mvt.decode(d, default_options={"y_coord_down": True}), ll


def tile_of(lon, lat, z):
    k = 2 ** z
    return int((lon + 180) / 360 * k), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * k)


def lines(g):
    if g["type"] == "LineString": yield g["coordinates"]
    elif g["type"] == "MultiLineString": yield from g["coordinates"]


def rings(g):
    if g["type"] == "Polygon": yield from g["coordinates"]
    elif g["type"] == "MultiPolygon":
        for p in g["coordinates"]: yield from p


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("area"); ap.add_argument("--county"); ap.add_argument("--min-km", type=float, default=0.3)
    ap.add_argument("--out"); ap.add_argument("--write", action="store_true"); ap.add_argument("--zoom", type=int, default=13)
    a = ap.parse_args()
    import build
    area = json.loads(Path(a.area).read_text())
    chunks, rows, cols = build.split(area["bbox"], area.get("max_chunk_km2", 4))
    S, W, N, E = area["bbox"]
    def cell(lon, lat):
        r, c = int((lat - S) / (N - S) * rows), int((lon - W) / (E - W) * cols)
        return (r, c) if 0 <= r < rows and 0 <= c < cols else None
    road = {}
    mlat = math.radians((S + N) / 2)
    for layers, ll in tiles(reader("transportation.pmtiles"), a.zoom, area["bbox"]):
        lay = layers.get("segment")
        if not lay: continue
        ext = lay.get("extent", 4096)
        for f in lay["features"]:
            p = f["properties"]
            if p.get("subtype") != "road" or p.get("class") not in DRIVE: continue
            for ln in lines(f["geometry"]):
                pts = [ll(x, y, ext) for x, y in ln if 0 <= x <= ext and 0 <= y <= ext]   # tile buffers would count twice
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    d = math.hypot((x1 - x0) * math.cos(mlat), y1 - y0) * 111.32
                    c = cell((x0 + x1) / 2, (y0 + y1) / 2)
                    if c: road[c] = road.get(c, 0.0) + d
    # The county's land outline, kept per tile: a tile's polygon is clipped to the tile, so a point is
    # tested against the pieces of its own tile only (even-odd over every piece's edges).
    segs, ZC = {}, 8
    if a.county:
        for layers, ll in tiles(reader("divisions.pmtiles"), ZC, area["bbox"]):
            lay = layers.get("division_area")
            if not lay: continue
            ext = lay.get("extent", 4096); key = tile_of(*ll(ext / 2, ext / 2, ext), ZC)
            for f in lay["features"]:
                p = f["properties"]
                if p.get("subtype") != "county" or p.get("class") != "land" or p.get("@name") != a.county: continue
                for rg in rings(f["geometry"]):
                    q = [ll(x, y, ext) for x, y in rg]
                    segs.setdefault(key, []).extend((x0, y0, x1, y1) for (x0, y0), (x1, y1) in zip(q, q[1:]))
        if not segs: sys.exit(f"no county outline named {a.county} found in the divisions tiles")
    def inside(lon, lat):
        return sum(1 for x0, y0, x1, y1 in segs.get(tile_of(lon, lat, ZC), ()) if (y0 > lat) != (y1 > lat) and lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0)) % 2 == 1
    out, keep, km2 = [], [], 0.0
    for c in chunks:
        cs, cw, cn, ce = c["bbox"]; h, w = build.km(c["bbox"]); n = 8
        share = 1.0 if not segs else sum(inside(cw + (ce - cw) * (i + .5) / n, cs + (cn - cs) * (j + .5) / n) for i in range(n) for j in range(n)) / n / n
        r = road.get((c["row"], c["col"]), 0.0)
        k = r >= a.min_km and share >= 0.1
        out.append({"id": c["id"], "road_km": round(r, 2), "county": round(share, 2), "km2": round(h * w, 2), "keep": k})
        if k: keep.append(c["id"]); km2 += h * w
    print(f"{area['name']}: {len(chunks)} chunks ({rows} x {cols}); {len(keep)} kept, {km2:.0f} km2, "
          f"{sum(o['road_km'] for o in out if o['keep']):.0f} km of drivable road")
    if a.out: Path(a.out).write_text(json.dumps({"rows": rows, "cols": cols, "chunks": out}))
    if a.write:
        area["keep"] = keep; Path(a.area).write_text(json.dumps(area, indent=2) + "\n")


if __name__ == "__main__":
    main()
