// data/meta.json in either format, as the viewer uses it (see tools/metafmt.py, which writes it).
// Format 1 (no meta.v) is used as it is. Format 2 (meta.v === 2) lists the packs in a few short arrays and keeps
// their tile tables in one file per chunk, data/tiles/<chunk id>.json: expandMeta() rebuilds meta.packs and the
// map's picture lists, with each pack's tiles left null until packTiles(meta, pack) has fetched its chunk's file.

const tileFiles = new WeakMap();   // expanded meta -> { where: pack id -> [chunk id, i, j, entry], files: chunk id -> promise }

export function expandMeta(meta) {
  if (meta?.v !== 2) return meta;
  const P = meta.packs, PK = meta.pack, packs = [], where = new Map();
  let k = 0;
  meta.chunks.forEach((c, ci) => {
    for (let n = 0; n < P.n[ci]; n++, k++) {   // n: the pack's entry in its chunk's tile file
      const i = P.ij[2 * k], j = P.ij[2 * k + 1], id = (P.pre[k] === '1' ? c.id + '_' : '') + `p_${i}_${j}`;
      packs.push({ id, h: id.startsWith('p_') ? 'h' + id.slice(1) : 'h_' + id, x: c.dx + i * PK, z: c.dz + j * PK,
        quads: null, dx: c.dx, dz: c.dz, dy: c.dy, tiles: null });
      where.set(id, [c.id, i, j, n]);
    }
  });
  const out = { ...meta, packs };
  delete out.v;
  const pics = (d, key) => typeof d?.[key] === 'string' ? { [key]: packs.map(p => [d[key] + p.id + '.png', p.x, p.z]) } : {};
  if (meta.map) {
    out.map = { ...meta.map, ...pics(meta.map, 'tiles') };
    if (meta.map.illus) out.map.illus = { ...meta.map.illus, ...pics(meta.map.illus, 'tiles'), ...pics(meta.map.illus, 'mid') };
  }
  tileFiles.set(out, { where, files: new Map(), dir: 'data/' + P.tiles });
  return out;
}

// Fills p.tiles ([[tx, tz, quads, offset, length], ...]) and p.quads from its chunk's tile file; nothing to do for
// format 1 or when they are there already. p may be a copy of a meta.packs entry (it is found by its id).
export async function packTiles(meta, p) {
  const T = tileFiles.get(meta); if (!T || p.tiles) return;
  const [cid, i, j, n] = T.where.get(p.id);
  let f = T.files.get(cid);
  if (!f) {
    f = fetch(T.dir + cid + '.json').then(r => { if (!r.ok) throw new Error(`tiles ${cid}: ${r.status}`); return r.json(); });
    T.files.set(cid, f); f.catch(() => T.files.delete(cid));   // a failed fetch is tried again with the next pack
  }
  const side = await f, per = meta.pack / meta.tile;
  const e = side.p[n];
  const m = side.m[e[0]], ks = [];
  for (let b = 0; b < per * per; b++) if (parseInt(m[15 - (b >> 2)], 16) >> (b & 3) & 1) ks.push(b);
  let off = 0; const tiles = [];
  ks.forEach((b, t) => { const q = e[1 + t], len = e[1 + ks.length + t]; tiles.push([i * per + b % per, j * per + Math.floor(b / per), q, off, len]); off += len; });
  p.tiles = tiles; p.quads = tiles.reduce((s, t) => s + t[2], 0);
}
