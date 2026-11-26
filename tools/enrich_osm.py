#!/usr/bin/env python3
"""Fill in building tags OpenStreetMap leaves empty, before the Arnis bake.

    enrich_osm.py IN.json S,W,N,E OUT.json [--stats STATS.json] [--cache DIR]
                  [--no-overture] [--no-naip] [--image FILE]

IN.json is Overpass JSON as Arnis saves it (--save-json-file); OUT.json is the same file with more
tags, for Arnis's --file. Only tags that are absent are written, so OSM always wins:

* roof:colour=#rrggbb from USDA NAIP aerial photos (public domain), read from the USGS NAIP Plus WMS
  that Arnis itself reads for its yard detail (imagery.nationalmap.gov, same layer, same NAD83 UTM
  grid, same GetMap request). The colour is a trimmed mean of the pixels inside the outline, shrunk
  by a pixel or two, without vegetation (a tree over the roof) and shadow; its chroma is clamped,
  and it is snapped to the colours of Arnis's own roof palette (see SNAP below), so two similar roofs
  get the same block. --image FILE (with a world file FILE.pgw/.jgw/.wld, in the NAD83 UTM zone of
  the bbox unless --image-epsg says otherwise) reads a local image instead, for tests.
* building:levels or height from Overture Maps buildings (ODbL), for buildings with neither tag. An
  Overture outline must cover at least half of the OSM one. Homes and house-sized buildings get
  storeys and everything else metres, the rule Arnis applies to its own laser heights
  (src/survey/heights.rs), because Arnis draws a storey in whole blocks.

Every network source is optional: a failure logs one line and the file is written without it. The
script exits 0 whenever it could write OUT.json, and copies IN.json unchanged if anything else fails.
Downloads are cached in --cache (default: a folder named enrich_cache next to OUT.json).
"""
import argparse, gzip, io, json, math, os, shutil, sys, time
from pathlib import Path

import numpy as np

UA = {"User-Agent": "mcdrives-build"}
OVERTURE_REL = "2026-11-18.0"   # the release tools/addresses.py reads
OVERTURE_URL = f"https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com/tiles/{OVERTURE_REL}/buildings.pmtiles"
# The service and layer of Arnis's USA photo source (src/survey/usa.rs, Photo::Wms), asked as
# src/survey/photo.rs fetch_wms asks it: WMS 1.3.0 GetMap in EPSG:269zz (NAD83 / UTM zone zz,
# easting first), JPEG, 2048 pixels a side.
NAIP_WMS = "https://imagery.nationalmap.gov/arcgis/services/USGSNAIPPlus/ImageServer/WMSServer"
NAIP_LAYER = "USGSNAIPPlus"
RES = 0.5            # metres per pixel asked for (NAIP in California is 60 cm)
TILE_PX = 2048       # pixels a side per request, as Arnis asks
PAD_M = 40.0         # photo fetched past the bbox, for outlines on the edge and the alignment search
BIG_ROOF_M2 = 500.0  # roofs this large that are not houses get grey tones only
MIN_PX = 24          # fewest clean interior pixels for a colour (6 m2 at 0.5 m)
MIN_CLEAN = 0.35     # share of interior pixels that must be clean (not plant, not shadow)
MAX_SHIFT_PX = 8     # alignment search, each way (4 m at 0.5 m)
CHROMA_MAX = 0.09    # Oklab chroma ceiling for a measured roof colour

LOG = lambda *a: print("enrich:", *a, file=sys.stderr, flush=True)

# --------------------------------------------------------------------------------------- colour
def oklab(rgb):
    """Oklab of sRGB colours (..., 3) in 0..255, as Arnis's colors.rs rgb_to_oklab computes it."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    l = np.cbrt(0.41222147 * r + 0.53633255 * g + 0.051445995 * b)
    m = np.cbrt(0.2119035 * r + 0.6806995 * g + 0.10739696 * b)
    s = np.cbrt(0.08830246 * r + 0.28171884 * g + 0.6299787 * b)
    return np.stack([0.21045426 * l + 0.7936178 * m - 0.004072047 * s,
                     1.9779985 * l - 2.4285922 * m + 0.4505937 * s,
                     0.025904037 * l + 0.78277177 * m - 0.80867577 * s], axis=-1)


def from_oklab(lab):
    """sRGB 0..255 of an Oklab colour (inverse of oklab), clipped."""
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = np.array([4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s]).clip(0, 1)
    srgb = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return tuple(int(round(v)) for v in (srgb * 255).clip(0, 255))


def tag_distance(a, b):
    """Arnis's block_palette.rs tag_match_distance: squared Oklab distance, lightness at half weight."""
    return (0.5 * (a[..., 0] - b[..., 0])) ** 2 + (a[..., 1] - b[..., 1]) ** 2 + (a[..., 2] - b[..., 2]) ** 2


# The colours a measured roof is snapped to. Each is an entry of Arnis's PALETTE flagged USE_ROOF
# (src/block_palette.rs), so a flat roof takes exactly that block (pick_for_usage returns an exact
# palette hit alone, with no random pick among neighbours), and a pitched roof goes through
# roof_family_for_colour (src/element_processing/buildings.rs) to the block in the last column, with
# no random pick either (Arnis may still move a roof off its wall's tone: roof_distinct_from_wall).
# Only colours roofs in a Southern California aerial photo come in: white and grey membranes,
# gravel, grey and brown concrete tile, clay tile, tan. Blue, green, purple and the saturated paints
# are left out on purpose: a photo's colour cast must not turn a roof into wool.
SNAP = [  # (name, rgb, block on a flat roof, block on a pitched roof)
    ("white",       (207, 213, 214), "white_concrete",        "polished_andesite"),
    ("quartz",      (236, 230, 223), "quartz_block",          "polished_andesite"),
    ("light grey",  (159, 159, 159), "smooth_stone",          "polished_andesite"),
    ("grey",        (126, 126, 126), "stone",                 "polished_andesite"),
    ("dark grey",   (80, 80, 83),    "deepslate",             "cobbled_deepslate"),
    ("charcoal",    (55, 58, 62),    "gray_concrete",         "deepslate_tiles"),
    ("blackish",    (48, 43, 50),    "polished_blackstone_bricks", "deepslate_tiles"),
    ("tan",         (216, 203, 156), "sandstone",             "polished_andesite"),
    ("buff",        (210, 178, 161), "white_terracotta",      "brick"),
    ("mud",         (137, 104, 79),  "mud_bricks",            "brick"),
    ("taupe",       (135, 107, 98),  "light_gray_terracotta", "brick"),
    ("warm grey",   (125, 125, 115), "light_gray_concrete",   "polished_andesite"),
    ("terracotta",  (152, 94, 68),   "terracotta",            "terracotta"),
    ("brick",       (151, 98, 83),   "brick",                 "brick"),
    ("red tile",    (143, 61, 47),   "red_terracotta",        "red_terracotta"),
    ("orange tile", (162, 84, 38),   "orange_terracotta",     "terracotta"),
    ("brown",       (77, 51, 36),    "brown_terracotta",      "mangrove_planks"),
    ("dark brown",  (58, 42, 36),    "gray_terracotta",       "deepslate_tiles"),
]
SNAP_LAB = oklab(np.array([s[1] for s in SNAP], dtype=float))


SNAP_GREY = np.hypot(SNAP_LAB[:, 1], SNAP_LAB[:, 2]) < 0.035


def roof_colour(rgb, neutral=False):
    """Measured mean colour -> the hex written: chroma clamped, lightness kept in range, snapped.
    A colour is snapped within its own side of the line Arnis draws for pitched roofs (Oklab chroma
    0.035: greys become slate or zinc by lightness, anything warmer clay tile), so the snap never
    moves a roof from one of those families to the other. neutral keeps only the lightness: a large
    flat roof drawn in a warm block reads as pink or maroon from above, so those stay grey."""
    lab = oklab(np.array(rgb, dtype=float))
    L, a, b = lab
    c = math.hypot(a, b)
    if c > CHROMA_MAX: a, b = a * CHROMA_MAX / c, b * CHROMA_MAX / c
    lab = np.array([min(max(L, 0.2), 0.95), a, b])
    d = tag_distance(SNAP_LAB, lab[None, :])
    if neutral: c = 0.0; lab = np.array([lab[0], 0.0, 0.0])
    d[SNAP_GREY != (c < 0.035)] = np.inf
    i = int(np.argmin(d))
    return "#%02x%02x%02x" % SNAP[i][1], SNAP[i][0]


# ------------------------------------------------------------------------------------- geometry
def utm_zone(lon): return int((lon + 180) // 6) + 1


def utm(lon, lat, zone):
    """GRS80 transverse Mercator (Krueger series, mm-exact), easting and northing in metres.
    NAD83 and WGS84 differ by about a metre here; the alignment search takes that up."""
    lon, lat = np.asarray(lon, float), np.asarray(lat, float)
    a, f, k0 = 6378137.0, 1 / 298.257222101, 0.9996
    n = f / (2 - f)
    A = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    al = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16, 13 * n ** 2 / 48 - 3 * n ** 3 / 5, 61 * n ** 3 / 240)
    phi, lam = np.radians(lat), np.radians(lon - (zone * 6 - 183))
    e2n = 2 * math.sqrt(n) / (1 + n)
    t = np.sinh(np.arctanh(np.sin(phi)) - e2n * np.arctanh(e2n * np.sin(phi)))
    xi, eta = np.arctan2(t, np.cos(lam)), np.arctanh(np.sin(lam) / np.sqrt(1 + t * t))
    E = eta + sum(al[j] * np.cos(2 * (j + 1) * xi) * np.sinh(2 * (j + 1) * eta) for j in range(3))
    N = xi + sum(al[j] * np.sin(2 * (j + 1) * xi) * np.cosh(2 * (j + 1) * eta) for j in range(3))
    return 500000 + k0 * A * E, k0 * A * N


def inside(px, py, poly):
    """Even-odd point-in-polygon for arrays of points against one ring (k, 2)."""
    res = np.zeros(px.shape, bool)
    x0, y0 = poly[:, 0], poly[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    for a, b, c, d in zip(x0, y0, x1, y1):
        if b == d: continue
        hit = ((b > py) != (d > py)) & (px < a + (py - b) * (c - a) / (d - b))
        res ^= hit
    return res


def area(poly):
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def buildings(data, bbox=None):
    """(element, lon/lat ring) of every closed building way with all its nodes in the file, and with
    one inside bbox (S, W, N, E) if given: the saved file reaches well past the bake."""
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in data["elements"] if e["type"] == "node" and "lon" in e}
    out = []
    for e in data["elements"]:
        t = e.get("tags") or {}
        if e["type"] != "way" or not t.get("building") or t.get("building") == "no": continue
        nd = e.get("nodes") or []
        if len(nd) < 4 or nd[0] != nd[-1] or any(n not in nodes for n in nd): continue
        ring = np.array([nodes[n] for n in nd[:-1]], float)
        if bbox is not None and not ((ring[:, 1] >= bbox[0]) & (ring[:, 0] >= bbox[1]) & (ring[:, 1] <= bbox[2]) & (ring[:, 0] <= bbox[3])).any():
            continue
        out.append((e, ring))
    return out


# -------------------------------------------------------------------------------------- Overture
HOMES = {"house", "detached", "semidetached_house", "terrace", "bungalow", "villa", "residential",
         "apartments", "dormitory"}
LOW_KINDS = {"roof", "carport", "garage", "garages", "shed", "hut", "kiosk", "greenhouse"}
HOUSES = {"house", "detached", "semidetached_house", "terrace", "bungalow", "villa", "farm", "cabin"}


def storeys(kind, wall_m):
    """Arnis's survey/heights.rs storeys(): the storeys a wall height holds, or None to keep metres."""
    if wall_m < 3.5: return None
    levels = max(1, round((wall_m - 0.4) / 2.8))
    if kind in HOMES: return levels
    if kind != "yes": return None
    if levels <= 7: return levels
    tall = round((wall_m - 1.0) / 4.0)
    return tall if tall > 7 else None


def overture_features(bbox, cache, session):
    """Overture building polygons over the bbox from the z14 PMTiles: [(id, ring lon/lat, height, floors)]."""
    from pmtiles.reader import Reader
    import mapbox_vector_tile as mvt
    s, w, n, e = bbox
    z = 14
    def tile_xy(lon, lat):
        k = 2 ** z
        return int((lon + 180) / 360 * k), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * k)
    (x0, y0), (x1, y1) = tile_xy(w, n), tile_xy(e, s)
    cdir = cache / "overture"; cdir.mkdir(parents=True, exist_ok=True)
    reader = None
    feats = []
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            cp = cdir / f"buildings_{OVERTURE_REL}_{z}_{tx}_{ty}.mvt"
            if cp.exists(): raw = cp.read_bytes()
            else:
                if reader is None:
                    def get(off, ln):
                        r = session.get(OVERTURE_URL, headers={"Range": f"bytes={off}-{off + ln - 1}", **UA}, timeout=60)
                        r.raise_for_status(); return r.content
                    reader = Reader(get)
                raw = reader.get(z, tx, ty) or b""
                cp.write_bytes(raw)
            if not raw: continue
            if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
            for layer in mvt.decode(raw, default_options={"y_coord_down": True}).values():
                ext = layer.get("extent", 4096)
                for f in layer["features"]:
                    p = f.get("properties", {})
                    h, fl = p.get("height"), p.get("num_floors")
                    if h is None and fl is None: continue
                    g = f["geometry"]
                    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"] if g["type"] == "MultiPolygon" else []
                    for poly in polys:
                        ring = np.array(poly[0], float)
                        k = 2 ** z
                        lon = (tx + ring[:, 0] / ext) / k * 360 - 180
                        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (ty + ring[:, 1] / ext) / k))))
                        feats.append((p.get("id") or f"{tx}/{ty}/{len(feats)}", np.stack([lon, lat], 1), h, fl))
    return feats


def fill_heights(blds, feats, bbox, stats):
    """building:levels / height from Overture for buildings with neither."""
    s, w, n, e = bbox
    lat0 = (s + n) / 2
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    to_m = lambda ll: np.stack([(ll[:, 0] - w) * kx, (ll[:, 1] - s) * ky], 1)
    cell = 60.0
    grid = {}
    ov = []
    for fid, ring, h, fl in feats:
        r = to_m(ring)
        i = len(ov); ov.append((fid, r, h, fl, area(r)))
        (ax, ay), (bx, by) = r.min(0), r.max(0)
        for gx in range(int(ax // cell), int(bx // cell) + 1):
            for gy in range(int(ay // cell), int(by // cell) + 1):
                grid.setdefault((gx, gy), []).append(i)
    # an id's pieces (a building cut by tile edges) count together
    by_id_area = {}
    by_id_val = {}
    for fid, r, h, fl, a in ov:
        by_id_area[fid] = by_id_area.get(fid, 0.0) + a; by_id_val[fid] = (h, fl)
    for el, ring in blds:
        t = el["tags"]
        if "height" in t or "building:levels" in t or "building:part" in t: continue
        stats["no_height_before"] += 1
        r = to_m(ring); a = area(r)
        if a < 4: continue
        (ax, ay), (bx, by) = r.min(0), r.max(0)
        step = max(0.5, math.sqrt(a / 80))
        gx, gy = np.meshgrid(np.arange(ax + step / 2, bx, step), np.arange(ay + step / 2, by, step))
        px, py = gx.ravel(), gy.ravel()
        m = inside(px, py, r); px, py = px[m], py[m]
        if len(px) < 5: continue
        cand = {i for x in range(int(ax // cell), int(bx // cell) + 1) for y in range(int(ay // cell), int(by // cell) + 1)
                for i in grid.get((x, y), ())}
        cover = {}
        for i in cand:
            fid, rr, h, fl, oa = ov[i]
            if rr[:, 0].max() < ax or rr[:, 0].min() > bx or rr[:, 1].max() < ay or rr[:, 1].min() > by: continue
            hit = inside(px, py, rr)
            if hit.any(): cover[fid] = cover.get(fid, np.zeros(len(px), bool)) | hit
        best = None
        for fid, hit in cover.items():
            share = hit.mean()
            # at least half the OSM outline under it, and not one big outline over a row of houses
            if share >= 0.5 and by_id_area[fid] <= 3.0 * a and (best is None or share > best[0]): best = (share, fid)
        if best is None: continue
        h, fl = by_id_val[best[1]]
        kind = t.get("building", "yes")
        house_sized = kind in HOUSES or (kind == "yes" and a <= 250)
        try: h = float(h) if h is not None else None
        except (TypeError, ValueError): h = None
        try: fl = int(round(float(fl))) if fl is not None else None
        except (TypeError, ValueError): fl = None
        if h is not None and not (2.0 <= h <= 300): h = None
        if fl is not None and not (1 <= fl <= 80): fl = None
        # A house-sized outline over 12 m, or of more than four floors, is a bad estimate (the
        # machine-learnt heights read hillsides and trees as building): leave it to Arnis.
        # Under 3 m on an outline of 60 m2 or more that is not a carport or shed is the same kind of
        # miss (seen on 200 to 255 m2 homes read as 2.1 to 2.4 m), and would flatten a building that
        # Arnis otherwise sizes itself.
        low = h is not None and h < 3.0 and a >= 60 and fl is None and kind not in LOW_KINDS
        if low or house_sized and ((h or 0) > 12 or (fl or 0) > 4):
            stats["overture_implausible"] = stats.get("overture_implausible", 0) + 1; continue
        if h is None and fl is None: continue
        # Overture's height is the top of the building; a house's walls stop under its roof.
        wall = None if h is None else (h - 1.5 if house_sized else h)
        lv = storeys(kind, wall) if wall is not None else None
        # a low home is still one storey of a home, not a shed's metres
        if lv is None and kind in HOMES and h is not None and h >= 3.0: lv = 1
        if fl is not None and (lv is not None or h is None): lv = fl
        if lv is not None:
            t["building:levels"] = str(lv); stats["levels_filled"] += 1
        else:
            t["height"] = f"{h:.1f}"; stats["heights_filled"] += 1


# ------------------------------------------------------------------------------------------ NAIP
def read_world_file(img_path):
    for ext in (".pgw", ".jgw", ".wld", ".tfw", ".pngw", ".jpgw"):
        wf = Path(img_path).with_suffix(ext)
        if wf.exists():
            a, d, b, e_, c, f = [float(v) for v in wf.read_text().split()[:6]]
            return a, c - a / 2, f - e_ / 2   # pixel size, west and north edge of the top-left pixel
    raise FileNotFoundError(f"no world file next to {img_path}")


class Mosaic:
    """RGB pixels on a north-up grid: x0/y0 the west/north edge, res metres a pixel."""
    def __init__(self, rgb, valid, x0, y0, res):
        self.rgb, self.valid, self.x0, self.y0, self.res = rgb, valid, x0, y0, res


def naip_mosaic(box_utm, epsg, cache, session):
    """Fetch (or reuse) the NAIP Plus tiles over box_utm on a fixed 1024 m grid at RES; None if none came."""
    from PIL import Image
    span = TILE_PX * RES
    W, S, E, N = box_utm
    tx0, ty0, tx1, ty1 = int(W // span), int(S // span), int(E // span), int(N // span)
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    rgb = np.zeros((ny * TILE_PX, nx * TILE_PX, 3), np.uint8)
    valid = np.zeros(rgb.shape[:2], bool)
    cdir = cache / "naip"; cdir.mkdir(parents=True, exist_ok=True)
    got = failed = 0; down = None; nbytes = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            w, s = tx * span, ty * span
            cp = cdir / f"naip_{epsg}_{RES}_{int(w)}_{int(s)}.jpg"
            img = None
            if cp.exists():
                try: img = Image.open(cp).convert("RGB")
                except Exception: img = None
            if img is None and down is None:
                params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": NAIP_LAYER, "STYLES": "",
                          "CRS": f"EPSG:{epsg}", "BBOX": f"{w},{s},{w + span},{s + span}",
                          "WIDTH": TILE_PX, "HEIGHT": TILE_PX, "FORMAT": "image/jpeg"}
                for attempt in range(3):
                    if attempt: time.sleep(0.8 * attempt)
                    try:
                        r = session.get(NAIP_WMS, params=params, headers=UA, timeout=90)
                    except Exception as ex:   # unreachable host: do not ask again
                        down = f"{type(ex).__name__}: {str(ex)[:160]}"; break
                    if r.status_code != 200: continue
                    try:
                        im = Image.open(io.BytesIO(r.content)).convert("RGB")
                    except Exception:
                        continue   # a busy WMS answers with an XML exception instead of the image
                    if im.size != (TILE_PX, TILE_PX): continue
                    cp.write_bytes(r.content); nbytes += len(r.content); img = im; break
            if img is None: failed += 1; continue
            a = np.asarray(img)
            r0, c0 = (ty1 - ty) * TILE_PX, (tx - tx0) * TILE_PX
            rgb[r0:r0 + TILE_PX, c0:c0 + TILE_PX] = a
            # NAIP outside its coverage comes back black or white
            valid[r0:r0 + TILE_PX, c0:c0 + TILE_PX] = ~((a.max(2) < 6) | (a.min(2) > 250))
            got += 1
    info = {"tiles": got, "failed": failed, "downloaded_mb": round(nbytes / 1e6, 1)}
    if down: info["error"] = down
    if not got: return None, info
    return Mosaic(rgb, valid, tx0 * span, (ty1 + 1) * span, RES), info


def image_mosaic(path):
    from PIL import Image
    res, x0, y0 = read_world_file(path)
    a = np.asarray(Image.open(path).convert("RGB"))
    return Mosaic(a, ~((a.max(2) < 6) | (a.min(2) > 250)), x0, y0, res)


def footprint_pixels(ring_xy, mos, erode):
    """Pixels of an outline on the photo grid: (rows, cols) of those inside it after shrinking it by
    `erode` pixels, (rows, cols) of its rim (inside, with a neighbour outside), and the full count."""
    from scipy.ndimage import binary_erosion
    col = (ring_xy[:, 0] - mos.x0) / mos.res - 0.5
    row = (mos.y0 - ring_xy[:, 1]) / mos.res - 0.5
    c0, c1 = int(math.floor(col.min())) - 1, int(math.ceil(col.max())) + 2
    r0, r1 = int(math.floor(row.min())) - 1, int(math.ceil(row.max())) + 2
    if c0 < 0 or r0 < 0 or c1 > mos.rgb.shape[1] or r1 > mos.rgb.shape[0]: return None
    cc, rr = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
    m = inside(cc.ravel().astype(float), rr.ravel().astype(float), np.stack([col, row], 1)).reshape(cc.shape)
    full = int(m.sum())
    one = binary_erosion(m)
    rim = m & ~one
    if erode: m = binary_erosion(m, iterations=erode)
    return rr[m], cc[m], rr[rim], cc[rim], full


def colour_of(px):
    """Trimmed mean colour of a roof's pixels (n, 3), without plants and shadow; None if too few are clean."""
    p = px.astype(float)
    r, g, b = p[:, 0], p[:, 1], p[:, 2]
    exg = (2 * g - r - b) / (r + g + b + 1)
    plant = (exg > 0.05) & (g >= r)               # tree crowns over the roof, lawn at the edge
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    ok = ~plant
    if ok.sum() < MIN_PX: return None, "plants"
    ref = np.percentile(lum[ok], 80)
    ok &= (lum >= 0.5 * ref) & (lum > 20)         # cast shadow; a roof's shaded slope stays in
    ok &= (p.min(1) < 252)                        # blown-out glints
    if ok.sum() < max(MIN_PX, MIN_CLEAN * len(p)): return None, "shadow" if (~plant).sum() >= MIN_PX else "plants"
    # The sunlit part: a pitched roof shows its material on the slope that faces the sun, and Arnis
    # shades the block itself. The brightest tenth is left out for glints and skylights.
    q = p[ok]; lq = lum[ok]
    lo, hi = np.percentile(lq, [55, 92])
    mid = q[(lq >= lo) & (lq <= hi)]
    return (mid if len(mid) >= 5 else q).mean(0), None


def find_shift(rims, mos):
    """The whole-pixel shift of the photo against the outlines that puts the outlines on its edges.
    Photo and outline disagree by a metre or more (NAD83 against WGS84, the orthophoto's own
    accuracy, outlines traced off other imagery); a roof sampled that far off takes in its yard or
    shadow. Every roof outline is an edge in the photo, so the shift where the brightness gradient
    along the outlines is strongest is the one that lines them up. Returns (drow, dcol, gain)."""
    from scipy.ndimage import sobel
    lum = (0.299 * mos.rgb[..., 0] + 0.587 * mos.rgb[..., 1] + 0.114 * mos.rgb[..., 2]).astype(np.float32)
    grad = np.hypot(sobel(lum, 0), sobel(lum, 1))
    grad[~mos.valid] = np.nan
    rims = sorted(rims, key=lambda r: -len(r[0]))[:800]
    rims = [r for r in rims if len(r[0]) >= 30]
    if len(rims) < 20: return 0, 0, 0.0
    H, W = lum.shape
    M = MAX_SHIFT_PX
    rims = [(r, c) for r, c in rims if r.min() >= M and c.min() >= M and r.max() < H - M and c.max() < W - M]
    if len(rims) < 20: return 0, 0, 0.0
    R = np.concatenate([r for r, _ in rims]); C = np.concatenate([c for _, c in rims])
    score = {(dr, dc): float(np.nanmean(grad[R + dr, C + dc])) for dr in range(-M, M + 1) for dc in range(-M, M + 1)}
    (dr, dc), best = max(score.items(), key=lambda kv: kv[1])
    base = score[(0, 0)]
    gain = best / base - 1 if base > 0 else 0.0
    if gain < 0.05: return 0, 0, gain   # no clear improvement: trust the georeference
    return dr, dc, gain


def fill_roof_colours(blds, mos, zone, stats):
    items, rims = [], []
    for el, ring in blds:
        if "roof:colour" in el["tags"]: stats["roof_colour_already"] += 1; continue
        E, N = utm(ring[:, 0], ring[:, 1], zone)
        xy = np.stack([E, N], 1)
        a_px = area(xy) / mos.res ** 2
        # shrink the outline: 1 pixel, 2 on larger roofs, so edge pixels (eaves, walls, yard) stay out
        fp = footprint_pixels(xy, mos, 2 if a_px > 400 else 1)
        if fp is None: stats["roof_skipped"]["outside_photo"] += 1; continue
        rr, cc, br, bc, full = fp
        rims.append((br, bc))
        if len(rr) < MIN_PX: stats["roof_skipped"]["too_small"] += 1; continue
        big = area(xy) >= BIG_ROOF_M2 and el["tags"].get("building") not in HOUSES
        items.append((el, rr, cc, big))
    dr, dc, gain = find_shift(rims, mos)
    stats["naip_shift_m"] = [round(dc * mos.res, 1), round(-dr * mos.res, 1)]   # east, north
    stats["naip_shift_gain"] = round(gain, 3)
    H, W = mos.valid.shape
    names = {}
    for el, rr, cc, big in items:
        r2, c2 = rr + dr, cc + dc
        keep = (r2 >= 0) & (c2 >= 0) & (r2 < H) & (c2 < W)
        r2, c2 = r2[keep], c2[keep]
        keep = mos.valid[r2, c2]
        if keep.sum() < MIN_PX: stats["roof_skipped"]["no_photo"] += 1; continue
        rgb, why = colour_of(mos.rgb[r2[keep], c2[keep]])
        if rgb is None: stats["roof_skipped"][why] += 1; continue
        hx, name = roof_colour(rgb, big)
        el["tags"]["roof:colour"] = hx
        names[name] = names.get(name, 0) + 1
        stats["roof_colours_written"] += 1
    stats["roof_colour_families"] = dict(sorted(names.items(), key=lambda kv: -kv[1]))


# ------------------------------------------------------------------------------------------ main
def enrich(data, bbox, cache, use_overture=True, use_naip=True, image=None, image_epsg=None):
    import requests
    from collections import Counter
    stats = {"buildings": 0, "roof_colours_written": 0, "roof_colour_already": 0, "roof_skipped": Counter(),
             "no_height_before": 0, "heights_filled": 0, "levels_filled": 0, "sources": {}}
    blds = buildings(data, bbox)
    stats["buildings"] = len(blds)
    session = requests.Session()
    s, w, n, e = bbox
    if use_overture:
        t = time.time()
        try:
            feats = overture_features(bbox, cache, session)
            fill_heights(blds, feats, bbox, stats)
            stats["sources"]["overture"] = {"ok": True, "features": len(feats), "seconds": round(time.time() - t, 1)}
        except Exception as ex:
            LOG(f"Overture heights skipped: {type(ex).__name__}: {str(ex)[:200]}")
            stats["sources"]["overture"] = {"ok": False, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    if use_naip or image:
        t = time.time()
        zone = utm_zone((w + e) / 2)
        key = "image" if image else "naip"
        try:
            if image:
                if image_epsg and image_epsg != 26900 + zone: raise ValueError(f"--image-epsg {image_epsg} is not NAD83 UTM zone {zone}")
                mos, info = image_mosaic(image), {"file": str(image)}
            else:
                E, N = utm(np.array([w, e, w, e]), np.array([s, s, n, n]), zone)
                box = (E.min() - PAD_M, N.min() - PAD_M, E.max() + PAD_M, N.max() + PAD_M)
                mos, info = naip_mosaic(box, 26900 + zone, cache, session)
            if mos is None:
                LOG(f"NAIP roof colours skipped: {info.get('error') or 'no photo tile came'}")
                stats["sources"][key] = {"ok": False, **info}
            else:
                fill_roof_colours(blds, mos, zone, stats)
                stats["sources"][key] = {"ok": True, **info, "seconds": round(time.time() - t, 1)}
                if info.get("failed"): LOG(f"NAIP: {info['failed']} of {info['failed'] + info['tiles']} photo tiles missing")
        except Exception as ex:
            LOG(f"roof colours skipped: {type(ex).__name__}: {str(ex)[:200]}")
            stats["sources"][key] = {"ok": False, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    stats["roof_skipped"] = dict(stats["roof_skipped"])
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src"); ap.add_argument("bbox"); ap.add_argument("out")
    ap.add_argument("--stats", help="write the counts here as JSON")
    ap.add_argument("--cache", help="download cache folder (default: enrich_cache next to OUT)")
    ap.add_argument("--no-overture", action="store_true")
    ap.add_argument("--no-naip", action="store_true")
    ap.add_argument("--image", help="local aerial image with a world file, in place of NAIP (tests)")
    ap.add_argument("--image-epsg", type=int)
    args = ap.parse_args()
    t0 = time.time()
    bbox = [float(v) for v in args.bbox.split(",")]
    out = Path(args.out)
    cache = Path(args.cache) if args.cache else out.parent / "enrich_cache"
    try:
        data = json.loads(Path(args.src).read_text())
        cache.mkdir(parents=True, exist_ok=True)
        stats = enrich(data, bbox, cache, not args.no_overture, not args.no_naip, args.image, args.image_epsg)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":"))); os.replace(tmp, out)
        stats["ok"] = True
    except Exception as ex:   # never fail a build: hand on the file as it came
        LOG(f"failed, writing the input unchanged: {type(ex).__name__}: {str(ex)[:300]}")
        shutil.copy(args.src, out)
        stats = {"ok": False, "error": f"{type(ex).__name__}: {str(ex)[:300]}"}
    stats["seconds"] = round(time.time() - t0, 1)
    if args.stats: Path(args.stats).write_text(json.dumps(stats, indent=1))
    LOG(f"{stats.get('buildings', 0)} buildings: {stats.get('roof_colours_written', 0)} roof colours, "
        f"{stats.get('heights_filled', 0)} heights and {stats.get('levels_filled', 0)} storey counts filled "
        f"({', '.join(k + (' ok' if v.get('ok') else ' failed') for k, v in stats.get('sources', {}).items()) or 'no source'}) "
        f"in {stats['seconds']} s")


if __name__ == "__main__":
    main()
