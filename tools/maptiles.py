"""Overhead colour layer for the 2D block map, read from a finished site's packs.

    maptiles.py SITE_DIR [--scale 2] [--overview 16]

For every pack it finds the top visible face of each block column (greedy quads facing up, plus the
tops of partial blocks), takes that block's material colour, and writes one small PNG per pack:
data/m_<pack>.png at SCALE blocks per pixel. Each pixel is the mean colour of its blocks, with game-map
shading (lighter where the ground rises toward the south, darker where it falls) and a dark outline
along the top edge of anything that stands 3 or more blocks above its neighbour, so buildings read as
outlined shapes. Columns with no surface stay transparent. One overview PNG for the whole area at
OVERVIEW blocks per pixel is made from the same data.

meta.json gains  map: {scale, tiles: [[file, x, z], ...], overview: {file, scale, x, z}}  with x/z the
world block origin of each image (the frame of meta.packs x/z). Nothing else in the site changes.

It also draws an illustrated layer (map.illus), which the viewer prefers: every block column is sorted into
land, grass, trees, water, pavement, bare ground or building (3 or more blocks above the drivable layer, or above
the ground when that layer is missing), specks are tidied away, and each class gets one flat colour, with
outlined buildings and soft shadows, like a drawn city map:
    i_<pack>.png  1 block per pixel, for close up
    j_<pack>.png  4 blocks per pixel, the same colours blended, for the middle zooms
    i_overview.png  16 blocks per pixel, the whole area
  map.illus = {tiles: [[file, x, z]], scale: 1, mid: [[file, x, z]], mscale: 4, overview: {file, scale, x, z}}
--only photo|illus makes just one of the two layers (the other's meta entry is kept).
"""
import sys, json, gzip, argparse, time
from pathlib import Path
import numpy as np
from PIL import Image
from scipy import ndimage as ndi

ap = argparse.ArgumentParser()
ap.add_argument("site")
ap.add_argument("--scale", type=int, default=2)
ap.add_argument("--overview", type=int, default=16)
ap.add_argument("--only", choices=["both", "photo", "illus"], default="both")
args = ap.parse_args()
site = Path(args.site); data = site / "data"
meta = json.loads((data / "meta.json").read_text())
PK, T, S, OV = meta["pack"], meta["tile"], args.scale, args.overview
assert PK % S == 0 and PK % OV == 0 and OV % S == 0
mats = meta["materials"]
RGB = np.array([[int(m["rgb"][i:i + 2], 16) for i in (1, 3, 5)] for m in mats], np.float32)
KIND = [m["kind"] for m in mats]
LEAF = np.array([k == "leaves" for k in KIND])
QD = np.dtype([("x", "u1"), ("z", "u1"), ("y", "<u2"), ("w", "u1"), ("h", "u1"), ("d", "u1"), ("m", "u1")])
BD = np.dtype([("x", "u1"), ("z", "u1"), ("y", "<u2"), ("m", "u1"), ("x0", "u1"), ("y0", "u1"), ("z0", "u1"),
               ("x1", "u1"), ("y1", "u1"), ("z1", "u1"), ("pad", "u1")])
NONE = -1 << 40


def pack_top(p):
    """Top surface of one pack: key = (y * 16 + top within block) << 8 | material, per block column."""
    buf = (data / f"{p['id']}.bin").read_bytes()
    top = np.full(PK * PK, NONE, np.int64)
    for tx, tz, _n, off, ln in p["tiles"]:
        b = gzip.decompress(buf[off:off + ln])
        nq = int.from_bytes(b[:4], "little")
        q = np.frombuffer(b, QD, nq, 4); q = q[q["d"] == 0]
        bx = np.frombuffer(b, BD, (len(b) - 4 - nq * 8) // 12, 4 + nq * 8)
        ox, oz = tx * T + p["dx"] - p["x"], tz * T + p["dz"] - p["z"]
        if len(q):
            w, h = q["w"].astype(np.int64), q["h"].astype(np.int64); a = w * h
            i = np.repeat(np.arange(len(q)), a); k = np.arange(a.sum()) - np.repeat(np.cumsum(a) - a, a)
            xs = ox + q["x"][i].astype(np.int64) + k % w[i]; zs = oz + q["z"][i].astype(np.int64) + k // w[i]
            key = ((q["y"][i].astype(np.int64) * 16 + 16) << 8) | q["m"][i]
            ok = (xs >= 0) & (xs < PK) & (zs >= 0) & (zs < PK)
            np.maximum.at(top, (zs * PK + xs)[ok], key[ok])
        if len(bx):
            xs = ox + bx["x"].astype(np.int64); zs = oz + bx["z"].astype(np.int64)
            key = ((bx["y"].astype(np.int64) * 16 + bx["y1"]) << 8) | bx["m"]
            ok = (xs >= 0) & (xs < PK) & (zs >= 0) & (zs < PK)
            np.maximum.at(top, (zs * PK + xs)[ok], key[ok])
    return top.reshape(PK, PK)


def shade(top, s):
    """RGBA image at s blocks per pixel from a pack's top surface."""
    n = PK // s
    has = top != NONE
    m = np.where(has, top & 255, 0)
    y = np.where(has, (top >> 8) / 16.0, np.nan)
    col = RGB[m] * has[..., None]
    cnt = has.reshape(n, s, n, s).sum((1, 3))
    rgb = col.reshape(n, s, n, s, 3).sum((1, 3)) / np.maximum(cnt, 1)[..., None]
    with np.errstate(all="ignore"), __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore")
        H = np.nanmax(y.reshape(n, s, n, s), (1, 3))
    leaf = LEAF[m].reshape(n, s, n, s).mean((1, 3)) > 0.5
    Hf = np.where(np.isnan(H), -1e9, H)
    north = np.vstack([Hf[:1], Hf[:-1]])
    d = np.where(north < -1e8, 0, Hf - north)
    f = 1 + np.clip(d, -2, 2) * 0.06  # gentle slope shading, like a paper game map
    rise = np.zeros_like(Hf)  # how far this pixel stands above its lowest neighbour
    for nb in (north, np.vstack([Hf[1:], Hf[-1:]]), np.hstack([Hf[:, :1], Hf[:, :-1]]), np.hstack([Hf[:, 1:], Hf[:, -1:]])):
        rise = np.maximum(rise, np.where(nb < -1e8, 0, Hf - nb))
    edge = (rise >= 3) & ~leaf
    f = np.where(edge, 0.55, f)
    out = np.clip(rgb * f[..., None], 0, 255).astype(np.uint8)
    a = np.where(cnt > 0, 255, 0).astype(np.uint8)
    return np.dstack([out, a]), H


# ---------- the illustrated layer

NONE_C, LAND, GRASS, TREE, WATER, PAVE, BLDG, BARE = range(8)
#               none            land            grass           tree            water           pavement        building        bare
PAL = np.array([[0, 0, 0, 0], [241, 234, 215, 255], [205, 228, 172, 255], [160, 205, 128, 255], [150, 203, 238, 255],
                [232, 229, 221, 255], [216, 202, 180, 255], [236, 222, 186, 255]], np.uint8)
TREE_RIM, WATER_EDGE, BLDG_LINE, BLDG_SHADOW = (128, 182, 100), (98, 165, 220), (138, 120, 94), (220, 211, 192)

def classes(top, hgt, RGB, KIND):
    """per block column: one of the classes above"""
    has = top != NONE
    m = np.where(has, top & 255, 0); y = np.where(has, (top >> 8) / 16.0, np.nan)
    kind = np.array(KIND)[m]
    # ground: the drivable layer from the build (h pack [0], one below the top on open ground); where it is missing,
    # or a whole pack of it is off (some packs come out all 0), the top surface with anything narrower than 61 blocks
    # taken off (a grey opening), so buildings, even big ones, stand out of it
    yf = np.where(has, y, np.nanmedian(y) if has.any() else 0)
    go = ndi.grey_opening(yf, size=61) - 1
    g = hgt[0].astype(np.float32) if hgt is not None else np.full(top.shape, -1, np.float32)
    ok = has & (g >= 0)
    if not ok.any() or abs(np.median((y - g)[ok]) - 1) > 1: g = go
    else: g = np.where(g < 0, go, g)
    up = y - g
    c = np.full(top.shape, NONE_C, np.uint8)
    rgb = RGB[m]; r, gg, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = rgb.max(-1), rgb.min(-1); lum = rgb.mean(-1)
    ground = has & (up < 3)
    c[ground] = LAND
    c[ground & (gg > r + 8) & (gg > b + 4)] = GRASS
    c[ground & (mx - mn < 28)] = PAVE
    c[ground & (r > b + 30) & (gg > b + 15) & (r >= gg) & (lum > 120)] = BARE
    c[has & (kind == 'leaves')] = TREE
    c[has & (kind == 'water')] = WATER
    c[has & (up >= 3) & ((kind == 'solid') | (kind == 'glass'))] = BLDG
    return c

def tidy(c):
    """drop specks: tiny buildings and tree bits, then a majority vote on the ground classes"""
    b = c == BLDG
    b = ndi.binary_fill_holes(b)
    lab, n = ndi.label(b); size = ndi.sum(b, lab, range(1, n + 1)); small = np.isin(lab, np.nonzero(size < 12)[0] + 1)
    b &= ~small
    t = c == TREE; t = ndi.binary_opening(t, iterations=1)
    w = c == WATER; w = ndi.binary_opening(w, iterations=1)
    # ground specks: a ground pixel with fewer than 3 of its 3 x 3 window in its own class takes the window's most common
    gk = np.array([LAND, GRASS, PAVE, BARE], np.uint8)
    votes = np.stack([ndi.uniform_filter((c == k).astype(np.float32), 3) for k in gk])
    own = np.zeros(c.shape, np.float32)
    for i, k in enumerate(gk): own = np.where(c == k, votes[i], own)
    out = c.copy(); g = np.isin(c, gk)
    fix = g & (own < 3 / 9 - 1e-3); out[fix] = gk[votes.argmax(0)][fix]
    out[(c == TREE) & ~t] = gk[votes.argmax(0)][(c == TREE) & ~t]
    out[(c == WATER) & ~w] = PAVE
    out[(c == BLDG) & ~b] = gk[votes.argmax(0)][(c == BLDG) & ~b]
    out[c == NONE_C] = NONE_C
    out[t] = TREE; out[w] = WATER; out[b] = BLDG
    return out

def render(c, s):
    """RGBA at s blocks per pixel: the most important class in each cell, flat colours, outlines and soft shadows"""
    n = c.shape[0] // s
    cells = c.reshape(n, s, n, s)
    # building wins if half the cell is building; else water, tree, then the most common ground class
    frac = lambda k: (cells == k).mean((1, 3))
    k = np.full((n, n), NONE_C, np.uint8)
    ground = np.stack([frac(x) for x in (LAND, GRASS, PAVE, BARE)])
    has = (cells != NONE_C).mean((1, 3)) > 0.3
    k[has] = np.array([LAND, GRASS, PAVE, BARE], np.uint8)[ground.argmax(0)][has]
    k[frac(TREE) >= 0.5] = TREE; k[frac(WATER) >= 0.5] = WATER; k[frac(BLDG) >= 0.5] = BLDG
    img = PAL[k].copy()
    def edge(mask):   # pixels of mask with a 4-neighbour outside it
        e = np.zeros_like(mask)
        e[1:] |= mask[1:] & ~mask[:-1]; e[:-1] |= mask[:-1] & ~mask[1:]; e[:, 1:] |= mask[:, 1:] & ~mask[:, :-1]; e[:, :-1] |= mask[:, :-1] & ~mask[:, 1:]
        return e
    b = k == BLDG
    sh = np.zeros_like(b); sh[1:, 1:] = b[:-1, :-1]; sh[1:, :] |= b[:-1, :]; sh &= ~b & (k != NONE_C) & (k != WATER)
    img[sh, :3] = BLDG_SHADOW
    img[edge(b), :3] = BLDG_LINE
    t = k == TREE; te = np.zeros_like(t); te[:-1] |= t[:-1] & ~t[1:]; te[:, :-1] |= t[:, :-1] & ~t[:, 1:]   # rim on the south and east
    img[te, :3] = TREE_RIM
    img[edge(k == WATER), :3] = WATER_EDGE
    return img, k


def soft(c, s):
    """RGBA at s blocks per pixel: the class colours blended, so a far view reads as areas, not specks"""
    n = c.shape[0] // s
    col = PAL[c][..., :3].astype(np.float32); on = (c != NONE_C).astype(np.float32)
    rgb = (col * on[..., None]).reshape(n, s, n, s, 3).sum((1, 3)); cov = on.reshape(n, s, n, s).sum((1, 3))
    rgb /= np.maximum(cov, 1)[..., None]
    wat = (c == WATER).reshape(n, s, n, s).mean((1, 3)) >= 0.4
    rgb[wat] = PAL[WATER][:3]
    return np.dstack([rgb.astype(np.uint8), np.where(cov > s * s * 0.3, 255, 0).astype(np.uint8)])


def ground_layer(p):
    f = data / f"{p['h']}.bin" if p.get("h") else None
    if not f or not f.exists(): return None
    return np.frombuffer(gzip.decompress(f.read_bytes()), "<i2").reshape(2, PK, PK)


t0 = time.time()
x0 = min(p["x"] for p in meta["packs"]); z0 = min(p["z"] for p in meta["packs"])
x1 = max(p["x"] for p in meta["packs"]) + PK; z1 = max(p["z"] for p in meta["packs"]) + PK
x0 -= x0 % OV; z0 -= z0 % OV
ov = np.zeros(((z1 - z0) // OV + 1, (x1 - x0) // OV + 1, 4), np.uint8)
tiles, itiles, jtiles = [], [], []
iov = np.zeros_like(ov)
r = OV // S
PHOTO, ILLUS = args.only in ("both", "photo"), args.only in ("both", "illus")
for i, p in enumerate(meta["packs"]):
    top = pack_top(p)
    if ILLUS:
        c = tidy(classes(top, ground_layer(p), RGB, KIND))
        if (c != NONE_C).any():
            img1, _ = render(c, 1)
            Image.fromarray(img1, "RGBA").quantize(16, method=Image.Quantize.FASTOCTREE).save(data / f"i_{p['id']}.png", optimize=True)
            itiles.append([f"i_{p['id']}.png", p["x"], p["z"]])
            Image.fromarray(soft(c, 4), "RGBA").quantize(64, method=Image.Quantize.FASTOCTREE).save(data / f"j_{p['id']}.png", optimize=True)
            jtiles.append([f"j_{p['id']}.png", p["x"], p["z"]])
            o = soft(c, OV); n = o.shape[0]; oz, ox = (p["z"] - z0) // OV, (p["x"] - x0) // OV
            dst = iov[oz:oz + n, ox:ox + n]; o = o[:dst.shape[0], :dst.shape[1]]; sel = o[..., 3] > 0
            dst[sel] = o[sel]
    if not PHOTO:
        if i % 50 == 0: print(f"{i}/{len(meta['packs'])} packs, {time.time() - t0:.0f} s", flush=True)
        continue
    img, _ = shade(top, S)
    f = f"m_{p['id']}.png"
    if img[..., 3].any():
        # 64-colour palette PNG: about a third of the size, and the flatter colours suit a map
        Image.fromarray(img, "RGBA").quantize(64, method=Image.Quantize.FASTOCTREE).save(data / f, optimize=True)
        tiles.append([f, p["x"], p["z"]])
        # overview: mean of the opaque pixels in each r x r block; later packs win where chunks overlap
        n = img.shape[0] // r
        a = img[..., 3:4].astype(np.float32) / 255
        c = (img[..., :3] * a).reshape(n, r, n, r, 3).sum((1, 3)); w = a.reshape(n, r, n, r, 1).sum((1, 3))
        o = np.dstack([c / np.maximum(w, 1e-6), (w > r * r * 0.3) * 255]).astype(np.uint8)
        oz, ox = (p["z"] - z0) // OV, (p["x"] - x0) // OV
        dst = ov[oz:oz + n, ox:ox + n]; o = o[:dst.shape[0], :dst.shape[1]]; sel = o[..., 3] > 0
        dst[sel] = o[sel]
    if i % 50 == 0: print(f"{i}/{len(meta['packs'])} packs, {time.time() - t0:.0f} s", flush=True)
mp = meta.get("map") or {}
if PHOTO:
    Image.fromarray(ov, "RGBA").quantize(128, method=Image.Quantize.FASTOCTREE).save(data / "m_overview.png", optimize=True)
    mp.update({"scale": S, "tiles": tiles, "overview": {"file": "m_overview.png", "scale": OV, "x": x0, "z": z0}})
    size = sum((data / t[0]).stat().st_size for t in tiles) + (data / "m_overview.png").stat().st_size
    print(f"map: {len(tiles)} tiles at {S} blocks/px, overview {ov.shape[1]}x{ov.shape[0]} at {OV}, {size / 1e6:.1f} MB")
if ILLUS:
    Image.fromarray(iov, "RGBA").quantize(128, method=Image.Quantize.FASTOCTREE).save(data / "i_overview.png", optimize=True)
    mp["illus"] = {"tiles": itiles, "scale": 1, "mid": jtiles, "mscale": 4, "overview": {"file": "i_overview.png", "scale": OV, "x": x0, "z": z0}}
    size = sum((data / t[0]).stat().st_size for t in itiles + jtiles) + (data / "i_overview.png").stat().st_size
    print(f"illustrated map: {len(itiles)} tiles, {size / 1e6:.1f} MB")
meta["map"] = mp
(data / "meta.json").write_text(json.dumps(meta))
print(f"{time.time() - t0:.0f} s")
