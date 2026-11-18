// Fletchery's page, on the phone and in the desktop window: reads /api/summary and draws Builds and Activity (live.js
// draws Live). Controls (start, stop, pause, re-run, restart) show when the backend says this screen may use them: the
// desktop window always, the phone unless workers.portal.controls is false. Queueing a new build is the window's only.
(function () {
  'use strict';
  const POLL_MS = 5000;      // while Builds or Activity is open
  const QUIET_MS = 60000;    // on Live (live.js reads its own feed): just the badges
  const RETRY_MS = 3000;     // ask again sooner while the PC cannot be reached (Tailscale may still be connecting)
  const WAIT_MS = 12000;     // give up on one read after this long
  const KEEP = 'fletchery-portal-last';
  const KEEP_MS = 24 * 3600 * 1000;
  const TABS = ['live', 'builds', 'activity'];
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
  let desktop = false, can = false;
  const BUILD = {scheduled: ['Scheduled', 'wait'], waiting: ['Waiting', 'wait'], queued: ['Queued', 'wait'], running: ['Building', 'run'],
    done: ['Finished', 'run'], failed: ['Failed', 'bad'], cancelled: ['Cancelled', '']};
  // Every control says what happened: a short note at the bottom of the screen. `ask` is a confirm for the ones that stop
  // or throw away work.
  let toastTimer = 0;
  function toast(text, bad) {
    const t = $('toast');
    t.textContent = text;
    t.className = 'toast' + (bad ? ' bad' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 9000 : 4000);
  }
  window.fletcheryToast = toast;
  async function act(path, body, done, ask) {
    if (ask && !confirm(ask)) return false;
    try {
      const out = await post(path, body);
      toast(out.message || done);
      refresh();
      return true;
    } catch (e) { toast(String(e.message || e), true); return false; }
  }
  window.fletcheryAct = act;
  function button(text, path, body, done, ask) {
    const b = el('button', {type: 'button', class: 'small', text});
    b.addEventListener('click', async () => {
      b.disabled = true;
      await act(path, body, done, ask);
      b.disabled = false;
    });
    return b;
  }
  function buildRow(b) {
    const [word, cls] = BUILD[b.state] || [b.state, ''];
    const time = b.finished ? 'Finished ' + when(b.finished) : b.started ? 'Started ' + when(b.started) + ' (' + lasted(b.started) + ')' : b.at ? 'Planned for ' + when(b.at) : '';
    const publish = {zipping: 'Zipping the download', uploading: 'Uploading the download', failed: 'Upload failed', skipped: 'Not uploaded'}[b.publish];
    const open = !['done', 'failed', 'cancelled'].includes(b.state);
    return el('li', null,
      el('div', {class: 'title', text: 'Build ' + b.version}),
      el('span', {class: 'chip ' + cls, text: word}),
      publish ? el('span', {class: 'chip ' + (b.publish === 'failed' ? 'bad' : 'wait'), text: publish}) : null,
      b.tests === 'full' ? el('span', {class: 'chip', text: 'Full tests'}) : null,
      b.repeat === 'daily' ? el('span', {class: 'chip', text: 'Daily'}) : null,
      can && b.state === 'scheduled' && !b.repeat ? button('Start now', '/api/builds/now', {id: b.id}, 'Starting ' + b.version + ' now.') : null,
      can && b.state === 'scheduled' && b.repeat ? button('Run once now', '/api/builds/now', {id: b.id}, 'Starting ' + b.version + ' now.') : null,
      can && open ? button(b.repeat ? 'Stop repeating' : b.state === 'running' ? 'Stop' : 'Cancel', '/api/builds/cancel', {id: b.id}, 'Done.',
        b.state === 'running' ? 'Stop build ' + b.version + '? The machine stops it and the work so far is lost.' : b.repeat ? 'Stop the daily ' + b.version + ' build?' : '') : null,
      can && ['failed', 'cancelled'].includes(b.state) ? button('Build again', '/api/builds/retry', {id: b.id}, 'Build ' + b.version + ' queued again.') : null,
      el('p', {class: 'sub', text: [time, b.machine && 'on ' + b.machine, b.ref && 'from ' + b.ref + (b.sha ? ' (' + b.sha + ')' : '')].filter(Boolean).join(' · ')}),
      b.reason && !(b.state === 'done' && b.links.length) ? el('p', {class: 'sub', text: b.reason}) : null,
      b.publish_reason && b.publish === 'failed' ? el('p', {class: 'sub', text: b.publish_reason}) : null,
      b.links.length ? el('div', {class: 'links'}, b.links.filter(l => web(l.url)).map(l =>
        el('a', {href: l.url, target: '_blank', rel: 'noopener noreferrer', text: l.label}))) : null);
  }
  const JOB = {queued: ['Queued', 'wait'], running: ['Running', 'run'], passed: ['Passed', 'run'], failed: ['Failed', 'bad'],
    cancelled: ['Cancelled', ''], lost: ['Lost', 'bad']};
  function steps(j) {
    if (!j.steps || !j.steps.length) return null;
    return el('ol', {class: 'steps'}, j.steps.map(s => el('li', {class: s.state === 'passed' ? 'done' : s.state},
      el('span', {class: 'mark', 'aria-hidden': 'true', text: {passed: '✓', running: '▶', failed: '✗'}[s.state] || '○'}),
      el('span', {class: 'name', text: s.name}),
      s.start ? el('span', {class: 'took', text: s.end ? took((Date.parse(s.end) - Date.parse(s.start)) / 1000) : lasted(s.start)}) : null)));
  }
  function jobRow(j) {
    const [word, cls] = JOB[j.state] || [j.state, ''];
    const what = j.recipe === 'package' ? 'Build ' + (j.version || '') : j.recipe;
    return el('li', null,
      el('div', {class: 'title'}, el('span', {class: 'num', text: '#' + j.id}), what),
      el('span', {class: 'chip ' + cls, text: word + (j.cancel && j.state === 'running' ? ' (stopping)' : '')}),
      can && ['queued', 'running'].includes(j.state) && !j.cancel ? button(j.state === 'running' ? 'Stop' : 'Cancel', '/api/jobs/cancel', {id: j.id},
        j.state === 'running' ? 'The machine stops job #' + j.id + ' at its next check-in (within a minute).' : 'Job #' + j.id + ' cancelled.',
        j.state === 'running' ? 'Stop job #' + j.id + ' now? Its processes on ' + (j.machine || 'the machine') + ' are ended.' : '') : null,
      can && ['failed', 'cancelled', 'lost'].includes(j.state) && j.recipe !== 'package' ? button('Run again', '/api/jobs/retry', {id: j.id}, 'Queued again.') : null,
      can && j.log_bytes ? el('a', {class: 'small-link', href: '/api/jobs/log?id=' + j.id, target: '_blank', rel: 'noopener', text: 'Log'}) : null,
      el('p', {class: 'sub', text: [j.machine && 'on ' + j.machine, j.started ? 'started ' + when(j.started) : 'queued ' + ago(j.created),
        j.finished ? 'finished ' + when(j.finished) : j.started ? 'running ' + lasted(j.started) : '',
        j.ref && 'from ' + j.ref + (j.sha ? ' (' + j.sha + ')' : '')].filter(Boolean).join(' · ')}),
      j.summary ? el('p', {class: 'sub', text: j.summary}) : null,
      j.state === 'running' ? steps(j) : null);
  }
  function machineRow(m) {
    const off = m.paused.includes('builds');
    return el('li', null,
      el('div', {class: 'title', text: m.name}),
      el('span', {class: 'chip ' + (m.online ? 'run' : 'bad'), text: m.online ? 'Worker online' : 'Worker offline'}),
      m.job ? el('span', {class: 'chip wait', text: 'Running job #' + m.job.id}) : m.online ? el('span', {class: 'chip', text: 'Idle'}) : null,
      off ? el('span', {class: 'chip hold', text: 'Builds paused'}) : null,
      can ? button(off ? 'Resume builds' : 'Pause builds', '/api/machines/pause', {name: m.name, kind: 'builds', on: !off},
        off ? m.name + ' takes builds again.' : m.name + ' takes no new builds. A running one finishes.') : null,
      !m.online && m.last_seen ? el('p', {class: 'sub', text: 'Last called in ' + ago(m.last_seen)}) : null,
      el('p', {class: 'sub', text: m.labels.join(' · ')}));
  }

  function draw(d) {
    desktop = d.desktop === true;
    can = d.controls === true;
    document.body.classList.toggle('desktop', desktop);
    document.body.classList.toggle('controls', can);
    for (const n of document.querySelectorAll('.desk')) n.hidden = !desktop || (n.id === 'newBuildCard' && !d.machines_on);
    for (const n of document.querySelectorAll('.ctrl')) n.hidden = !can;
    $('project').textContent = d.project && d.project !== 'project' ? d.project : 'Fletchery';
    const alerts = d.errors.map(t => el('div', {class: 'alert bad', role: 'status'}, el('strong', {text: 'Problem'}), t));
    if (d.restarting) alerts.unshift(el('div', {class: 'alert', role: 'status'}, el('strong', {text: 'Restarting'}), d.restarting));
    $('alerts').replaceChildren(...alerts);
    const open = d.builds.filter(b => !['done', 'failed', 'cancelled'].includes(b.state)).length;
    const badge = $('b-builds');
    badge.hidden = !open;
    badge.textContent = open;
    badge.setAttribute('aria-label', open + ' builds scheduled or running');
    const ab = $('b-activity');
    ab.hidden = !d.errors.length;
    ab.textContent = d.errors.length ? '!' : '';
    if (!$('bRef').value && !$('bRef').dataset.touched) $('bRef').placeholder = d.base || 'main';
    fill($('builds'), d.builds.map(buildRow), d.machines_on ? 'No builds yet.' + (desktop ? '' : ' Queue one in the Fletchery window on the PC; Build again works here.') : 'No build machines are set up (workers.machines in the profile).');
    fill($('buildMachines'), d.machines.map(machineRow), 'No build machines are registered.');
    $('heldNote').hidden = !d.held;
    $('holdAll').textContent = d.held ? 'Resume all machines' : 'Pause all machines';
    $('holdAll').dataset.on = d.held ? '' : '1';
    fill($('jobs'), d.jobs.slice(0, 25).map(jobRow), 'No machine jobs yet.');
    const sc = d.scratch || {};
    fill($('health'), [
      el('li', null, el('div', {class: 'title', text: 'Backend'}), el('span', {class: 'chip run', text: 'Running'}),
        el('p', {class: 'sub', text: 'Up ' + lasted(d.started) + ', since ' + when(d.started) + (d.version ? ' · code ' + d.version : '') +
          (d.repo ? ' · builds from ' + d.repo : '')})),
      d.machines_on ? el('li', null, el('div', {class: 'title', text: 'Build machines'}),
        el('span', {class: 'chip ' + (d.held ? 'hold' : 'run'), text: d.held ? 'All paused' : 'Taking jobs'}),
        el('p', {class: 'sub', text: d.machines.filter(m => m.online).length + ' of ' + d.machines.length + ' online · ' +
          d.jobs.filter(j => j.state === 'running').length + ' running, ' + d.jobs.filter(j => j.state === 'queued').length + ' queued'})) : null,
      sc.checked_at ? el('li', null, el('div', {class: 'title', text: 'Scratch clean-up'}),
        el('span', {class: 'chip ' + (sc.error ? 'bad' : ''), text: sc.error ? 'Stopped' : sc.mode || 'on'}),
        el('p', {class: 'sub', text: sc.error || ('Last ran ' + ago(sc.checked_at) + (sc.freed_gb ? ', freed ' + sc.freed_gb + ' GB' : ''))})) : null,
      ...d.errors.map(t => el('li', null, el('div', {class: 'title', text: 'Problem'}), el('p', {class: 'sub', text: t})))].filter(Boolean), '');
    fill($('log'), d.log.slice().reverse().map(r => el('li', null, el('time', {text: when(r.at)}), r.line)), 'Nothing yet.');
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
    text.textContent = 'Connected · updated ' + ago(new Date(lastOk).toISOString());
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
      failed = e instanceof TypeError || (e && e.name === 'AbortError') ? (desktop ? 'The Fletchery backend is not answering.' : 'Can’t reach the PC. Is Tailscale on, and is the PC awake?') : String(e.message || e);
    }
    clearTimeout(giveUp);
    freshness();
    const open = (location.hash.slice(1) || 'live') !== 'live';
    if (!document.hidden) timer = setTimeout(refresh, failed ? RETRY_MS : open ? POLL_MS : QUIET_MS);
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
    if (tab !== 'live' && !quiet) refresh();
  }
  document.querySelector('.tabs').addEventListener('click', e => { const b = e.target.closest('[data-tab]'); if (b) show(b.dataset.tab); });
  window.addEventListener('hashchange', () => show(location.hash.slice(1), true));
  show(location.hash.slice(1), true);

  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
  window.addEventListener('online', refresh);
  setInterval(freshness, 5000);

  // ---- the desktop window's form ----
  $('bRef').addEventListener('input', () => { $('bRef').dataset.touched = '1'; });
  $('newBuild').addEventListener('submit', async e => {
    e.preventDefault();
    const msg = $('bMsg');
    msg.textContent = ''; msg.className = 'msg';
    const at = $('bAt').value ? new Date($('bAt').value).toISOString() : '';
    try {
      const row = await post('/api/builds/add', {version: $('bVersion').value.trim(), ref: $('bRef').value.trim(), at,
        repeat: $('bDaily').checked ? 'daily' : '', tests: $('bTests').checked ? 'full' : ''});
      msg.textContent = 'Build ' + row.version + ' queued.';
      $('newBuild').reset(); delete $('bRef').dataset.touched;
      refresh();
    } catch (err) { msg.textContent = String(err.message || err); msg.className = 'msg bad'; }
  });
  $('holdAll').addEventListener('click', () => {
    const on = !!$('holdAll').dataset.on;
    act('/api/machines/hold', {on}, on ? 'All machines paused. Running jobs finish; nothing new starts.' : 'Machines take jobs again.',
      on ? 'Pause all build machines? Running jobs finish; nothing new starts until you resume.' : '');
  });
  $('reload').addEventListener('click', () => location.reload());
  $('refreshTop').addEventListener('click', () => {
    if (can) post('/api/live/refresh', {}).catch(() => {});
    refresh();
    document.dispatchEvent(new CustomEvent('fletchery-refresh'));
    toast('Refreshing.');
  });
  $('checkNow').addEventListener('click', () => act('/api/live/refresh', {}, 'Reading GitHub now. Live updates in a few seconds.'));
  $('restart').addEventListener('click', async () => {
    if (await act('/api/restart', {}, 'Restarting. Back in a few seconds.',
      'Restart the Fletchery backend? It is back in a few seconds; builds on the machines carry on.')) $('rMsg').textContent = 'Restarting…';
  });
  recall();
  refresh();

  // ---- notifications (optional) ----
  const standalone = () => navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  const isApple = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let registration = null;
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js', {updateViaCache: 'none'}).then(r => { registration = r; markBell(); r.update().catch(() => {}); }).catch(() => {});
    // A new version took over while this page was open (after an update on the PC): load it once.
    const hadController = !!navigator.serviceWorker.controller;
    let reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => { if (hadController && !reloaded) { reloaded = true; location.reload(); } });
    navigator.serviceWorker.addEventListener('message', e => { if (e.data && e.data.tab) { show(e.data.tab); refresh(); } });
  }
  function keyBytes(text) {
    const pad = text + '='.repeat((4 - text.length % 4) % 4);
    return Uint8Array.from(atob(pad.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  }
  window.fletcheryPost = (path, body) => post(path, body);
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
      text.textContent = 'On for this phone. You hear when something gets stuck or blocked, and when a build is ready.';
      $('notifyTest').hidden = false; $('notifyOff').hidden = false;
    } else {
      text.textContent = 'Get a notification when something gets stuck or blocked, or a build is ready. Messages are encrypted for this phone.';
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
