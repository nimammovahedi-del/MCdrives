// Map Craft homepage: draws the blocky asphalt logo and pixel icons, and runs the small popups.
(() => {
  // Old links to the map went to the site root with query options (?to=, ?trip=, ...). Send them on to the map.
  if (document.body.dataset.page === 'title' && location.search &&
      /[?&](to|trip|mode|car|cam|sky|ui|layout|traffic|units|route|map|htiles|aa|dpr|fps|budget)=/.test(location.search)) {
    location.replace('map' + location.search + location.hash); return;
  }

  const G = {
    A: '01110 10001 10001 11111 10001 10001 10001', B: '11110 10001 10001 11110 10001 10001 11110', C: '01111 10000 10000 10000 10000 10000 01111',
    D: '11110 10001 10001 10001 10001 10001 11110', E: '11111 10000 10000 11110 10000 10000 11111', F: '11111 10000 10000 11110 10000 10000 10000',
    G: '01111 10000 10000 10111 10001 10001 01111', H: '10001 10001 10001 11111 10001 10001 10001', I: '111 010 010 010 010 010 111',
    K: '10001 10010 10100 11000 10100 10010 10001', L: '10000 10000 10000 10000 10000 10000 11111', M: '10001 11011 10101 10101 10001 10001 10001',
    N: '10001 11001 10101 10011 10001 10001 10001', O: '01110 10001 10001 10001 10001 10001 01110', P: '11110 10001 10001 11110 10000 10000 10000',
    R: '11110 10001 10001 11110 10100 10010 10001', S: '01111 10000 10000 01110 00001 00001 11110', T: '11111 00100 00100 00100 00100 00100 00100',
    U: '10001 10001 10001 10001 10001 10001 01110', V: '10001 10001 10001 10001 10001 01010 00100', W: '10001 10001 10001 10101 10101 10101 01010',
    Y: '10001 10001 01010 00100 00100 00100 00100', ' ': '000 000 000 000 000 000 000' };

  // Each lit pixel of the letters is a little block of asphalt: a rough grey face, a dark extruded side,
  // a white edge line along the top row and a yellow lane dash through the middle row.
  function drawLogo(c) {
    const text = (c.dataset.text || 'MAP CRAFT').toUpperCase(), stack = c.dataset.stack === 'auto' ? innerWidth < 760 : c.dataset.stack === '1';
    const words = stack ? text.split(' ') : [text], S = +(c.dataset.px || (stack ? 13 : 17)), D = Math.round(S * .45), gap = 1, pad = 1;
    const glyph = ch => (G[ch] || G[' ']).split(' ');
    const lineW = w => [...w].reduce((a, ch) => a + glyph(ch)[0].length + gap, -gap);
    const W = Math.max(...words.map(lineW));
    c.width = (W + pad * 2) * S + D; c.height = (words.length * 8 - 1 + pad * 2) * S + D;
    const x = c.getContext('2d'); let seed = 7; const R = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    const cells = [];
    words.forEach((w, li) => { let cx = pad + Math.floor((W - lineW(w)) / 2);
      for (const ch of w) { const rows = glyph(ch); rows.forEach((r, ry) => [...r].forEach((b, rx) => { if (b === '1') cells.push([cx + rx, pad + li * 8 + ry, ry]); })); cx += rows[0].length + gap; } });
    for (let d = D; d > 0; d--) { x.fillStyle = d === D ? '#0a0a0a' : '#1f1f22'; for (const [i, j] of cells) x.fillRect(i * S + d, j * S + d, S, S); }
    for (const [i, j, ry] of cells) {
      const v = (c.dataset.tone === 'light' ? 128 : 58) + R() * 22 | 0; x.fillStyle = `rgb(${v},${v},${v + 3})`; x.fillRect(i * S, j * S, S, S);
      if (ry === 3) { x.fillStyle = '#ffd23a'; x.fillRect(i * S + S * .15, j * S + S * .42, S * .55, S * .16); }
      if (ry === 0) { x.fillStyle = 'rgba(255,255,255,.85)'; x.fillRect(i * S, j * S + 1, S, S * .14); }
      x.strokeStyle = 'rgba(0,0,0,.35)'; x.lineWidth = 1; x.strokeRect(i * S + .5, j * S + .5, S - 1, S - 1);
    }
  }

  const ICON = {
    car:   ['........', '..####..', '.#oooo#.', '########', '#y####y#', '########', '.#....#.', '........'],
    phone: ['..####..', '..#oo#..', '..#oo#..', '..#oo#..', '..#oo#..', '..####..', '..#yy#..', '..####..'],
    map:   ['########', '#g#gg#g#', '#gyyyyg#', '#g#gg#y#', '#gg#g#y#', '#yyyyyy#', '#g#gg#g#', '########'],
    gear:  ['...##...', '.######.', '.##..##.', '###..###', '###..###', '.##..##.', '.######.', '...##...'],
    book:  ['.######.', '#oooooo#', '#o####o#', '#oooooo#', '#o###oo#', '#oooooo#', '#yyyyyy#', '.######.'] };
  const COL = { '#': '#f2f2f2', o: '#7fc6ff', y: '#ffd23a', g: '#6cc34b' };
  function drawIcon(cv) { const m = ICON[cv.dataset.i]; if (!m) return; cv.width = cv.height = 8; const x = cv.getContext('2d');
    m.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== '.') { x.fillStyle = COL[ch]; x.fillRect(i, j, 1, 1); } })); }

  const draw = () => { document.querySelectorAll('canvas.logo').forEach(drawLogo); };
  document.querySelectorAll('canvas.ico').forEach(drawIcon);
  draw(); let lastStack = innerWidth < 760;
  addEventListener('resize', () => { if ((innerWidth < 760) !== lastStack) { lastStack = !lastStack; draw(); } });

  // popups
  document.querySelectorAll('[data-pop]').forEach(b => b.addEventListener('click', e => { e.preventDefault(); document.getElementById(b.dataset.pop).classList.add('show'); }));
  document.querySelectorAll('.pop').forEach(p => p.addEventListener('click', e => { if (e.target === p || e.target.closest('[data-close]')) p.classList.remove('show'); }));
  addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelectorAll('.pop.show').forEach(p => p.classList.remove('show')); });

  // optional background video: shown only once it can play, never on save-data or reduced motion
  const v = document.querySelector('video[data-src]');
  if (v && !matchMedia('(prefers-reduced-motion: reduce)').matches && !(navigator.connection && navigator.connection.saveData)) {
    v.addEventListener('canplay', () => { v.style.opacity = 1; v.play().catch(() => {}); }, { once: true });
    v.src = v.dataset.src;
  }
})();
