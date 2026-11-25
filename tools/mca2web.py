"""Convert an Arnis-generated Minecraft Java world into compact web tiles for the three.js viewer.

Reads every region file, maps Minecraft block names to our own original palette, keeps only
blocks with at least one face open to air, and writes one binary file per 64x64-block tile:
Tiles are grouped into one "pack" file per 512x512-block region so the viewer can stream them.

Memory stays flat however large the world is: regions are decoded one at a time into a temp
cache, then each region is meshed with a margin borrowed from its eight neighbours.

    mca2web.py WORLD_DIR OUT_DIR [--b64]     (--b64 wraps packs as base64 JSON for hosts that refuse .bin)

Beside each pack p_<i>_<j> goes its height tile h_<i>_<j> (named in meta.json as the pack's "h"):
gzipped int16 [2, PACK, PACK] over the pack's columns, [0] the lowest drivable surface and [1] the
highest (bridge decks), in blocks above miny, -1 where there is none.
"""
import sys, os, re, zlib, gzip, json, struct, glob, io, math, time
import numpy as np
import nbtlib

WORLD, OUT = sys.argv[1], sys.argv[2]
B64 = '--b64' in sys.argv[3:]
PACK, MARGIN = 512, 64
TILE = 64
os.makedirs(OUT, exist_ok=True)

# ---- Colours. Each distinct (rgb, kind) becomes one material id. kind: solid | water | glass | leaves
MATS = [("air", 0, "air")]
MAT_ID = {}
def mat(rgb, kind="solid"):
    k = (rgb, kind)
    if k not in MAT_ID:
        MATS.append((f"m{len(MATS)}", rgb, kind)); MAT_ID[k] = len(MATS) - 1
        if len(MATS) > 255: raise SystemExit("too many materials")
    return MAT_ID[k]
DYES = ["light_blue", "light_gray", "white", "orange", "magenta", "yellow", "lime", "pink", "gray", "cyan", "purple", "blue", "brown", "green", "red", "black"]
CONCRETE = dict(white=0xCFD5D6, orange=0xE06101, magenta=0xA9309F, light_blue=0x2389C6, yellow=0xF0AF15, lime=0x5EA918, pink=0xD5658E,
                gray=0x3A3D41, light_gray=0x7D7D73, cyan=0x157788, purple=0x641F9C, blue=0x2C2E8F, brown=0x603B1F, green=0x495B24, red=0x8E2020, black=0x15171C)
TERRACOTTA = dict(white=0xD1B1A1, orange=0xA05325, magenta=0x95576C, light_blue=0x706C8A, yellow=0xBA8524, lime=0x677534, pink=0xA04D4E,
                  gray=0x3F3531, light_gray=0x876A61, cyan=0x565B5B, purple=0x764656, blue=0x4A3B5B, brown=0x4D3323, green=0x4C522A, red=0x8E3C2E, black=0x2A1D18)
WOOL = dict(white=0xE9ECEC, orange=0xF07613, magenta=0xBD44B3, light_blue=0x3AAFD9, yellow=0xF8C527, lime=0x70B919, pink=0xED8DAC,
            gray=0x474F52, light_gray=0x8E8E86, cyan=0x158991, purple=0x792AAC, blue=0x35399D, brown=0x724728, green=0x546D1B, red=0xA02722, black=0x1D1D21)
def lighten(c, t):
    return sum(int(((c >> s) & 255) * (1 - t) + 255 * t) << s for s in (16, 8, 0))
NAMED = {  # checked in order: first substring match wins
    "grass_block": 0x6FA34A, "moss": 0x5A7A2E, "podzol": 0x6A4E2A, "mycelium": 0x6F6369, "coarse_dirt": 0x77553B, "rooted_dirt": 0x8A6240,
    "dirt_path": 0x94793F, "farmland": 0x6B4A2F, "packed_mud": 0x8E6A4F, "mud_brick": 0x896751, "mud": 0x3C393C, "dirt": 0x86603F,
    "smooth_stone": 0x9E9E9E, "stone_brick": 0x7A7A7A, "cobblestone": 0x7F7F7F, "polished_andesite": 0x848685, "andesite": 0x888888,
    "polished_diorite": 0xC1C1C3, "diorite": 0xBCBCBC, "polished_granite": 0x9A6B59, "granite": 0x956756,
    "red_sandstone": 0xB5621F, "sandstone": 0xD8CB9B, "red_sand": 0xBE6621, "sand": 0xDBCFA3, "gravel": 0x837F7E, "clay": 0xA0A6B3,
    "quartz": 0xEBE5DE, "calcite": 0xDFE0DC, "smooth_basalt": 0x48484E, "basalt": 0x505153, "polished_blackstone": 0x353038, "blackstone": 0x2A2328,
    "deepslate": 0x505050, "tuff": 0x6C6D66, "dripstone": 0x866B5C, "bedrock": 0x555555, "obsidian": 0x0F0A18,
    "red_nether_brick": 0x450709, "nether_brick": 0x2C161A, "bricks": 0x966153, "brick": 0x966153, "prismarine": 0x4E8C7C,
    "iron": 0xD8D8D8, "gold_block": 0xF6D03D, "copper": 0xC06C4F, "coal_block": 0x101010, "netherite": 0x423D3F,
    "snow": 0xF9FEFE, "packed_ice": 0x8DB4FA, "blue_ice": 0x74A7FD, "hay": 0xA68B0C, "bone_block": 0xD1CEB3, "sponge": 0xC3C04A,
    "mushroom_stem": 0xCBC4B9, "red_mushroom_block": 0xC82E2D, "brown_mushroom_block": 0x957051, "bamboo": 0xC0AC4F,
    "end_rod": 0xE9E6D8, "lightning_rod": 0xC06C4F, "daylight_detector": 0x82745E, "furnace": 0x6E6E6E, "smoker": 0x55504A,
    "cauldron": 0x49494A, "anvil": 0x444444, "grindstone": 0x8E8E8E, "barrel": 0x7B5A32, "bookshelf": 0xA2834F, "lantern": 0xE8B04A,
    "glowstone": 0xAB8354, "sea_lantern": 0xACC8BE, "shroomlight": 0xF09246, "redstone_lamp": 0x5F3A1E, "target": 0xE5B0A8, "tnt": 0xB1352B,
    "pumpkin": 0xC67617, "melon": 0x6F911E, "cactus": 0x587D2B, "honeycomb": 0xE5941D, "crying": 0x200A3C, "purpur": 0xA97DA9,
    "stone": 0x7D7D7D,
}
WOODS = dict(dark_oak=(0x432B14, 0x3C2E1A), pale_oak=(0xE3D9D8, 0x57504E), oak=(0xA2834F, 0x6D5533), spruce=(0x735531, 0x3B2611), birch=(0xC0AF79, 0xD8D7D2),
             jungle=(0xA07351, 0x554419), acacia=(0xA85A32, 0x676157), mangrove=(0x763631, 0x544329), cherry=(0xE2B2AC, 0x361E27),
             crimson=(0x653147, 0x5D1A1E), warped=(0x2B6963, 0x3A3A4D))
LEAVES = dict(oak=0x4A7A2A, spruce=0x3B5E3B, birch=0x688B45, jungle=0x3F8A1F, acacia=0x5E8A2D, dark_oak=0x3A6A1E, mangrove=0x4F7F28, cherry=0xE8A9C4, azalea=0x5C7A2E, pale_oak=0x7C8275)
SKIP = re.compile(r"(^air$|cave_air|void_air|short_grass|tall_grass|^fern|large_fern|flower|tulip|poppy|dandelion|orchid|allium|bluet|daisy|"
                  r"rose_bush|lilac|peony|sunflower|torch|button|lever|pressure_plate|rail|sign|banner|redstone_wire|tripwire|"
                  r"sapling|dead_bush|vine|lily_pad|seagrass|kelp|candle|chain$|flower_pot|head$|skull|ladder|lichen|roots$|"
                  r"cobweb|bush$|^light$|structure_void|barrier|item_frame|painting|wheat|carrots|potatoes|beetroots|sugar_cane|"
                  r"sweet_berry|leaf_litter|pointed_dripstone|bell|bed$|fire|petals|wildflowers|sprouts|fungus|propagule|dripleaf|spore_blossom)")
def colour(n):
    if n in ("water", "bubble_column"): return 0x3C7FD6, "water"
    if "leaves" in n:
        return next((c for w, c in LEAVES.items() if n.startswith(w)), 0x4A7A2A), "leaves"
    dye = next((d for d in DYES if n.startswith(d + "_")), None)
    if "glass" in n: return (lighten(CONCRETE[dye], 0.25) if dye else 0xBFE3F0), "glass"
    if n in ("ice", "frosted_ice"): return 0x91B7FD, "glass"
    if dye:
        if "terracotta" in n: return TERRACOTTA[dye], "solid"
        if "concrete_powder" in n: return lighten(CONCRETE[dye], 0.12), "solid"
        if "concrete" in n: return CONCRETE[dye], "solid"
        return WOOL[dye], "solid"   # wool, carpet, shulker boxes, beds, candles
    if n == "terracotta": return 0x985E43, "solid"
    for w, (plank, bark) in WOODS.items():
        if n.startswith(w + "_") or n.startswith("stripped_" + w):
            return (bark if (n.endswith("_log") or n.endswith("_wood") or n.endswith("_stem") or n.endswith("_hyphae")) and not n.startswith("stripped") else plank), "solid"
    for k, c in NAMED.items():
        if k in n: return c, "solid"
    unknown.add(n)
    return 0x8E8E8E, "solid"
unknown = set()

# Every colour the rules above can produce is registered up front, in a fixed order, so the
# material table is the same for every world. Chunks of one area can then share pack files and a
# single palette: a quad's material byte means the same thing whichever bake it came from.
def register_palette():
    mat(0x3C7FD6, "water")
    for c in LEAVES.values(): mat(c, "leaves")
    mat(0x4A7A2A, "leaves")
    for c in CONCRETE.values(): mat(lighten(c, 0.25), "glass")
    mat(0xBFE3F0, "glass"); mat(0x91B7FD, "glass")
    for table in (TERRACOTTA, CONCRETE, WOOL):
        for c in table.values(): mat(c, "solid")
    for c in CONCRETE.values(): mat(lighten(c, 0.12), "solid")
    mat(0x985E43, "solid")
    for plank, bark in WOODS.values(): mat(plank, "solid"); mat(bark, "solid")
    for c in NAMED.values(): mat(c, "solid")
    mat(0x8E8E8E, "solid")
register_palette()

FULL = None
def boxes(n, pr):
    """Returns None for a full cube, [] to skip, or a list of boxes in 1/16 block units for partial shapes."""
    g = lambda k, d="": str(pr.get(k, d)) if pr else d
    if n.endswith("_slab"):
        t = g("type", "bottom")
        return FULL if t == "double" else [(0, 8, 0, 16, 16, 16)] if t == "top" else [(0, 0, 0, 16, 8, 16)]
    if n.endswith("_stairs"):
        top = g("half") == "top"; f = g("facing", "north")
        base = (0, 8, 0, 16, 16, 16) if top else (0, 0, 0, 16, 8, 16)
        y0, y1 = (0, 8) if top else (8, 16)
        step = {"north": (0, y0, 0, 16, y1, 8), "south": (0, y0, 8, 16, y1, 16), "east": (8, y0, 0, 16, y1, 16), "west": (0, y0, 0, 8, y1, 16)}[f]
        return [base, step]
    if n.endswith("_carpet"): return [(0, 0, 0, 16, 1, 16)]
    if n == "snow": return [(0, 0, 0, 16, 2 * int(g("layers", "1")), 16)]
    side = {"north": (0, 0, 13, 16, 16, 16), "south": (0, 0, 0, 16, 16, 3), "east": (0, 0, 0, 3, 16, 16), "west": (13, 0, 0, 16, 16, 16)}
    if n.endswith("_trapdoor"):
        if g("open") == "true": return [side[g("facing", "north")]]
        return [(0, 13, 0, 16, 16, 16)] if g("half") == "top" else [(0, 0, 0, 16, 3, 16)]
    if n.endswith("_door"): return [side[g("facing", "north")]]
    if n.endswith("_fence_gate"):
        return [(0, 5, 7, 16, 16, 9)] if g("facing", "north") in ("north", "south") else [(7, 5, 0, 9, 16, 16)]
    kind = "wall" if n.endswith("_wall") else "fence" if n.endswith("_fence") else "pane" if (n.endswith("_pane") or n == "iron_bars") else None
    if kind:
        lo, hi, top, arm0 = {"wall": (5, 11, 16, 0), "fence": (6, 10, 16, 6), "pane": (7, 9, 16, 0)}[kind]
        on = lambda d: g(d, "none") not in ("none", "false")
        arms = [d for d in ("north", "south", "east", "west") if on(d)]
        out = [(lo, 0, lo, hi, top, hi)] if kind != "pane" or not arms else []
        at = 14 if kind == "wall" else 15 if kind == "fence" else 16
        a, b2 = (lo, hi) if kind == "wall" else (7, 9)
        for d in arms:
            out.append({"north": (a, arm0, 0, b2, at, 8), "south": (a, arm0, 8, b2, at, 16), "east": (8, arm0, a, 16, at, b2), "west": (0, arm0, a, 8, at, b2)}[d])
        if kind == "pane" and not arms: out = [(7, 0, 0, 9, 16, 16), (0, 0, 7, 16, 16, 9)]
        return out
    if n in ("end_rod", "lightning_rod"): return [(7, 0, 7, 9, 16, 9)]
    if n == "daylight_detector": return [(0, 0, 0, 16, 6, 16)]
    if n.endswith("lantern") and n != "sea_lantern": return [(5, 0, 5, 11, 9, 11)]
    if n == "dirt_path" or n == "farmland": return [(0, 0, 0, 16, 15, 16)]
    return FULL

cache = {}
SHAPES = [None]          # shape id -> list of boxes; id 0 = full cube
SHAPE_ID = {}
# Blocks Arnis lays as road and parking surface, used to find where a canopy hangs over a road.
ROAD_NAMES = re.compile(r"^(gray_concrete_powder|cyan_terracotta|black_concrete|gray_concrete|polished_andesite|"
                        r"white_concrete|yellow_concrete|light_gray_concrete)$")
ROAD_MATS = set()

def classify(p):
    """Returns (material id, shape id). Material 0 means skip."""
    name = str(p["Name"]); pr = p.get("Properties")
    key = (name, tuple(sorted((str(k), str(v)) for k, v in pr.items())) if pr else None)
    if key in cache: return cache[key]
    n = name.split(":")[-1]
    if SKIP.search(n): r = (0, 0)
    else:
        bx = boxes(n, pr)
        if bx == []: r = (0, 0)
        else:
            sid = 0
            if bx is not None:
                t = tuple(bx)
                if t not in SHAPE_ID: SHAPES.append(t); SHAPE_ID[t] = len(SHAPES) - 1
                sid = SHAPE_ID[t]
            r = (mat(*colour(n)), sid)
            if ROAD_NAMES.match(n): ROAD_MATS.add(r[0])
    cache[key] = r
    return r

def read_region(path):
    rx, rz = map(int, os.path.basename(path).split(".")[1:3])
    data = open(path, "rb").read()
    for i in range(1024):
        off = int.from_bytes(data[i*4:i*4+3], "big"); cnt = data[i*4+3]
        if off == 0 or cnt == 0: continue
        p = off * 4096
        ln = int.from_bytes(data[p:p+4], "big"); comp = data[p+4]
        raw = data[p+5:p+4+ln]
        raw = zlib.decompress(raw) if comp == 2 else gzip.decompress(raw) if comp == 1 else raw
        yield nbtlib.File.parse(io.BytesIO(raw))

def unpack(longs, bits, n=4096):
    arr = np.array(longs, dtype=np.int64).view(np.uint64)
    per = 64 // bits
    shifts = (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    vals = (arr[:, None] >> shifts[None, :]) & np.uint64((1 << bits) - 1)
    return vals.reshape(-1)[:n].astype(np.int32)

t0 = time.time()
import tempfile, shutil, base64
from functools import lru_cache
from scipy import ndimage
TMP = tempfile.mkdtemp(prefix="mca2web_")
regions = sorted(glob.glob(os.path.join(WORLD, "region", "r.*.mca")))
REG = {}  # (rx, rz) -> y of the lowest stored layer
gminy, gmaxy = 10**9, -10**9
nchunks = 0
for rp in regions:
    rx, rz = map(int, os.path.basename(rp).split(".")[1:3])
    secs = []; present = np.zeros((32, 32), dtype=bool)
    for ch in read_region(rp):
        root = ch.get("") if "" in ch else ch
        cx, cz = int(root["xPos"]) - rx * 32, int(root["zPos"]) - rz * 32
        present[cz, cx] = True; nchunks += 1
        for s in root.get("sections", []):
            bs = s.get("block_states")
            if bs is None: continue
            pal = bs["palette"]
            cl = [classify(q) for q in pal]
            ids = np.array([c[0] for c in cl], dtype=np.uint8); shp = np.array([c[1] for c in cl], dtype=np.uint16)
            if not ids.any(): continue
            if "data" in bs:
                bits = max(4, math.ceil(math.log2(len(pal))))
                idx = unpack(bs["data"], bits)
                vox = ids[idx]; sh = shp[idx]
            else:
                vox = np.full(4096, ids[0], dtype=np.uint8); sh = np.full(4096, shp[0], dtype=np.uint16)
            if not vox.any(): continue
            secs.append((cx, cz, int(s["Y"]), vox.reshape(16, 16, 16), sh.reshape(16, 16, 16)))  # [y, z, x]
    if not secs: continue
    y0 = min(s[2] for s in secs) * 16; y1 = max(s[2] for s in secs) * 16 + 16
    g = np.zeros((y1 - y0, PACK, PACK), dtype=np.uint8); dm = np.zeros_like(g); ds = np.zeros(g.shape, dtype=np.uint16)
    for cx, cz, sy, v, sh in secs:
        sl = (slice(sy * 16 - y0, sy * 16 - y0 + 16), slice(cz * 16, cz * 16 + 16), slice(cx * 16, cx * 16 + 16))
        g[sl] = np.where(sh == 0, v, 0); dm[sl] = np.where(sh != 0, v, 0); ds[sl] = sh
    np.savez_compressed(os.path.join(TMP, f"{rx}_{rz}.npz"), g=g, dm=dm, ds=ds, present=present)
    REG[(rx, rz)] = y0; gminy = min(gminy, y0); gmaxy = max(gmaxy, y1 - 1)
    del g, dm, ds, secs
H = gmaxy - gminy + 1
rx0 = min(k[0] for k in REG); rz0 = min(k[1] for k in REG)
NX = max(k[0] for k in REG) - rx0 + 1; NZ = max(k[1] for k in REG) - rz0 + 1
minx, minz, W, D = rx0 * PACK, rz0 * PACK, NX * PACK, NZ * PACK
print(f"read {nchunks} chunks from {len(REG)} regions in {time.time()-t0:.1f}s; x {minx}..{minx+W} z {minz}..{minz+D} y {gminy}..{gmaxy}")

kinds = np.array([0 if k == "air" else 1 if k == "solid" else 2 if k == "water" else 3 if k == "glass" else 4 for _, _, k in MATS], dtype=np.uint8)
# A face is drawn when the neighbour is air, or a see-through block of a different material.
see = np.zeros(256, dtype=bool); see[0] = True
for i, (_, _, k) in enumerate(MATS):
    if k in ("water", "glass", "leaves"): see[i] = True

@lru_cache(maxsize=12)
def load(rx, rz):
    """One region padded to the global height: (cubes, partial material, partial shape, in-world columns)."""
    if (rx, rz) not in REG: return None
    z = np.load(os.path.join(TMP, f"{rx}_{rz}.npz"))
    o = REG[(rx, rz)] - gminy; n = z["g"].shape[0]
    g = np.zeros((H, PACK, PACK), dtype=np.uint8); dm = np.zeros_like(g); ds = np.zeros(g.shape, dtype=np.uint16)
    g[o:o+n] = z["g"]; dm[o:o+n] = z["dm"]; ds[o:o+n] = z["ds"]
    return g, dm, ds, np.repeat(np.repeat(z["present"], 16, axis=0), 16, axis=1)

from numba import njit
@njit(cache=True)
def greedy(plane, out, n):
    # plane: 2D [v, u] material ids (0 = no face). Appends (u, v, w, h, mat) rectangles to out.
    V, U = plane.shape
    done = np.zeros((V, U), dtype=np.bool_)
    for v in range(V):
        for u in range(U):
            m = plane[v, u]
            if m == 0 or done[v, u]: continue
            w = 1
            while u + w < U and plane[v, u + w] == m and not done[v, u + w] and w < 255: w += 1
            h = 1
            ok = True
            while v + h < V and h < 255 and ok:
                for k in range(w):
                    if plane[v + h, u + k] != m or done[v + h, u + k]:
                        ok = False; break
                if ok: h += 1
            for dv in range(h):
                for du in range(w):
                    done[v + dv, u + du] = True
            out[n, 0] = u; out[n, 1] = v; out[n, 2] = w; out[n, 3] = h; out[n, 4] = m
            n += 1
    return n


QT = np.dtype([("x", "u1"), ("z", "u1"), ("y", "<u2"), ("w", "u1"), ("h", "u1"), ("d", "u1"), ("m", "u1")])
BT = np.dtype([("x", "u1"), ("z", "u1"), ("y", "<u2"), ("m", "u1"), ("x0", "u1"), ("y0", "u1"), ("z0", "u1"), ("x1", "u1"), ("y1", "u1"), ("z1", "u1"), ("pad", "u1")])
M, S = MARGIN, PACK + 2 * MARGIN
dirs = [((1, 0, 0), 0), ((-1, 0, 0), 1), ((0, 1, 0), 2), ((0, -1, 0), 3), ((0, 0, 1), 4), ((0, 0, -1), 5)]  # +y -y +z -z +x -x as (dy,dz,dx)
packs = []; total = 0; nquads = 0; nboxes = 0; faces = 0; nleafcut = 0
for (rx, rz) in sorted(REG):
    # Window = this region plus a margin from its neighbours, so faces and sky reach are right at the seams.
    wg = np.zeros((H, S, S), dtype=np.uint8); inworld = np.zeros((S, S), dtype=bool)
    for dz in (-1, 0, 1):
        for dx in (-1, 0, 1):
            r = load(rx + dx, rz + dz)
            if r is None: continue
            # destination range in the window, source range in the neighbour, per axis
            def rng(d): return (slice(0, M), slice(PACK - M, PACK)) if d < 0 else (slice(M, M + PACK), slice(0, PACK)) if d == 0 else (slice(M + PACK, S), slice(0, M))
            (wz, sz), (wx, sx) = rng(dz), rng(dx)
            wg[:, wz, wx] = r[0][:, sz, sx]; inworld[wz, wx] = r[3][sz, sx]
    _, dmat, dshape, _ = load(rx, rz)
    dmat = dmat.copy()
    # Trees over a road hide the way ahead in the driving view, so a canopy is cleared where it
    # overhangs road or parking surface. Leaves 12 blocks above the surface or less go; higher
    # branches and the trunk stay, which keeps the street looking planted.
    isroad = np.zeros(256, dtype=bool)
    for i in ROAD_MATS: isroad[i] = True
    road = isroad[wg[:, M:M+PACK, M:M+PACK]].any(axis=0) | isroad[dmat].any(axis=0)
    road = ndimage.binary_dilation(road, np.ones((7, 7), bool))  # reach over the kerb and verge
    roadtop = np.where(isroad[wg[:, M:M+PACK, M:M+PACK]], np.arange(H)[:, None, None], -1).max(axis=0)
    isleaf = np.zeros(256, dtype=bool)
    for i, (_, _, k) in enumerate(MATS):
        if k == "leaves": isleaf[i] = True
    over = road[None] & (np.arange(H)[:, None, None] > roadtop[None]) & (np.arange(H)[:, None, None] <= roadtop[None] + 12)
    cut = over & isleaf[wg[:, M:M+PACK, M:M+PACK]]
    ncut = int(cut.sum())
    wg[:, M:M+PACK, M:M+PACK] = np.where(cut, 0, wg[:, M:M+PACK, M:M+PACK])
    dmat[cut & isleaf[dmat]] = 0
    nleafcut += ncut
    # Only draw faces that touch air connected to the open sky: hides sealed interiors, the hollow
    # under the terrain shell and the world's cut edges (columns outside the world are not passable).
    # Columns with nothing above the floor layers (outside the baked outline) would let the sky leak under the terrain.
    inworld &= (wg[2:] != 0).any(axis=0)
    # Slabs, paths and other partial blocks that cover their whole cell seal the ground like a full block,
    # so sky does not leak through them into the hollow under the terrain. (Centre region only: the margin
    # has no shape data, which can only make the centre slightly more open, never wrongly closed.)
    footprint = np.array([False] + [any(b[0] == 0 and b[2] == 0 and b[3] == 16 and b[5] == 16 and b[4] >= 8 for b in sh) for sh in SHAPES[1:]])
    cover = footprint[dshape] & (dmat != 0)
    passable = np.zeros((H + 1, S, S), dtype=bool); passable[:H] = ((wg == 0) | see[wg]) & inworld[None]; passable[H] = True
    passable[:H, M:M+PACK, M:M+PACK] &= ~cover
    lab, _ = ndimage.label(passable)
    reach = np.zeros((H + 2, S + 2, S + 2), dtype=bool)
    reach[1:H+1, 1:S+1, 1:S+1] = lab[:H] == lab[H, 0, 0]
    reach[H + 1] = True
    del lab, passable
    pad = np.pad(wg, 1, constant_values=0)
    c = pad[1:-1, 1:-1, 1:-1]
    wmask = np.zeros(wg.shape, dtype=np.uint8)
    for (dy, dz, dx), bit in dirs:
        sl = (slice(1+dy, H+1+dy), slice(1+dz, S+1+dz), slice(1+dx, S+1+dx))
        nb = pad[sl]
        wmask |= ((see[nb] & (nb != c) & reach[sl]).astype(np.uint8) << bit)
    wmask[c == 0] = 0
    del pad
    grid = wg[:, M:M+PACK, M:M+PACK]; mask = wmask[:, M:M+PACK, M:M+PACK]
    dmat[~reach[1:H+1, 1+M:1+M+PACK, 1+M:1+M+PACK]] = 0
    faces += sum(int(np.count_nonzero(mask & (1 << b))) for b in range(6))
    ox, oz = (rx - rx0) * PACK, (rz - rz0) * PACK
    # Surfaces a car can stand on: a solid block (not leaves or water) with open sky-connected space and
    # three blocks of headroom above it. Tree canopies and roofs over the road no longer lift the car.
    hard = (kinds[grid] == 1) | (kinds[grid] == 3) | (cover & (kinds[dmat] == 1))  # slabs and paths count as surface too
    up = np.zeros((H + 3,) + hard.shape[1:], dtype=bool); up[:H] = hard
    surf = hard & reach[2:H+2, 1+M:1+M+PACK, 1+M:1+M+PACK] & ~up[1:H+1] & ~up[2:H+2] & ~up[3:H+3]
    anys = surf.any(axis=0)
    hts = np.stack([np.where(anys, surf.argmax(axis=0), -1), np.where(anys, H - 1 - surf[::-1].argmax(axis=0), -1)]).astype("<i2")
    blob_all = bytearray(); tiles = []
    for b in range(0, PACK, TILE):
        for a in range(0, PACK, TILE):
            g = grid[:, b:b+TILE, a:a+TILE]; mk = mask[:, b:b+TILE, a:a+TILE]
            dm = dmat[:, b:b+TILE, a:a+TILE]
            if not mk.any() and not dm.any(): continue
            quads = []
            for bit in range(6):
                f = np.where(mk & (1 << bit), g, 0).astype(np.uint8)
                if not f.any(): continue
                buf = np.zeros((f.size // 2 + 16, 5), dtype=np.int32)
                if bit < 2:      # +-y: slices over y, plane [z, x]
                    for y in np.flatnonzero(f.any(axis=(1, 2))):
                        n = greedy(np.ascontiguousarray(f[y]), buf, 0)
                        for u, v, w, h, m in buf[:n]: quads.append((u, v, y, w, h, bit, m))
                elif bit < 4:    # +-z: slices over z, plane [y, x]
                    for z in np.flatnonzero(f.any(axis=(0, 2))):
                        n = greedy(np.ascontiguousarray(f[:, z, :]), buf, 0)
                        for u, v, w, h, m in buf[:n]: quads.append((u, z, v, w, h, bit, m))
                else:            # +-x: slices over x, plane [y, z]
                    for x in np.flatnonzero(f.any(axis=(0, 1))):
                        n = greedy(np.ascontiguousarray(f[:, :, x]), buf, 0)
                        for u, v, w, h, m in buf[:n]: quads.append((x, u, v, w, h, bit, m))
            rec = np.array(quads, dtype=QT)
            dy, dz, dx = np.nonzero(dm)
            bl = []
            for y, z, x in zip(dy.tolist(), dz.tolist(), dx.tolist()):
                m = int(dm[y, z, x])
                for bx in SHAPES[int(dshape[y, b + z, a + x])]: bl.append((x, z, y, m) + bx + (0,))
            brec = np.array(bl, dtype=BT) if bl else np.zeros(0, dtype=BT)
            nboxes += len(brec); nquads += len(rec)
            blob = gzip.compress(struct.pack("<I", len(rec)) + rec.tobytes() + brec.tobytes(), 6)
            tiles.append([(ox + a) // TILE, (oz + b) // TILE, len(rec), len(blob_all), len(blob)])
            blob_all += blob
    if not tiles: continue
    pid, hid = f"p_{rx - rx0}_{rz - rz0}", f"h_{rx - rx0}_{rz - rz0}"
    for name, blob in ((pid, bytes(blob_all)), (hid, gzip.compress(hts.tobytes(), 6))):
        if B64: json.dump({"base64": base64.b64encode(blob).decode()}, open(os.path.join(OUT, name + ".json"), "w"))
        else: open(os.path.join(OUT, name + ".bin"), "wb").write(blob)
    packs.append({"id": pid, "h": hid, "x": ox, "z": oz, "quads": sum(t[2] for t in tiles), "tiles": tiles}); total += len(blob_all)
    print(f"  {pid}: {packs[-1]['quads']:,} quads, {len(blob_all)/1e6:.1f} MB", flush=True)
shutil.rmtree(TMP, ignore_errors=True)
print(f"canopy over roads removed: {nleafcut:,} leaf blocks")
print(f"greedy meshing: {nquads:,} quads (from {faces:,} block faces); {nboxes:,} detail boxes for partial blocks")

meta = {"tile": TILE, "pack": PACK, "ext": "json" if B64 else "bin", "width": int(W), "depth": int(D), "height": int(H),
        "minx": int(minx), "minz": int(minz), "miny": int(gminy), "quads": int(nquads), "bytes": int(total), "packs": packs,
        "materials": [{"name": n, "rgb": f"#{c:06x}", "kind": k} for n, c, k in MATS]}
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"))
print(f"wrote {len(packs)} packs, {total/1e6:.1f} MB compressed, in {time.time()-t0:.1f}s total")
print("block names with no colour rule (drawn mid-grey):", ", ".join(sorted(unknown))[:1500])
