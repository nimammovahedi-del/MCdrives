"""The joined site's data/meta.json, written small for the first load (format 2) and read back whole.

Format 1 (older builds, and every per-chunk meta.json) lists each pack as one object with its tile table
inline: [[tx, tz, quads, offset, length], ...] per tile, about 4.8 MB of a 5.4 MB meta.json for 123 chunks.
Format 2 ("v": 2) keeps every top-level key and value except two, which it writes in a form the readers expand
again (web/drive/meta.js for the viewer, read() here for the Python tools):

  packs: {"n": [packs per chunk, in meta.chunks order],
          "ij": [i, j, ...]  the pack's place in its chunk: x = chunk.dx + i * pack, z = chunk.dz + j * pack,
          "pre": "0110..."   per pack 1 when its id carries its chunk's id ("<chunk>_p_i_j") rather than "p_i_j",
          "tiles": "tiles/"} where the tile tables are: one file data/tiles/<chunk id>.json per chunk, fetched
                             when the first pack of that chunk loads.
     Each pack is otherwise rebuilt: id, h (h_<i>_<j>, as build.py join() names them), x, z, dx, dz, dy (its
     chunk's), quads (the sum of its tiles' quads), tiles.
  map.tiles, map.illus.tiles, map.illus.mid: a string P instead of the list, when the list is exactly
     [[P + pack id + ".png", pack x, pack z] for every pack], as tools/maptiles.py writes them.

  data/tiles/<chunk id>.json: {"v": 2, "m": [tile masks], "p": [[mask index, quads..., lengths...] per pack]}
     A mask is 16 hex digits, bit k (k = tz_in_pack * 8 + tx_in_pack, for pack / tile = 8) set for each tile the
     pack has; its tiles follow in k order, at byte offsets that add up the lengths from 0.

write() checks that read() gives back exactly the format 1 meta it was given and writes format 1 when it does not
(a pack that breaks one of the rules above), so a site is never written in a form that loses anything.
"""
import json, re
from pathlib import Path

V = 2
SEP = (",", ":")
PACK_KEYS = {"id", "h", "x", "z", "quads", "dx", "dz", "dy", "tiles"}
ID = re.compile(r"(?:(.+)_)?p_(\d+)_(\d+)")
SAFE = re.compile(r"[A-Za-z0-9_-]+")
TILE_DIR = "tiles/"


def hid(pid):
    """The height tile of pack pid, named as build.py join() names it."""
    return "h" + pid[1:] if pid.startswith("p_") else "h_" + pid


def _map_lists(meta):
    m = meta.get("map")
    if not isinstance(m, dict): return []
    out = [(m, "tiles")]
    if isinstance(m.get("illus"), dict): out += [(m["illus"], "tiles"), (m["illus"], "mid")]
    return [(d, k) for d, k in out if isinstance(d.get(k), list)]


def compact(meta):
    """(format 2 meta, {file name under data/: object}) for a format 1 meta, or None when it does not fit format 2."""
    if meta.get("v") is not None or not isinstance(meta.get("packs"), list): return None
    chunks, packs, PK, T = meta.get("chunks"), meta["packs"], meta.get("pack"), meta.get("tile")
    if not chunks or not packs or not PK or not T or PK % T: return None
    per = PK // T
    if per * per > 64: return None
    if any(not SAFE.fullmatch(str(c.get("id", ""))) for c in chunks) or len({c["id"] for c in chunks}) != len(chunks): return None
    n = [0] * len(chunks); ij = []; pre = []; side = {}; ci = 0
    for p in packs:
        if set(p) != PACK_KEYS: return None
        while ci < len(chunks) and (chunks[ci].get("dx"), chunks[ci].get("dz"), chunks[ci].get("dy")) != (p["dx"], p["dz"], p["dy"]): ci += 1
        if ci == len(chunks): return None   # the packs are not grouped by chunk, in chunk order
        c = chunks[ci]
        mt = ID.fullmatch(p["id"])
        if not mt or mt[1] not in (None, c["id"]): return None
        i, j = int(mt[2]), int(mt[3])
        if (p["x"], p["z"], p["h"]) != (c["dx"] + i * PK, c["dz"] + j * PK, hid(p["id"])): return None
        mask = 0; off = 0; qs = []; ls = []; last = -1
        for t in p["tiles"]:
            if len(t) != 5: return None
            tx, tz, q, o, ln = t
            lx, lz = tx - i * per, tz - j * per
            if not (0 <= lx < per and 0 <= lz < per) or o != off: return None
            k = lz * per + lx
            if k <= last: return None
            last = k; mask |= 1 << k; off += ln; qs.append(q); ls.append(ln)
        if sum(qs) != p["quads"]: return None
        f = side.setdefault(c["id"], {"v": V, "m": [], "p": []})
        hx = f"{mask:016x}"
        if hx not in f["m"]: f["m"].append(hx)
        f["p"].append([f["m"].index(hx)] + qs + ls)
        n[ci] += 1; ij += [i, j]; pre.append("1" if mt[1] else "0")
    out = {**meta, "v": V, "packs": {"n": n, "ij": ij, "pre": "".join(pre), "tiles": TILE_DIR}}
    # the map's per-pack picture lists, when they follow the packs exactly
    mp = json.loads(json.dumps(meta["map"])) if "map" in meta else None
    if mp is not None:
        out["map"] = mp
        for d, k in _map_lists(out):
            L = d[k]
            if len(L) != len(packs) or not L or not isinstance(L[0], list) or not isinstance(L[0][0], str): continue
            f0 = L[0][0]; at = f0.find(packs[0]["id"])
            if at < 0: continue
            P = f0[:at]
            if all(e == [P + p["id"] + ".png", p["x"], p["z"]] for e, p in zip(L, packs)): d[k] = P
    return out, {TILE_DIR + cid + ".json": f for cid, f in side.items()}


def packs_of(meta):
    """Format 2 packs without their tile tables (tiles None, quads None), in order, with each pack's chunk id."""
    P, cs, out = meta["packs"], meta["chunks"], []
    k = 0
    for c, cnt in zip(cs, P["n"]):
        for _ in range(cnt):
            i, j = P["ij"][2 * k], P["ij"][2 * k + 1]
            pid = (c["id"] + "_" if P["pre"][k] == "1" else "") + f"p_{i}_{j}"
            out.append(({"id": pid, "h": hid(pid), "x": c["dx"] + i * meta["pack"], "z": c["dz"] + j * meta["pack"],
                         "quads": None, "dx": c["dx"], "dz": c["dz"], "dy": c["dy"], "tiles": None}, c["id"], i, j))
            k += 1
    return out


def fill_tiles(meta, entries, side):
    """Put tiles and quads into the packs of one chunk (entries: [(pack, i, j)] in order) from its tiles file."""
    per = meta["pack"] // meta["tile"]
    for (p, i, j), e in zip(entries, side["p"]):
        mask = int(side["m"][e[0]], 16)
        ks = [k for k in range(per * per) if mask >> k & 1]
        qs, ls = e[1:1 + len(ks)], e[1 + len(ks):1 + 2 * len(ks)]
        off, tiles = 0, []
        for k, q, ln in zip(ks, qs, ls):
            tiles.append([i * per + k % per, j * per + k // per, q, off, ln]); off += ln
        p["tiles"] = tiles; p["quads"] = sum(qs)


def expand(meta, read_side):
    """Format 1 meta from any format; read_side(name) gives the object in data/<name> (format 2 only)."""
    if meta.get("v") != V: return meta
    out = {k: v for k, v in meta.items() if k != "v"}
    rows = packs_of(meta)
    by = {}
    for p, cid, i, j in rows: by.setdefault(cid, []).append((p, i, j))
    for cid, entries in by.items(): fill_tiles(meta, entries, read_side(meta["packs"]["tiles"] + cid + ".json"))
    out["packs"] = packs = [p for p, *_ in rows]
    if "map" in meta:
        out["map"] = mp = json.loads(json.dumps(meta["map"]))
        for d, k in [(mp, "tiles")] + ([(mp["illus"], "tiles"), (mp["illus"], "mid")] if isinstance(mp.get("illus"), dict) else []):
            if isinstance(d.get(k), str): d[k] = [[d[k] + p["id"] + ".png", p["x"], p["z"]] for p in packs]
    return out


def read(data):
    """The whole meta of a site's data folder, in format 1."""
    data = Path(data)
    return expand(json.loads((data / "meta.json").read_text()), lambda name: json.loads((data / name).read_text()))


def version(data):
    """The format data/meta.json is in now (1 or 2)."""
    return json.loads((Path(data) / "meta.json").read_text()).get("v") or 1


def write(data, meta, log=print, v=V):
    """Write meta (format 1) to data/meta.json: in format 2 when v is 2 and it fits and reads back the same.
    (v=1 for a tool that rewrites an older site, whose viewer reads format 1 only.)"""
    data = Path(data)
    c = compact(meta) if v == V else None
    if c is not None:
        small, side = c
        if expand(small, lambda name: json.loads(json.dumps(side[name]))) != meta: c = None
    if c is None:
        if v == V: log("meta: written in format 1 (the packs do not fit format 2)")
        (data / "meta.json").write_text(json.dumps(meta, separators=SEP))
        return
    for name, obj in side.items():
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_text(json.dumps(obj, separators=SEP))
    (data / "meta.json").write_text(json.dumps(small, separators=SEP))


if __name__ == "__main__":
    # metafmt.py IN.json OUT_DIR: write IN (format 1) as OUT_DIR/meta.json plus its tile files, and check read()
    import sys
    src, dst = json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    write(dst, src)
    print("read back equal:", read(dst) == src)
