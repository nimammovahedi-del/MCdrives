// Touch-first controls: round-free blocky map buttons, a bottom sheet of big tiles (a side panel on
// desktop), search, and the little place card that offers "Drive here". DOM only.

// ---------- pixel icons: 12 x 12 grids, '#' ink, '+' light ink, '.' empty
const ICONS = {
  search: ['....####....', '...#....#...', '..#......#..', '..#......#..', '..#......#..', '...#....#...', '....#####...', '.........##.', '..........##', '...........#', '............', '............'],
  map: ['............', '.###.##.###.', '.#+#.#+.#+#.', '.#+#.#+.#+#.', '.#+##.+##+#.', '.#+.#.+#.+#.', '.#+.#.+#.+#.', '.#+##.+##+#.', '.#+#.#+.#+#.', '.###.###.##.', '............', '............'],
  explore: ['.....##.....', '.....##.....', '....####....', '...#.##.#...', '..#..##..#..', '##..####..##', '##..####..##', '..#..##..#..', '...#.##.#...', '....####....', '.....##.....', '.....##.....'],
  recenter: ['.....#......', '....###.....', '...#####....', '..###.###...', '.###...###..', '###.....###.', '##.......##.', '............', '............', '............', '............', '............'],
  menu: ['............', '............', '.##########.', '.##########.', '............', '.##########.', '.##########.', '............', '.##########.', '.##########.', '............', '............'],
  close: ['............', '.##......##.', '.###....###.', '..###..###..', '...######...', '....####....', '....####....', '...######...', '..###..###..', '.###....###.', '.##......##.', '............'],
  drive: ['............', '.....##.....', '....####....', '...######...', '..###..###..', '.###....###.', '###......###', '##........##', '............', '............', '............', '............'],
  street: ['....###.....', '....###.....', '.....#......', '...#####....', '..#.###.#...', '....###.....', '....#.#.....', '....#.#.....', '...##.##....', '............', '............', '............'],
  pin: ['...######...', '..##++++##..', '..#++##++#..', '..#++##++#..', '..##++++##..', '...##++##...', '....####....', '.....##.....', '.....##.....', '............', '............', '............'],
};
export function icon(name, color = '#fff', light = 'rgba(255,255,255,.55)') {
  const g = ICONS[name], c = document.createElement('canvas'); c.width = c.height = 12; const x = c.getContext('2d');
  g.forEach((row, y) => [...row].forEach((ch, i) => { if (ch === '.') return; x.fillStyle = ch === '#' ? color : light; x.fillRect(i, y, 1, 1); }));
  c.className = 'pix'; return c;
}
const h = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

// ---------- floating map buttons (right edge, above the trip bar)
export function createMapButtons(stage, { onSearch, onMap, onExplore, onRecenter }) {
  const wrap = h('div', 'mapbtns'); wrap.id = 'mapBtns';
  const btn = (name, label, fn) => { const b = h('button', 'mb'); b.append(icon(name)); b.title = label; b.setAttribute('aria-label', label); b.onclick = e => { e.stopPropagation(); fn(); }; wrap.append(b); return b; };
  const b = { search: btn('search', 'Search', onSearch), map: btn('map', 'Map', onMap), explore: btn('explore', 'Explore', onExplore) };
  const re = h('button', 'recenter'); re.id = 'recenter'; re.append(icon('recenter', '#fff'), h('span', null, 'Re-center')); re.onclick = e => { e.stopPropagation(); onRecenter(); };
  stage.append(wrap, re);
  return {
    el: wrap, recenter: re,
    set({ mode, looking }) {
      b.map.classList.toggle('on', mode === 'map'); b.explore.classList.toggle('on', mode === 'orbit');
      re.classList.toggle('show', mode !== 'nav' || looking);
      re.lastChild.textContent = mode === 'nav' ? 'Re-center' : 'Back to driving';
    },
  };
}

// ---------- bottom sheet (side panel on desktop) of tile groups
/** sections: [{ title, tiles: [{ k, label, icon?: canvas|fn }], get: () => k, set: k => void, small?: bool } | { title, node }] */
export function createSheet(stage, { title = 'Menu', sections, footer }) {
  const scrim = h('div', 'scrim'); const sheet = h('div', 'sheet panel'); sheet.id = 'sheet';
  const head = h('div', 'sh-head'); const t = h('b', null, title); const x = h('button', 'mb sh-x'); x.append(icon('close')); x.setAttribute('aria-label', 'Close');
  head.append(h('span', 'grab'), t, x); sheet.append(head);
  const body = h('div', 'sh-body'); sheet.append(body);
  const groups = [];
  for (const s of sections) {
    const sec = h('section'); sec.append(h('div', 'sh-title', s.title));
    if (s.node) sec.append(s.node);
    else {
      const grid = h('div', 'tiles' + (s.small ? ' small' : '')); const els = new Map();
      for (const tl of s.tiles) {
        const b = h('button', 'tile'); if (tl.icon) b.append(typeof tl.icon === 'function' ? tl.icon() : tl.icon);
        b.append(h('span', null, tl.label)); b.onclick = () => { s.set(tl.k); refresh(); }; grid.append(b); els.set(tl.k, b);
      }
      sec.append(grid); groups.push({ s, els });
    }
    body.append(sec);
  }
  if (footer) body.append(footer);
  stage.append(scrim, sheet);
  function refresh() { for (const { s, els } of groups) { const v = s.get(); for (const [k, b] of els) b.classList.toggle('on', k === v); } }
  const api = {
    el: sheet, refresh,
    get open() { return sheet.classList.contains('open'); },
    toggle(v = !api.open) { sheet.classList.toggle('open', v); scrim.classList.toggle('open', v); if (v) refresh(); },
  };
  x.onclick = () => api.toggle(false); scrim.onclick = () => api.toggle(false);
  // swipe the handle down to close on phones
  let y0 = null; head.addEventListener('pointerdown', e => { y0 = e.clientY; });
  addEventListener('pointerup', e => { if (y0 != null && e.clientY - y0 > 50) api.toggle(false); y0 = null; });
  refresh(); return api;
}

// ---------- search: streets and places by name, forgiving of typos, abbreviations and informal names
const ABBR = { st: 'street', ave: 'avenue', av: 'avenue', pkwy: 'parkway', pky: 'parkway', dr: 'drive', blvd: 'boulevard', rd: 'road', ln: 'lane',
  ctr: 'center', cntr: 'center', centre: 'center', pl: 'place', ct: 'court', hwy: 'highway', fwy: 'freeway', n: 'north', s: 'south', e: 'east', w: 'west',
  cir: 'circle', ter: 'terrace', trl: 'trail', sq: 'square', mt: 'mount', univ: 'university', uni: 'university', hs: 'high school', elem: 'elementary', apt: 'apartments', i: 'interstate',
  plz: 'plaza', crk: 'creek', gln: 'glen', vw: 'view', grv: 'grove', rdg: 'ridge', spgs: 'springs', pne: 'pine', vly: 'valley', cyn: 'canyon', hl: 'hill', mdw: 'meadow', mdws: 'meadows', xing: 'crossing', lp: 'loop', aly: 'alley', hts: 'heights', pt: 'point', pk: 'park', lk: 'lake', est: 'estates', cv: 'cove', sprgs: 'springs',
  4: 'for', 2: 'to' };   // "Cha 4 Tea"
export const normName = t => t.toLowerCase().replace(/&/g, ' and ').replace(/[’']/g, '').replace(/([a-z])([0-9])/g, '$1 $2').replace(/[^a-z0-9]+/g, ' ').trim().split(' ').filter(Boolean).map(w => ABBR[w] ?? w).join(' ');
function lev(a, b) {     // edit distance with adjacent swaps
  const m = a.length, n = b.length; if (!m || !n) return m || n;
  let p2 = null, p = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const c = [i];
    for (let j = 1; j <= n; j++) {
      c[j] = Math.min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (p2 && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) c[j] = Math.min(c[j], p2[j - 2] + 1);
    }
    p2 = p; p = c;
  }
  return p[n];
}
// 0 = starts with the query, 1 = a word starts with it, 2 = contains it, 3..4 = close spelling, null = no match
export function matchScore(q, name) {
  if (!q) return 3.5;
  if (name.startsWith(q)) return 0;
  if ((' ' + name).includes(' ' + q)) return 1;
  if (name.includes(q)) return 2;
  const qw = q.split(' '), nw = name.split(' '); let tot = 0;
  for (const [i, w] of qw.entries()) {
    const last = i === qw.length - 1; let best = 0;
    for (const v of nw) {
      if (v.startsWith(w) && (last || v === w)) { best = 1; break; }
      const cut = last && v.length > w.length ? v.slice(0, w.length) : v;  // the word being typed is compared with a prefix
      const d = lev(w, cut), sim = 1 - d / Math.max(w.length, cut.length);
      if (d <= (w.length > 6 ? 2 : w.length > 3 ? 1 : 0)) best = Math.max(best, sim);
    }
    if (!best) return null; tot += best;
  }
  return 4 - tot / qw.length;
}
// address points (tools/addresses.py): street names come in county style, "IRVINE CENTER DR"
const SHOW = { dr: 'Drive', blvd: 'Boulevard', pkwy: 'Parkway', rd: 'Road', ave: 'Avenue', av: 'Avenue', st: 'Street', ln: 'Lane', ct: 'Court', pl: 'Place',
  cir: 'Circle', ter: 'Terrace', trl: 'Trail', hwy: 'Highway', ctr: 'Center', sq: 'Square', pt: 'Point', n: 'N', s: 'S', e: 'E', w: 'W',
  plz: 'Plaza', crk: 'Creek', gln: 'Glen', vw: 'View', grv: 'Grove', rdg: 'Ridge', spgs: 'Springs', pne: 'Pine', vly: 'Valley', cyn: 'Canyon', hl: 'Hill', mdw: 'Meadow', mdws: 'Meadows', xing: 'Crossing', lp: 'Loop', aly: 'Alley', hts: 'Heights', pk: 'Park', lk: 'Lake', est: 'Estates', cv: 'Cove', sprgs: 'Springs' };
export const titleStreet = t => t.toLowerCase().split(' ').map(w => SHOW[w] ?? w.replace(/^./, c => c.toUpperCase())).join(' ');
export function addressIndex(data, pos) {
  const streets = data.streets.map(s => ({ raw: s, name: titleStreet(s), key: normName(s), nums: new Map() }));
  for (let i = 0; i < data.a.length; i += 4) streets[data.a[i]].nums.set(String(data.a[i + 1]).toUpperCase(), [data.a[i + 2], data.a[i + 3]]);
  return {
    count: data.a.length / 4,
    /** "8909 irvine centr" -> [{ it: {name, pos, kind}, sc }] ; exact house numbers first, else the nearest number on that street */
    find(text) {
      const m = text.trim().match(/^(\d+[a-z]?)\s*,?\s+(.+)$/i); if (!m) return [];
      const num = m[1].toUpperCase(), q = normName(m[2].replace(/,.*$/, ''));
      const hits = streets.map(s => ({ s, sc: matchScore(q, s.key) })).filter(r => r.sc != null).sort((a, b) => a.sc - b.sc).slice(0, 4);
      const out = [];
      for (const { s, sc } of hits) {
        let at = s.nums.get(num), shown = num, near = false;
        if (!at) {   // no such number: the closest one on the street (same side of the street when possible)
          const n0 = parseInt(num, 10); let best = null;
          for (const [k, v] of s.nums) { const n = parseInt(k, 10); if (isNaN(n)) continue; const d = Math.abs(n - n0) + ((n - n0) % 2 ? 0.5 : 0); if (!best || d < best.d) best = { d, k, v }; }
          if (!best || best.d > 300) continue; at = best.v; shown = best.k; near = true;
        }
        out.push({ it: { name: `${shown} ${s.name}`, pos: pos(at[0], at[1]), kind: near ? `Closest to ${num}` : 'Address' }, sc: sc + (near ? 0.5 : 0) - 0.01 });
      }
      return out;
    },
  };
}
/** items: [{ name, pos, kind?, aliases?: [], cats?: [] }] (cats: what it is, "gas", "school": matched, ranked below names); addresses: async () => addressIndex | null, loaded the first time search opens */
export function createSearch(stage, { items, from, onPick, fmt, addresses, placeholder = 'Search addresses, streets and places' }) {
  const box = h('div', 'search panel'); box.id = 'search';
  const row = h('div', 'se-row'); row.append(icon('search', '#cfd6dd'));
  const inp = h('input'); inp.type = 'search'; inp.placeholder = placeholder; inp.setAttribute('aria-label', placeholder); inp.autocomplete = 'off'; inp.spellcheck = false;
  const x = h('button', 'mb'); x.append(icon('close')); x.setAttribute('aria-label', 'Close search');
  row.append(inp, x); const list = h('div', 'se-list'); box.append(row, list); stage.append(box);
  let index = [];
  const reindex = its => { index = its.map(it => ({ it, keys: [it.name, ...(it.aliases ?? [])].map(normName), cats: (it.cats ?? []).map(normName) })); };
  reindex(items);
  let addr = null, addrState = 0;   // 0 not asked, 1 loading, 2 ready, 3 none
  function search(text) {
    const q = normName(text), p = from(), dist = it => p ? Math.hypot(it.pos.x - p.x, it.pos.z - p.z) : 0;
    const named = index.map(({ it, keys, cats }) => {
      let sc = null; for (const k of keys) { const v = matchScore(q, k); if (v != null && (sc == null || v < sc)) sc = v; }
      // by kind ("gas", "school"): whole words only, after anything matched by name, nearest first
      // (a whole kind word asked for leads: "gas" means gas stations before Gascogne Avenue)
      let cat = false;
      if (q.length >= 3) for (const c of cats) if (c === q || (c.startsWith(q) && q.length >= 4)) { const v = c === q ? -0.1 : 2.5; if (sc == null || v < sc) { sc = v; cat = true; } }
      return sc == null ? null : { it, sc, d: dist(it), cat };
    }).filter(Boolean);
    const addrs = addr ? addr.find(text).map(r => ({ ...r, d: dist(r.it) })) : [];
    // a query that starts with a house number is an address: those lead
    // at most 5 matched only by kind, so places and streets by that name still show
    let nCat = 0;
    return (/^\s*\d/.test(text) ? [...addrs, ...named] : [...named, ...addrs]).sort((a, b) => a.sc - b.sc || a.d - b.d).filter(r => !r.cat || nCat++ < 5).slice(0, 8);
  }
  function render() {
    const res = search(inp.value), p = from(), seen = new Map();
    for (const { it } of res) seen.set(it.name, (seen.get(it.name) ?? 0) + 1);
    const rows = res.map(({ it, d }) => {
      // two of the same name (a chain's branches): say which street each is on instead of what it is
      const what = seen.get(it.name) > 1 && it.where ? it.where : it.kind;
      const b = h('button', 'se-item'); b.append(icon('pin', '#e53935', '#ffd0cc'), h('span', 'nm', it.name), h('span', 'dd', (what ? what + ' · ' : '') + (p ? fmt(d) : '')));
      b.onclick = () => { api.close(); onPick(it); }; return b;
    });
    // nothing typed matched exactly: say which close spelling we are offering
    if (inp.value.trim() && res.length && res[0].sc >= 3) rows.unshift(h('div', 'se-hint', `Did you mean <b>${res[0].it.name.replace(/</g, '&lt;')}</b>?`));
    if (!res.length) rows.push(h('div', 'se-none', addrState === 1 && /^\s*\d/.test(inp.value) ? 'Loading addresses…' : 'Nothing by that name on this map yet. Try a street name or the map view.'));
    list.replaceChildren(...rows);
  }
  inp.oninput = render;
  inp.onkeydown = e => { if (e.key === 'Enter') list.querySelector('.se-item')?.click(); if (e.key === 'Escape') api.close(); };
  x.onclick = () => api.close();
  const api = {
    search, setItems(its) { reindex(its); if (api.open) render(); },
    get open() { return box.classList.contains('open'); },
    show() {
      box.classList.add('open'); document.body.classList.add('searching'); inp.value = ''; render(); setTimeout(() => inp.focus(), 50);
      if (!addrState && addresses) { addrState = 1; addresses().then(a => { addr = a; addrState = a ? 2 : 3; if (api.open) render(); }, () => { addrState = 3; }); }
    },
    close() { box.classList.remove('open'); document.body.classList.remove('searching'); inp.blur(); },
  };
  return api;
}

// ---------- place card: name, a line of detail and actions
export function createChip(stage) {
  const el = h('div', 'chip panel'); el.id = 'chip'; stage.append(el);
  return {
    el,
    show({ title, sub, actions, icon: ic }) {
      el.replaceChildren(); const t = h('div', 'ch-t'); if (ic) ic.classList.add('pix'); t.append(ic ?? icon('pin', '#e53935', '#ffd0cc'), h('b', null, title)); el.append(t);
      if (sub) el.append(h('div', 'ch-s', sub));
      const r = h('div', 'ch-a'); for (const a of actions) { const b = h('button', 'tile' + (a.primary ? ' go' : '')); b.textContent = a.label; b.onclick = a.fn; r.append(b); }
      el.append(r); el.classList.add('open'); document.body.classList.add('chip-open');
    },
    hide() { el.classList.remove('open'); document.body.classList.remove('chip-open'); },
    get open() { return el.classList.contains('open'); },
  };
}
