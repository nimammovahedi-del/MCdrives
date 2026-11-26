// Map Craft inner pages: nav logo, map-vs-blocks slider, feature tabs, chunk map and pixel icons.
(() => {
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // ---- pixel logo (light letters, yellow lane row)
  const G = { A: '01110 10001 10001 11111 10001 10001 10001', C: '01111 10000 10000 10000 10000 10000 01111', F: '11111 10000 10000 11110 10000 10000 10000',
    M: '10001 11011 10101 10101 10001 10001 10001', P: '11110 10001 10001 11110 10000 10000 10000', R: '11110 10001 10001 11110 10100 10010 10001',
    T: '11111 00100 00100 00100 00100 00100 00100', ' ': '00 00 00 00 00 00 00' };
  $$('canvas.logo').forEach(c => {
    const t = 'MAP CRAFT', S = 4, D = 2; let w = -1; for (const ch of t) w += G[ch].split(' ')[0].length + 1;
    c.width = w * S + D; c.height = 7 * S + D; const x = c.getContext('2d'); let cx = 0;
    for (const ch of t) { const rows = G[ch].split(' ');
      rows.forEach((r, ry) => [...r].forEach((b, rx) => { if (b !== '1') return;
        x.fillStyle = '#000'; x.fillRect((cx + rx) * S + D, ry * S + D, S, S);
        x.fillStyle = ry === 3 ? '#ffd23a' : '#e9e9ec'; x.fillRect((cx + rx) * S, ry * S, S, S); }));
      cx += rows[0].length + 1; }
  });

  // ---- small pixel icons (8x8)
  const ICON = {
    data:  ['.######.', '#yyyyyy#', '.######.', '#gggggg#', '.######.', '#bbbbbb#', '.######.', '........'],
    bake:  ['...##...', '..#gg#..', '.#gggg#.', '#gggggg#', '#dddddd#', '#dddddd#', '.#dddd#.', '..####..'],
    pack:  ['########', '#yy#bb##', '#yy#bb##', '########', '#gg#rr##', '#gg#rr##', '########', '........'],
    graph: ['y......y', '.#....#.', '..#..#..', '...yy...', '...yy...', '..#..#..', '.#....#.', 'y......y'],
    view:  ['########', '#bbbbbb#', '#bggggb#', '#bgyygb#', '#bggggb#', '#bbbbbb#', '########', '..####..'],
    live:  ['...r....', '..rrr...', '..rrr...', '.rrrrr..', '.rr.rr..', 'rrrrrrr.', 'rrr.rrr.', 'rrrrrrr.'],
    ship:  ['...##...', '..#yy#..', '..#yy#..', '.##yy##.', '#.#yy#.#', '..#yy#..', '..r##r..', '...rr...'] };
  const COL = { '#': '#e9e9ec', y: '#ffd23a', g: '#5fae3c', b: '#3d8bff', r: '#ff5a4f', d: '#8a6a3d' };
  $$('canvas.ico').forEach(cv => { const m = ICON[cv.dataset.i]; if (!m) return; cv.width = cv.height = 8; const x = cv.getContext('2d');
    m.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== '.') { x.fillStyle = COL[ch]; x.fillRect(i, j, 1, 1); } })); });

  // ---- compare slider: sweeps on its own until touched, then follows the pointer
  $$('.cmp').forEach(el => {
    const set = e => { const r = el.getBoundingClientRect(); const p = Math.max(3, Math.min(97, (e.clientX - r.left) / r.width * 100));
      el.classList.remove('auto'); el.style.setProperty('--x', p + '%'); };
    let down = false;
    el.addEventListener('pointerdown', e => { down = true; el.setPointerCapture(e.pointerId); set(e); });
    el.addEventListener('pointermove', e => { if (down || e.pointerType === 'mouse') set(e); });
    el.addEventListener('pointerup', () => { down = false; });
    el.addEventListener('keydown', e => { const cur = parseFloat(getComputedStyle(el).getPropertyValue('--x')) || 50;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { el.classList.remove('auto'); el.style.setProperty('--x', Math.max(3, Math.min(97, cur + (e.key === 'ArrowLeft' ? -5 : 5))) + '%'); } });
  });

  // ---- tabs (also opened from the level cards and from #tab-name links)
  const tabs = $$('.tab'), panes = $$('.pane');
  const show = (k, focus) => { tabs.forEach(t => t.setAttribute('aria-selected', t.dataset.k === k)); panes.forEach(p => p.classList.toggle('on', p.dataset.k === k));
    if (focus) $('#features')?.scrollIntoView({ block: 'start' }); };
  tabs.forEach(t => t.addEventListener('click', () => show(t.dataset.k)));
  $$('[data-open]').forEach(b => b.addEventListener('click', e => { e.preventDefault(); show(b.dataset.open, true); }));
  if (tabs.length) { const h = location.hash.replace('#', ''); show(tabs.some(t => t.dataset.k === h) ? h : tabs[0].dataset.k); }

  // ---- chunk map (How It's Built): 41 built chunks of the 8x8 grid, coloured by mesh detail
  const cm = $('#chunkmap');
  if (cm) {
    const rows = JSON.parse($('#chunkdata').textContent), byId = Object.fromEntries(rows.map(r => [r[0], r]));
    const q = rows.map(r => r[1]), lo = Math.min(...q), hi = Math.max(...q);
    const ramp = t => { const s = [[35, 58, 29], [95, 174, 60], [255, 210, 58], [255, 138, 61]]; const f = t * 3, i = Math.min(2, Math.floor(f)), u = f - i;
      return `rgb(${s[i].map((v, k) => Math.round(v + (s[i + 1][k] - v) * u)).join(',')})`; };
    const tip = $('#chunktip');
    for (let r = 7; r >= 0; r--) for (let c = 0; c < 8; c++) {   // north at the top
      const id = `r${r}c${c}`, d = document.createElement('div'), row = byId[id];
      if (row) { d.className = 'k'; d.style.background = ramp((row[1] - lo) / (hi - lo));
        d.addEventListener('mouseenter', () => { tip.innerHTML = `<b>Chunk ${id}</b> · ${(row[1] / 1e6).toFixed(1)} million block faces · ${row[2].toFixed(1)} MB · ${row[3]} km²`; }); }
      else d.style.background = 'rgba(255,255,255,.03)';
      cm.append(d);
    }
  }
})();
