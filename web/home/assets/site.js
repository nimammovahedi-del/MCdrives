// Map Craft inner pages: pixel logo, compare slider, level tour with hotspots, feature dialogs, day/night toggle.
(() => {
  const one = (s, r = document) => r.querySelector(s), all = (s, r = document) => [...r.querySelectorAll(s)];

  // pixel logo: dark letters, green lane row
  const G = { A: '01110 10001 10001 11111 10001 10001 10001', C: '01111 10000 10000 10000 10000 10000 01111', F: '11111 10000 10000 11110 10000 10000 10000',
    M: '10001 11011 10101 10101 10001 10001 10001', P: '11110 10001 10001 11110 10000 10000 10000', R: '11110 10001 10001 11110 10100 10010 10001',
    T: '11111 00100 00100 00100 00100 00100 00100', ' ': '00 00 00 00 00 00 00' };
  all('canvas.logo').forEach(c => { const t = 'MAP CRAFT', S = 4, D = 2; let w = -1; for (const ch of t) w += G[ch].split(' ')[0].length + 1;
    c.width = w * S + D; c.height = 7 * S + D; const x = c.getContext('2d'); let cx = 0;
    for (const ch of t) { const rows = G[ch].split(' ');
      rows.forEach((r, ry) => [...r].forEach((b, rx) => { if (b !== '1') return; x.fillStyle = '#c4c0b7'; x.fillRect((cx + rx) * S + D, ry * S + D, S, S);
        x.fillStyle = ry === 3 ? '#2f7d32' : '#161616'; x.fillRect((cx + rx) * S, ry * S, S, S); })); cx += rows[0].length + 1; } });

  // compare slider
  all('.cmp').forEach(el => { const set = e => { const r = el.getBoundingClientRect(); el.classList.remove('auto'); el.style.setProperty('--x', Math.max(3, Math.min(97, (e.clientX - r.left) / r.width * 100)) + '%'); };
    let down = false; el.addEventListener('pointerdown', e => { down = true; el.setPointerCapture(e.pointerId); set(e); });
    el.addEventListener('pointermove', e => { if (down || e.pointerType === 'mouse') set(e); }); el.addEventListener('pointerup', () => down = false);
    el.addEventListener('keydown', e => { if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return; const cur = parseFloat(el.style.getPropertyValue('--x')) || 50;
      el.classList.remove('auto'); el.style.setProperty('--x', Math.max(3, Math.min(97, cur + (e.key === 'ArrowLeft' ? -5 : 5))) + '%'); }); });

  // level tour
  const tour = one('[data-tour]');
  if (tour) {
    const L = JSON.parse(one('#tourdata').textContent), btns = all('.lvb', tour), img = one('.frame img', tour), frame = one('.frame', tour);
    const title = one('.info .h2', tour), lead = one('.info .lead', tour), tag = one('.info .tag', tour), goal = one('.goal', tour), goalT = one('.goal span', tour), calls = one('.calls', tour), prog = one('.prog i', tour), next = one('[data-next]', tour);
    let cur = 0;
    const hi = n => { all('.pin', frame).forEach(p => p.classList.toggle('on', +p.dataset.n === n)); all('li', calls).forEach(li => li.classList.toggle('on', +li.dataset.n === n)); };
    const show = i => {
      cur = i; const d = L[i];
      btns.forEach((b, j) => { b.setAttribute('aria-selected', j === i); b.classList.toggle('done', j < i); });
      img.src = d.img; img.style.objectPosition = d.pos || 'top'; tag.textContent = 'Level ' + (i + 1); title.textContent = d.title; if (lead) lead.textContent = d.lead || ''; goalT.textContent = d.goal;
      goal.classList.remove('ok'); prog.style.width = (i + 1) * 25 + '%';
      all('.pin', frame).forEach(p => p.remove()); calls.innerHTML = '';
      d.pins.forEach(([x, y, label, desc], j) => {
        const p = document.createElement('button'); p.className = 'pin'; p.dataset.n = j + 1; p.textContent = j + 1; p.style.left = x + '%'; p.style.top = y + '%'; p.setAttribute('aria-label', label);
        p.onmouseenter = p.onfocus = () => hi(j + 1); p.onclick = () => hi(j + 1); frame.append(p);
        const li = document.createElement('li'); li.dataset.n = j + 1; li.innerHTML = `<span class="pn">${j + 1}</span><span><b>${label}</b>${desc ? `<small>${desc}</small>` : ''}</span>`; li.onmouseenter = () => hi(j + 1); calls.append(li);
      });
      next.innerHTML = i < L.length - 1 ? `Next level <svg viewBox="0 0 3 6" aria-hidden="true"><rect width="1" height="6" fill="currentColor"/><rect x="1" y="1" width="1" height="4" fill="currentColor"/><rect x="2" y="2" width="1" height="2" fill="currentColor"/></svg>` : 'Start over';
    };
    btns.forEach((b, i) => b.addEventListener('click', () => show(i)));
    next.addEventListener('click', () => { goal.classList.add('ok'); setTimeout(() => show((cur + 1) % L.length), 380); });
    frame.addEventListener('mouseleave', () => hi(0));
    show(0);
  }

  // feature dialogs
  const dlg = one('dialog.dlg');
  if (dlg) {
    all('[data-feat]').forEach(c => c.addEventListener('click', () => {
      const f = JSON.parse(c.dataset.feat);
      const src = c.querySelector('img'); one('.im img', dlg).src = src.src; one('.im', dlg).classList.toggle('wide', src.naturalWidth > src.naturalHeight); one('.im img', dlg).alt = f.title;
      one('.tag', dlg).textContent = f.tag; one('.h2', dlg).textContent = f.title; const dd = one('.d', dlg); if (dd) dd.textContent = f.d || '';
      one('.chips', dlg).innerHTML = f.chips.map(t => `<span>${t}</span>`).join('');
      one('[data-try]', dlg).href = f.href || 'map';
      dlg.showModal();
    }));
    one('.x', dlg).addEventListener('click', () => dlg.close());
    dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
  }

  // day / night
  const dn = one('[data-dn]');
  if (dn) {
    const pic = one('.dn', dn), bs = all('.seg button', dn), clock = one('.clock', dn);
    const h = +new Intl.DateTimeFormat('en-US', { hour: 'numeric', hourCycle: 'h23', timeZone: 'America/Los_Angeles' }).format(new Date()), autoNight = h < 6 || h >= 18;
    const set = m => { const night = m === 'night' || (m === 'auto' && autoNight); pic.classList.toggle('night', night);
      bs.forEach(b => b.setAttribute('aria-pressed', b.dataset.m === m)); clock.textContent = m === 'auto' ? (night ? 'Auto · night now in OC' : 'Auto · day now in OC') : (night ? 'Night' : 'Day'); };
    bs.forEach(b => b.addEventListener('click', () => set(b.dataset.m)));
    set('day');
  }
})();

// live place search (the same places list the map uses), forgiving of typos
(() => {
  const card = document.querySelector('[data-search]'); if (!card) return;
  const input = card.querySelector('input'), dym = card.querySelector('.dym'), res = card.querySelector('.res');
  const KIND = { food: 'Food and drink', shop: 'Shop', mall: 'Shopping center', office: 'Office', fuel: 'Gas', park: 'Park', school: 'School', university: 'University', hotel: 'Hotel', hospital: 'Hospital', cinema: 'Cinema', place: 'Place' };
  const lev = (a, b) => { const m = a.length, n = b.length; let p = Array.from({ length: n + 1 }, (_, j) => j);
    for (let i = 1; i <= m; i++) { const c = [i]; for (let j = 1; j <= n; j++) c[j] = Math.min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)); p = c; } return p[n]; };
  let P = null;
  const run = () => {
    const q = input.value.trim().toLowerCase(); res.innerHTML = ''; dym.innerHTML = '';
    if (!P || q.length < 2) return;
    const tol = q.length >= 7 ? 2 : q.length >= 4 ? 1 : 0, hits = [];
    for (const p of P) {
      const nm = p[0].toLowerCase(), names = [nm, ...(p[5] || []).map(a => a.toLowerCase())];
      let s = Infinity;
      for (const n of names) { if (n.startsWith(q)) s = Math.min(s, 0); else if (n.includes(' ' + q)) s = Math.min(s, 0.5); else if (n.includes(q)) s = Math.min(s, 1);
        else if (tol) for (const w of n.split(/[\s-]+/)) for (let L = q.length - 1; L <= q.length + 1; L++) { const d = lev(q, w.slice(0, L)); if (d <= tol) s = Math.min(s, 1 + d); } }
      if (s < Infinity) hits.push([s + (p[1] === 'mall' ? -0.3 : 0) + nm.length / 400, p]);
    }
    hits.sort((a, b) => a[0] - b[0]);
    if (!hits.length) { res.innerHTML = '<li class="none">Nothing found. Try another place in Orange County.</li>'; return; }
    if (hits[0][0] >= 1.5) dym.innerHTML = `Did you mean <b>${hits[0][1][0]}</b>?`;
    res.innerHTML = hits.slice(0, 5).map(([, p]) => `<li><a href="map?place=${encodeURIComponent(p[0])}"><i></i><b>${p[0]}</b><span>${KIND[p[1]] || p[1][0].toUpperCase() + p[1].slice(1)}</span></a></li>`).join('');
  };
  fetch('places.json').then(r => r.json()).then(d => { P = d; run(); }).catch(() => { dym.textContent = 'Search loads on the live site.'; });
  input.addEventListener('input', run);
})();

// pixel icons (8x8)
(() => {
  const ICON = {
    data:  ['.######.', '#yyyyyy#', '.######.', '#gggggg#', '.######.', '#bbbbbb#', '.######.', '........'],
    bake:  ['...##...', '..#gg#..', '.#gggg#.', '#gggggg#', '#dddddd#', '#dddddd#', '.#dddd#.', '..####..'],
    pack:  ['########', '#yy#bb##', '#yy#bb##', '########', '#gg#rr##', '#gg#rr##', '########', '........'],
    graph: ['y......y', '.#....#.', '..#..#..', '...yy...', '...yy...', '..#..#..', '.#....#.', 'y......y'],
    view:  ['########', '#bbbbbb#', '#bggggb#', '#bgyygb#', '#bggggb#', '#bbbbbb#', '########', '..####..'],
    live:  ['...r....', '..rrr...', '..rrr...', '.rrrrr..', '.rr.rr..', 'rrrrrrr.', 'rrr.rrr.', 'rrrrrrr.'] };
  const COL = { '#': '#161616', y: '#e8b400', g: '#2f7d32', b: '#3d6fd6', r: '#c0392b', d: '#8a6a3d' };
  document.querySelectorAll('canvas.ico').forEach(cv => { const m = ICON[cv.dataset.i]; if (!m) return; cv.width = cv.height = 8; const x = cv.getContext('2d');
    m.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== '.') { x.fillStyle = COL[ch]; x.fillRect(i, j, 1, 1); } })); });
})();

// pipeline stepper
(() => {
  const el = document.querySelector('[data-pipe]'); if (!el) return;
  const S = JSON.parse(document.getElementById('stepdata').textContent), bs = [...el.querySelectorAll('.step')];
  const dsc = el.querySelector('.sv .d'), img = el.querySelector('.sv img'), tag = el.querySelector('.sv .tag'), h = el.querySelector('.sv .h2'), chips = el.querySelector('.sv .chips'), road = el.querySelector('.road');
  let cur = 0;
  const show = i => { cur = (i + S.length) % S.length; const s = S[cur];
    bs.forEach((b, j) => b.setAttribute('aria-selected', j === cur)); road.style.setProperty('--p', cur);
    img.style.opacity = 0; const n = new Image(); n.onload = () => { img.src = s.img; img.alt = s.t; img.classList.toggle('tall', n.naturalHeight > n.naturalWidth); img.style.opacity = 1; }; n.src = s.img;
    tag.textContent = s.n; h.textContent = s.t; if (dsc) dsc.textContent = s.d || ''; chips.innerHTML = s.chips.map(c => `<span>${c}</span>`).join(''); };
  bs.forEach((b, i) => b.addEventListener('click', () => show(i)));
  el.querySelector('[data-nextstep]').addEventListener('click', () => show(cur + 1));
  el.querySelector('[data-prev]').addEventListener('click', () => show(cur - 1));
  show(0);
})();

// data source filter
(() => {
  const g = document.querySelector('.srcs'); if (!g) return;
  const bs = [...document.querySelectorAll('.layers button')];
  bs.forEach(b => b.addEventListener('click', () => { const l = b.dataset.l; bs.forEach(x => x.setAttribute('aria-pressed', x === b));
    g.classList.toggle('f', !!l); g.querySelectorAll('.src').forEach(s => s.classList.toggle('lit', !!l && s.dataset.ls.split(' ').includes(l))); }));
})();

// chunk map (the built chunks of the area's grid, trimmed to their extent, shaded by detail)
(() => {
  const cm = document.getElementById('chunkmap'); if (!cm) return;
  const rows = JSON.parse(document.getElementById('chunkdata').textContent), by = Object.fromEntries(rows.map(r => [r[0], r]));
  const q = rows.map(r => r[1]), lo = Math.min(...q), hi = Math.max(...q), tip = document.getElementById('chunktip');
  const S = [[207, 227, 194], [111, 174, 90], [47, 125, 50], [20, 64, 26]];
  const ramp = t => { const f = t * 3, i = Math.min(2, Math.floor(f)), u = f - i; return `rgb(${S[i].map((v, k) => Math.round(v + (S[i + 1][k] - v) * u)).join(',')})`; };
  const rc = rows.map(r => r[0].match(/r(\d+)c(\d+)/).slice(1).map(Number)), R0 = Math.min(...rc.map(v => v[0])), R1 = Math.max(...rc.map(v => v[0])), C0 = Math.min(...rc.map(v => v[1])), C1 = Math.max(...rc.map(v => v[1]));
  cm.style.gridTemplateColumns = `repeat(${C1 - C0 + 1}, 1fr)`; cm.style.aspectRatio = `${C1 - C0 + 1} / ${R1 - R0 + 1}`; if (C1 - C0 > 12) cm.style.gap = '1px';
  for (let r = R1; r >= R0; r--) for (let c = C0; c <= C1; c++) {
    const id = `r${r}c${c}`, d = document.createElement('div'), row = by[id];
    if (row) { d.className = 'k'; d.style.background = ramp((row[1] - lo) / (hi - lo));
      const say = () => { tip.innerHTML = `<b>${(row[1] / 1e6).toFixed(1)}M faces</b> · ${row[2].toFixed(0)} MB · ${row[3]} km²`; };
      d.addEventListener('mouseenter', say); d.addEventListener('click', say); }
    cm.append(d);
  }
})();

// streaming radius demo
(() => {
  const el = document.querySelector('[data-stream]'); if (!el) return;
  const cv = el.querySelector('canvas'), x = cv.getContext('2d'), inp = el.querySelector('input'), per = +el.dataset.per, city = +el.dataset.city;
  const draw = () => {
    const r = +inp.value, W = cv.width, n = 9, cell = W / n, sq = 512, scale = cell / sq, cx = W / 2 + cell * 0.18, cy = W / 2 - cell * 0.12;
    x.clearRect(0, 0, W, W); let count = 0;
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
      const x0 = i * cell, y0 = j * cell, nx = Math.max(x0, Math.min(cx, x0 + cell)), ny = Math.max(y0, Math.min(cy, y0 + cell));
      const hit = Math.hypot(nx - cx, ny - cy) / scale <= r; if (hit) count++;
      x.fillStyle = hit ? '#2f7d32' : '#f6f5f1'; x.fillRect(x0 + 1.5, y0 + 1.5, cell - 3, cell - 3);
      if (hit) { x.fillStyle = 'rgba(0,0,0,.18)'; x.fillRect(x0 + 1.5, y0 + cell - 5, cell - 3, 3.5); }
    }
    x.strokeStyle = '#161616'; x.lineWidth = 2; x.setLineDash([6, 5]); x.beginPath(); x.arc(cx, cy, r * scale, 0, 7); x.stroke(); x.setLineDash([]);
    x.fillStyle = '#161616'; x.fillRect(cx - 7, cy - 7, 14, 14); x.fillStyle = '#e8b400'; x.fillRect(cx - 3, cy - 3, 6, 6);
    const mb = count * per;
    el.querySelector('.r').textContent = r.toLocaleString(); el.querySelector('.nsq').textContent = count;
    el.querySelector('.mb').textContent = mb < 100 ? Math.round(mb) + ' MB' : (mb / 1000).toFixed(2) + ' GB';
    el.querySelector('.fill').style.width = Math.max(0.5, mb / city * 100) + '%';
  };
  inp.addEventListener('input', draw); draw();
})();

// before/after fixes on How It's Built
(() => {
  const el = document.querySelector('[data-fixes]'); if (!el) return;
  const D = JSON.parse(document.getElementById('fixdata').textContent), bs = [...el.querySelectorAll('.seg button')];
  const [a, b] = el.querySelectorAll('.cmp img'), cap = el.querySelector('.cp');
  const show = k => { a.src = `assets/img/fix-${k}-before.jpg`; b.src = `assets/img/fix-${k}-after.jpg`; cap.textContent = D[k].c;
    bs.forEach(x => x.setAttribute('aria-pressed', x.dataset.k === k)); };
  bs.forEach(x => x.addEventListener('click', () => show(x.dataset.k)));
  show(bs[0].dataset.k);
})();
