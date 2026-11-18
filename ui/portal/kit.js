// Fletchery's page kit, loaded before portal.js and live.js:
//  - every card folds: its heading is a button; what is folded is kept on this device (localStorage);
//  - long lists show their first few rows and a "Show N more" button; open, a list scrolls and can be resized.
(function () {
  'use strict';
  const FOLD_KEY = 'fletchery-folded';
  const FOLDED_FIRST = ['h-liss', 'h-lrun', 'h-jobs'];   // folded until someone opens them
  let folded;
  try { folded = JSON.parse(localStorage.getItem(FOLD_KEY) || 'null'); } catch (e) { folded = null; }
  if (!Array.isArray(folded)) folded = FOLDED_FIRST.slice();
  const save = () => { try { localStorage.setItem(FOLD_KEY, JSON.stringify(folded)); } catch (e) { /* private mode */ } };

  function setFolded(card, h2, button, on) {
    card.classList.toggle('folded', on);
    button.setAttribute('aria-expanded', String(!on));
    const i = folded.indexOf(h2.id);
    if (on && i < 0) folded.push(h2.id);
    if (!on && i >= 0) folded.splice(i, 1);
  }
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
    setFolded(card, h2, button, folded.includes(h2.id));
    button.addEventListener('click', () => { setFolded(card, h2, button, !card.classList.contains('folded')); save(); });
  }
  document.querySelectorAll('main .card').forEach(foldable);

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
