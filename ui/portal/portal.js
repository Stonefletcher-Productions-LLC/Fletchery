// Fletchery's phone portal: reads /api/summary and draws it. It never sends anything that changes Fletchery.
(function () {
  'use strict';
  const POLL_MS = 10000;
  const RETRY_MS = 3000;     // ask again sooner while the PC cannot be reached (Tailscale may still be connecting)
  const WAIT_MS = 12000;     // give up on one read after this long
  const KEEP = 'fletchery-portal-last';
  const KEEP_MS = 24 * 3600 * 1000;
  const TABS = ['live', 'now', 'tasks', 'merges', 'builds'];
  const $ = id => document.getElementById(id);
  let last = null;        // the newest summary
  let lastOk = 0;         // when it arrived (ms)
  let failed = '';        // why the newest read failed
  let timer = 0;

  // ---- small helpers ----
  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false || kid === '') continue;
      node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  const web = url => typeof url === 'string' && url.startsWith('https://') ? url : '';
  function link(url, cls, ...kids) {
    return web(url) ? el('a', {class: cls, href: url, target: '_blank', rel: 'noopener noreferrer'}, ...kids) : el('div', {class: cls}, ...kids);
  }
  function fill(node, rows, empty) {
    node.replaceChildren(...(rows.length ? rows : [el('li', {class: 'empty', text: empty})]));
  }
  function ago(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) { const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h + ' h' + (m ? ' ' + m + ' min' : '') + ' ago'; }
    return Math.floor(s / 86400) + ' d ago';
  }
  function lasted(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '';
    return took(Math.max(0, (Date.now() - t) / 1000));
  }
  function took(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds)) return '';
    const s = Math.round(seconds);
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.round(s / 60) + ' min';
    return Math.floor(s / 3600) + ' h ' + Math.round(s % 3600 / 60) + ' min';
  }
  function when(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '';
    const d = new Date(t), same = d.toDateString() === new Date().toDateString();
    return (same ? '' : d.toLocaleDateString([], {month: 'short', day: 'numeric'}) + ', ') + d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  }
  const num = n => el('span', {class: 'num', text: '#' + n});

  // ---- drawing ----
  function taskRow(t, chip) {
    return el('li', null,
      link(t.url, 'title', num(t.number), t.title),
      chip,
      t.why && t.why !== 'ready' ? el('p', {class: 'sub', text: t.why}) : null,
      t.pr ? el('p', {class: 'sub'}, 'Pull request ', web(t.pr_url) ? el('a', {href: t.pr_url, target: '_blank', rel: 'noopener noreferrer', text: '#' + t.pr}) : '#' + t.pr) : null);
  }
  function workerRow(w) {
    return el('li', null,
      el('div', {class: 'title'}, num(w.task), w.title),
      el('span', {class: 'chip run', text: w.stage || 'Working'}),
      w.agent ? el('span', {class: 'chip', text: w.agent}) : null,
      w.emergency ? el('span', {class: 'chip hold', text: 'Repair'}) : null,
      w.since ? el('span', {class: 'chip', text: lasted(w.since)}) : null,
      w.last ? el('p', {class: 'sub mono', text: w.last + (w.last_at ? '  · ' + ago(w.last_at) : '')}) : null);
  }
  const STEP_MARK = {done: '✓', running: '▶', failed: '✗', waiting: '…', todo: '○'};
  const STEP_WORD = {done: 'done', running: 'running now', failed: 'failed', waiting: 'waiting', todo: 'not started'};
  function mergeBlock(m, full) {
    const out = [];
    const chips = [];
    if (m.active) chips.push(el('span', {class: 'chip run', text: 'Round ' + m.round + ' running'}));
    else if (!m.on) chips.push(el('span', {class: 'chip', text: 'Merge rounds are off'}));
    else if (m.held) chips.push(el('span', {class: 'chip hold', text: 'Merging paused'}));
    else chips.push(el('span', {class: 'chip', text: 'No round running'}));
    if (m.stale) chips.push(el('span', {class: 'chip hold', text: 'No update for a while'}));
    if (m.backlog) chips.push(el('span', {class: 'chip wait', text: m.backlog + ' waiting'}));
    if (m.agent && m.active) chips.push(el('span', {class: 'chip', text: m.agent}));
    out.push(el('p', {class: 'headline', text: m.headline || (m.active ? 'A merge round is running.' : 'Nothing is merging.')}), el('div', null, chips));
    if (m.why && (!m.active || full)) out.push(el('p', {class: 'sub', text: m.why}));
    const steps = full ? m.steps : m.steps.filter(s => s.state === 'running' || s.state === 'failed');
    if (m.active && steps.length) {
      out.push(el('ol', {class: 'steps'}, steps.map(s => el('li', {class: s.state},
        el('span', {class: 'mark', 'aria-hidden': 'true', text: STEP_MARK[s.state] || '○'}),
        el('span', {class: 'name'}, s.name, el('span', {class: 'sr', text: ', ' + (STEP_WORD[s.state] || s.state)}),
          full && s.message && s.state !== 'done' ? el('p', {class: 'sub', text: s.message}) : null),
        s.seconds ? el('span', {class: 'took', text: took(s.seconds)}) : null))));
    }
    if (m.active && full && m.prs.length) out.push(el('p', {class: 'sub', text: 'In this round: ' + m.prs.map(n => '#' + n).join(', ')}));
    if (m.active && m.started) out.push(el('p', {class: 'sub', text: 'Started ' + ago(m.started) + (m.updated ? ', last update ' + ago(m.updated) : '')}));
    if (m.previous && (full || !m.active)) {
      out.push(el('p', {class: 'sub', text: 'Last round ' + m.previous.round + ': ' + (m.previous.result || 'ended') + (m.previous.ended ? ' (' + ago(m.previous.ended) + ')' : '')}));
    }
    return out;
  }
  const BUILD = {scheduled: ['Scheduled', 'wait'], waiting: ['Waiting', 'wait'], queued: ['Queued', 'wait'], running: ['Building', 'run'],
    done: ['Finished', 'run'], failed: ['Failed', 'bad'], cancelled: ['Cancelled', '']};
  function buildRow(b) {
    const [word, cls] = BUILD[b.state] || [b.state, ''];
    const time = b.finished ? 'Finished ' + when(b.finished) : b.started ? 'Started ' + when(b.started) : b.at ? 'Planned for ' + when(b.at) : '';
    const publish = {uploading: 'Uploading the download', failed: 'Upload failed', skipped: 'Not uploaded'}[b.publish];
    return el('li', null,
      el('div', {class: 'title', text: 'Build ' + b.version}),
      el('span', {class: 'chip ' + cls, text: word}),
      publish ? el('span', {class: 'chip ' + (b.publish === 'failed' ? 'bad' : 'wait'), text: publish}) : null,
      b.tests === 'full' ? el('span', {class: 'chip', text: 'Full tests'}) : null,
      b.repeat === 'daily' ? el('span', {class: 'chip', text: 'Daily'}) : null,
      el('p', {class: 'sub', text: [time, b.machine && 'on ' + b.machine, b.ref && 'from ' + b.ref + (b.sha ? ' (' + b.sha + ')' : '')].filter(Boolean).join(' · ')}),
      b.reason && !(b.state === 'done' && b.links.length) ? el('p', {class: 'sub', text: b.reason}) : null,
      b.links.length ? el('div', {class: 'links'}, b.links.filter(l => web(l.url)).map(l =>
        el('a', {href: l.url, target: '_blank', rel: 'noopener noreferrer', text: l.label}))) : null);
  }
  function machineRow(m) {
    return el('li', null,
      el('div', {class: 'title', text: m.name}),
      el('span', {class: 'chip ' + (m.online ? 'run' : 'bad'), text: m.online ? 'Online' : 'Offline'}),
      m.job ? el('span', {class: 'chip wait', text: 'Running: ' + m.job}) : m.online ? el('span', {class: 'chip', text: 'Idle'}) : null,
      m.paused.length ? el('span', {class: 'chip hold', text: 'Paused: ' + m.paused.join(', ')}) : null,
      !m.online && m.last_seen ? el('p', {class: 'sub', text: 'Last seen ' + ago(m.last_seen)}) : null);
  }

  function draw(d) {
    $('project').textContent = d.project && d.project !== 'project' ? d.project : 'Fletchery';
    document.title = (d.needs.length ? '(' + d.needs.length + ') ' : '') + 'Fletchery';
    $('alerts').replaceChildren(...d.pauses.map(p => el('div', {class: 'alert' + (p.kind === 'signed_out' || p.kind === 'sync' ? ' bad' : ''), role: 'status'},
      el('strong', {text: {signed_out: 'Sign-in needed', held: 'Paused', limit: 'Usage limit', restarting: 'Restarting', sync: 'GitHub trouble'}[p.kind] || 'Note'}), p.text)));
    const stat = (n, label, hot) => el('div', {class: 'stat' + (hot && n ? ' hot' : '')}, el('b', {text: String(n)}), el('span', {text: label}));
    $('stats').replaceChildren(stat(d.needs.length, 'Needs you', true), stat(d.workers.length, 'Working'), stat(d.queued.length, 'Queued'), stat(d.merge.waiting.length, 'To merge'));
    const badge = $('b-now');
    badge.hidden = !d.needs.length;
    badge.textContent = d.needs.length;
    badge.setAttribute('aria-label', d.needs.length + ' need you');

    const needs = () => d.needs.map(t => taskRow(t, el('span', {class: 'chip needs', text: 'Needs you'})));
    fill($('needs'), needs(), 'Nothing needs you.');
    fill($('workers'), d.workers.map(workerRow), 'No worker is running right now.');
    $('mergeMini').replaceChildren(...mergeBlock(d.merge, false));
    $('machinesCard').hidden = !d.machines.length;
    fill($('machines'), d.machines.map(machineRow), '');
    fill($('log'), d.log.slice().reverse().map(r => el('li', null, el('time', {text: when(r.at)}), r.line)), 'Nothing yet.');

    fill($('t-needs'), needs(), 'Nothing needs you.');
    fill($('t-running'), d.running.map(t => taskRow(t, t.agent ? el('span', {class: 'chip run', text: t.agent}) : el('span', {class: 'chip run', text: 'Working'}))), 'Nothing is being worked on.');
    fill($('t-queued'), d.queued.map(t => taskRow(t, el('span', {class: 'chip wait', text: t.why === 'ready' || !t.why ? 'Ready' : 'Waiting'}))), 'The queue is empty.');
    fill($('t-pr'), d.in_pr.map(t => taskRow(t, null)), 'No open pull requests from workers.');
    for (const [id, list] of [['n-needs', d.needs], ['n-running', d.running], ['n-queued', d.queued], ['n-pr', d.in_pr], ['n-waiting', d.merge.waiting]]) $(id).textContent = list.length ? '(' + list.length + ')' : '';

    $('mergeFull').replaceChildren(...mergeBlock(d.merge, true));
    fill($('waiting'), d.merge.waiting.map(p => el('li', null,
      link(p.url, 'title', num(p.pr), p.title),
      el('span', {class: 'chip ' + (p.ready ? 'run' : 'wait'), text: p.ready ? 'Ready for the next round' : 'Not ready yet'}),
      p.flags.map(f => el('span', {class: 'chip hold', text: f})))), 'No pull request is waiting to merge.');
    fill($('builds'), d.builds.map(buildRow), 'No builds yet. Schedule one in Fletchery on the PC.');
  }

  function freshness() {
    const dot = $('dot'), text = $('freshText');
    if (failed) {
      dot.className = 'dot bad';
      text.textContent = failed + (lastOk ? ' Showing what it said ' + ago(new Date(lastOk).toISOString()) + '.' : '');
      return;
    }
    if (!last) {
      dot.className = 'dot';
      text.textContent = 'Connecting…' + (lastOk ? ' Showing what it said ' + ago(new Date(lastOk).toISOString()) + '.' : '');
      return;
    }
    dot.className = 'dot ok';
    const pass = last.passing ? 'checking GitHub now' : last.last_pass ? 'last check ' + ago(last.last_pass) : '';
    text.textContent = 'Live' + (pass ? ' · ' + pass : '') + (last.daily_budget ? ' · $' + Number(last.spent_today).toFixed(0) + ' today' : '');
  }

  // The last answer is kept on this phone only so the page has something to show the moment it opens.
  function remember(body) {
    try { localStorage.setItem(KEEP, JSON.stringify({at: Date.now(), body})); } catch (e) { /* private mode, or full */ }
  }
  function recall() {
    try {
      const kept = JSON.parse(localStorage.getItem(KEEP) || 'null');
      if (!kept || !kept.body || !(Date.now() - kept.at < KEEP_MS)) return;
      draw(kept.body);
      lastOk = kept.at;
      freshness();
    } catch (e) { /* nothing usable was kept */ }
  }

  async function refresh() {
    clearTimeout(timer);
    const stop = new AbortController();
    const giveUp = setTimeout(() => stop.abort(), WAIT_MS);
    try {
      const r = await fetch('/api/summary', {cache: 'no-store', headers: {Accept: 'application/json'}, signal: stop.signal});
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || 'Fletchery answered ' + r.status + '.');
      last = body; lastOk = Date.now(); failed = '';
      draw(body);
      remember(body);
      $('dot').classList.add('pulse');
    } catch (e) {
      failed = e instanceof TypeError || (e && e.name === 'AbortError') ? 'Can’t reach the PC. Is Tailscale on, and is the PC awake?' : String(e.message || e);
    }
    clearTimeout(giveUp);
    freshness();
    if (!document.hidden) timer = setTimeout(refresh, failed ? RETRY_MS : POLL_MS);
  }

  // ---- tabs ----
  function show(tab, quiet) {
    if (!TABS.includes(tab)) tab = 'live';
    for (const t of TABS) {
      $('view-' + t).hidden = t !== tab;
      if (t === tab) $('tab-' + t).setAttribute('aria-current', 'page'); else $('tab-' + t).removeAttribute('aria-current');
    }
    if (!quiet) { history.replaceState(null, '', '#' + tab); window.scrollTo(0, 0); }
    document.dispatchEvent(new CustomEvent('fletchery-tab', {detail: tab}));
  }
  document.querySelector('.tabs').addEventListener('click', e => { const b = e.target.closest('[data-tab]'); if (b) show(b.dataset.tab); });
  window.addEventListener('hashchange', () => show(location.hash.slice(1), true));
  show(location.hash.slice(1), true);

  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
  window.addEventListener('online', refresh);
  setInterval(freshness, 15000);
  recall();
  refresh();

  // ---- notifications (optional) ----
  const standalone = () => navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  const isApple = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let registration = null;
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').then(r => { registration = r; markBell(); }).catch(() => {});
    navigator.serviceWorker.addEventListener('message', e => { if (e.data && e.data.tab) { show(e.data.tab); refresh(); } });
  }
  function keyBytes(text) {
    const pad = text + '='.repeat((4 - text.length % 4) % 4);
    return Uint8Array.from(atob(pad.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  }
  async function post(path, body) {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || 'Fletchery answered ' + r.status + '.');
    return out;
  }
  async function subscription() {
    if (!registration || !registration.pushManager) return null;
    return registration.pushManager.getSubscription();
  }
  async function markBell() {
    try { $('bell').classList.toggle('on', !!(await subscription())); } catch (e) { /* no push here */ }
  }
  function say(text, bad) { $('notifyMsg').textContent = text || ''; $('notifyMsg').className = 'msg' + (bad ? ' bad' : ''); }
  async function notifyState() {
    const text = $('notifyText');
    for (const id of ['notifyOn', 'notifyTest', 'notifyOff', 'installSteps']) $(id).hidden = true;
    let info;
    try {
      const r = await fetch('/api/push', {cache: 'no-store'});
      info = await r.json();
      if (!r.ok) throw new Error(info.error);
    } catch (e) { text.textContent = 'Can’t reach the PC to check notifications.'; return; }
    if (!info.available) { text.textContent = info.why || 'Notifications are not set up on the PC.'; return; }
    if (isApple && !standalone()) {
      text.textContent = 'On iPhone and iPad, notifications work once Fletchery is on your Home Screen:';
      $('installSteps').hidden = false;
      return;
    }
    if (!registration || !('PushManager' in window) || !('Notification' in window)) {
      text.textContent = 'This browser cannot show notifications for web apps' + (isApple ? ' (iOS 16.4 or newer is needed).' : '.');
      return;
    }
    if (Notification.permission === 'denied') {
      text.textContent = 'Notifications are blocked for Fletchery on this device. Allow them in Settings > Notifications > Fletchery.';
      return;
    }
    const sub = await subscription();
    if (sub) {
      text.textContent = 'On for this phone. You hear about tasks that need you, finished merge rounds and finished builds.';
      $('notifyTest').hidden = false; $('notifyOff').hidden = false;
    } else {
      text.textContent = 'Get a notification when a task needs you, a merge round finishes or a build is ready. Messages are encrypted for this phone.';
      $('notifyOn').hidden = false;
      $('notifyOn').dataset.key = info.key;
    }
  }
  $('bell').addEventListener('click', () => { say(''); $('notify').showModal(); notifyState(); });
  $('notifyClose').addEventListener('click', () => $('notify').close());
  $('notifyOn').addEventListener('click', async () => {
    say('');
    try {
      if (await Notification.requestPermission() !== 'granted') { await notifyState(); return; }
      const sub = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes($('notifyOn').dataset.key)});
      await post('/api/push/subscribe', {subscription: sub.toJSON(), name: isApple ? 'iPhone' : 'Browser'});
      say('Notifications are on.');
    } catch (e) { say(String(e.message || e), true); }
    await notifyState(); markBell();
  });
  $('notifyTest').addEventListener('click', async () => {
    say('');
    try { const sub = await subscription(); await post('/api/push/test', {subscription: sub.toJSON()}); say('Sent. It should arrive in a few seconds.'); }
    catch (e) { say(String(e.message || e), true); }
  });
  $('notifyOff').addEventListener('click', async () => {
    say('');
    try {
      const sub = await subscription();
      if (sub) { await post('/api/push/unsubscribe', {subscription: sub.toJSON()}).catch(() => {}); await sub.unsubscribe(); }
      say('Notifications are off on this phone.');
    } catch (e) { say(String(e.message || e), true); }
    await notifyState(); markBell();
  });
})();
