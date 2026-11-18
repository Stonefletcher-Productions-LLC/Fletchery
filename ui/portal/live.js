// Fletchery Live tab: reads /api/live (devwatch.py) and draws every machine, runner, Actions run, PR and issue.
// Read-only. It asks every 5 s while the tab is open (30 s otherwise, for the badge), and says loudly when what it shows
// is old, so a quiet screen never means "nothing is happening" when it really means "the feed stopped".
(function () {
  'use strict';
  const OPEN_MS = 5000, BACK_MS = 30000, RETRY_MS = 3000, WAIT_MS = 12000;
  const STALE_S = 45;          // the feed answered, but its own data is older than this: say so
  const KEEP = 'fletchery-live-last';
  const $ = id => document.getElementById(id);
  let last = null, lastOk = 0, failed = '', timer = 0, tab = (location.hash.slice(1) || 'live');

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v; else if (k === 'text') node.textContent = v; else node.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false || kid === '') continue;
      node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  const web = url => typeof url === 'string' && url.startsWith('https://') ? url : '';
  const a = (url, cls, ...kids) => web(url) ? el('a', {class: cls, href: url, target: '_blank', rel: 'noopener noreferrer'}, ...kids) : el('span', {class: cls}, ...kids);
  function fill(node, rows, empty) { node.replaceChildren(...(rows.length ? rows : [el('li', {class: 'empty', text: empty})])); }
  function span(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.floor(s / 60) + ' min';
    if (s < 86400) return Math.floor(s / 3600) + ' h ' + Math.floor(s % 3600 / 60) + ' min';
    return Math.floor(s / 86400) + ' d';
  }
  const secs = iso => { const t = Date.parse(iso || ''); return t ? (Date.now() - t) / 1000 : NaN; };
  // Times tick on the phone between reads: a <span data-since> shows "for 12 min", <span data-ago> "3 min ago".
  const since = iso => isFinite(secs(iso)) ? el('span', {'data-since': iso, text: span(secs(iso))}) : '';
  const ago = iso => isFinite(secs(iso)) ? el('span', {'data-ago': iso, text: span(secs(iso)) + ' ago'}) : '';
  function tick() {
    document.querySelectorAll('#view-live [data-since]').forEach(n => { n.textContent = span(secs(n.dataset.since)); });
    document.querySelectorAll('#view-live [data-ago]').forEach(n => { n.textContent = span(secs(n.dataset.ago)) + ' ago'; });
    stale();
  }
  const KIND = {check: 'PR check', merging: 'Merging', proof: 'Proof', building: 'Building', importing: 'Importing',
                coding: 'Coding', queued: 'Queued', working: 'Working'};
  const dot = (cls, label) => el('span', {class: 'state ' + cls, role: 'img', 'aria-label': label});
  const join = (...parts) => parts.filter(p => p !== '' && p != null);
  function sep(list) { const out = []; list.forEach((p, i) => { if (i) out.push(' · '); out.push(...[].concat(p)); }); return out; }

  // ---- drawing ----
  function stats(d) {
    const c = d.counts || {};
    const tile = (n, label, hot) => el('div', {class: 'stat' + (hot ? ' hot' : '')}, el('b', {text: String(n)}), el('span', {text: label}));
    $('liveStats').replaceChildren(
      tile((c.busy || 0) + '/' + (c.machines || 0), 'machines busy'),
      tile(c.runs || 0, (c.queued ? c.queued + ' queued · ' : '') + 'runs going'),
      tile(c.prs || 0, 'open PRs'),
      tile(c.blockers || 0, 'stuck/blocked', c.blockers > 0));
    const b = $('b-live');
    if (b) { b.hidden = !c.blockers; b.textContent = c.blockers > 99 ? '99+' : String(c.blockers || ''); }
  }
  function blockerRow(b) {
    const label = {feed: 'Feed', machine: 'Silent', stuck: 'Stuck', check: 'Failed', runner: 'Offline', label: 'Blocked'}[b.kind] || 'Look';
    return el('li', {class: 'blk'}, el('span', {class: 'chip needs', text: label}), a(b.url, 'title', b.title),
      b.detail || b.since ? el('p', {class: 'sub'}, ...sep(join(b.detail, b.since ? since(b.since) : ''))) : null);
  }
  function actRow(x) {
    const k = KIND[x.kind] ? x.kind : 'working';
    return el('li', null, el('span', {class: 'chip k-' + k, text: KIND[k]}),
      el('div', {class: 'what'}, web(x.url) ? a(x.url, '', x.title) : el('span', {class: 't', text: x.title}),
        el('p', {class: 'sub'}, ...sep(join(x.detail, x.since ? since(x.since) : '')))));
  }
  function machineRow(m) {
    const st = m.stats || {};
    const bits = join(
      typeof st.cpu === 'number' ? 'CPU ' + Math.round(st.cpu) + '%' : '',
      typeof st.ram_used_gb === 'number' ? 'RAM ' + st.ram_used_gb + '/' + st.ram_total_gb + ' GB' : '',
      ...(m.disks || []).map(x => x.name + ' ' + Math.round(x.free_gb) + ' GB free'));
    const state = !m.online ? ['off', 'Not reporting'] : m.busy ? ['busy', 'Busy'] : ['on', 'Idle'];
    const tools = Object.entries(m.procs || {}).filter(([k]) => k !== 'claude').map(([k, n]) => k + (n > 1 ? ' ×' + n : ''));
    return el('li', null,
      el('div', {class: 'm-head'}, el('span', {class: 'm-name'}, dot(state[0], state[1]), m.name),
        el('span', {class: 'm-stats', text: bits.join(' · ')})),
      !m.online ? el('p', {class: 'sub'}, m.last_report ? ['Last report ', ago(m.last_report)] : 'No reporter on this machine yet') : null,
      m.activities && m.activities.length ? el('ul', {class: 'acts'}, m.activities.map(actRow)) :
        el('p', {class: 'sub', text: m.online ? 'Idle' : ''}),
      tools.length ? el('p', {class: 'sub', text: 'Running: ' + tools.join(', ')}) : null,
      m.runners && m.runners.length ? el('p', {class: 'sub', text: 'Runners: ' + m.runners.join(', ')}) : null);
  }
  function runnerRow(r) {
    const chip = !r.online ? el('span', {class: 'chip needs', text: 'Offline'}) :
      r.busy || r.job ? el('span', {class: 'chip wait', text: 'Busy'}) : el('span', {class: 'chip run', text: 'Idle'});
    return el('li', null, el('div', {class: 'r-row'},
      el('div', null, el('div', {class: 'title', text: r.name}), el('p', {class: 'sub', text: join(r.machine, (r.labels || []).join(', ')).join(' · ')}),
        r.job ? el('p', {class: 'sub'}, a(r.job.url, '', r.job.title), ...sep(['', ...join(r.job.step, r.job.started ? since(r.job.started) : '')])) : null),
      chip));
  }
  function bar(part) {   // width set through the DOM: the page's CSP allows no inline style attributes
    const fillEl = el('i');
    fillEl.style.width = Math.round(100 * Math.min(1, Math.max(0, part))) + '%';
    return el('div', {class: 'bar', 'aria-hidden': 'true'}, fillEl);
  }
  function runRow(r) {
    const going = r.status === 'in_progress', waiting = r.status !== 'completed' && !going;
    const ico = going ? ['run', '◐', 'Running'] : waiting ? ['wait', '○', 'Queued'] :
      r.conclusion === 'success' ? ['ok', '✓', 'Passed'] : r.conclusion === 'skipped' || r.conclusion === 'neutral' ? ['wait', '–', 'Skipped'] :
      r.conclusion === 'cancelled' ? ['wait', '⊘', 'Cancelled'] : ['bad', '✕', 'Failed'];
    const where = join(r.repo.split('/')[1], r.runner, going && r.step ? 'step ' + (r.steps_done + 1) + '/' + r.steps_total + ': ' + r.step : '');
    const when = going ? ['for ', since(r.started)] : waiting ? ['queued ', since(r.created)] : [ago(r.updated)];
    return el('li', null, el('div', {class: 'run-row'}, el('span', {class: 'ico ' + ico[0], role: 'img', 'aria-label': ico[2], text: ico[1]}),
      el('div', null, a(r.url, 'title', r.name + (r.pr ? ' · PR #' + r.pr : '')),
        el('p', {class: 'sub'}, ...sep(where), where.length ? ' · ' : '', ...when),
        going && r.steps_total ? bar(r.steps_done / r.steps_total) : null)));
  }
  function prRow(p) {
    const c = p.checks || {};
    const chip = p.draft ? ['', 'Draft'] : {failed: ['bad', 'Failing'], running: ['wait', 'Checking'], queued: ['wait', 'Check queued'],
      passed: ['run', 'Checks pass'], none: ['', 'No checks']}[c.state] || ['', c.state];
    const what = c.state === 'failed' ? 'failed: ' + (c.failed || []).join(', ') : c.state === 'running' || c.state === 'queued' ?
      (c.running || []).join(', ') + (c.total ? ' (' + c.done + '/' + c.total + ' done)' : '') : '';
    return el('li', null, el('div', {class: 'r-row'},
      el('div', null, a(p.url, 'title', el('span', {class: 'num', text: '#' + p.number}), p.title),
        el('p', {class: 'sub'}, ...sep(join(p.repo.split('/')[1], p.author, what, ago(p.updated))))),
      el('span', {class: 'chip ' + chip[0], text: chip[1]})));
  }
  function issueRow(i) {
    return el('li', null, a(i.url, 'title', el('span', {class: 'num', text: '#' + i.number}), i.title),
      i.blocked ? el('span', {class: 'chip needs', text: 'Blocked'}) : null,
      ...(i.labels || []).slice(0, 4).map(l => el('span', {class: 'chip', text: l})),
      el('p', {class: 'sub'}, ...sep(join(i.repo.split('/')[1], ['updated ', ago(i.updated)]))));
  }
  function draw(d) {
    stats(d);
    const blk = d.blockers || [];
    $('liveBlockersCard').classList.toggle('calm', !blk.length);
    $('n-lblock').textContent = blk.length ? '(' + blk.length + ')' : '';
    fill($('liveBlockers'), blk.map(blockerRow), 'Nothing stuck. Everything is moving or idle.');
    fill($('liveMachines'), (d.machines || []).map(machineRow), 'No machine has reported yet, and no runner is registered.');
    const note = (d.fresh || {}).runner_note;
    $('liveRunnerNote').hidden = !note; $('liveRunnerNote').textContent = note || '';
    $('n-lrun').textContent = (d.runners || []).length ? '(' + d.runners.length + ')' : '';
    fill($('liveRunners'), (d.runners || []).map(runnerRow), 'No self-hosted runners found.');
    fill($('liveRuns'), (d.runs || []).slice(0, 25).map(runRow), 'No recent Actions runs.');
    $('n-lpr').textContent = (d.prs || []).length ? '(' + d.prs.length + ')' : '';
    fill($('livePrs'), (d.prs || []).map(prRow), 'No open pull requests.');
    $('n-liss').textContent = (d.issues || []).length ? '(' + d.issues.length + ')' : '';
    fill($('liveIssues'), (d.issues || []).slice(0, 40).map(issueRow), 'No open issues.');
    const f = d.fresh || {};
    $('liveFoot').replaceChildren(...sep(join(
      (d.repos || []).length + ' repositories watched',
      f.github ? ['GitHub read ', ago(f.github)] : 'GitHub not read yet',
      f.every ? 'every ' + f.every + ' s' : '',
      f.remaining >= 0 ? f.remaining + ' GitHub requests left this hour' : '')));
  }
  function stale() {
    const box = $('liveStale');
    let msg = '';
    if (failed) msg = failed + (lastOk ? ' Showing what it said ' + span((Date.now() - lastOk) / 1000) + ' ago.' : '');
    else if (last && secs(last.at) > STALE_S) msg = 'This page has not had fresh data for ' + span(secs(last.at)) + '. What it shows may be out of date.';
    else if (last && last.fresh && last.fresh.github && secs(last.fresh.github) > Math.max(180, 4 * (last.fresh.every || 15)))
      msg = 'Fletchery has not been able to read GitHub for ' + span(secs(last.fresh.github)) + '. Machine reports are still live.';
    box.hidden = !msg; box.textContent = msg;
  }

  async function refresh() {
    clearTimeout(timer);
    const stop = new AbortController(), giveUp = setTimeout(() => stop.abort(), WAIT_MS);
    try {
      const r = await fetch('/api/live', {cache: 'no-store', headers: {Accept: 'application/json'}, signal: stop.signal});
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || 'Fletchery answered ' + r.status + '.');
      last = body; lastOk = Date.now(); failed = '';
      draw(body);
      try { localStorage.setItem(KEEP, JSON.stringify({at: lastOk, body})); } catch (e) { /* private mode */ }
    } catch (e) {
      failed = e instanceof TypeError || (e && e.name === 'AbortError') ? 'Can’t reach the PC. Is Tailscale on, and is the PC awake?' : String(e.message || e);
    }
    clearTimeout(giveUp);
    stale();
    if (!document.hidden) timer = setTimeout(refresh, failed ? RETRY_MS : tab === 'live' ? OPEN_MS : BACK_MS);
  }
  try {
    const kept = JSON.parse(localStorage.getItem(KEEP) || 'null');
    if (kept && kept.body && Date.now() - kept.at < 86400000) { last = kept.body; lastOk = kept.at; draw(kept.body); }
  } catch (e) { /* nothing kept */ }
  document.addEventListener('fletchery-tab', e => { tab = e.detail; if (tab === 'live') refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
  window.addEventListener('online', refresh);
  setInterval(tick, 1000);
  refresh();
})();
