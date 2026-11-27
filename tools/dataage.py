#!/usr/bin/env python3
"""How old is the laser scan under a chunk? Finds OSM buildings the scan saw as bare ground.

    dataage.py chunk OSM.json S,W,N,E [--out OUT.json]      one chunk, from the OSM file of its bake
    dataage.py area AREA.json [--only ids | --keep-key K] [--osm-dir DIR] [--out DIR]
                                                              every chunk of an area, before any bake

Arnis (src/survey/usa.rs, index.rs) reads each 1 km tile of a US area from the USGS 3DEP survey
that covers it, the newest first (the year in the survey's name), then the densest. Where that
survey was flown before a neighbourhood was built, OSM has houses and streets on what the scan
saw as graded ground: Arnis then gives the houses no measured height or roof shape, the yards
come out as open ground, trees are missing. This tool reads the same surveys, from the same
public bucket (s3://usgs-lidar-public, Entwine point tiles), at about one point a square metre
(a few tens of MB a chunk, not the bake's hundreds), and for each OSM building counts the laser
returns inside its outline that stand clear of the ground:

* scan_built: at least RAISED_BUILT of the returns inside the outline stand MIN_RAISE_M or more
  above the scan's ground (class 2) and are not noise or water;
* scan_bare: at most RAISED_BARE do (map says building, scan says bare);
* mixed: in between (a tree over a roof, an outline a few metres off);
* unjudged: too few returns expected for the outline's area.

Roads: every drivable OSM way is walked in 5 m steps. A step is in a neighbourhood when OSM
buildings cover NEIGHBOURHOOD_COVER of the square ROAD_REACH_M around it, and runs through bare
scan when the laser sees almost nothing raised there (BARE_RAISED): the street of a newer
neighbourhood, which was graded dirt, or not yet there, when the scan was flown.

Clusters: scan_bare buildings are gathered on a CLUSTER_M grid; neighbouring cells with at least
CLUSTER_MIN of them make one cluster (centre, count, bbox). A cluster of at least LIKELY_MIN
whose bare buildings are at least LIKELY_SHARE of the judged ones there is `likely_newer`: a
neighbourhood built after the scan (on batch 1 these are the known post-2011 ones; the scattered
bare spots of older suburbs, 0.1 to 2 % of their buildings, stay below it).

The output also says which survey Arnis picks where (the per-tile rule of index.rs choose(), on
tiles of about 1 km in latitude and longitude rather than the UTM grid), and when its points
under the chunk were flown, from their GPS time (adjusted standard GPS time; surveys in GPS
week time have no date). In `area` mode it also asks USGS's NAIP Plus image service which photo
lies under each chunk (best effort: the service's field names are printed as they come).

`area` mode reads OSM for each chunk from --osm-dir/<id>/osm.json when there is one, else from
Overpass (buildings and highways only; Arnis's own OSM source may differ a little). It never
fails a build: a chunk that cannot be read is reported as an error line and the rest go on.
Needs laspy with a LAZ backend (pip install "laspy[lazrs]").
"""
import argparse, concurrent.futures as cf, datetime, io, json, math, os, sys, time, urllib.parse, urllib.request
from pathlib import Path

import numpy as np

UA = {"User-Agent": "mcdrives-build"}
BUCKET = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/"
STAC = "https://s3-us-west-2.amazonaws.com/usgs-lidar-stac/ept/"
INDEX_URL = "https://usgs.entwine.io/boundaries/resources.geojson"   # what Arnis reads (usa.rs INDEX_URL)
NAIP_IMAGE = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer"
OVERPASS = ("overpass-api.de", "overpass.private.coffee", "overpass.kumi.systems")
CACHE = Path(os.environ.get("DATAAGE_CACHE") or Path.home() / ".cache" / "mcdrives-dataage")

TARGET_PPM2 = 0.5        # points a square metre to read at least (the full surveys have 2 to 30)
MIN_RAISE_M = 1.8        # a return this far above the ground is something standing (a carport roof is 2.3 m)
RAISED_BUILT = 0.35
RAISED_BARE = 0.10
MIN_EXPECTED = 8         # fewest returns an outline must expect to be judged
ROAD_STEP_M = 5.0
ROAD_REACH_M = 30        # half side of the square around a road step
NEIGHBOURHOOD_COVER = 0.05
BARE_RAISED = 0.02
CLUSTER_M = 200.0
CLUSTER_MIN = 3
# A cluster this large, with this share of its judged buildings bare, is a newer neighbourhood: on batch 1
# the known post-scan ones (Cypress Village, Baker Ranch, Orchard Hills, Tustin Legacy) have shares of
# 0.6 to 1.0, while the bare spots of older suburbs stay under 0.45 (trees over small roofs, outlines off).
LIKELY_MIN = 20
LIKELY_SHARE = 0.5
GROUND_CELL_M = 4.0
NOT_STANDING = {2, 7, 9, 18}  # ground, noise, water, high noise
DRIVABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
            "living_street", "service", "motorway_link", "trunk_link", "primary_link", "secondary_link",
            "tertiary_link"}

LOG = lambda *a: print("dataage:", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------------------- web
def get(url, tries=4, timeout=60, data=None):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404): raise
            err = e
        except Exception as e:
            err = e
        time.sleep(2 * (i + 1))
    raise err


def get_json(url, **kw): return json.loads(get(url, **kw))


def cached_json(name, url, days=7):
    path = CACHE / name
    if path.exists() and time.time() - path.stat().st_mtime < days * 86400:
        try: return json.loads(path.read_text())
        except ValueError: pass
    j = get_json(url, timeout=180)
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(j))
    return j


# ----------------------------------------------------------------------------------- geometry
def merc(lon, lat):
    return np.radians(lon) * 6378137.0, np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * 6378137.0


def unmerc(x, y):
    return np.degrees(x / 6378137.0), np.degrees(2 * np.arctan(np.exp(y / 6378137.0)) - np.pi / 2)


class Local:
    """Metres east and north of a bbox's south-west corner (equirectangular; a chunk is 2.5 km)."""
    def __init__(self, bbox):
        self.s, self.w, self.n, self.e = bbox
        self.k = 111320.0 * math.cos(math.radians((self.s + self.n) / 2))
        self.width, self.height = (self.e - self.w) * self.k, (self.n - self.s) * 110574.0
    def xy(self, lon, lat): return (np.asarray(lon) - self.w) * self.k, (np.asarray(lat) - self.s) * 110574.0
    def ll(self, x, y): return self.w + np.asarray(x) / self.k, self.s + np.asarray(y) / 110574.0


def inside(px, py, ring):
    """Even-odd test of points (px, py) against a ring (n, 2)."""
    res = np.zeros(px.shape, bool)
    x0, y0 = ring[:, 0], ring[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    for a, b, c, d in zip(x0, y0, x1, y1):
        if b == d: continue
        hit = ((b > py) != (d > py)) & (px < (c - a) * (py - b) / (d - b) + a)
        res ^= hit
    return res


def survey_year(name):
    """The year Arnis reads from a survey's name (index.rs survey_year): the first four digits
    between 1990 and 2099, else a trailing letter and two digits (B22 = fiscal 2022)."""
    i = 0
    while i < len(name):
        if name[i].isdigit():
            j = i
            while j < len(name) and name[j].isdigit(): j += 1
            if j - i == 4 and 1990 <= int(name[i:j]) < 2100: return int(name[i:j])
            i = j
        else: i += 1
    for t in reversed(name.replace("-", "_").replace(" ", "_").split("_")):
        if len(t) == 3 and t[0].isupper() and t[1:].isdigit(): return 2000 + int(t[1:])
    return 0


# ------------------------------------------------------------------------------- survey index
class Footprint:
    def __init__(self, name, url, geom, count):
        self.name, self.url, self.year = name, url, survey_year(name)
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        self.rings = [np.array(r, float)[:, :2] for p in polys for r in p if len(r) >= 3]
        allp = np.concatenate(self.rings)
        self.bbox = (allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max())
        k = math.cos(math.radians((self.bbox[1] + self.bbox[3]) / 2))
        a = sum(abs(np.dot(r[:, 0], np.roll(r[:, 1], -1)) - np.dot(np.roll(r[:, 0], -1), r[:, 1])) / 2 for r in self.rings)
        m2 = a * k * 111320.0 * 110574.0
        self.density = count / m2 if m2 > 0 else 0.0
    def meets(self, w, s, e, n):
        return self.bbox[0] < e and self.bbox[2] > w and self.bbox[1] < n and self.bbox[3] > s
    def contains(self, lon, lat):
        res = np.zeros(np.shape(lon), bool)
        for r in self.rings: res ^= inside(np.asarray(lon, float), np.asarray(lat, float), r)
        return res


def load_index(bbox):
    """The surveys whose footprint meets bbox (S, W, N, E): from the index Arnis reads, else (where
    that host is out of reach) from the bucket's own ept.json bounds and the USGS STAC items."""
    s, w, n, e = bbox
    try:
        j = cached_json("resources.geojson", INDEX_URL)
        out = []
        for f in j["features"]:
            p = f["properties"]
            try: fp = Footprint(p["name"], p["url"], f["geometry"], float(p.get("count") or 0))
            except Exception: continue
            if fp.meets(w, s, e, n): out.append(fp)
        return out, "entwine index"
    except Exception as ex:
        LOG(f"survey index {INDEX_URL} unavailable ({ex}); using the bucket's ept.json bounds and STAC items")
    bounds = CACHE / "ept_bounds.json"
    have = json.loads(bounds.read_text()) if bounds.exists() and time.time() - bounds.stat().st_mtime < 7 * 86400 else None
    if have is None:
        names, marker = [], ""
        while True:
            xml = get(BUCKET + "?delimiter=/&marker=" + urllib.parse.quote(marker)).decode()
            got = [p.split("</Prefix>")[0] for p in xml.split("<Prefix>")[2:]]
            names += [g.rstrip("/") for g in got if g]
            if "<IsTruncated>true" not in xml or not got: break
            marker = got[-1]
        def one(nm):
            try:
                ej = get_json(BUCKET + nm + "/ept.json", tries=2)
                b = ej.get("boundsConforming") or ej["bounds"]
                (lw, ls), (le, ln) = unmerc(b[0], b[1]), unmerc(b[3], b[4])
                return nm, [float(lw), float(ls), float(le), float(ln), ej.get("points", 0)]
            except Exception: return nm, None
        with cf.ThreadPoolExecutor(32) as ex: have = {k: v for k, v in ex.map(one, names) if v}
        bounds.parent.mkdir(parents=True, exist_ok=True); bounds.write_text(json.dumps(have))
    out = []
    for nm, (bw, bs, be, bn, cnt) in have.items():
        if not (bw < e and be > w and bs < n and bn > s): continue
        try:
            item = cached_json(f"stac/{nm}.json", STAC + nm + ".json", days=30)
            out.append(Footprint(nm, BUCKET + nm + "/ept.json", item["geometry"], cnt))
        except Exception as ex:
            LOG(f"{nm}: no STAC footprint ({ex}); using its bounds")
            ring = [[bw, bs], [be, bs], [be, bn], [bw, bn], [bw, bs]]
            out.append(Footprint(nm, BUCKET + nm + "/ept.json", {"type": "Polygon", "coordinates": [ring]}, cnt))
    return out, "bucket bounds + STAC"


_EPTS = {}
def ept_of(f):
    if f.url not in _EPTS:
        try: _EPTS[f.url] = Ept(f.url)
        except Exception as ex: LOG(f"{f.name}: {ex}"); _EPTS[f.url] = None
    return _EPTS[f.url]


def choose(foot, bbox, cell_deg=0.009, by_points=True):
    """Which survey Arnis reads where: per cell of about 1 km, 5 x 5 probes; of the surveys that
    cover at least three quarters as many probes as the best, the newest, then the densest
    (index.rs choose()). A probe is covered where the survey's footprint holds it and, with
    by_points, where its octree has points within about 150 m (the footprints this tool can reach
    from some networks are coarse hulls). Returns [(cell (s, w, n, e), footprint or None, [(name,
    probes by footprint, probes by points)])]."""
    s, w, n, e = bbox
    rows, cols = max(1, round((n - s) / cell_deg)), max(1, round((e - w) / (cell_deg / math.cos(math.radians(s)))))
    out = []
    for r in range(rows):
        for c in range(cols):
            cs, cn = s + (n - s) * r / rows, s + (n - s) * (r + 1) / rows
            cw, ce = w + (e - w) * c / cols, w + (e - w) * (c + 1) / cols
            g = (np.arange(5) * 0.2 + 0.1)
            plon, plat = np.meshgrid(cw + (ce - cw) * g, cs + (cn - cs) * g)
            cands, seen = [], []
            for f in foot:
                if not f.meets(cw, cs, ce, cn): continue
                inf = f.contains(plon, plat)
                k, kp = int(inf.sum()), None
                if by_points and k:
                    ept = ept_of(f)
                    kp = int((inf & ept.covers(plon, plat)).sum()) if ept else 0
                    k = kp
                seen.append((f.name, int(inf.sum()), kp))
                cands.append((k, f))
            most = max([k for k, _ in cands], default=0)
            best = max(((f.year, f.density, f.name), f) for k, f in cands if most and 4 * k >= 3 * most)[1] if most else None
            out.append(((cs, cw, cn, ce), best, seen))
    return out


# --------------------------------------------------------------------------------- EPT reading
class Ept:
    def __init__(self, url):
        self.base = url.rsplit("/", 1)[0] + "/"
        self.meta = get_json(url)
        self.b = self.meta["bounds"]
        self.span = self.meta.get("span", 128)
        self.hier = {}
        self.hier.update(get_json(self.base + "ept-hierarchy/0-0-0-0.json"))

    def _node(self, key):
        """The hierarchy's count for a node key (loading its subtree file when needed), 0 if absent."""
        v = self.hier.get(key)
        if v == -1:
            try: self.hier.update(get_json(self.base + f"ept-hierarchy/{key}.json"))
            except Exception as ex: LOG(f"hierarchy {key}: {ex}"); return 0
            v = self.hier.get(key, 0)
            if v == -1: return 0
        return v or 0

    def covers(self, lon, lat, node_m=150.0):
        """Whether the octree has points in the node of about node_m (ground metres) holding each point."""
        lon, lat = np.asarray(lon, float), np.asarray(lat, float)
        size0 = self.b[3] - self.b[0]
        k = math.cos(math.radians(float(lat.mean())))
        d = max(0, int(round(math.log2(size0 * k / node_m))))
        x, y = merc(lon, lat)
        res = np.zeros(lon.shape, bool)
        for idx in np.ndindex(lon.shape):
            if not (self.b[0] <= x[idx] < self.b[3] and self.b[1] <= y[idx] < self.b[4]): continue
            res[idx] = self._has(d, int((x[idx] - self.b[0]) / size0 * 2 ** d), int((y[idx] - self.b[1]) / size0 * 2 ** d))
        return res

    def _has(self, d, X, Y):
        """Any node at depth d over column (X, Y), walking down from the root through existing nodes."""
        level = [0]   # the z of the nodes over the column at this depth
        for dd in range(0, d + 1):
            sh = d - dd
            px, py = X >> sh, Y >> sh
            nxt = []
            for z in (level if dd else [0]):
                for zz in ((z,) if dd == 0 else (2 * z, 2 * z + 1)):
                    if self._node(f"{dd}-{px}-{py}-{zz}"): nxt.append(zz)
            if not nxt: return False
            level = nxt
        return True

    def nodes(self, rect, target=TARGET_PPM2, maxd=24):
        """Keys of the nodes whose XY square meets rect (x0, y0, x1, y1) in EPSG:3857, down to the
        shallowest depth at which the hierarchy's point counts reach `target` points a ground square
        metre over rect (EPT nodes overlap in XY only through their parents, so levels add up)."""
        x0, y0, x1, y1 = rect
        k = math.cos(math.atan(math.sinh(((y0 + y1) / 2) / 6378137.0)))   # Mercator metres to ground metres
        want = target * (x1 - x0) * (y1 - y0) * k * k
        size0 = self.b[3] - self.b[0]
        level, have, d = [(0, 0, 0, 0)], 0.0, 0
        out = []
        while level and d <= maxd:
            nxt = []
            for (dd, x, y, z) in level:
                key = f"{dd}-{x}-{y}-{z}"
                v = self.hier.get(key)
                if v is None: continue
                if v == -1:
                    try: self.hier.update(get_json(self.base + f"ept-hierarchy/{key}.json"))
                    except Exception as ex: LOG(f"hierarchy {key}: {ex}"); continue
                    v = self.hier.get(key, 0)
                    if v == -1: continue
                w = size0 / 2 ** dd
                nx0, ny0 = self.b[0] + x * w, self.b[1] + y * w
                if nx0 > x1 or nx0 + w < x0 or ny0 > y1 or ny0 + w < y0: continue
                if v > 0:
                    out.append(key)
                    # the share of the node's square inside rect, as the share of its points that count
                    ov = max(0.0, min(x1, nx0 + w) - max(x0, nx0)) * max(0.0, min(y1, ny0 + w) - max(y0, ny0))
                    have += v * ov / (w * w)
                for dx in (0, 1):
                    for dy in (0, 1):
                        for dz in (0, 1): nxt.append((dd + 1, 2 * x + dx, 2 * y + dy, 2 * z + dz))
            if have >= want: break
            level, d = nxt, d + 1
        return out

    def read(self, key):
        import laspy
        raw = get(self.base + f"ept-data/{key}.laz", timeout=120)
        las = laspy.read(io.BytesIO(raw))
        gps = np.asarray(las.gps_time, float) if "gps_time" in las.point_format.dimension_names else np.zeros(len(las.x))
        return (np.asarray(las.x, float), np.asarray(las.y, float), np.asarray(las.z, float),
                np.asarray(las.classification, np.uint8), gps)


def read_points(ept, bbox, lat, target, workers=16):
    s, w, n, e = bbox
    (x0, y0), (x1, y1) = merc(w, s), merc(e, n)
    keys = ept.nodes((x0, y0, x1, y1), target)
    parts = []
    with cf.ThreadPoolExecutor(workers) as ex:
        for got in ex.map(lambda k: _try_read(ept, k), keys):
            if got is None: continue
            X, Y, Z, C, G = got
            m = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)
            if m.any(): parts.append((X[m], Y[m], Z[m], C[m], G[m]))
    if not parts: return None, len(keys)
    return tuple(np.concatenate([p[i] for p in parts]) for i in range(5)), len(keys)


def _try_read(ept, key):
    try: return ept.read(key)
    except Exception as ex: LOG(f"node {key}: {ex}"); return None


def gps_dates(g):
    """1st, 50th and 99th percentile dates of adjusted standard GPS times, or None (week time)."""
    g = g[np.isfinite(g)]
    if not len(g): return None
    q = np.percentile(g, [1, 50, 99])
    if not ((q > 604800).all() or (q < 0).all()): return None
    base = datetime.datetime(1980, 1, 6)
    return [(base + datetime.timedelta(seconds=float(v) + 1e9)).strftime("%Y-%m-%d") for v in q]


# ------------------------------------------------------------------------------------------ OSM
def osm_features(data, bbox):
    """Building outlines (kind, lon/lat ring) with a vertex inside bbox, and drivable ways (lon/lat lines)."""
    nodes = {el["id"]: (el["lon"], el["lat"]) for el in data["elements"] if el["type"] == "node" and "lon" in el}
    s, w, n, e = bbox
    blds, roads = [], []
    for el in data["elements"]:
        if el["type"] != "way": continue
        t = el.get("tags") or {}
        if "geometry" in el: pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
        else:
            nd = el.get("nodes") or []
            if any(k not in nodes for k in nd): continue
            pts = [nodes[k] for k in nd]
        if len(pts) < 2: continue
        a = np.array(pts, float)
        if not ((a[:, 1] >= s) & (a[:, 1] <= n) & (a[:, 0] >= w) & (a[:, 0] <= e)).any(): continue
        if t.get("building") and t["building"] != "no" and len(pts) >= 4 and pts[0] == pts[-1]:
            blds.append((el["id"], t["building"], a[:-1]))
        elif t.get("highway") in DRIVABLE and t.get("area") != "yes":
            roads.append((el["id"], t["highway"], a))
    return blds, roads


def overpass(bbox):
    s, w, n, e = bbox
    q = (f'[out:json][timeout:180];(way["building"]({s},{w},{n},{e});way["highway"]({s},{w},{n},{e}););out geom;')
    last = None
    for host in OVERPASS:
        try:
            return json.loads(get(f"https://{host}/api/interpreter", data=("data=" + urllib.parse.quote(q)).encode(),
                                  timeout=240, tries=2))
        except Exception as ex: last = ex; LOG(f"overpass {host}: {ex}")
    raise RuntimeError(f"no Overpass host answered ({last})")


# ------------------------------------------------------------------------------------- the check
def check(data, bbox, target=TARGET_PPM2, index=None):
    t0 = time.time()
    loc = Local(bbox)
    lat = (bbox[0] + bbox[2]) / 2
    foot, how = index or load_index(bbox)
    cells = choose(foot, bbox)
    used = {}
    for cb, f, _ in cells:
        if f is not None: used.setdefault(f.name, (f, []))[1].append(cb)
    # where the footprint alone would pick another survey than the points do (a coarse footprint)
    cell_rows = [{"bbox": [round(v, 5) for v in cb], "survey": f.name if f else None,
                  "candidates": [{"survey": nm, "probes_footprint": a, "probes_points": b} for nm, a, b in seen if a]}
                 for cb, f, seen in cells]
    W, H = int(math.ceil(loc.width)) + 1, int(math.ceil(loc.height)) + 1
    cnt = np.zeros((H, W), np.int32); up = np.zeros((H, W), np.int32)
    surveys, mb = [], 0.0
    for name, (f, cbs) in used.items():
        ept = Ept(f.url)
        s = min(c[0] for c in cbs); w = min(c[1] for c in cbs); n = max(c[2] for c in cbs); e = max(c[3] for c in cbs)
        pts, nnodes = read_points(ept, (s, w, n, e), lat, target)
        info = {"survey": name, "year_in_name": f.year, "cells": len(cbs), "nodes": nnodes}
        if pts is None: surveys.append(info); continue
        X, Y, Z, C, G = pts
        lon, la = unmerc(X, Y)
        # only the points in the cells this survey was chosen for
        keep = np.zeros(len(X), bool)
        for cs, cw, cn, ce in cbs: keep |= (la >= cs) & (la < cn) & (lon >= cw) & (lon < ce)
        lon, la, Z, C, G = lon[keep], la[keep], Z[keep], C[keep], G[keep]
        px, py = loc.xy(lon, la)
        # the ground: the mean of the class 2 returns per GROUND_CELL_M cell, holes filled from the nearest cell
        gi = (py // GROUND_CELL_M).astype(int).clip(0, int(H // GROUND_CELL_M)); gj = (px // GROUND_CELL_M).astype(int).clip(0, int(W // GROUND_CELL_M))
        gh, gw = int(H // GROUND_CELL_M) + 1, int(W // GROUND_CELL_M) + 1
        g = C == 2
        zs = np.bincount(gi[g] * gw + gj[g], Z[g], gh * gw); zn = np.bincount(gi[g] * gw + gj[g], None, gh * gw)
        ground = np.where(zn > 0, zs / np.maximum(zn, 1), np.nan).reshape(gh, gw)
        if np.isnan(ground).all(): surveys.append({**info, "error": "no ground returns"}); continue
        from scipy import ndimage
        idx = ndimage.distance_transform_edt(np.isnan(ground), return_distances=False, return_indices=True)
        ground = ground[tuple(idx)]
        raised = (Z - ground[gi, gj] >= MIN_RAISE_M) & ~np.isin(C, list(NOT_STANDING))
        ci, cj = py.astype(int).clip(0, H - 1), px.astype(int).clip(0, W - 1)
        np.add.at(cnt, (ci, cj), 1); np.add.at(up, (ci, cj), raised.astype(np.int32))
        area_km2 = sum((c[2] - c[0]) * 110.574 * (c[3] - c[1]) * loc.k / 1000 for c in cbs)
        info.update(points=int(len(Z)), points_per_m2=round(len(Z) / (area_km2 * 1e6), 2),
                    classes={int(k): int(v) for k, v in zip(*np.unique(C, return_counts=True))},
                    flown=gps_dates(G))
        surveys.append(info)
    covered = np.zeros((H, W), bool)
    for cb, f, _ in cells:
        if f is None: continue
        x0, y0 = loc.xy(cb[1], cb[0]); x1, y1 = loc.xy(cb[3], cb[2])
        covered[int(max(0, y0)):int(min(H, y1 + 1)), int(max(0, x0)):int(min(W, x1 + 1))] = True
    ppm2 = cnt[covered].mean() if covered.any() else 0.0

    blds, roads = osm_features(data, bbox)
    bmask = np.zeros((H, W), bool)
    rows = []
    for bid, kind, ring in blds:
        x, y = loc.xy(ring[:, 0], ring[:, 1]); r = np.stack([x, y], 1)
        i0, i1 = int(max(0, math.floor(y.min()))), int(min(H, math.ceil(y.max()) + 1))
        j0, j1 = int(max(0, math.floor(x.min()))), int(min(W, math.ceil(x.max()) + 1))
        if i0 >= i1 or j0 >= j1: continue
        jj, ii = np.meshgrid(np.arange(j0, j1) + 0.5, np.arange(i0, i1) + 0.5)
        m = inside(jj, ii, r)
        if not m.any(): continue
        bmask[i0:i1, j0:j1] |= m
        cx, cy = float(x.mean()), float(y.mean())
        if not (0 <= cx < W - 1 and 0 <= cy < H - 1): continue   # the neighbour's building
        if not covered[int(min(H - 1, max(0, cy))), int(min(W - 1, max(0, cx)))]:
            rows.append((bid, kind, cx, cy, int(m.sum()), 0, 0, "no_scan")); continue
        n_in = int(cnt[i0:i1, j0:j1][m].sum()); n_up = int(up[i0:i1, j0:j1][m].sum())
        if m.sum() * ppm2 < MIN_EXPECTED: verdict = "unjudged"
        elif n_in == 0: verdict = "no_points"     # the chosen survey has a hole there
        elif n_in < MIN_EXPECTED // 2: verdict = "unjudged"
        else:
            share = n_up / n_in
            verdict = "scan_built" if share >= RAISED_BUILT else "scan_bare" if share <= RAISED_BARE else "mixed"
        rows.append((bid, kind, cx, cy, int(m.sum()), n_in, n_up, verdict))

    # roads: 5 m steps, the square of ROAD_REACH_M around each
    from scipy import ndimage
    k = 2 * ROAD_REACH_M + 1
    cover = ndimage.uniform_filter(bmask.astype(np.float32), k, mode="constant")
    near_cnt = ndimage.uniform_filter(cnt.astype(np.float32), k, mode="constant")
    near_up = ndimage.uniform_filter(up.astype(np.float32), k, mode="constant")
    road_m = {"total": 0.0, "in_neighbourhood": 0.0, "through_bare_scan": 0.0}
    bare_steps = []
    for rid, kind, line in roads:
        x, y = loc.xy(line[:, 0], line[:, 1])
        for a in range(len(x) - 1):
            seg = math.hypot(x[a + 1] - x[a], y[a + 1] - y[a])
            ns = max(1, int(seg / ROAD_STEP_M))
            for t in (np.arange(ns) + 0.5) / ns:
                sx, sy = x[a] + (x[a + 1] - x[a]) * t, y[a] + (y[a + 1] - y[a]) * t
                if not (0 <= sx < W and 0 <= sy < H): continue
                i, j = int(sy), int(sx)
                if not covered[i, j]: continue
                step = seg / ns
                road_m["total"] += step
                if cover[i, j] < NEIGHBOURHOOD_COVER: continue
                road_m["in_neighbourhood"] += step
                if near_cnt[i, j] > 0 and near_up[i, j] / near_cnt[i, j] <= BARE_RAISED:
                    road_m["through_bare_scan"] += step; bare_steps.append((sx, sy))

    # clusters of scan_bare buildings
    bare = [(cx, cy) for _, _, cx, cy, _, _, _, v in rows if v == "scan_bare"]
    judged = [r for r in rows if r[7] in ("scan_built", "scan_bare", "mixed")]
    grid = {}
    for cx, cy in bare: grid.setdefault((int(cy // CLUSTER_M), int(cx // CLUSTER_M)), []).append((cx, cy))
    allc = {}
    for r in judged: allc[(int(r[3] // CLUSTER_M), int(r[2] // CLUSTER_M))] = allc.get((int(r[3] // CLUSTER_M), int(r[2] // CLUSTER_M)), 0) + 1
    seeds = {c for c, v in grid.items() if len(v) >= CLUSTER_MIN}
    clusters, seen = [], set()
    for c0 in sorted(seeds):
        if c0 in seen: continue
        comp, todo = [], [c0]; seen.add(c0)
        while todo:
            c = todo.pop(); comp.append(c)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nb = (c[0] + di, c[1] + dj)
                    if nb in seeds and nb not in seen: seen.add(nb); todo.append(nb)
        pts = np.array([p for c in comp for p in grid[c]])
        tot = sum(allc.get(c, 0) for c in comp)
        lon, la = loc.ll(pts[:, 0].mean(), pts[:, 1].mean())
        lo0, la0 = loc.ll(pts[:, 0].min(), pts[:, 1].min()); lo1, la1 = loc.ll(pts[:, 0].max(), pts[:, 1].max())
        clusters.append({"lat": round(float(la), 5), "lon": round(float(lon), 5), "scan_bare": int(len(pts)),
                         "judged": int(tot), "share": round(len(pts) / max(1, tot), 2),
                         "likely_newer": bool(len(pts) >= LIKELY_MIN and len(pts) >= LIKELY_SHARE * tot),
                         "bbox": [round(float(v), 5) for v in (la0, lo0, la1, lo1)]})
    clusters.sort(key=lambda c: -c["scan_bare"])

    counts = {}
    for r in rows: counts[r[7]] = counts.get(r[7], 0) + 1
    nj = sum(counts.get(k, 0) for k in ("scan_built", "scan_bare", "mixed"))
    return {
        "bbox": list(bbox), "index": how, "surveys": surveys, "cells": cell_rows, "points_per_m2": round(float(ppm2), 2),
        "buildings": len(rows), "verdicts": counts,
        "bare_share": round(counts.get("scan_bare", 0) / nj, 4) if nj else None,
        "roads_m": {k: round(v) for k, v in road_m.items()},
        "bare_road_share": round(road_m["through_bare_scan"] / road_m["in_neighbourhood"], 4) if road_m["in_neighbourhood"] else None,
        "clusters": clusters,
        "bare_buildings": [{"id": bid, "kind": kind, "lat": round(float(loc.ll(cx, cy)[1]), 6), "lon": round(float(loc.ll(cx, cy)[0]), 6),
                            "cells": a, "returns": n, "raised": u}
                           for bid, kind, cx, cy, a, n, u, v in rows if v == "scan_bare"][:2000],
        "seconds": round(time.time() - t0, 1),
    }


def summary_line(cid, r):
    sv = "; ".join(f"{s['survey']}" + (f" flown {s['flown'][0]}..{s['flown'][2]}" if s.get("flown") else "")
                   for s in r["surveys"]) or "no survey"
    v = r["verdicts"]
    return (f"[{cid}] data age: {r['buildings']} buildings, {v.get('scan_bare', 0)} on bare scan"
            f" ({100 * (r['bare_share'] or 0):.1f}% of {sum(v.get(k, 0) for k in ('scan_built', 'scan_bare', 'mixed'))} judged),"
            f" {r['roads_m']['through_bare_scan']} of {r['roads_m']['in_neighbourhood']} m of neighbourhood road through bare scan,"
            f" {sum(c['likely_newer'] for c in r['clusters'])} likely newer neighbourhood(s)"
            + "".join(f" [{c['scan_bare']} at {c['lat']},{c['lon']}]" for c in r['clusters'] if c['likely_newer']) + ";"
            f" {r['points_per_m2']} pts/m2 read; {sv}")


# ----------------------------------------------------------------------------------------- NAIP
def naip_at(lat, lon):
    """Catalogue attributes of the NAIP Plus image under a point (best effort; field names as served)."""
    params = {"geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
              "geometryType": "esriGeometryPoint", "returnGeometry": "false", "returnCatalogItems": "true",
              "returnPixelValues": "false", "f": "json"}
    j = get_json(NAIP_IMAGE + "/identify?" + urllib.parse.urlencode(params), tries=2, timeout=60)
    feats = (j.get("catalogItems") or {}).get("features") or []
    out = []
    for f in feats:
        a = f.get("attributes") or {}
        keep = {k: v for k, v in a.items() if any(w in k.lower() for w in ("date", "year", "name", "res", "state", "acq"))}
        for k, v in list(keep.items()):   # ArcGIS dates are milliseconds since 1970
            if isinstance(v, (int, float)) and "date" in k.lower() and v > 1e11:
                keep[k] = datetime.datetime.utcfromtimestamp(v / 1000).strftime("%Y-%m-%d")
        out.append(keep)
    return out


# ------------------------------------------------------------------------------------------ CLI
def vintage(bbox, index):
    """Which survey Arnis reads over a chunk (share of its cells), when those points were flown
    (from one node of each survey near the middle of its cells), without OSM or a bake."""
    cells = choose(index[0], bbox)
    used = {}
    for cb, f, _ in cells:
        if f is not None: used.setdefault(f.name, (f, []))[1].append(cb)
    out = []
    for name, (f, cbs) in used.items():
        ept = ept_of(f)
        cb = cbs[len(cbs) // 2]
        lat, lon = (cb[0] + cb[2]) / 2, (cb[1] + cb[3]) / 2
        (x0, y0), (x1, y1) = merc(lon - 0.002, lat - 0.002), merc(lon + 0.002, lat + 0.002)
        keys = ept.nodes((x0, y0, x1, y1), 0.05) if ept else []
        flown = None
        for k in sorted(keys, key=lambda k: -int(k.split("-")[0]))[:2]:
            got = _try_read(ept, k)
            if got is not None and len(got[4]): flown = gps_dates(got[4]); break
        out.append({"survey": name, "year_in_name": f.year, "share": round(len(cbs) / len(cells), 2), "flown": flown})
    return {"bbox": list(bbox), "index": index[1], "surveys": out,
            "uncovered_share": round(sum(1 for c in cells if c[1] is None) / len(cells), 2)}


def area_chunks(area_path, only=None, keep_key=None, shard=None):
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import build
    area = json.loads(Path(area_path).read_text())
    chunks, _, _ = build.split(area["bbox"], area.get("max_chunk_km2", 4))
    ids = None
    if only: ids = set(only.split(","))
    elif keep_key: ids = set(area[keep_key])
    elif area.get("keep_all") or area.get("keep"): ids = set(area.get("keep_all") or area["keep"])
    chunks = [c for c in chunks if ids is None or c["id"] in ids]
    if shard:   # "i/n": every n-th chunk from the i-th, for a matrix of CI jobs
        i, n = (int(v) for v in shard.split("/"))
        chunks = chunks[i::n]
    return area, chunks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("chunk", help="one chunk, from its OSM file")
    a.add_argument("osm"); a.add_argument("bbox", help="S,W,N,E: the chunk's own bbox")
    a.add_argument("--out"); a.add_argument("--id", default="chunk")
    a.add_argument("--target", type=float, default=TARGET_PPM2, help="laser points a square metre to read")
    b = sub.add_parser("area", help="every chunk of an area (or --only), OSM from --osm-dir or Overpass")
    b.add_argument("area"); b.add_argument("--only"); b.add_argument("--keep-key", help='area key listing the chunk ids, e.g. "keep_south"')
    b.add_argument("--osm-dir", help="folder with <id>/osm.json (a build/<area> folder, say)")
    b.add_argument("--out", default="dataage", help="folder for <id>.json and summary.tsv")
    b.add_argument("--target", type=float, default=TARGET_PPM2)
    b.add_argument("--no-naip", action="store_true", help="do not ask the NAIP image service")
    b.add_argument("--shard", help='"i/n": only every n-th chunk from the i-th (0-based), for parallel jobs')
    v = sub.add_parser("vintage", help="which survey and photo lie under each chunk, and when they were flown (no OSM)")
    v.add_argument("area"); v.add_argument("--only"); v.add_argument("--keep-key")
    v.add_argument("--out", default="dataage"); v.add_argument("--no-naip", action="store_true")
    v.add_argument("--shard")
    args = ap.parse_args()

    if args.mode == "chunk":
        bbox = [float(v) for v in args.bbox.split(",")]
        try:
            r = check(json.loads(Path(args.osm).read_text()), bbox, args.target)
        except Exception as ex:
            print(f"[{args.id}] data age: not checked ({type(ex).__name__}: {ex})"); return
        if args.out: Path(args.out).write_text(json.dumps(r, indent=1))
        print(summary_line(args.id, r)); return

    area, chunks = area_chunks(args.area, args.only, args.keep_key, args.shard)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    try: index = load_index(area["bbox"])
    except Exception as ex: sys.exit(f"no survey index: {ex}")
    LOG(f"{len(index[0])} surveys meet the area ({index[1]}): " + ", ".join(sorted(f.name for f in index[0])))
    if args.mode == "vintage":
        lines = ["id\tlat\tlon\tsurveys (share of the chunk)\tflown (1st/50th/99th percentile)\tuncovered\tnaip"]
        for c in chunks:
            s, w, n, e = c["bbox"]
            try:
                r = vintage(c["bbox"], index)
                naip = None
                if not args.no_naip:
                    try: naip = naip_at((s + n) / 2, (w + e) / 2)
                    except Exception as ex: naip = f"unavailable: {type(ex).__name__}"
                lines.append("\t".join(map(str, [c["id"], round((s + n) / 2, 4), round((w + e) / 2, 4),
                    ", ".join(f"{x['survey']} ({x['share']})" for x in r["surveys"]),
                    ", ".join("/".join(x["flown"]) if x["flown"] else "no date" for x in r["surveys"]),
                    r["uncovered_share"], json.dumps(naip)[:300] if naip is not None else ""])))
            except Exception as ex:
                lines.append(f"{c['id']}\t{round((s + n) / 2, 4)}\t{round((w + e) / 2, 4)}\terror: {ex}")
            print(lines[-1], flush=True)
            (out / "vintage.tsv").write_text("\n".join(lines) + "\n")
        return
    lines = ["id\tlat\tlon\tsurveys\tflown\tbuildings\tjudged\tscan_bare\tbare_share\troad_nbhd_m\troad_bare_m\tlikely_newer\tlargest_cluster\tnaip"]
    for c in chunks:
        s, w, n, e = c["bbox"]
        try:
            p = Path(args.osm_dir) / c["id"] / "osm.json" if args.osm_dir else None
            data = json.loads(p.read_text()) if p and p.exists() else overpass(c["bbox"])
            r = check(data, c["bbox"], args.target, index)
            if not args.no_naip:
                try: r["naip"] = naip_at((s + n) / 2, (w + e) / 2)
                except Exception as ex: r["naip"] = f"unavailable: {ex}"
            (out / f"{c['id']}.json").write_text(json.dumps(r, indent=1))
            print(summary_line(c["id"], r), flush=True)
            v = r["verdicts"]; nj = sum(v.get(k, 0) for k in ("scan_built", "scan_bare", "mixed"))
            big = r["clusters"][0] if r["clusters"] else None
            naip = r.get("naip")
            lines.append("\t".join(map(str, [
                c["id"], round((s + n) / 2, 4), round((w + e) / 2, 4),
                ",".join(x["survey"] for x in r["surveys"]),
                ",".join(f"{x['flown'][0]}..{x['flown'][2]}" for x in r["surveys"] if x.get("flown")),
                r["buildings"], nj, v.get("scan_bare", 0), r["bare_share"], r["roads_m"]["in_neighbourhood"],
                r["roads_m"]["through_bare_scan"], sum(c["likely_newer"] for c in r["clusters"]),
                f"{big['scan_bare']}@{big['lat']},{big['lon']}" if big else "",
                json.dumps(naip)[:200] if naip is not None else ""])))
        except Exception as ex:
            print(f"[{c['id']}] data age: not checked ({type(ex).__name__}: {ex})", flush=True)
            lines.append(f"{c['id']}\t{round((s + n) / 2, 4)}\t{round((w + e) / 2, 4)}\terror: {ex}")
        (out / "summary.tsv").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
