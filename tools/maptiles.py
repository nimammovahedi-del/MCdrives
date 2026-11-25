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
"""
import sys, json, gzip, argparse, time
from pathlib import Path
import numpy as np
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("site")
ap.add_argument("--scale", type=int, default=2)
ap.add_argument("--overview", type=int, default=16)
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


t0 = time.time()
x0 = min(p["x"] for p in meta["packs"]); z0 = min(p["z"] for p in meta["packs"])
x1 = max(p["x"] for p in meta["packs"]) + PK; z1 = max(p["z"] for p in meta["packs"]) + PK
x0 -= x0 % OV; z0 -= z0 % OV
ov = np.zeros(((z1 - z0) // OV + 1, (x1 - x0) // OV + 1, 4), np.uint8)
tiles = []
r = OV // S
for i, p in enumerate(meta["packs"]):
    img, _ = shade(pack_top(p), S)
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
Image.fromarray(ov, "RGBA").quantize(128, method=Image.Quantize.FASTOCTREE).save(data / "m_overview.png", optimize=True)
meta["map"] = {"scale": S, "tiles": tiles, "overview": {"file": "m_overview.png", "scale": OV, "x": x0, "z": z0}}
(data / "meta.json").write_text(json.dumps(meta))
size = sum((data / t[0]).stat().st_size for t in tiles) + (data / "m_overview.png").stat().st_size
print(f"map: {len(tiles)} tiles at {S} blocks/px, overview {ov.shape[1]}x{ov.shape[0]} at {OV}, {size / 1e6:.1f} MB, {time.time() - t0:.0f} s")
