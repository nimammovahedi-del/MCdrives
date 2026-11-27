// Round trip of data/meta.json through format 2 (tools/metafmt.py writes it, web/drive/meta.js reads it).
//   node tools/test_meta.mjs META.json [WORK_DIR]
// META.json is a format 1 meta.json (a joined site's, e.g. from a built/<area> branch). The test writes it in
// format 2 into WORK_DIR (default: a temporary folder, removed after), then checks that
//   - tools/metafmt.py reads it back equal to the original,
//   - the viewer's expandMeta() + packTiles() for every pack give back the original exactly (every key and value),
//   - expandMeta() leaves a format 1 meta as it is, and packTiles() does nothing for it,
//   - meta.json is much smaller than the original.
import { execFileSync } from 'node:child_process';
import { readFileSync, mkdtempSync, rmSync, mkdirSync } from 'node:fs';
import { isDeepStrictEqual } from 'node:util';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const [src, work0] = process.argv.slice(2);
if (!src) { console.error('usage: node tools/test_meta.mjs META.json [WORK_DIR]'); process.exit(2); }
const work = work0 ?? mkdtempSync(join(tmpdir(), 'meta-')); mkdirSync(work, { recursive: true });
let failed = 0;
const check = (ok, what) => { console.log((ok ? 'ok   ' : 'FAIL ') + what); if (!ok) failed++; };
try {
  const py = execFileSync('python3', [join(here, 'metafmt.py'), src, work], { encoding: 'utf8' });
  check(/read back equal: True/.test(py), 'metafmt.py reads format 2 back equal to the original');
  const orig = JSON.parse(readFileSync(src, 'utf8')), small = JSON.parse(readFileSync(join(work, 'meta.json'), 'utf8'));
  check(small.v === 2, 'meta.json is format 2');
  const { expandMeta, packTiles } = await import(join(here, '..', 'web', 'drive', 'meta.js'));
  // the viewer fetches data/tiles/<chunk>.json relative to the page: serve them from WORK_DIR
  let fetches = 0;
  globalThis.fetch = async url => {
    fetches++;
    const f = join(work, String(url).replace(/^data\//, ''));
    try { const t = readFileSync(f, 'utf8'); return { ok: true, status: 200, json: async () => JSON.parse(t) }; }
    catch { return { ok: false, status: 404, json: async () => { throw new Error('404'); } }; }
  };
  const meta = expandMeta(small);
  // as index.html does: the packs it streams are copies of meta.packs, filled when they load
  const copies = meta.packs.map(p => ({ ...p, state: 0 }));
  await Promise.all(copies.map(p => packTiles(meta, p)));
  check(fetches === new Set(small.chunks.filter((c, i) => small.packs.n[i]).map(c => c.id)).size, `one tile file fetch per chunk (${fetches})`);
  const full = { ...meta, packs: copies.map(({ state, ...p }) => p) };
  check(isDeepStrictEqual(full, orig), 'expandMeta + packTiles give back the original meta exactly');
  for (const k of new Set([...Object.keys(orig), ...Object.keys(full)]))
    if (!isDeepStrictEqual(full[k], orig[k])) console.log('   differs:', k);
  // what the viewer reads before any pack loads: everything but each pack's tiles and quads
  const strip = m => ({ ...m, packs: m.packs.map(({ tiles, quads, ...p }) => p) });
  check(isDeepStrictEqual(strip(meta), strip(orig)), 'expandMeta alone gives every key the viewer reads at start');
  const again = { ...copies[0], tiles: null }; await packTiles(meta, again);
  check(isDeepStrictEqual(again.tiles, orig.packs[0].tiles) && fetches === new Set(small.chunks.filter((c, i) => small.packs.n[i]).map(c => c.id)).size,
    'a pack loaded again reuses its chunk file');
  const old = JSON.parse(readFileSync(src, 'utf8')), same = expandMeta(old), p0 = same.packs[0], t0 = p0.tiles;
  await packTiles(same, p0);
  check(same === old && p0.tiles === t0 && isDeepStrictEqual(old, orig), 'format 1 passes through untouched');
  const a = readFileSync(src).length, b = readFileSync(join(work, 'meta.json')).length;
  check(b < a / 20, `meta.json ${a} -> ${b} bytes`);
} finally { if (!work0) rmSync(work, { recursive: true, force: true }); }
process.exit(failed ? 1 : 0);
