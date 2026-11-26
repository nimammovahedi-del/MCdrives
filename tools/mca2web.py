"""Convert an Arnis-generated Minecraft Java world into compact web tiles for the three.js viewer.

Reads every region file, maps Minecraft block names to our own original palette, keeps only
blocks with at least one face open to air, and writes one binary file per 64x64-block tile:
Tiles are grouped into one "pack" file per 512x512-block region so the viewer can stream them.

Memory stays flat however large the world is: regions are decoded one at a time into a temp
cache, then each region is meshed with a margin borrowed from its eight neighbours.

    mca2web.py WORLD_DIR OUT_DIR [--b64] [--osm OSM.json [--no-flat-roofs]]
        --b64 wraps packs as base64 JSON for hosts that refuse .bin; --osm (the OSM data of the bake) re-grades the roads
        and lays the flat roofs flush with their walls (flat_roofs()), which --no-flat-roofs leaves as baked

Beside each pack p_<i>_<j> goes its height tile h_<i>_<j> (named in meta.json as the pack's "h"):
gzipped int16 [2, PACK, PACK] over the pack's columns, [0] the lowest drivable surface and [1] the
highest (bridge decks), in blocks above miny, -1 where there is none.
"""
import sys, os, re, zlib, gzip, json, struct, glob, io, math, time
import numpy as np
import nbtlib

WORLD, OUT = sys.argv[1], sys.argv[2]
B64 = '--b64' in sys.argv[3:]
# --osm OSM.json: the OSM data the world was baked from; road surfaces are then re-graded along it (road_grade.py),
# and its building footprints drive flat_roofs()
OSM = sys.argv[sys.argv.index('--osm') + 1] if '--osm' in sys.argv else None
GRADE_ONLY = '--grade-only' in sys.argv   # debugging: stop after the re-grade, writing its stats and top maps
# --bounds S,W,N,E: the chunk's own bbox without build.py's lap, where it meets its neighbours (road_grade.py boundaries())
BOUNDS = tuple(float(v) for v in sys.argv[sys.argv.index('--bounds') + 1].split(',')) if '--bounds' in sys.argv else None
NO_GRADE = '--no-regrade' in sys.argv   # keep road surfaces as baked, while the OSM data still serves the roofs
NO_ROOFS = '--no-flat-roofs' in sys.argv   # leave Arnis's flat roofs as baked (see flat_roofs())
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
    ks = [k for k in NAMED if k in n]   # the longest name wins: "polished_blackstone_bricks" is not "stone_bricks"
    if ks: return NAMED[max(ks, key=len)], "solid"
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
    mat(PATH_MUTED, "solid")   # ground-level cycle paths, see road_fixes(); last, so earlier ids keep their meaning
    for n in SHADE_BASES:      # per-building sibling shades, see building_shades(); appended after everything above
        c = colour(n)[0]
        for k in (1, -1):
            if (shade(c, k), "solid") in MAT_ID: raise SystemExit(f"shade of {n} collides with an existing material")
            mat(shade(c, k), "solid")
PATH_MUTED = 0x7E6E66          # a warm grey between the asphalt and the sidewalk, in place of Arnis's red
# Wall and roof blocks that Arnis builds most houses from (measured over the Irvine bakes: these cover about 90% of
# the exposed wall and roof faces of buildings). Each gets a lighter and a darker sibling for building_shades().
SHADE_BASES = ["white_concrete", "bricks", "cobbled_deepslate", "stone_bricks", "smooth_stone", "polished_andesite", "sandstone",
               "light_gray_concrete", "gray_concrete", "white_terracotta", "polished_granite", "mud_bricks", "dark_oak_planks",
               "granite", "mushroom_stem", "spruce_planks"]
def shade(c, k, step=0.08, least=0.035, hue=0.008):
    """c made 8% lighter (k=1) or darker (k=-1) in HLS lightness, by at least 0.035 so the dark roof blocks still show
    the step, with a slight warm or cool turn of the hue."""
    import colorsys
    h, l, s = colorsys.rgb_to_hls(*(((c >> sh) & 255) / 255 for sh in (16, 8, 0)))
    r, g, b = colorsys.hls_to_rgb((h + k * hue) % 1, min(1, l + k * max(l * step, least)), s)
    return (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)
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

def regrade():
    """Re-grades road surfaces along the OSM roads (see road_grade.py), in place in the region caches, so every window
    that reads a region afterwards sees the same blocks."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import road_grade
    tg = time.time()
    lut = lambda names: np.isin(np.arange(256), [mat(*colour(n)) for n in names])
    k256 = np.zeros(256, dtype=np.uint8); k256[:len(kinds)] = kinds
    solid_lut = (k256 == 1) | (k256 == 3)
    block_lut = solid_lut.copy()                      # what fills a block for headroom: not leaves, not water
    carr_lut = lut(["gray_concrete_powder", "cyan_terracotta", "white_concrete", "yellow_concrete"])
    red_lut = lut(["red_terracotta", "red_concrete"])   # cycle paths: lifted on earth, so road_fixes() still mutes them
    gr = road_grade.Grader(W, D, H, log=print)
    def padded(rx, rz):
        z = np.load(os.path.join(TMP, f"{rx}_{rz}.npz"))
        o = REG[(rx, rz)] - gminy; n = z["g"].shape[0]
        out = []
        for key in ("g", "dm", "ds"):
            a = np.zeros((H,) + z[key].shape[1:], dtype=z[key].dtype); a[o:o + n] = z[key]; out.append(a)
        return out, z["present"], o, n
    for (rx, rz) in sorted(REG):
        (g, dm, _), _, _, _ = padded(rx, rz)
        gr.scan_region((rx - rx0) * PACK, (rz - rz0) * PACK, g, dm, solid_lut, carr_lut, block_lut)
    print(f"road grade: scanned in {time.time() - tg:.1f}s", flush=True)
    if GRADE_ONLY:
        np.save(os.path.join(OUT, "grade_top_before.npy"), gr.lev[0])
        np.savez(os.path.join(OUT, "grade_scan.npz"), lev=gr.lev, carr=gr.carr, ground=gr.ground, occ=gr.occ, WDH=np.array([W, D, H]))
    st = gr.run(OSM, WORLD, (minx, minz), BOUNDS)
    fill_id, pave_id = mat(*colour("stone_bricks")), mat(*colour("gray_concrete_powder"))
    nr = nc = 0
    for (rx, rz) in sorted(REG):
        (g, dm, ds), present, o, n = padded(rx, rz)
        a, b = gr.apply_region((rx - rx0) * PACK, (rz - rz0) * PACK, g, dm, ds, fill_id, pave_id, carr_lut, block_lut, red_lut)
        nr += a; nc += b
        if a or b:
            # the stored y range may have to grow by what was raised
            ys = np.flatnonzero((g != 0).any(axis=(1, 2)) | (dm != 0).any(axis=(1, 2)))
            lo = min(o, int(ys[0]) // 16 * 16) if len(ys) else o
            hi = max(o + n, -(-(int(ys[-1]) + 1) // 16) * 16) if len(ys) else o + n
            np.savez_compressed(os.path.join(TMP, f"{rx}_{rz}.npz"), g=g[lo:hi], dm=dm[lo:hi], ds=ds[lo:hi], present=present)
            REG[(rx, rz)] = gminy + lo
            if GRADE_ONLY: gr.scan_region((rx - rx0) * PACK, (rz - rz0) * PACK, g, dm, solid_lut, carr_lut, block_lut)
    st.update(blocks_raised=nr, blocks_carved=nc, seconds=round(time.time() - tg, 1))
    print(f"road grade: applied at {time.time() - tg:.1f}s", flush=True)
    print("road grade: " + ", ".join(f"{k} {v:,}" for k, v in st.items()), flush=True)
    if GRADE_ONLY:
        np.save(os.path.join(OUT, "grade_top_after.npy"), gr.lev[0])
        np.savez(os.path.join(OUT, "grade_scan_after.npz"), lev=gr.lev, carr=gr.carr, ground=gr.ground, occ=gr.occ, WDH=np.array([W, D, H]))
        json.dump(st, open(os.path.join(OUT, "grade_stats.json"), "w"))
        json.dump({"mats": [[n, c, k] for n, c, k in MATS], "reg": {f"{k[0]}_{k[1]}": v for k, v in REG.items()}, "gminy": gminy},
                  open(os.path.join(OUT, "grade_regions.json"), "w"))
        shutil.rmtree(os.path.join(OUT, "grade_npz"), ignore_errors=True); shutil.copytree(TMP, os.path.join(OUT, "grade_npz"))
        shutil.rmtree(TMP, ignore_errors=True)
        sys.exit(0)

if OSM and not NO_GRADE: regrade()
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

# ---- Road surface clean-up, applied to each region window's cube grid before the sky-reach labelling.
# Every rule looks only at the window itself, so a column gets the same answer from each window it is in.
RF = {}
def _road_fix_tables():
    if RF: return RF
    def lut(names):
        t = np.zeros(256, dtype=bool)
        for n in names: t[mat(*colour(n))] = True
        return t
    road = np.zeros(256, dtype=bool)
    for i in ROAD_MATS: road[i] = True
    RF.update(
        road=road,
        asphalt=lut(["gray_concrete_powder", "cyan_terracotta"]),
        marking=lut(["white_concrete", "yellow_concrete"]), yellow=lut(["yellow_concrete"]),
        # Arnis "built-up" land-cover ground fill, which shows through where the stamps of meeting roads leave a gap
        fill=lut(["stone_bricks", "cracked_stone_bricks", "stone", "cobblestone"]),
        # bare ground that can sit in a road as a stray speck
        soft=lut(["grass_block", "dirt", "coarse_dirt", "rooted_dirt", "podzol", "moss_block", "sand", "gravel", "mud"]),
        terrain=lut(["grass_block", "dirt", "coarse_dirt", "rooted_dirt", "podzol", "mud", "sand", "red_sand", "gravel", "stone",
                     "andesite", "diorite", "granite", "deepslate", "tuff", "clay", "moss_block", "dirt_path"]),
        red=lut(["red_terracotta", "red_concrete"]),
        # Arnis lays sand ground as sand over two blocks of sandstone; a cycle path on a beach or a sandy river bank
        # replaces the sand, so its red lies on that sandstone (a roof on a sandstone wall has more wall, glass or
        # bricks below; the colour also covers smooth, cut and chiseled sandstone)
        sandstone=lut(["sandstone"]),
        log=lut([w + "_log" for w in WOODS]),
        leaf=np.array([k == "leaves" for _, _, k in MATS] + [False] * (256 - len(MATS))),
        path=MAT_ID[(PATH_MUTED, "solid")])
    return RF

def road_fixes(wg, centre, st):
    """Cleans the road surface in place in the window grid wg [y, z, x] (cubes only):
    1. gap fill: a small patch of built-up fill (or a speck of bare ground) at road height that road surrounds is paved
       with the asphalt around it;
    2. cycle paths: red terracotta or concrete lying on terrain becomes one muted path colour (roofs sit on air or walls);
    3. clutter: leaves and logs standing on the road surface, up to 12 blocks up, are cleared.
    centre is the (z, x) slice pair of the region inside the window; only cells there are counted in st."""
    T = _road_fix_tables()
    Hh, Sz, Sx = wg.shape
    solid = (kinds[wg] == 1) & ~T["log"][wg]
    has = solid.any(axis=0)
    ty = np.where(has, Hh - 1 - solid[::-1].argmax(axis=0), -1)
    zz, xx = np.indices(ty.shape)
    tm = np.where(has, wg[np.maximum(ty, 0), zz, xx], 0)
    road = T["road"][tm] & has
    inc = np.zeros(ty.shape, dtype=bool); inc[centre] = True
    # 1. gaps: (patch kind, max extent, max area, min share of the rim that is road at the patch's level)
    filled = np.zeros(ty.shape, dtype=bool)
    cross = ndimage.generate_binary_structure(2, 1)
    for key, maxext, maxarea, minfrac in (("fill", 24, 400, 0.75), ("soft", 3, 4, 1.0)):
        cand = T[key][tm] & has
        lab, _ = ndimage.label(cand, cross)
        for i, sl in enumerate(ndimage.find_objects(lab), 1):
            if sl is None: continue
            (z0, z1), (x0, x1) = (sl[0].start, sl[0].stop), (sl[1].start, sl[1].stop)
            if max(z1 - z0, x1 - x0) > maxext or z0 == 0 or x0 == 0 or z1 == Sz or x1 == Sx: continue
            s0, s1 = slice(z0 - 1, z1 + 1), slice(x0 - 1, x1 + 1)
            m = lab[s0, s1] == i
            if m.sum() > maxarea: continue
            rim = ndimage.binary_dilation(m, cross) & ~m
            r_ty, r_road, r_tm = ty[s0, s1][rim], road[s0, s1][rim], tm[s0, s1][rim]
            if not r_road.any(): continue
            ry = int(np.median(r_ty[r_road]))
            if (r_road & (np.abs(r_ty - ry) <= 1)).mean() < minfrac: continue
            cells = m & (ty[s0, s1] == ry)
            src = r_tm[r_road & T["asphalt"][r_tm]]
            if not len(src): src = r_tm[r_road & ~T["marking"][r_tm]]
            if not len(src) or not cells.any(): continue
            fillm = np.bincount(src, minlength=256).argmax()
            cz, cx = np.nonzero(cells)
            wg[ry, cz + z0 - 1, cx + x0 - 1] = fillm
            filled[s0, s1] |= cells
            st["fill_" + key] += int((cells & inc[s0, s1]).sum())
            st["skip_" + key] += int(((m & ~cells) & inc[s0, s1]).sum())
    # 1b. pits: a single column of carriageway sitting up to 3 blocks below its four road neighbours, which are level
    # with each other, is brought up to them
    c = (slice(1, -1), slice(1, -1))
    nb = [(slice(0, -2), c[1]), (slice(2, None), c[1]), (c[0], slice(0, -2)), (c[0], slice(2, None))]
    lvl = ty[nb[0]]
    pit = road[nb[0]] & (ty[c] < lvl) & (ty[c] >= lvl - 3) & (lvl < Hh)
    for q in nb[1:]: pit &= road[q] & (ty[q] == lvl)
    pz, px = np.nonzero(pit)
    if len(pz):
        py = lvl[pz, px]; src = tm[nb[0]][pz, px]
        src = np.where(T["marking"][src], np.bincount(tm[road & T["asphalt"][tm]], minlength=256).argmax() if (road & T["asphalt"][tm]).any() else src, src)
        wg[py, pz + 1, px + 1] = src
        filled[pz + 1, px + 1] = True
        st["pits"] += int(inc[pz + 1, px + 1].sum())
    # (on a slope Arnis stacks up to a few red blocks under a step; the whole stack goes, so no red riser shows)
    run = np.zeros(ty.shape, dtype=np.int32); alive = T["red"][tm] & has
    for d in range(6):
        alive &= (ty - d >= 0) & T["red"][wg[np.maximum(ty - d, 0), zz, xx]]
        run += alive
    under = lambda k: wg[np.maximum(ty - run - k, 0), zz, xx]
    # the sand-ground profile: at most two red blocks on exactly two blocks of sandstone, then terrain or at least four
    # blocks of air (below the terrain shell); a roof on sandstone has a wall, glass or a floor a few blocks down
    hollow = (under(2) == 0) & (under(3) == 0) & (under(4) == 0) & (under(5) == 0)
    sand = ((run <= 2) & T["sandstone"][under(0)] & T["sandstone"][under(1)] & (T["terrain"][under(2)] | hollow)
            & (ty - run - 5 >= 0))
    red = (run > 0) & (T["terrain"][wg[np.maximum(ty - run, 0), zz, xx]] | sand) & (ty - run >= 0)
    for d in range(6):
        rz, rx = np.nonzero(red & (run > d))
        wg[ty[rz, rx] - d, rz, rx] = T["path"]
    st["red"] += int((red & inc).sum()); st["red_blocks"] += int(np.where(red & inc, run, 0).sum())
    # 3. leaves and logs on the road
    paved = (T["asphalt"][tm] | T["yellow"][tm] | filled) & has   # the carriageway, not paths, plazas or white roofs
    Y = np.arange(Hh)[:, None, None]
    lf = T["leaf"][wg]
    clr = paved[None] & (Y > ty[None]) & (Y <= ty[None] + 12) & (lf | T["log"][wg])
    st["leaf_cleared"] += int((clr & lf & inc[None]).sum())
    st["log_cleared"] += int((clr & ~lf & inc[None]).sum())
    wg[clr] = 0

# ---- Per-building shades. Arnis builds neighbouring houses from the same few blocks, so a street of them reads as one
# long building. Each building is given one of three sibling shades (base, lighter, darker) of its wall and roof
# materials, picked by a hash of where its footprint is, so walls and roofs of a building shift together and the
# rows read as separate buildings. Only material ids change: no format change, no runtime cost.
SHADE_PAD, SHADE_EDGE, SHADE_THIN = 12, 3, 8
SH = {}
def _shade_tables():
    if SH: return SH
    built = np.array([k in ("solid", "glass") for _, _, k in MATS] + [False] * (256 - len(MATS)))
    for n in ["grass_block", "dirt", "coarse_dirt", "rooted_dirt", "podzol", "mud", "sand", "red_sand", "gravel", "clay",
              "moss_block", "dirt_path", "farmland", "snow", "bedrock"] + [w + s for w in WOODS for s in ("_log", "_wood")]:
        built[mat(*colour(n))] = False   # bare ground and tree trunks are not buildings
    lut = np.tile(np.arange(256, dtype=np.uint8), (3, 1))
    for n in SHADE_BASES:
        c = colour(n)[0]; b = mat(c)
        lut[1, b], lut[2, b] = MAT_ID[(shade(c, 1), "solid")], MAT_ID[(shade(c, -1), "solid")]
    SH.update(built=built, lut=lut)
    return SH

def _hash3(z, x):
    """0, 1 or 2 from a world column, the same in every window and every run."""
    h = (z * 0x9E3779B1 + x * 0x85EBCA77 + 0x165667B1) & 0xFFFFFFFFFFFFFFFF
    h ^= h >> 33; h = (h * 0xFF51AFD7ED558CCD) & 0xFFFFFFFFFFFFFFFF; h ^= h >> 33
    return h % 3

def building_shades(wg, dmat, inworld, rx, rz, st):
    """Gives each building in the centre region of the window one of three shades, in place in wg and dmat.

    A building is a 4-connected group of built columns in plan view: columns with a wall, glass, roof slab or roof stair
    above the ground surface (the ground is the top of the 3-block terrain shell Arnis lays in every column). A 3x3
    opening first cuts the one-block garden walls and fences that would chain houses together; a cut-off piece of at
    most SHADE_THIN columns that touches one building only (a bay, a chimney) goes back to it.
    The shade is a hash of the world position of the building's first column (least z, then least x), so every window
    that holds the whole building picks the same one. A building is only shaded when its footprint, padded by
    SHADE_PAD, lies well inside the window of every region it reaches, so that all of those windows see all of it and
    agree; and never when it comes within SHADE_EDGE of the edge of the baked world, where a bake (or a chunk of a
    joined area, which is converted on its own) cuts it: both halves of a building across a chunk seam keep the base
    shade. Only blocks above the ground in the building's columns change (and a wall's footing block at ground level),
    and only to a sibling of the same kind, so faces, sky reach and drivable heights are untouched."""
    T = _shade_tables(); built, lut = T["built"], T["lut"]
    Hh, Sz, Sx = wg.shape
    occ = wg[1:] != 0                                   # layer 0 is bedrock
    g = np.where(occ.any(axis=0), occ.argmax(axis=0) + 3, Hh)   # ground surface = bottom of the shell + 2
    del occ
    Y = np.arange(Hh)[:, None, None]
    bm = (built[wg] & (Y > g[None])).any(axis=0)
    # roof slabs and stairs (partial blocks covering their whole cell) of all nine regions, so every window sees the same
    roofish = np.array([False] + [any(b[0] == 0 and b[2] == 0 and b[3] == 16 and b[5] == 16 and b[4] >= 8 for b in sh) for sh in SHAPES[1:]])
    def rng(d): return (slice(0, M), slice(PACK - M, PACK)) if d < 0 else (slice(M, M + PACK), slice(0, PACK)) if d == 0 else (slice(M + PACK, S), slice(0, M))
    for dz in (-1, 0, 1):
        for dx in (-1, 0, 1):
            r = load(rx + dx, rz + dz)
            if r is None: continue
            (wz, sz), (wx, sx) = rng(dz), rng(dx)
            bm[wz, wx] |= (built[r[1][:, sz, sx]] & roofish[r[2][:, sz, sx]] & (Y > g[None, wz, wx])).any(axis=0)
    iw = inworld & (wg[2:] != 0).any(axis=0)
    cross = ndimage.generate_binary_structure(2, 1)
    op = ndimage.binary_opening(bm, np.ones((3, 3), bool))
    lab, _ = ndimage.label(op, cross)
    thin, _ = ndimage.label(bm & ~op, cross)
    tsz = np.bincount(thin.ravel())
    for i, sl in enumerate(ndimage.find_objects(thin), 1):
        if sl is None or tsz[i] > SHADE_THIN: continue
        s0 = slice(max(sl[0].start - 1, 0), sl[0].stop + 1); s1 = slice(max(sl[1].start - 1, 0), sl[1].stop + 1)
        m = thin[s0, s1] == i
        ls = np.unique(lab[s0, s1][ndimage.binary_dilation(m, cross) & ~m]); ls = ls[ls > 0]
        if len(ls) == 1: lab[s0, s1][m] = ls[0]
    var = np.zeros((Sz, Sx), dtype=np.uint8)
    oz, ox = rz * PACK - M, rx * PACK - M   # world (converter) coordinates of window cell (0, 0)
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None: continue
        z0, z1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        if z1 <= M or z0 >= M + PACK or x1 <= M or x0 >= M + PACK: continue   # not in this region
        m = lab[sl] == i
        cz, cx = divmod(int(m.argmax()), m.shape[1])
        key = (oz + z0 + cz, ox + x0 + cx)
        mine = M <= z0 + cz < M + PACK and M <= x0 + cx < M + PACK   # counted by the region holding its first column
        # padded footprint in world coordinates, and every region whose centre it reaches
        a0, a1, b0, b1 = oz + z0 - SHADE_PAD, oz + z1 + SHADE_PAD, ox + x0 - SHADE_PAD, ox + x1 + SHADE_PAD
        ok = all(q * PACK - M + SHADE_EDGE <= a0 and a1 <= (q + 1) * PACK + M - SHADE_EDGE and
                 p * PACK - M + SHADE_EDGE <= b0 and b1 <= (p + 1) * PACK + M - SHADE_EDGE
                 for q in range(a0 // PACK, (a1 - 1) // PACK + 1) for p in range(b0 // PACK, (b1 - 1) // PACK + 1) if (p, q) in REG)
        edge = False
        if ok:
            e0, e1 = slice(z0 - SHADE_EDGE, z1 + SHADE_EDGE), slice(x0 - SHADE_EDGE, x1 + SHADE_EDGE)
            near = ndimage.binary_dilation(np.pad(m, SHADE_EDGE), np.ones((2 * SHADE_EDGE + 1,) * 2, bool))
            edge = bool((near & ~iw[e0, e1]).any())
        v = _hash3(*key) if ok and not edge else 0
        if mine:
            st["buildings"] += 1; st["cut"] += not ok; st["world_edge"] += ok and edge; st["shaded"] += v > 0
            st["cols"] += int(m.sum()); st["cols_shaded"] += int(m.sum()) if v else 0
        if v: var[sl][m] = v
    c = (slice(M, M + PACK), slice(M, M + PACK))
    vc, gc = var[c], g[c]
    if not vc.any(): return
    grid = wg[:, c[0], c[1]]
    footing = np.zeros(grid.shape, dtype=bool); footing[:-1] = built[grid[1:]]   # a wall's footing block at ground level
    for v in (1, 2):
        cols = (vc == v)[None]
        sel = cols & ((Y > gc[None]) | ((Y == gc[None]) & footing))
        before = grid[sel]; grid[sel] = lut[v][before]; st["blocks"] += int((grid[sel] != before).sum())
        sel = cols & (Y > gc[None])
        before = dmat[sel]; dmat[sel] = lut[v][before]; st["blocks"] += int((dmat[sel] != before).sum())

# ---- Flat roofs. Arnis gives a flat-roofed building a roof one block below the top of its walls, and on the walls a
# one-block parapet with a coping slab: from above, every building is a ring of wall standing one and a half blocks over
# its roof. Where buildings touch (rows of shops, OSM footprints that overlap) each keeps its own ring, so walls run
# across the joined roof; thin and diagonal footprints are more wall than roof. Seen from the air that reads as roofless
# ruins. A few buildings get no roof at all (only the walls and the ground floor). Driven by the OSM footprints of the
# bake (--osm), this pass lays the roof of each flat-roofed building one block higher, flush with the top of its walls,
# in the roof's own block (its colour), so only the coping slab rims the edge; covers a roofless one the same way (in
# the block nearest its OSM roof:colour); brings a building up one block to an attached neighbour one block taller; roofs
# over the one-block slits between two outlines; and where a wall runs between two roofs, takes off its coping and gives
# its top block the roof's block. Pitched roofs (a roof not one block under the wall top), open structures (FR_SKIP),
# courtyards (inner rings) and buildings cut by a region window or the edge of the world are left as they are.
FR_SKIP = {"roof", "carport", "parking", "stadium", "construction", "grandstand", "ruins", "canopy", "bridge", "shelter",
           "tent", "greenhouse", "transformer_tower", "storage_tank", "silo", "no", "demolished", "abandoned", "proposed"}
FR_PAD = 3
FR_ROOF_NAMES = ["smooth_stone", "white_concrete", "light_gray_concrete", "gray_concrete", "white_terracotta", "light_gray_terracotta",
                 "polished_andesite", "stone_bricks", "bricks", "sandstone", "terracotta", "black_concrete", "brown_terracotta",
                 "red_terracotta", "cyan_terracotta", "quartz_block"]
FR = {}
def _flat_roof_buildings():
    """OSM building footprints of the bake, in grid coordinates: [(id, tags, [outer rings], [inner rings], bbox)]."""
    if "b" in FR: return FR["b"]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import road_grade
    pj = road_grade.projection(WORLD)
    d = json.load(open(OSM))
    ll = {e["id"]: (e["lon"], e["lat"]) for e in d["elements"] if e["type"] == "node"}
    ways = {e["id"]: e for e in d["elements"] if e["type"] == "way"}
    def ring(w):
        pts = [pj(*ll[n]) for n in w.get("nodes", []) if n in ll]
        return np.array([(x - minx, z - minz) for x, z in pts], dtype=float) if len(pts) > 2 else None
    out = []
    for e in d["elements"]:
        t = e.get("tags", {})
        b = t.get("building")
        if not b or "building:part" in t or b in FR_SKIP: continue
        if t.get("roof:shape", "flat") != "flat" or t.get("amenity") == "parking" or t.get("parking"): continue
        if any(k in t for k in ("min_height", "building:min_level", "min_level")): continue   # floating structures
        if e["type"] == "way": outer, inner = [ring(e)], []
        elif e["type"] == "relation":
            mem = [(m.get("role"), ways.get(m["ref"])) for m in e.get("members", []) if m.get("type") == "way"]
            outer = [ring(w) for r, w in mem if w is not None and r != "inner"]
            inner = [ring(w) for r, w in mem if w is not None and r == "inner"]
        else: continue
        outer = [r for r in outer if r is not None]; inner = [r for r in inner if r is not None]
        if not outer: continue
        allp = np.concatenate(outer)
        out.append((e["id"], t, outer, inner, (allp[:, 0].min(), allp[:, 0].max(), allp[:, 1].min(), allp[:, 1].max())))
    FR["b"] = out
    # the block nearest a roof:colour, for a roof that Arnis left out
    FR["roofmats"] = [(colour(n)[0], mat(*colour(n))) for n in FR_ROOF_NAMES]
    return out

def _inside(rings, px, pz):
    """Even-odd point in polygon over all rings."""
    res = np.zeros(px.shape, dtype=bool)
    for r in rings:
        x0, z0, x1, z1 = r[:-1, 0], r[:-1, 1], r[1:, 0], r[1:, 1]
        if not (r[0] == r[-1]).all(): x0, z0, x1, z1 = r[:, 0], r[:, 1], np.roll(r[:, 0], -1), np.roll(r[:, 1], -1)
        for a, b, c, d in zip(x0, z0, x1, z1):
            if b == d: continue
            res ^= ((b > pz) != (d > pz)) & (px < a + (pz - b) * (c - a) / (d - b))
    return res

def _nearest_roof(hexcol):
    try: c = int(str(hexcol).lstrip("#")[:6], 16)
    except ValueError: return None
    rgb = lambda v: np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255], float)
    return min(FR["roofmats"], key=lambda m: ((rgb(m[0]) - rgb(c)) ** 2).sum())[1]

def flat_roofs(wg, dmat, dshape, inworld, rx, rz, st):
    """Lays flat roofs flush with their walls, in place in wg (cubes, whole window) and dmat/dshape (centre region).
    Every decision reads only the window's cube grid as it was before this pass, over the building's own footprint and
    a few blocks around it, and a building is only touched when its padded footprint lies inside the window of every
    region it reaches (as in building_shades), so each window that holds a building makes the same changes to it."""
    Bs = _flat_roof_buildings()
    Hh, Sz, Sx = wg.shape
    ox, oz = (rx - rx0) * PACK - M, (rz - rz0) * PACK - M     # grid coordinates of window cell (0, 0)
    cube = (kinds == 1) | (kinds == 3)
    cube = np.concatenate([cube, np.zeros(256 - len(cube), bool)])
    occ = wg[1:] != 0
    g = np.where(occ.any(axis=0), occ.argmax(axis=0) + 3, Hh)  # ground surface, as in building_shades
    del occ
    solid = cube[wg]
    top0 = np.where(solid.any(axis=0), Hh - 1 - solid[::-1].argmax(axis=0), -1)   # highest cube, before this pass
    del solid
    slab = np.array([False] + [len(sh) == 1 and sh[0] == (0, 0, 0, 16, 8, 16) for sh in SHAPES[1:]])
    lay = np.full((Sz, Sx), -1, dtype=np.int32)            # roof level laid in each column (-1: none)
    jobs = []
    cross = ndimage.generate_binary_structure(2, 1)
    for bid, t, outer, inner, (bx0, bx1, bz0, bz1) in Bs:
        x0, x1 = int(math.floor(bx0)) - FR_PAD, int(math.ceil(bx1)) + FR_PAD + 1
        z0, z1 = int(math.floor(bz0)) - FR_PAD, int(math.ceil(bz1)) + FR_PAD + 1
        if x1 <= ox or z1 <= oz or x0 >= ox + Sx or z0 >= oz + Sz: continue
        mine = (rx - rx0) * PACK <= (x0 + x1) // 2 < (rx - rx0 + 1) * PACK and (rz - rz0) * PACK <= (z0 + z1) // 2 < (rz - rz0 + 1) * PACK
        # inside the window of every region the padded footprint reaches, so all of them agree
        ok = all(q * PACK - M <= z0 + rz0 * PACK and z1 + rz0 * PACK <= (q + 1) * PACK + M and
                 p * PACK - M <= x0 + rx0 * PACK and x1 + rx0 * PACK <= (p + 1) * PACK + M
                 for q in range((z0 + rz0 * PACK) // PACK, (z1 - 1 + rz0 * PACK) // PACK + 1)
                 for p in range((x0 + rx0 * PACK) // PACK, (x1 - 1 + rx0 * PACK) // PACK + 1) if (p, q) in REG)
        if not ok or x0 < ox or z0 < oz or x1 > ox + Sx or z1 > oz + Sz:
            if mine: st["cut"] += 1
            continue
        sl = (slice(z0 - oz, z1 - oz), slice(x0 - ox, x1 - ox))
        if not inworld[sl].all():
            if mine: st["world_edge"] += 1
            continue
        zz, xx = np.mgrid[z0:z1, x0:x1]
        F = _inside(outer, xx + .5, zz + .5) & ~_inside(inner, xx + .5, zz + .5)
        if F.sum() < 12: continue
        I = ndimage.binary_erosion(F, cross)
        if I.sum() < 6: continue
        ring = ndimage.binary_dilation(F, cross) & ~I
        tp, gr = top0[sl], g[sl]
        gm = int(np.median(gr[F]))
        tall = ring & (tp >= gr + 2)
        if tall.sum() < 0.4 * ring.sum():
            if mine: st["low"] += 1
            continue
        vals, cnt = np.unique(tp[tall], return_counts=True)
        Tc = int(vals[cnt.argmax()])                       # the top of the walls (full blocks; the coping slab is above)
        if cnt.max() < 0.4 * tall.sum() or Tc + 2 >= Hh: continue
        ti = tp[I]
        vals, cnt = np.unique(ti, return_counts=True)
        R = int(vals[cnt.argmax()])
        flat = R == Tc - 1 and cnt.max() >= 0.5 * len(ti)  # roof one block under the wall top: the parapet layout
        open_ = (ti <= Tc - 2).mean() >= 0.5 and Tc >= gm + 2   # most of the inside lies two or more blocks lower
        if not (flat or open_):
            if mine: st["pitched"] += 1
            continue
        if flat or (cnt.max() >= 0.5 * len(ti) and R >= gm + 2):   # the roof's own block, where there is a roof
            m = wg[R][sl][I & (tp == R)]
            roofm = int(np.bincount(m, minlength=256).argmax())
        else:                                              # none: the block nearest the roof colour of the bake
            roofm = _nearest_roof(t.get("roof:colour", "")) or mat(*colour("light_gray_concrete"))
        jobs.append(dict(sl=sl, F=F, I=I, tall=tall, Tc=Tc, roofm=roofm, mine=mine, flat=flat,
                         box=(z0 - oz, z1 - oz, x0 - ox, x1 - ox)))
    # Attached buildings whose walls end one block apart (OSM heights a few decimetres apart, rounded) would keep a step
    # with its own wall and coping between their roofs: the lower one is brought up to its neighbour. Only neighbours
    # that touch it count, and only one block, so the answer does not depend on how far a row of buildings reaches.
    for a in jobs:
        za0, za1, xa0, xa1 = a["box"]
        grow = ndimage.binary_dilation(a["F"], cross, iterations=2)   # walls drawn on the outline: rasters a block or two apart
        for b in jobs:
            if b["Tc"] != a["Tc"] + 1: continue
            zb0, zb1, xb0, xb1 = b["box"]
            if zb0 >= za1 or zb1 <= za0 or xb0 >= xa1 or xb1 <= xa0: continue
            z0_, z1_, x0_, x1_ = max(za0, zb0), min(za1, zb1), max(xa0, xb0), min(xa1, xb1)
            if (grow[z0_ - za0:z1_ - za0, x0_ - xa0:x1_ - xa0] & b["F"][z0_ - zb0:z1_ - zb0, x0_ - xb0:x1_ - xb0]).any():
                a["up"] = True; break
    rmat = np.zeros((Sz, Sx), dtype=np.uint8)              # the roof block laid in each column
    near = np.zeros((Sz, Sx), dtype=bool)                  # within two blocks of a footprint whose roof was laid
    for j in jobs:
        sl, F, I, Tc, up = j["sl"], j["F"], j["I"], j["Tc"], j.get("up", False)
        tp = top0[sl]
        L = Tc + 1 if up else Tc
        if L >= Hh - 1: continue
        # the roof goes on every inside column, and on the footprint's edge columns that carry wall or roof up there;
        # a building brought up a block also gets its walls one block taller, in their own blocks
        put = (I | (F & (tp >= Tc - 2))) & (wg[L][sl] == 0)
        w = wg[L][sl]
        if up:
            wall = (j["tall"] & (tp == Tc) & ~I) & (wg[L][sl] == 0)
            w[wall] = wg[Tc][sl][wall]
            put |= wall
        lay[sl][put] = np.maximum(lay[sl][put], L)
        w[put & (w == 0)] = j["roofm"]
        r = rmat[sl]; r[(put | I) & (r == 0)] = j["roofm"]
        near[sl] |= ndimage.binary_dilation(F, cross, iterations=2)
        if j["mine"]:
            st["flat" if j["flat"] else "roofless"] += 1
            st["raised"] += up
            st["cols"] += int(put.sum())
    c = (slice(M, M + PACK), slice(M, M + PACK))
    def tops():
        solid = cube[wg]
        return np.where(solid.any(axis=0), Hh - 1 - solid[::-1].argmax(axis=0), -1)
    def nb(a, dz, dx): return np.roll(np.roll(a, dz, 0), dx, 1)
    box3 = np.ones((3, 3), bool)
    # Walls drawn on two outlines that run a block apart leave a slit between two roofs: it is roofed over too
    top = tops()
    Ld = ndimage.grey_dilation(lay, footprint=box3)
    Rm = ndimage.grey_dilation(rmat, footprint=box3)
    hi = lambda dz, dx: nb(top, dz, dx) >= Ld
    slit = near & (lay < 0) & (Ld >= 0) & (Ld < Hh) & (top < Ld) & (Rm > 0) & ((hi(1, 0) & hi(-1, 0)) | (hi(0, 1) & hi(0, -1)))
    zz, xx = np.nonzero(slit)
    wg[Ld[zz, xx], zz, xx] = Rm[zz, xx]; lay[zz, xx] = Ld[zz, xx]; rmat[zz, xx] = Rm[zz, xx]
    st["slits"] += int(slit[c].sum())
    # A wall between two roofs (every side covered up to the roof level) loses its coping slab, and its top block takes
    # the roof's block, so attached buildings read as one roof; walls on the outside keep both
    top = tops()
    L = ndimage.grey_dilation(lay, footprint=box3)
    Rm = ndimage.grey_dilation(rmat, footprint=box3)
    cov = near & (L >= 0) & (top == L)
    for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        cov &= nb(top, dz, dx) >= L
    zz, xx = np.nonzero(cov & (lay < 0) & (Rm > 0))
    wg[L[zz, xx], zz, xx] = Rm[zz, xx]
    st["party"] += int((cov & (lay < 0) & (Rm > 0))[c].sum())
    cz, cx = np.nonzero(cov[c])
    if len(cz):
        y = L[c][cz, cx] + 1
        ok = (y < Hh)
        cz, cx, y = cz[ok], cx[ok], y[ok]
        hit = (dmat[y, cz, cx] != 0) & slab[dshape[y, cz, cx]]
        dmat[y[hit], cz[hit], cx[hit]] = 0
        st["copings"] += int(hit.sum())
    # rooftop fixtures that Arnis set on the old roof (daylight sensors, trapdoors) are now inside the new one
    lc = lay[c]
    zz, xx = np.nonzero(lc >= 0)
    if len(zz):
        y = lc[zz, xx]
        hit = dmat[y, zz, xx] != 0
        dmat[y[hit], zz[hit], xx[hit]] = 0
        st["fixtures"] += int(hit.sum())

FRS = dict(flat=0, roofless=0, raised=0, pitched=0, low=0, cut=0, world_edge=0, cols=0, slits=0, party=0, copings=0, fixtures=0)
BSS = dict(buildings=0, cut=0, world_edge=0, shaded=0, cols=0, cols_shaded=0, blocks=0)
RFS = dict(pits=0, fill_fill=0, skip_fill=0, fill_soft=0, skip_soft=0, red=0, red_blocks=0, leaf_cleared=0, log_cleared=0)
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
    road_fixes(wg, (slice(M, M + PACK), slice(M, M + PACK)), RFS)
    if OSM and not NO_ROOFS: flat_roofs(wg, dmat, dshape, inworld, rx, rz, FRS)
    building_shades(wg, dmat, inworld, rx, rz, BSS)
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
    # A sealing partial block (roof slab, stair, path) is itself seen when open air touches any of its
    # sides. Without this every slab roof and slab-built detail was dropped and the block under it lost its top.
    near = np.zeros((H, PACK, PACK), dtype=bool)
    for (dy, dz, dx), _ in dirs:
        near |= reach[1+dy:H+1+dy, 1+M+dz:1+M+PACK+dz, 1+M+dx:1+M+PACK+dx]
    seen = reach.copy()
    seen[1:H+1, 1+M:1+M+PACK, 1+M:1+M+PACK] |= cover & near
    del near
    pad = np.pad(wg, 1, constant_values=0)
    c = pad[1:-1, 1:-1, 1:-1]
    wmask = np.zeros(wg.shape, dtype=np.uint8)
    for (dy, dz, dx), bit in dirs:
        sl = (slice(1+dy, H+1+dy), slice(1+dz, S+1+dz), slice(1+dx, S+1+dx))
        nb = pad[sl]
        wmask |= ((see[nb] & (nb != c) & seen[sl]).astype(np.uint8) << bit)
    wmask[c == 0] = 0
    del pad
    grid = wg[:, M:M+PACK, M:M+PACK]; mask = wmask[:, M:M+PACK, M:M+PACK]
    dmat[~seen[1:H+1, 1+M:1+M+PACK, 1+M:1+M+PACK]] = 0
    faces += sum(int(np.count_nonzero(mask & (1 << b))) for b in range(6))
    ox, oz = (rx - rx0) * PACK, (rz - rz0) * PACK
    # Surfaces a car can stand on: a solid block (not leaves or water) with open sky-connected space and
    # three blocks of headroom above it. Tree canopies and roofs over the road no longer lift the car.
    hard = (kinds[grid] == 1) | (kinds[grid] == 3)   # full blocks only: a slab roof is not somewhere to drive
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
print("road fixes (region centres): " + ", ".join(f"{k} {v:,}" for k, v in RFS.items()))
if OSM and not NO_ROOFS: print("flat roofs: " + ", ".join(f"{k} {v:,}" for k, v in FRS.items()))
print("building shades: " + ", ".join(f"{k} {v:,}" for k, v in BSS.items()))
print(f"greedy meshing: {nquads:,} quads (from {faces:,} block faces); {nboxes:,} detail boxes for partial blocks")

meta = {"tile": TILE, "pack": PACK, "ext": "json" if B64 else "bin", "width": int(W), "depth": int(D), "height": int(H),
        "minx": int(minx), "minz": int(minz), "miny": int(gminy), "quads": int(nquads), "bytes": int(total), "packs": packs,
        "materials": [{"name": n, "rgb": f"#{c:06x}", "kind": k} for n, c, k in MATS]}
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"))
print(f"wrote {len(packs)} packs, {total/1e6:.1f} MB compressed, in {time.time()-t0:.1f}s total")
print("block names with no colour rule (drawn mid-grey):", ", ".join(sorted(unknown))[:1500])
