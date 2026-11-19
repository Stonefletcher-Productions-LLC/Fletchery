// Fletchery's page kit, loaded before portal.js and live.js:
//  - every card folds: its heading is a button;
//  - every card moves: drag it by the grip (⠿) in its heading, up and down and between columns, or focus the grip and
//    use the arrow keys. A wide window has 1-3 columns (Activity > Page layout);
//  - where the cards sit and what is folded is saved on the PC (GET/POST /api/layout, a file beside the profile, so
//    a new install keeps it) and on this device (localStorage), which is all a read-only phone keeps;
//  - long lists show their first few rows and a "Show N more" button; open, a list scrolls and can be resized.
(function () {
  'use strict';
  const FOLD_KEY = 'fletchery-folded';
  const LAYOUT_KEY = 'fletchery-layout';
  const FOLDED_FIRST = ['h-liss', 'h-lrun'];   // folded until someone opens them
  const read = key => { try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; } };
  const keep = (key, v) => { try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* private mode */ } };
  let folded = read(FOLD_KEY);
  if (!Array.isArray(folded)) folded = FOLDED_FIRST.slice();
  const save = () => { keep(FOLD_KEY, folded); saveLayout(); };

  const say = (() => {   // what a keyboard move did, for screen readers
    const live = document.createElement('p');
    live.className = 'sr-only';
    live.setAttribute('aria-live', 'polite');
    document.body.append(live);
    return text => { live.textContent = ''; setTimeout(() => { live.textContent = text; }, 30); };
  })();

  const folds = new Map();   // heading id -> {card, h2, button}
  function setFolded(card, h2, button, on) {
    card.classList.toggle('folded', on);
    button.setAttribute('aria-expanded', String(!on));
    const i = folded.indexOf(h2.id);
    if (on && i < 0) folded.push(h2.id);
    if (!on && i >= 0) folded.splice(i, 1);
  }
  const title = card => (card.querySelector(':scope > h2 .fold') || card.querySelector(':scope > h2') || {}).firstChild?.textContent?.trim() || 'this';
  function foldable(card) {
    const h2 = card.querySelector(':scope > h2[id]');
    if (!h2 || card.dataset.fold === 'no' || card.querySelector(':scope > .card-body')) return;
    const body = document.createElement('div');
    body.className = 'card-body';
    body.id = h2.id + '-body';
    while (h2.nextSibling) body.append(h2.nextSibling);
    card.append(body);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'fold';
    button.setAttribute('aria-controls', body.id);
    button.append(...h2.childNodes);
    const chev = document.createElement('span');
    chev.className = 'chev';
    chev.setAttribute('aria-hidden', 'true');
    button.append(chev);
    h2.append(button);
    folds.set(h2.id, {card, h2, button});
    setFolded(card, h2, button, folded.includes(h2.id));
    button.addEventListener('click', () => { setFolded(card, h2, button, !card.classList.contains('folded')); save(); });
  }
  document.querySelectorAll('main .card').forEach(foldable);

  // ---- where the cards sit ----
  const MAX_COLS = 3;
  const views = [...document.querySelectorAll('main > .view[id^="view-"]')];
  const ORDER = new Map([...document.querySelectorAll('main .card[id]')].map((c, i) => [c.id, i]));   // the page's own order
  let columns = 2;
  let canSave = false;
  let saved = read(LAYOUT_KEY);   // {version, columns, views: {live: [[ids], ...]}, folded}
  const viewName = view => view.id.slice(5);
  const colsOf = view => view.querySelector(':scope > .cols');
  const cardsOf = view => [...view.querySelectorAll(':scope > .cols > .col > .card[id]')];

  // The page's own arrangement: Live and Builds split in two (as they always were), Queue and Activity in one.
  function defaults(view, cards, n) {
    cards = cards.slice().sort((a, b) => ORDER.get(a.id) - ORDER.get(b.id));
    const out = Array.from({length: n}, () => []);
    if (n === 1 || !['live', 'builds'].includes(viewName(view))) { out[0] = cards.map(c => c.id); return out; }
    const per = Math.ceil(cards.length / n);
    cards.forEach((c, i) => out[Math.min(n - 1, Math.floor(i / per))].push(c.id));
    return out;
  }

  // Put each tab's cards into `columns` columns: the saved order first, then any card the save does not know yet.
  function arrange() {
    for (const view of views) {
      let box = colsOf(view);
      const cards = box ? cardsOf(view) : [...view.querySelectorAll(':scope > .card[id]')];
      if (!cards.length) continue;
      if (!box) {
        box = document.createElement('div');
        box.className = 'cols';
        cards[0].before(box);
      }
      const plan = (saved && saved.views && Array.isArray(saved.views[viewName(view)]) ? saved.views[viewName(view)] : defaults(view, cards, columns))
        .map(col => (Array.isArray(col) ? col : []));
      const byId = new Map(cards.map(c => [c.id, c]));
      const out = Array.from({length: columns}, () => []);
      plan.forEach((col, i) => col.forEach(id => { if (byId.has(id)) { out[Math.min(i, columns - 1)].push(byId.get(id)); byId.delete(id); } }));
      for (const card of byId.values()) {   // new on this page: where the page's own order would put it
        const home = defaults(view, cards, columns).findIndex(col => col.includes(card.id));
        out[Math.max(0, home)].push(card);
      }
      while (box.children.length > columns) box.lastElementChild.remove();
      while (box.children.length < columns) { const c = document.createElement('div'); c.className = 'col'; box.append(c); }
      out.forEach((list, i) => box.children[i].append(...list));
      box.style.setProperty('--cols', String(columns));
    }
    document.querySelectorAll('.layout-cols button').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.cols) === columns)));
  }

  function current() {
    const out = {version: 1, columns, views: {}, folded: folded.slice()};
    for (const view of views) {
      const box = colsOf(view);
      if (box) out.views[viewName(view)] = [...box.children].map(col => [...col.querySelectorAll(':scope > .card[id]')].map(c => c.id));
    }
    return out;
  }

  let timer = 0;
  function saveLayout(now) {
    if (!views.some(colsOf)) return;
    saved = current();
    keep(LAYOUT_KEY, saved);
    clearTimeout(timer);
    if (!canSave) return;
    const send = () => fetch('/api/layout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({layout: saved})})
      .then(r => { if (!r.ok) throw new Error(r.status); note('Saved on the PC.'); })
      .catch(() => note('Kept on this screen only: the PC did not take it.', true));
    if (now) send(); else timer = setTimeout(send, 500);
  }
  function note(text, bad) {
    const m = document.getElementById('layoutMsg');
    if (m) { m.textContent = text; m.classList.toggle('bad', !!bad); }
  }

  function apply(layout) {
    if (!layout || typeof layout !== 'object') return;
    saved = layout;
    columns = Math.min(MAX_COLS, Math.max(1, Number(layout.columns) || 2));
    if (Array.isArray(layout.folded)) {
      folded = layout.folded.slice();
      keep(FOLD_KEY, folded);
      for (const {card, h2, button} of folds.values()) setFolded(card, h2, button, folded.includes(h2.id));
    }
    arrange();
  }

  if (saved) apply(saved); else arrange();
  fetch('/api/layout', {cache: 'no-store', headers: {Accept: 'application/json'}})
    .then(r => (r.ok ? r.json() : null))
    .then(d => {
      if (!d) return;
      canSave = !!d.can_save;
      if (d.layout) { apply(d.layout); keep(LAYOUT_KEY, d.layout); } else if (canSave && read(LAYOUT_KEY)) saveLayout(true);   // first time: this screen's arrangement goes to the PC
      const where = document.getElementById('layoutWhere');
      if (where) where.hidden = !canSave;
    })
    .catch(() => {});

  // Columns and reset (Activity > Page layout).
  document.querySelectorAll('.layout-cols button').forEach(b => b.addEventListener('click', () => {
    const n = Number(b.dataset.cols);
    if (n === columns) return;
    const before = current();
    columns = n;
    // keep each column's cards; a column that goes away gives its cards to the last one left
    for (const [name, cols] of Object.entries(before.views)) {
      const next = Array.from({length: n}, (_, i) => (cols[i] || []).slice());
      cols.slice(n).forEach(col => next[n - 1].push(...col));
      before.views[name] = next;
    }
    saved = {...before, columns: n};
    arrange();
    saveLayout(true);
  }));
  document.getElementById('layoutReset')?.addEventListener('click', () => {
    saved = null;
    columns = 2;
    try { localStorage.removeItem(LAYOUT_KEY); } catch (e) { /* private mode */ }
    arrange();
    if (canSave) fetch('/api/layout/reset', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
      .then(r => note(r.ok ? 'Back to the usual layout.' : 'The PC did not take it.', !r.ok)).catch(() => note('The PC did not take it.', true));
    else note('Back to the usual layout.');
  });

  // ---- moving a card: drag the grip, or arrow keys on it ----
  function grip(card) {
    const h2 = card.querySelector(':scope > h2');
    if (!h2 || !card.id || card.querySelector(':scope > h2 > .grip')) return;
    const g = document.createElement('button');
    g.type = 'button';
    g.className = 'grip';
    g.setAttribute('aria-label', 'Move the ' + title(card) + ' card. Arrow keys move it; left and right change column.');
    g.title = 'Drag to move this card';
    g.textContent = '⠿';
    h2.prepend(g);
    g.addEventListener('keydown', e => {
      const col = card.parentElement;
      if (!col || !col.classList.contains('col')) return;
      const cols = [...col.parentElement.children];
      const at = cols.indexOf(col);
      const sib = up => { let s = up ? card.previousElementSibling : card.nextElementSibling; while (s && s.hidden) s = up ? s.previousElementSibling : s.nextElementSibling; return s; };
      let done = '';
      if (e.key === 'ArrowUp' && sib(true)) { sib(true).before(card); done = 'up'; }
      else if (e.key === 'ArrowDown' && sib(false)) { sib(false).after(card); done = 'down'; }
      else if (e.key === 'ArrowLeft' && at > 0) { cols[at - 1].append(card); done = 'to column ' + at; }
      else if (e.key === 'ArrowRight' && at < cols.length - 1) { cols[at + 1].append(card); done = 'to column ' + (at + 2); }
      else if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault();
      g.focus();
      if (done) { say(title(card) + ' moved ' + done + '.'); saveLayout(); }
    });
    g.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      e.preventDefault();
      drag(card, g, e);
    });
  }

  function drag(card, g, e) {
    const view = card.closest('.view');
    const box = card.getBoundingClientRect();
    const dx = e.clientX - box.left, dy = e.clientY - box.top;
    const hole = document.createElement('div');
    hole.className = 'card-hole';
    hole.style.height = box.height + 'px';
    card.before(hole);
    card.classList.add('dragging');
    Object.assign(card.style, {width: box.width + 'px', left: box.left + 'px', top: box.top + 'px'});
    document.body.classList.add('arranging');
    let last = e;
    let scroll = 0;
    const place = ev => {
      card.style.left = (ev.clientX - dx) + 'px';
      card.style.top = (ev.clientY - dy) + 'px';
      const under = document.elementFromPoint(ev.clientX, ev.clientY);
      const col = under && under.closest('.col');
      if (!col || col.closest('.view') !== view) return;
      const other = under.closest('.card[id]');
      if (other && other !== card && other.parentElement === col) {
        const r = other.getBoundingClientRect();
        if (ev.clientY < r.top + r.height / 2) other.before(hole); else other.after(hole);
      } else if (!other && under.closest('.card-hole') !== hole) {
        const below = [...col.children].find(c => c !== hole && c !== card && c.getBoundingClientRect().top > ev.clientY);
        if (below) below.before(hole); else col.append(hole);
      }
    };
    const edge = () => {   // near the top or bottom of the window: scroll
      const y = last.clientY, h = innerHeight;
      const by = y < 60 ? -12 : y > h - 60 ? 12 : 0;
      if (by) { scrollBy(0, by); place(last); }
      scroll = requestAnimationFrame(edge);
    };
    scroll = requestAnimationFrame(edge);
    const move = ev => { last = ev; place(ev); };
    const end = () => {
      cancelAnimationFrame(scroll);
      removeEventListener('pointermove', move);
      removeEventListener('pointerup', end);
      removeEventListener('pointercancel', end);
      hole.replaceWith(card);
      card.classList.remove('dragging');
      card.style.width = card.style.left = card.style.top = '';
      document.body.classList.remove('arranging');
      saveLayout();
    };
    addEventListener('pointermove', move);
    addEventListener('pointerup', end);
    addEventListener('pointercancel', end);
  }
  views.forEach(view => cardsOf(view).forEach(grip));

  // ---- long lists ----
  const open = new Set();   // lists opened in full on this page
  // Keep the first `n` rows of `node` (a <ul> or <ol> already filled) and add a button for the rest.
  function cap(node, n) {
    const rows = [...node.children].filter(li => !li.classList.contains('more'));
    node.querySelectorAll(':scope > li.more').forEach(li => li.remove());
    const all = open.has(node.id);
    node.classList.toggle('all', all && rows.length > n);
    rows.forEach((li, i) => { li.hidden = !all && i >= n; });
    if (rows.length <= n) return;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'small more-btn';
    b.textContent = all ? 'Show fewer' : 'Show ' + (rows.length - n) + ' more';
    b.setAttribute('aria-controls', node.id);
    b.addEventListener('click', () => {
      if (open.has(node.id)) open.delete(node.id); else open.add(node.id);
      cap(node, n);
      if (!open.has(node.id)) node.closest('.card')?.scrollIntoView({block: 'nearest'});
    });
    const li = document.createElement('li');
    li.className = 'more';
    li.append(b);
    node.append(li);
  }
  window.fletcheryKit = {cap};
})();
