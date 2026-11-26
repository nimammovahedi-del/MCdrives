"""Street addresses for in-app search, from Overture Maps (in Orange County: the county's public-domain
address points via OpenAddresses), placed in Arnis block coordinates like roads.json.

    addresses.py S,W,N,E out.json

Reads the Overture addresses PMTiles at zoom 14 with HTTP range requests (no full download).
Output: {"v": 1, "streets": [...], "a": [street_index, number, x, z, ...]} with unit duplicates dropped
(one point per number + street), numbers kept as text ("12A"), x/z in blocks, rounded to 1.
About 15 bytes per address before gzip; the viewer loads it the first time someone searches.
"""
import sys, json, math, gzip, requests
from pmtiles.reader import Reader
import mapbox_vector_tile as mvt

S, W, N, E = map(float, sys.argv[1].split(","))
OUT = sys.argv[2]
REL = "2026-11-18.0"
URL = f"https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/tiles/{REL}/addresses.pmtiles"
R = 6371000.0
def hav(dlat, dlon, lat):
    a = math.sin(math.radians(dlat) / 2) ** 2 + math.cos(math.radians(lat)) ** 2 * math.sin(math.radians(dlon) / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
# the same projection as tools/roadgraph.py
sz = math.floor(hav(N - S, 0, 0)); sx = math.floor(hav(0, E - W, (S + N) / 2))
# --scale K: the bake's Arnis --scale (a chunk of a conformal area, see build.py frame()), which multiplies both
K = float(sys.argv[sys.argv.index("--scale") + 1]) if "--scale" in sys.argv else 1.0
if K != 1: sx *= K; sz *= K
def xz(lon, lat): return ((lon - W) / (E - W) * sx, (1 - (lat - S) / (N - S)) * sz)

sess = requests.Session()
def get(off, ln):
    for _ in range(4):
        try:
            r = sess.get(URL, headers={"Range": f"bytes={off}-{off + ln - 1}"}, timeout=120); r.raise_for_status(); return r.content
        except requests.RequestException as e: err = e
    raise err
rd = Reader(get)
Z = 14; n = 2 ** Z
def tile(lon, lat): return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
x0, y0 = tile(W, N); x1, y1 = tile(E, S)
streets, sidx, seen, out = [], {}, set(), []
for tx in range(int(x0), int(x1) + 1):
    for ty in range(int(y0), int(y1) + 1):
        data = rd.get(Z, tx, ty)
        if not data: continue
        if data[:2] == b"\x1f\x8b": data = gzip.decompress(data)
        layer = mvt.decode(data, default_options={"y_coord_down": True}).get("address")
        if not layer: continue
        ext = layer.get("extent", 4096)
        for f in layer["features"]:
            p = f["properties"]; num, st = (p.get("number") or "").strip(), (p.get("street") or "").strip()
            if not num or not st or f["geometry"]["type"] != "Point": continue
            gx, gy = f["geometry"]["coordinates"]
            lon = (tx + gx / ext) / n * 360 - 180
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + gy / ext) / n))))
            if not (S <= lat <= N and W <= lon <= E): continue
            key = (num.upper(), st.upper())
            if key in seen: continue
            seen.add(key)
            if key[1] not in sidx: sidx[key[1]] = len(streets); streets.append(key[1])
            x, z = xz(lon, lat)
            out += [sidx[key[1]], int(num) if num.isdigit() else num, round(x), round(z)]
json.dump({"v": 1, "source": "Overture Maps addresses " + REL, "streets": streets, "a": out}, open(OUT, "w"), separators=(",", ":"))
print(f"{len(out) // 4} addresses on {len(streets)} streets -> {OUT}")
