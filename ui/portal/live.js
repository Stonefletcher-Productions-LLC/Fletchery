// Fletchery Live tab: reads /api/live (devwatch.py) and draws every machine, runner, Actions run, PR and issue.
// Read-only. It asks every 5 s while the tab is open (30 s otherwise, for the badge), and says loudly when what it shows
// is old, so a quiet screen never means "nothing is happening" when it really means "the feed stopped".
(function () {
  'use strict';
  const OPEN_MS = 5000, BACK_MS = 30000, RETRY_MS = 3000, WAIT_MS = 12000;
  const STALE_S = 45;          // the feed answered, but its own data is older than this: say so
  const UPLOAD_ETA_FRESH_S = 120;
  const KEEP = 'fletchery-live-last';
  const $ = id => document.getElementById(id);
  let last = null, lastOk = 0, failed = '', timer = 0, tab = (location.hash.slice(1) || 'live');
  let repo = '';                 // one project only ('' = all), remembered on this device
  try { repo = localStorage.getItem('fletchery-live-repo') || ''; } catch (e) { /* private mode */ }

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
  // How many rows each list shows before "Show N more" (kit.js).
  const CAP = {queueCancelled: 6, queueHolds: 6, liveProblems: 5, liveBuilding: 6, liveChanges: 5, liveAgents: 6, liveBlockers: 4, liveMachines: 4, liveRunners: 6, liveRuns: 5, livePrs: 6, liveIssues: 6};
  function fill(node, rows, empty, more) {
    const extra = more > 0 ? [el('li', {class: 'empty', text: '… and ' + more + ' more on GitHub'})] : [];
    node.replaceChildren(...(rows.length ? rows.concat(extra) : [el('li', {class: 'empty', text: empty})]));
    if (CAP[node.id] && window.fletcheryKit) window.fletcheryKit.cap(node, CAP[node.id]);
  }
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
  function expireUploadEstimate(node) {
    if (secs(node.dataset.uploadAt) <= UPLOAD_ETA_FRESH_S) return;
    node.textContent = 'Waiting for a new upload update to estimate time left';
    delete node.dataset.uploadAt;
  }
  function tick() {
    document.querySelectorAll('#view-live [data-since]').forEach(n => { n.textContent = span(secs(n.dataset.since)); });
    document.querySelectorAll('#view-live [data-ago]').forEach(n => { n.textContent = span(secs(n.dataset.ago)) + ' ago'; });
    document.querySelectorAll('[data-upload-at]').forEach(expireUploadEstimate);
    stale();
  }
  const KIND = {check: 'PR check', merging: 'Merging', proof: 'Proof', building: 'Building', importing: 'Importing',
                coding: 'Coding', queued: 'Queued', working: 'Working'};
  const dot = (cls, label) => el('span', {class: 'state ' + cls, role: 'img', 'aria-label': label});
  const join = (...parts) => parts.filter(p => p !== '' && p != null);
  function sep(list) { const out = []; list.forEach((p, i) => { if (i) out.push(' · '); out.push(...[].concat(p)); }); return out; }

  // ---- drawing ----
  function prCount(full, scopedRows) {
    const counts = full.open_pr_counts || {}, status = full.pr_count_status;
    if (!status) {
      const total = repo ? (full.open_pr_counts ? [...picked].reduce((n, name) => n + (counts[name] || 0), 0) : scopedRows.length) :
        full.counts?.prs ?? scopedRows.length;
      return {total, partial: false};
    }
    const stale = status.stale || {};
    const names = repo ? [...picked] : [...new Set([...Object.keys(counts), ...Object.keys(stale), ...(full.repos || []).map(r => r.name)])];
    const known = names.filter(name => Number.isInteger(counts[name]));
    const unconfirmed = names.filter(name => Object.hasOwn(stale, name) || !Number.isInteger(counts[name]));
    const discovering = !repo && status.inventory_complete === false;
    const partial = unconfirmed.length > 0 || discovering || (!repo && status.partial === true);
    const total = known.length || !partial ? known.reduce((n, name) => n + counts[name], 0) : null;
    return {total, partial, discovering, unconfirmed, counts, stale};
  }
  function countNote(pr) {
    const box = $('prCountNote'), details = $('prCountDetails');
    box.hidden = !pr.partial;
    if (box.hidden) { details.open = false; return; }
    const reasons = [];
    for (const name of pr.unconfirmed) {
      const n = pr.counts[name], at = pr.stale[name];
      if (!Number.isInteger(n)) reasons.push([name + ' · open PR count unavailable']);
      else reasons.push([name + ' · ' + n + (n === 1 ? ' open PR' : ' open PRs') + ' last confirmed', ...(at ? [' ', ago(at)] : [' by GitHub'])]);
    }
    if (pr.discovering) reasons.push(['GitHub’s repository list is incomplete.']);
    $('prCountSummary').replaceChildren(...(reasons.shift() || ['Waiting for updated PR counts from GitHub.']));
    details.hidden = !reasons.length;
    if (!reasons.length) details.open = false;
    $('prCountMore').textContent = reasons.length + ' more ' + (reasons.length === 1 ? 'detail' : 'details');
    $('prCountRepos').replaceChildren(...reasons.map(reason => el('li', null, ...reason)));
  }
  function stats(d, pr) {
    const c = d.counts || {};
    const tile = (n, label, hot) => el('div', {class: 'stat' + (hot ? ' hot' : '')}, el('b', {text: String(n)}), el('span', {text: label}));
    $('liveStats').replaceChildren(
      tile((c.busy || 0) + '/' + (c.machines || 0), 'machines busy'),
      tile((c.runs || 0) - (c.queued || 0), 'running now' + (c.queued ? ' · ' + c.queued + ' queued' : '')),
      tile(pr.total ?? '—', pr.partial ? 'open PRs · Partial' : 'open PRs'),
      tile(c.blockers || 0, 'stuck/blocked', c.blockers > 0));
    const b = $('b-live');
    if (b) { b.hidden = !c.blockers; b.textContent = c.blockers > 99 ? '99+' : String(c.blockers || ''); }
    document.title = (c.blockers ? '(' + c.blockers + ') ' : '') + 'Fletchery';
    countNote(pr);
  }
  // ---- Flow: is work moving? (flow.py) ----
  const VERDICT = {moving: ['run', 'Moving'], idle: ['', 'Idle'], slow: ['wait', 'Slow'], stopped: ['bad', 'Stopped']};
  const dur = s => s == null ? '–' : s < 90 ? Math.round(s) + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : s < 172800 ? (s / 3600).toFixed(1).replace('.0', '') + ' h' : Math.round(s / 86400) + ' d';
  function drawFlow(full) {
    const box = $('liveFlow'), all = full.flow || {};
    const f = (repo ? (all.projects || {})[repo] || (all.repos || {})[repo] : all.all) || null;
    box.hidden = !f;
    if (!f) return;
    const v = VERDICT[(f.verdict || {}).state] || ['', (f.verdict || {}).state || ''];
    box.className = 'flow ' + ((f.verdict || {}).state || '');
    const num = (label, value, small) => el('div', null, el('dt', {text: label}), el('dd', null, value, small ? el('small', {text: ' ' + small}) : null));
    const peak = Math.max(1, ...(f.hours || []));
    const bars = (f.hours || []).map((n, i) => {
      const b = el('i', {class: n ? '' : 'zero', title: n + ' merge' + (n === 1 ? '' : 's') + ', ' + (23 - i) + '–' + (24 - i) + ' h ago'});
      b.style.height = Math.max(8, Math.round(100 * n / peak)) + '%';
      return b;
    });
    const lastBits = (f.last || []).slice(0, 4).map(x => (repo ? '' : (x.repo.split('/')[1] || x.repo) + ' ') + x.base + ' ' + dur(x.ago) + ' ago');
    box.replaceChildren(
      el('p', {class: 'verdict'}, el('span', {class: 'chip ' + v[0], text: v[1]}), el('span', {text: (f.verdict || {}).why || ''})),
      el('dl', {class: 'nums'},
        num('Merges', String(f.merges_hour || 0), 'last hour · ' + (f.merges_day || 0) + ' today'),
        num('Since last merge', dur(f.since_merge)),
        num('PR to merge', dur(f.lead_p50), 'median, 7 days'),
        num('All jobs · machine wait', dur(f.wait_p50), f.wait_p90 != null ? 'p90 ' + dur(f.wait_p90) : 'last 24 h'),
        num('Base went red', f.red_share == null ? '–' : Math.round(100 * f.red_share) + '%', f.red_n ? 'of ' + f.red_n + ' runs, 7 days' : '')),
      ...(f.build_queues || []).map(q => el('section', {'aria-label': q.repo + ' PR build queue'},
        el('b', {text: q.repo.split('/').pop() + ' · PR Compile and test'}),
        el('dl', {class: 'nums'},
          num('Median build wait', dur(q.median), q.samples + ' observed · last 24 h'),
          num('90th percentile', dur(q.p90)),
          num('Waiting now', q.waiting == null ? 'Unavailable' : String(q.waiting)),
          num('Oldest wait', q.oldest != null ? dur(q.oldest) : q.waiting === 0 ? 'None' : 'Unavailable')))),
      el('div', {class: 'hours', role: 'img', 'aria-label': 'Merges per hour, last 24 hours: ' + (f.hours || []).join(', ')}, ...bars),
      el('div', {class: 'axis', 'aria-hidden': 'true'}, el('span', {text: '24 h ago'}), el('span', {text: 'now'})),
      lastBits.length ? el('p', {class: 'bases', text: 'Last merge into ' + lastBits.join(' · ')}) : null);
  }
  // ---- Base health: a red base branch (rules.py), on top of every tab ----
  const CHK = {running: 'checks running', queued: 'checks queued', passed: 'checks pass', failed: 'checks failing', rerun: 'needs a re-run', none: 'no checks yet'};
  function drawBases(full) {
    const box = $('baseBanner');
    const red = (full.bases || []).filter(h => h.state === 'red');
    box.hidden = !red.length;
    box.replaceChildren(...red.map(h => {
      const name = (full.repos || []).length > 1 ? (h.repo.split('/')[1] || h.repo) + ' ' + h.branch : h.branch;
      const tests = f => f.tests && f.tests.length ? ' (' + f.tests.join(', ') + ')' : '';
      const sus = h.suspects || [], first = sus.find(x => x.first_bad) || sus[sus.length - 1];
      const prLink = x => a('https://github.com/' + h.repo + '/pull/' + x.number, '', '#' + x.number);
      return el('section', {class: 'base-red', 'aria-label': name + ' is red'},
        el('h2', null, name + ' is red · for ', since(h.since), h.checking ? ' · a new commit is being checked' : ''),
        el('p', null, el('b', {text: 'Failing: '}), (h.failing || []).map(f => f.name + tests(f)).join(' · ') || 'a check'),
        first ? el('p', null, el('b', {text: first.first_bad ? 'First bad: ' : 'Merged before the failure: '}), prLink(first), ' ' + (first.title || '') + (first.agent ? ' (' + first.agent + ')' : ''),
          sus.length > 1 ? [' · also merged since the last green: ', ...sep(sus.filter(x => x !== first).map(prLink))] : '') : null,
        el('p', null, el('b', {text: 'Fix in line: '}), (h.fixes || []).length ? sep(h.fixes.map(f => [a(f.url, '', '#' + f.number), ' ' + (f.title || '') +
          ' (' + (CHK[f.state] || f.state || 'waiting') + (f.auto_merge ? ', merges by itself' : '') + ')'])) : 'none yet',
          (h.issues || []).length ? [' · tracked in ', ...sep(h.issues.map(i => a(i.url, '', '#' + i.number)))] : ''),
        h.held ? el('p', {class: 'sub', text: h.held + ' open PR' + (h.held > 1 ? 's fail' : ' fails') + ' the same check' + (h.held > 1 ? 's' : '') + ' because of this; fixing ' + h.branch + ' clears ' + (h.held > 1 ? 'them' : 'it') + '.'}) : null);
    }));
  }
  // ---- Why PRs are red: problems by cause (rules.py) ----
  const CAUSE = {own: 'Own bug', base: 'Base is red', machine: 'Machine', flaky: 'Flaky', cancelled: 'Cancelled'};
  const CAUSE_N = {own: ['own bug', 'own bugs'], base: ['inherited from a red base', 'inherited from a red base'],
                   machine: ['machine problem', 'machine problems'], flaky: ['flaky', 'flaky'], cancelled: ['cancelled', 'cancelled']};
  function problemRow(g) {
    const again = desk() && ['machine', 'cancelled', 'flaky'].includes(g.cause) ? g.prs.filter(p => p.job).slice(0, 4) : [];
    return el('li', {class: 'prob'}, el('span', {class: 'chip c-' + g.cause, text: CAUSE[g.cause] || g.cause}),
      el('div', {class: 'what'}, a((g.prs[0] || {}).url, 't', g.title),
        el('p', {class: 'prs'}, ...sep(g.prs.slice(0, 6).map(p => [a('https://github.com/' + p.repo + '/pull/' + p.number, '', '#' + p.number), ' ' + p.title]))),
        el('p', {class: 'sub'}, ...sep(join(g.repo.split('/')[1], g.runner, g.since ? ['red for ', since(g.since)] : ''))),
        again.length ? el('div', {class: 'ctl'}, again.map(p => control('Re-run #' + p.number, '/api/actions/rerun-job', {repo: p.repo, id: p.job}))) : null));
  }
  function drawProblems(d, full) {
    const probs = (d.problems || []).filter(mine), c = full.causes || {};
    const red = repo ? new Set(probs.flatMap(g => g.prs.map(p => p.number))).size : c.red || 0;
    $('n-lprob').textContent = probs.length ? '(' + probs.length + ')' : '';
    $('liveProblemsCard').classList.toggle('calm', !probs.length);
    const parts = repo ? [] : Object.keys(CAUSE).filter(k => c[k]).map(k => c[k] + ' ' + CAUSE_N[k][c[k] > 1 ? 1 : 0]);
    $('liveCauses').textContent = red ? red + ' red PR' + (red > 1 ? 's' : '') + (parts.length ? ': ' + parts.join(' · ') : '') +
      '. Only own bugs count as stuck.' : '';
    $('liveCauses').hidden = !red;
    fill($('liveProblems'), probs.map(problemRow), 'No PR is red.');
  }
  // ---- Queue tab (devwatch._queue): pools of machines, what runs and waits, holds, cancels ----
  const inMin = s => s == null ? '' : s < 60 ? 'now' : 'in ' + dur(s);
  const HOW = {fletchery: x => 'cancelled ' + (x.who || 'in Fletchery'), person: x => 'cancelled by @' + x.who,
               replaced: () => 'replaced by a newer run of the same check', unknown: () => 'cancelled outside Fletchery (GitHub does not say by whom)'};
  function poolBlock(p) {
    const head = el('h3', null, p.name === 'any' ? 'Any machine' : p.name,
      el('small', {text: (p.machines || []).join(', ')}));
    const nums = el('p', {class: 'nums'},
      el('span', null, el('b', {text: p.busy + '/' + p.online}), ' busy'),
      p.offline ? el('span', null, el('b', {text: String(p.offline)}), ' offline') : null,
      el('span', null, el('b', {text: String(p.waiting_n)}), ' waiting'),
      p.wait_p50 != null ? el('span', null, 'wait today ', el('b', {text: dur(p.wait_p50)}), p.wait_p90 != null ? ' (p90 ' + dur(p.wait_p90) + ')' : '') : null,
      p.util != null ? el('span', null, 'in use ', el('b', {text: Math.round(100 * p.util) + '%'}), ' of the last day') : null);
    const rows = [];
    (p.running || []).forEach(r => rows.push(el('li', {class: 'now'}, el('span', {class: 'pos', text: '▶'}),
      a(r.url, 't', r.name + (r.pr ? ' · PR #' + r.pr : '') + (r.job ? ' · ' + r.job : '')),
      el('p', {class: 'sub'}, ...sep(join(r.runner, ['running for ', since(r.since)], r.late ? 'taking longer than usual' : r.left != null ? '~' + dur(r.left) + ' left' : ''))))));
    (p.waiting || []).forEach((w, i) => rows.push(el('li', null, el('span', {class: 'pos', text: String(i + 1)}),
      a(w.url, 't', w.name + (w.pr ? ' · PR #' + w.pr : '') + (w.job && w.job !== w.name ? ' · ' + w.job : '')),
      el('p', {class: 'sub'}, ...sep(join((w.repo || '').split('/')[1], ['waiting ', since(w.since)],
        w.start_in != null ? 'starts ' + inMin(w.start_in) + (w.finish_in != null ? ', done ' + inMin(w.finish_in) : '') : 'start time still being learned'))))));
    return el('section', {class: 'pool', 'aria-label': p.name}, head, nums,
      rows.length ? el('ol', null, ...rows) : el('p', {class: 'sub', text: p.online ? 'Nothing running or waiting.' : 'No machine of this kind is online.'}));
  }
  function drawQueue(full) {
    const q = full.queue || {}, pools = (q.pools || []);
    const shown = repo ? pools.map(p => ({...p, running: (p.running || []).filter(mine), waiting: (p.waiting || []).filter(mine)})) : pools;
    const n = shown.reduce((t, p) => t + (repo ? p.waiting.length : p.waiting_n), 0);
    $('n-qpool').textContent = n ? '(' + n + ')' : '';
    $('queuePools').replaceChildren(...(shown.length ? shown.map(poolBlock) : [el('p', {class: 'sub', text: 'No runners found yet.'})]));
    const holds = q.holds || [];
    $('n-qhold').textContent = holds.length ? '(' + holds.length + ')' : '';
    $('queueHoldsCard').classList.toggle('calm', !holds.length);
    fill($('queueHolds'), holds.map(x => actRow({...x, detail: join(x.machine, x.detail).join(' · ')})), 'Nothing is waiting for a machine’s hold.');
    const can = (q.cancelled || []).filter(mine);
    $('n-qcan').textContent = can.length ? '(' + can.length + ')' : '';
    $('queueCancelledCard').classList.toggle('calm', !can.length);
    fill($('queueCancelled'), can.map(x => el('li', null, a(x.url, 'title', x.name + (x.pr ? ' · PR #' + x.pr : '')),
      el('p', {class: 'sub'}, ...sep(join((x.repo || '').split('/')[1], (HOW[x.how] || HOW.unknown)(x), ago(x.at)))))), 'Nothing was cancelled today.');
    const b = $('b-queue');
    if (b) { const w = q.waiting || 0; b.hidden = !w; b.textContent = w > 99 ? '99+' : String(w || ''); }
  }
  // Whether a blocker reached the phone (alerts.py): waiting out its rule, alerted, snoozed, or never alerts.
  const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  function snooze(body, hours, text) {
    const b = el('button', {type: 'button', class: 'small', text});
    b.addEventListener('click', async () => {
      b.disabled = true;
      if (await window.fletcheryAct('/api/alerts/snooze', {...body, hours}, hours ? 'Snoozed for ' + hours + ' h. It stays listed here.' : 'Alerts back on.')) setTimeout(refresh, 500);
      b.disabled = false;
    });
    return b;
  }
  function alertLine(b) {
    const x = b.alert || {};
    const keyed = {key: b.key};
    if (x.state === 'pending') return el('p', {class: 'sub alarm'}, el('span', {class: 'chip wait', text: 'Not alerted yet'}),
      ' alerts in ' + dur(x.fires_in || 0) + ' if it lasts');
    if (x.state === 'off') return el('p', {class: 'sub alarm', text: 'Shown here only: no alert for this kind'});
    if (x.snoozed_until) return el('p', {class: 'sub alarm'}, el('span', {class: 'chip', text: 'Snoozed'}), ' until ' + clock(x.snoozed_until),
      desk() ? el('span', {class: 'ctl'}, snooze(keyed, 0, 'Unsnooze'), snooze({group: x.group}, 0, 'Unsnooze group')) : null);
    if (x.state !== 'firing') return null;
    return el('p', {class: 'sub alarm'}, el('span', {class: 'chip bad', text: 'Alerted'}),
      desk() ? el('span', {class: 'ctl'}, ' Snooze ', snooze(keyed, 1, '1 h'), snooze(keyed, 4, '4 h'), snooze(keyed, 24, '24 h'),
        x.group ? snooze({group: x.group}, 4, 'Group 4 h') : null) : null);
  }
  function blockerRow(b) {
    const label = {base: 'Base red', feed: 'Feed', machine: 'Silent', stuck: 'Stuck', check: 'Failed', runner: 'Offline', label: 'Blocked',
                   disk: 'Disk', flow: 'Not merging'}[b.kind] || 'Look';
    return el('li', {class: 'blk'}, el('span', {class: 'chip needs', text: label}),
      el('div', {class: 'what'}, a(b.url, 't', b.title),
        b.detail || b.since ? el('p', {class: 'sub'}, ...sep(join(b.detail, b.since ? since(b.since) : ''))) : null,
        alertLine(b)));
  }
  function reservationDetail(info) {
    const release = info.release || {};
    const holder = info.holder && web(release.url) ? a(release.url, '', info.holder) : info.holder;
    const place = info.ahead ? info.ahead + (info.ahead === 1 ? ' reservation ahead' : ' reservations ahead') : 'Next in line';
    let timing = 'Start time unknown';
    if (info.dead) timing += ' · the holder process has stopped';
    else if (info.start_in != null) timing = 'Rough start in ~' + mins(info.start_in);
    else if (release.seconds != null) timing += ' · current holder may release in ~' + mins(release.seconds);
    else timing += ' · no comparable duration yet';
    const unit = {reservation: 'hold', job: 'job', step: 'workflow step'}[release.basis];
    const basis = unit ? release.samples + ' earlier ' + unit + (release.samples === 1 ? '' : 's') :
      '';
    return el('div', null,
      el('p', {class: 'sub'}, holder ? [info.machine + ' is held by ', holder] : info.machine + ' has not reported a holder',
        holder && info.since ? [' · held for ', since(info.since)] : null),
      el('p', {class: 'say'}, place, ' · ', timing),
      basis ? el('p', {class: 'sub'}, 'Based on ', basis,
        release.basis === 'step' ? ' including any wait inside the step' : '',
        release.failed ? ' · ' + release.failed + ' ended in failure' : '') : null);
  }
  function uploadDetail(output) {
    const u = output?.upload;
    if (!u || !Number.isSafeInteger(u.done) || !Number.isSafeInteger(u.total) ||
        u.total <= 0 || u.done < 0 || u.done > u.total || !Number.isFinite(secs(output.at))) return null;
    const complete = u.done === u.total;
    const percent = complete ? 100 : Math.min(99, Math.round(100 * u.done / u.total));
    const files = u.done.toLocaleString() + ' of ' + u.total.toLocaleString() + (u.total === 1 ? ' file uploaded' : ' files uploaded');
    let eta;
    if (!complete) {
      const estimate = Number.isFinite(u.eta_seconds) && u.eta_seconds >= 0 ?
        'About ' + mins(u.eta_seconds) + ' left in the upload' : 'Upload time remaining is still unknown';
      eta = el('p', {class: 'upload-eta', 'data-upload-at': output.at, text: estimate});
      expireUploadEstimate(eta);
    }
    return el('div', {class: 'upload-progress'},
      el('p', {class: 'task-title', text: complete ? 'Upload complete' : output.label}),
      el('div', {class: 'upload-count'}, el('span', {text: files}), el('b', {text: percent + '%'})),
      el('progress', {max: u.total, value: u.done, 'aria-label': 'Files uploaded', 'aria-valuetext': files}),
      eta, el('p', {class: 'sub'}, 'Last upload update ', ago(output.at)));
  }
  function buildDetail(progress, job) {
    if (!progress) return null;
    const {phase, last_output: output, eta = {}} = progress;
    const waiting = progress.waiting_machine;
    const label = waiting ? 'Waiting for ' + (progress.reservation?.machine || job.machine || 'a machine') :
      phase?.label || output?.label || job.step || 'Machine reserved for the PR check';
    const observed = output && !waiting;
    // A later phase supersedes a transfer recorded earlier in the same check.
    const upload = observed && (!phase?.since || Date.parse(output.at) >= Date.parse(phase.since)) ? uploadDetail(output) : null;
    const count = eta.samples + (eta.samples === 1 ? ' past check' : ' past checks') + (eta.same_pr ? ' on this PR' : '');
    const estimate = waiting ? 'Start time unknown until the machine reports its reservation' :
      eta.seconds != null ? 'About ' + mins(eta.seconds) + ' left in this job · ' + count :
      eta.samples ? 'Finish time unknown · this job has outlasted ' + count :
      'Finish time unknown · no comparable timing yet';
    let timing;
    if (waiting && (phase?.since || job.since)) timing = ['Waiting for ', since(phase?.since || job.since)];
    else if (phase?.since) timing = [upload ? phase.label + ' started ' : 'Phase started ', ago(phase.since)];
    else if (job.step_started) timing = ['In this step for ', since(job.step_started)];
    else if (job.reservation_holder && job.since) timing = ['Machine held for ', since(job.since)];
    const status = join(timing, !waiting && !phase && !observed ? 'Waiting for a task update from the runner' : '');
    return el('div', {class: 'build-detail'},
      upload || el('p', {class: 'task-title', text: label}),
      observed && !upload ? el('p', {class: 'sub'}, phase ? ['Latest output · ', output.label, ' · '] : 'Last observed activity · ',
        ago(output.at)) : null,
      status.length ? el('p', {class: 'sub'}, ...sep(status)) : null,
      upload ? null : waiting && progress.reservation ? reservationDetail(progress.reservation) :
        el('p', {class: 'sub', text: estimate}));
  }
  function githubSteps(x) {
    if (!x.steps_total) return null;
    return el('p', {class: 'sub workflow-steps'}, ...sep(join('GitHub steps · ' + x.steps_done + ' of ' + x.steps_total + ' complete', x.step)));
  }
  function actRow(x, c) {
    const leasePR = /\bPR check #(\d+)\b/i.exec(x.title || '');
    const fromReporter = leasePR && (x.reservation_holder || x.kind === 'queued' || x.last_output);
    if (x.progress || x.steps_total || fromReporter) {
      const title = c ? '#' + c.number + ' ' + c.title : leasePR ? 'PR #' + leasePR[1] : x.title;
      const progress = x.progress || (fromReporter ? {waiting_machine: x.kind === 'queued', last_output: x.last_output} : null);
      return el('li', {class: 'mact'},
        x.alive === false ? el('p', {class: 'task-title', text: 'The check stopped while holding the machine'}) :
          progress ? buildDetail(progress, x) : el('p', {class: 'task-title'}, x.step || x.job_name || 'GitHub job running'),
        !progress && x.step_started ? el('p', {class: 'sub'}, 'In this step for ', since(x.step_started)) : null,
        el('p', {class: 'sub task-context'}, ...sep(join(a(x.url || c?.url, '', title), x.machine, x.job_name))),
        githubSteps(x));
    }
    if (c) return machineAct(x, c);
    const k = KIND[x.kind] ? x.kind : 'working';
    return el('li', null, el('span', {class: 'chip k-' + k, text: KIND[k]}),
      el('div', {class: 'what'}, web(x.url) ? a(x.url, '', x.title) : el('span', {class: 't', text: x.title}),
        el('p', {class: 'sub'}, ...sep(join(x.machine, x.detail, x.since ? since(x.since) : '')))));
  }
  // A drive that has been filling: when it will be full at that rate (rules.disk_forecast).
  const fullIn = x => x && typeof x.full_in === 'number' ? ' (full in ~' + dur(x.full_in) + ')' : '';
  function diskRow(g, d) {
    const full = g.state === 'full' || (g.runner_stopped || []).length;
    const chip = full ? ['bad', 'PR runner offline: disk full'] : g.low ? ['wait', 'Under ' + Math.round(g.warn_gb) + ' GB'] : ['run', 'Disk OK'];
    return el('div', {class: 'm-disk'},
      el('p', {class: 'sub'}, el('span', {class: 'chip ' + chip[0], text: chip[1]}), ' ',
        (g.drive || 'Build drive') + ' ' + Math.round(g.free_gb) + (g.total_gb ? '/' + Math.round(g.total_gb) : '') + ' GB free' + fullIn(d),
        g.updated ? [' · checked ', ago(g.updated)] : ''),
      (g.holders || []).length ? el('p', {class: 'sub', text: 'Biggest: ' + g.holders.slice(0, 5).map(h => h.name + ' ' + Math.round(h.gb) + ' GB').join(' · ')}) : null);
  }
  // One coding agent working now. From its own heartbeat: what it is on, its step, where it runs, when it checked in.
  // From GitHub only (an agent that sends no heartbeat): its PR or branch and what that waits on, marked as a guess.
  const CHECKS = {failed: 'checks failing', rerun: 'checks need a re-run', running: 'checks running', queued: 'checks queued', passed: 'checks pass'};
  function agentRow(x) {
    const said = x.source === 'heartbeat';
    const head = a(x.url, 't', x.number ? el('span', {class: 'num', text: '#' + x.number}) : null, x.title);
    const repo = (x.repo || '').split('/')[1] || x.repo || '';
    return el('li', null, el('span', {class: 'chip k-' + (x.agent === 'Codex' ? 'codex' : 'coding'), text: x.agent}),
      el('div', {class: 'what'}, head,
        el('p', {class: 'sub'}, el('span', {class: 'chip ' + (x.tone || ''), text: x.state}), ' ',
          said ? (x.step || '') : el('span', {class: 'guess', title: 'This agent sends no heartbeat: Fletchery guessed from its pushes and PR', text: 'seen on GitHub'})),
        el('p', {class: 'sub'}, ...sep(join(said ? x.where : '', repo, said && CHECKS[x.checks] ? CHECKS[x.checks] : '',
          x.last ? [said ? 'checked in ' : 'active ', ago(x.last)] : '')))));
  }
  // ---- where a change is in the whole process (pipeline.py) ----
  const STATUS = {running: ['run', 'Running'], queued: ['wait', 'Queued'], waiting: ['wait', 'Waiting'], failed: ['bad', 'Failed'],
                  rerun: ['needs', 'Needs re-run'], done: ['run', 'Done']};
  // Widths through the style object: the page's CSP refuses style="" attributes.
  const widthOf = (node, pct) => { node.style.width = pct + '%'; return node; };
  const mins = s => s == null ? '' : s < 90 ? Math.max(1, Math.round(s / 60)) + ' min' : span(s);
  const dotMark = {done: '✓', skipped: '–', failed: '!'};
  function stepper(c) {
    return el('ol', {class: 'stepper', 'aria-label': 'Steps'}, c.stages.map((st, i) =>
      el('li', {class: 'st ' + st.state, 'aria-current': st.state === 'now' || st.state === 'failed' ? 'step' : null},
        el('i', {'aria-hidden': 'true', text: dotMark[st.state] || String(i + 1)}), el('span', {text: st.label}))));
  }
  function stageLabel(c) { return (c.stages.find(st => st.id === c.stage) || {}).label || ''; }
  function badges(c, opts) {
    const st = STATUS[c.status] || ['', c.status], step = c.step || {};
    const left = step.late ? null : step.left != null ? step.left : null;
    return el('div', {class: 'badges'},
      opts && opts.noStage ? null : el('span', {class: 'chip ' + st[0], text: stageLabel(c)}),
      el('span', {class: 'chip ' + st[0], text: st[1]}),
      step.late ? el('span', {class: 'chip needs', text: 'Taking longer than usual'}) : null,
      c.status === 'running' || c.status === 'queued' ? (left != null ? el('span', {class: 'chip eta', text: '~' + mins(left) + ' left'}) : null) : null,
      c.kind && c.kind !== 'Change' ? el('span', {class: 'chip kind', text: c.kind}) : null);
  }
  function meter(label, pct, value, cls) {
    return el('div', {class: 'pb'}, el('span', {class: 'lbl', text: label}), el('b', {text: value}),
      el('span', {class: 'meter ' + (cls || ''), role: 'progressbar', 'aria-label': label, 'aria-valuemin': '0', 'aria-valuemax': '100',
                  'aria-valuenow': pct == null ? null : String(pct)},
        widthOf(el('span'), pct == null ? 0 : Math.max(2, pct))));
  }
  function bars(c) {
    const o = c.overall || {}, step = c.step;
    const end = c.merged ? 'to the end of its staging build' : 'to a staging build';
    const overall = o.pct != null
      ? meter('Overall', o.pct, o.pct + '%' + (o.left ? ' · ~' + mins(o.left) + ' ' + end : ''), c.status === 'failed' ? 'bad' : '')
      : el('p', {class: 'sub', text: 'Overall: still learning how long each step takes here.'});
    let here = null;
    const stopped = c.status === 'failed' || c.status === 'rerun';
    if (stopped && c.since) {
      // A failed or cancelled step's clock stops when it stopped; what counts now is how long it has waited.
      const clock = new Date(c.since).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
      here = el('p', {class: 'sub stopped'}, ...sep(join(
        step && step.elapsed ? 'This step ' + (c.status === 'failed' ? 'failed' : 'stopped') + ' after ' + mins(step.elapsed) : '',
        [c.status === 'failed' ? 'Waiting for a fix since ' : 'Waiting for a re-run since ', clock, ' (', since(c.since), ')'])));
    } else if (step && step.avg) {
      const cls = c.status === 'failed' ? 'bad' : step.late ? 'late' : 'step';
      here = meter('This step', step.late ? 100 : step.pct, mins(step.elapsed) + ' of ~' + mins(step.avg), cls);
    } else if (step && step.elapsed) {
      here = meter('This step', null, mins(step.elapsed) + ' so far · no usual time yet', 'learning');
    }
    return [overall, here];
  }
  function basis(c, onMachine) {
    const step = c.step || {}, parts = step.parts || [], part = parts[step.part];
    return el('p', {class: 'basis'}, ...sep(join(
      onMachine ? '' : step.machine || '',
      part && !onMachine ? 'now: ' + part.name + ' (' + (step.part + 1) + ' of ' + parts.length + ')' : '',
      step.avg ? 'usually ' + mins(step.avg) + (step.own ? ' for this change' : step.scope === 'all' && c.kind && c.kind !== 'Change' && c.stage !== 'staging' ? ' for any change (no ' + c.kind.toLowerCase() + ' timed yet)' : c.kind && c.kind !== 'Change' && c.stage !== 'staging' ? ' for a ' + c.kind.toLowerCase() : '') + ' (' + step.samples + ' run' + (step.samples > 1 ? 's' : '') + ')' : '',
      c.note || '')));
  }
  function changeRow(c) {
    return el('li', {class: 'chg'},
      a(c.url, 't', el('span', {class: 'num', text: '#' + c.number}), c.title),
      stepper(c), badges(c), ...bars(c), basis(c));
  }
  // B: a machine's activity in plain words, when Fletchery knows which change it is for.
  function sentence(c) {
    const what = '#' + c.number + ' ' + c.title;
    if (c.stage === 'staging') return ['Building ', el('b', {text: c.base}), ' with ', el('b', {text: what}), ' and everything merged before it'];
    if (c.stage === 'quick') return ['Quick checks on ', el('b', {text: what})];
    return ['Testing ', el('b', {text: what}), ' before it can merge'];
  }
  function partsBar(step) {
    const parts = step.parts || [];
    if (!parts.length) return null;
    return el('div', {class: 'parts'},
      el('div', {class: 'seg', 'aria-hidden': 'true'}, parts.map((p, i) => {
        const pct = i === step.part && p.state === 'now' && p.avg && step.part_elapsed != null ? Math.min(100, Math.round(100 * step.part_elapsed / p.avg)) : 50;
        return el('span', {class: p.state}, p.state === 'now' ? widthOf(el('i'), pct) : null);
      })),
      el('div', {class: 'legend'}, parts.map((p, i) => el('span', {class: i === step.part ? 'on' : '', text: p.name}))));
  }
  function machineAct(x, c) {
    return el('li', {class: 'mact'},
      el('p', {class: 'say'}, ...sentence(c)),
      x.machine ? el('p', {class: 'sub', text: x.machine}) : null,
      c.step ? partsBar(c.step) : null,
      badges(c), ...bars(c), basis(c, true),
      c.why ? el('p', {class: 'why', text: 'Why: ' + c.why}) : null);
  }
  let changeByKey = {};
  function machineRow(m) {
    const st = m.stats || {}, g = m.disk_guard;
    const bits = join(
      typeof st.cpu === 'number' ? 'CPU ' + Math.round(st.cpu) + '%' : '',
      typeof st.ram_used_gb === 'number' ? 'RAM ' + st.ram_used_gb + '/' + st.ram_total_gb + ' GB' : '',
      ...(m.disks || []).filter(x => !g || x.name.replace(/[:\\/]+$/, '').toUpperCase() !== (g.drive || '').replace(/[:\\/]+$/, '').toUpperCase())
        .map(x => x.name + ' ' + Math.round(x.free_gb) + (x.total_gb ? '/' + Math.round(x.total_gb) : '') + ' GB free' + fullIn(x)));
    const state = !m.online ? ['off', 'Not reporting'] : m.busy ? ['busy', 'Busy'] : ['on', 'Idle'];
    const tools = Object.entries(m.procs || {}).filter(([k]) => k !== 'claude').map(([k, n]) => k + (n > 1 ? ' ×' + n : ''));
    return el('li', null,
      el('div', {class: 'm-head'}, el('span', {class: 'm-name'}, dot(state[0], state[1]), m.name),
        el('span', {class: 'm-stats', text: bits.join(' · ')})),
      g ? diskRow(g, (m.disks || []).find(x => x.name.replace(/[:\\/]+$/, '').toUpperCase() === (g.drive || '').replace(/[:\\/]+$/, '').toUpperCase())) : null,
      !m.online ? el('p', {class: 'sub'}, m.last_report ? ['Last report ', ago(m.last_report)] : 'No reporter on this machine yet') : null,
      m.activities && m.activities.length ? el('ul', {class: 'acts'}, (seen => m.activities.map(x => { const c = changeByKey[x.change]; if (c && seen.has(c.key)) return null; if (c) seen.add(c.key); return actRow(x, c); }))(new Set())) :
        el('p', {class: 'sub', text: m.online ? 'Idle' : ''}),
      tools.length ? el('p', {class: 'sub', text: 'Running: ' + tools.join(', ')}) : null,
      m.runners && m.runners.length ? el('p', {class: 'sub', text: 'Runners: ' + m.runners.join(', ')}) : null);
  }
  function runnerRow(r) {
    const j = r.job, stepFor = j && j.step_started ? (Date.now() - Date.parse(j.step_started)) / 60000 : 0;
    const chip = !r.online ? el('span', {class: 'chip needs', text: 'Offline'}) :
      j && j.pr_closed ? el('span', {class: 'chip needs', text: 'Not needed'}) :
      stepFor > 60 ? el('span', {class: 'chip needs', text: 'Step over 1 h'}) :
      r.busy || j ? el('span', {class: 'chip wait', text: 'Busy'}) : el('span', {class: 'chip run', text: 'Idle'});
    return el('li', null, el('div', {class: 'r-row'},
      el('div', null, el('div', {class: 'title', text: r.name}), el('p', {class: 'sub', text: join(r.machine, (r.labels || []).join(', ')).join(' · ')}),
        j ? buildDetail(j.progress, j) : null,
        j && j.progress ? el('p', {class: 'sub'}, a(j.url, '', j.title)) :
          j ? el('p', {class: 'sub'}, a(j.url, '', j.title), ...sep(['', ...join(j.step ? 'step "' + j.step + '"' + (j.step_started ? ' for ' : '') : '',
          j.step_started ? since(j.step_started) : j.started ? since(j.started) : '')])) : null,
        j && j.progress ? githubSteps(j) : null,
        j && j.pr_closed ? el('p', {class: 'sub', text: 'Its pull request is closed: nothing needs this run.'}) : null,
        j && j.id && desk() ? el('div', {class: 'ctl'}, control('Cancel run', '/api/actions/cancel', {repo: j.repo, id: j.id}, 'Cancel this GitHub Actions run?')) : null),
      chip));
  }
  function bar(part) {   // width set through the DOM: the page's CSP allows no inline style attributes
    const fillEl = el('i');
    fillEl.style.width = Math.round(100 * Math.min(1, Math.max(0, part))) + '%';
    return el('div', {class: 'bar', 'aria-hidden': 'true'}, fillEl);
  }
  function control(text, path, body, ask) {
    const b = el('button', {type: 'button', class: 'small', text});
    b.addEventListener('click', async () => {
      b.disabled = true;
      if (await window.fletcheryAct(path, body, 'Sent to GitHub.', ask)) setTimeout(refresh, 1500);
      b.disabled = false;
    });
    return b;
  }
  const desk = () => document.body.classList.contains('controls') && typeof window.fletcheryAct === 'function';
  function runControls(r) {
    if (!desk()) return null;
    const id = {repo: r.repo, id: r.id};
    if (r.status !== 'completed') return el('div', {class: 'ctl'}, control('Cancel run', '/api/actions/cancel', id, 'Cancel this GitHub Actions run?'));
    const bad = ['failure', 'timed_out', 'cancelled', 'startup_failure'].includes(r.conclusion);
    return el('div', {class: 'ctl'}, bad ? control('Re-run failed jobs', '/api/actions/rerun', {...id, failed: true}) : null,
      control('Re-run', '/api/actions/rerun', id));
  }
  function runRow(r) {
    const going = r.status === 'in_progress', waiting = r.status !== 'completed' && !going;
    const ico = going ? ['run', '◐', 'Running'] : waiting ? ['wait', '○', 'Queued'] :
      r.conclusion === 'success' ? ['ok', '✓', 'Passed'] : r.conclusion === 'skipped' || r.conclusion === 'neutral' ? ['wait', '–', 'Skipped'] :
      r.conclusion === 'cancelled' ? ['wait', '⊘', 'Cancelled'] : ['bad', '✕', 'Failed'];
    const where = join(r.repo.split('/')[1], r.runner, going && r.step && !r.progress ? 'step ' + (r.steps_done + 1) + '/' + r.steps_total + ': ' + r.step : '');
    const when = going ? ['for ', since(r.started)] : waiting ? ['queued ', since(r.created)] : [ago(r.updated)];
    return el('li', null, el('div', {class: 'run-row'}, el('span', {class: 'ico ' + ico[0], role: 'img', 'aria-label': ico[2], text: ico[1]}),
      el('div', null, a(r.url, 'title', r.name + (r.pr ? ' · PR #' + r.pr : '')),
        buildDetail(r.progress, r), el('p', {class: 'sub'}, ...sep(where), where.length ? ' · ' : '', ...when),
        r.progress ? githubSteps(r) : going && r.steps_total ? bar(r.steps_done / r.steps_total) : null, runControls(r))));
  }
  function prRow(p) {
    const c = p.checks || {};
    const chip = p.draft ? ['', 'Draft'] : {failed: ['bad', 'Failing'], running: ['wait', 'Checking'], queued: ['wait', 'Check queued'],
      rerun: ['wait', 'Needs re-run'], passed: ['run', 'Checks pass'], none: ['', 'No checks']}[c.state] || ['', c.state];
    const again = c.rerun || [];
    const what = c.state === 'failed' ? 'failed: ' + (c.failed || []).join(', ') : c.state === 'running' || c.state === 'queued' ?
      (c.running || []).join(', ') + (c.total ? ' (' + c.done + '/' + c.total + ' done)' : '') :
      c.state === 'rerun' ? 'no verdict: ' + again.map(x => x.name + ' (' + x.why + ')').join(', ') : '';
    return el('li', null, el('div', {class: 'r-row'},
      el('div', null, a(p.url, 'title', el('span', {class: 'num', text: '#' + p.number}), p.title),
        el('p', {class: 'sub'}, ...sep(join(p.repo.split('/')[1], p.author, what, ago(p.updated))))),
      el('div', {class: 'chips'}, el('span', {class: 'chip ' + chip[0], text: chip[1]}),
        c.cause && c.state === 'failed' ? el('span', {class: 'chip c-' + c.cause, text: CAUSE[c.cause]}) : null,
        p.auto_merge ? el('span', {class: 'chip run', text: 'Auto-merge on'}) : null)),
      desk() && again.some(x => x.job) ? el('div', {class: 'ctl'}, again.filter(x => x.job).map(x =>
        control('Re-run ' + x.name, '/api/actions/rerun-job', {repo: p.repo, id: x.job}))) : null);
  }
  function issueRow(i) {
    return el('li', null, a(i.url, 'title', el('span', {class: 'num', text: '#' + i.number}), i.title),
      i.blocked ? el('span', {class: 'chip needs', text: 'Blocked'}) : null,
      ...(i.labels || []).slice(0, 4).map(l => el('span', {class: 'chip', text: l})),
      el('p', {class: 'sub'}, ...sep(join(i.repo.split('/')[1], ['updated ', ago(i.updated)]))));
  }
  // Projects: the profile's (several repositories each), else one per repository with something to show.
  let picked = new Set();       // the chosen project's repositories
  function repoChips(d) {
    const seen = new Set([...(d.runs || []), ...(d.prs || []), ...(d.issues || [])].map(x => x.repo).filter(Boolean));
    for (const [name, count] of Object.entries(d.open_pr_counts || {})) if (count || name === repo) seen.add(name);
    for (const name of Object.keys(d.pr_count_status?.stale || {})) seen.add(name);
    if (d.pr_count_status) for (const r of d.repos || []) if (!Object.hasOwn(d.open_pr_counts || {}, r.name)) seen.add(r.name);
    const projects = (d.projects || []).filter(p => p.name === repo || (p.repos || []).some(r => seen.has(r)));
    const covered = new Set(projects.flatMap(p => p.repos));
    const list = [...projects, ...[...seen].filter(r => !covered.has(r)).sort().map(r => ({name: r.split('/')[1] || r, repos: [r]}))];
    const key = p => p.repos.length > 1 ? p.name : p.repos[0];
    const cur = list.find(p => key(p) === repo);
    if (repo && !cur) repo = '';
    picked = new Set(cur ? cur.repos : []);
    $('scopeName').textContent = cur ? cur.name : 'All projects';
    const box = $('liveRepos');
    box.hidden = !list.length || (list.length === 1 && !repo && !d.pr_count_status?.partial);
    const chip = (value, label) => {
      const b = el('button', {type: 'button', 'aria-pressed': String(repo === value), text: label});
      b.addEventListener('click', () => { repo = value; try { localStorage.setItem('fletchery-live-repo', repo); } catch (e) { /* ignore */ } if (last) draw(last); });
      return b;
    };
    box.replaceChildren(chip('', 'All projects'), ...list.map(p => chip(key(p), p.name)));
  }
  const mine = x => !repo || !x.repo || picked.has(x.repo);
  function draw(full) {
    if (full.github_login) {
      try {
        const previous = localStorage.getItem('fletchery-live-login');
        if (previous && previous !== full.github_login) {
          repo = '';
          localStorage.removeItem('fletchery-live-repo');
        }
        localStorage.setItem('fletchery-live-login', full.github_login);
      } catch (e) { /* storage unavailable */ }
    }
    repoChips(full);
    const d = repo ? {...full, problems: (full.problems || []).filter(mine), blockers: (full.blockers || []).filter(mine), parked: (full.parked || []).filter(mine), runs: (full.runs || []).filter(mine),
                      prs: (full.prs || []).filter(mine), issues: (full.issues || []).filter(mine)} : full;
    const scopedPrs = d.prs || [], pr = prCount(full, scopedPrs);
    stats(full, pr);
    drawBases(full);
    drawFlow(full);
    drawProblems(d, full);
    drawQueue(full);
    const blk = d.blockers || [];
    $('liveBlockersCard').classList.toggle('calm', !blk.length);
    $('n-lblock').textContent = blk.length ? '(' + blk.length + ')' : '';
    fill($('liveBlockers'), blk.map(blockerRow), 'Nothing stuck. Everything is moving or idle.');
    // Alerts that cleared in the last two hours: named under the card.
    const gone = ((full.alerts || {}).cleared || []).filter(mine);
    const cl = $('liveCleared');
    if (cl) {
      cl.hidden = !gone.length;
      cl.replaceChildren(...(gone.length ? ['Cleared recently: ', ...sep(gone.slice(0, 6).map(x => el('span', null, x.title, ' (', clock(x.cleared), ')')))] : []));
    }
    // Issues parked as blocked on other work: named under the card, not counted as stuck.
    const parked = (d.parked || []).filter(mine);
    const pk = $('liveParked');
    if (pk) {
      pk.hidden = !parked.length;
      pk.replaceChildren(...(parked.length ? [parked.length + ' issue' + (parked.length > 1 ? 's' : '') + ' parked until other work lands (not stuck): ',
        ...sep(parked.map(x => a(x.url, '', x.title.split(' ')[0])))] : []));
    }
    const building = full.building || [];
    $('n-lbuild').textContent = building.length ? '(' + building.length + ')' : '';
    $('liveBuildingCard').classList.toggle('calm', !building.length);
    changeByKey = Object.fromEntries((full.changes || []).map(c => [c.key, c]));
    fill($('liveBuilding'), building.map(x => actRow(x, changeByKey[x.change])), 'Nothing is building right now.');
    const changes = (full.changes || []).filter(mine);
    $('n-lchg').textContent = changes.length ? '(' + changes.length + ')' : '';
    $('liveChangesCard').classList.toggle('calm', !changes.length);
    fill($('liveChanges'), changes.map(changeRow), 'No open or recently merged changes.');
    const agents = (full.agents || []).filter(x => !repo || picked.has(x.repo));
    $('n-lagents').textContent = agents.length ? '(' + agents.length + ')' : '';
    $('liveAgentsCard').classList.toggle('calm', !agents.length);
    fill($('liveAgents'), agents.map(agentRow), 'No Claude or Codex agent is working right now.');
    const quiet = (full.counts || {}).agents_idle || 0;
    $('liveAgentsQuiet').textContent = quiet ? quiet + ' more agent' + (quiet > 1 ? 's were' : ' was') + ' active earlier today, but not in the last 15 minutes.' : '';
    $('liveAgentsQuiet').hidden = !quiet;
    fill($('liveMachines'), (d.machines || []).map(machineRow), 'No machine has reported yet, and no runner is registered.');
    const note = (d.fresh || {}).runner_note;
    $('liveRunnerNote').hidden = !note; $('liveRunnerNote').textContent = note || '';
    $('n-lrun').textContent = (d.runners || []).length ? '(' + d.runners.length + ')' : '';
    fill($('liveRunners'), (d.runners || []).map(runnerRow), 'No self-hosted runners found.');
    fill($('liveRuns'), (d.runs || []).slice(0, 25).map(runRow), 'No recent Actions runs.', (d.runs || []).length - 25);
    $('n-lpr').textContent = '(' + (pr.total ?? '—') + (pr.partial ? ' · Partial' : '') + ')';
    const c = full.counts || {};
    fill($('livePrs'), scopedPrs.map(prRow), pr.partial || pr.total > 0 ? 'Waiting for pull request details from GitHub.' : 'No open pull requests.',
      pr.partial || repo ? 0 : pr.total - scopedPrs.length);
    const nIss = repo ? (d.issues || []).length : (full.counts || {}).issues || (d.issues || []).length;
    $('n-liss').textContent = nIss ? '(' + nIss + ')' : '';
    fill($('liveIssues'), (d.issues || []).slice(0, 40).map(issueRow), 'No open issues.', nIss - Math.min(40, (d.issues || []).length));
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
      failed = e instanceof TypeError || (e && e.name === 'AbortError') ? (document.body.classList.contains('desktop') ? 'The Fletchery backend is not answering.' : 'Can’t reach the PC from this phone. Check that Tailscale is on (Connected) on the phone; if it is, Fletchery on the PC may have stopped.') : String(e.message || e);
    }
    clearTimeout(giveUp);
    stale();
    if (!document.hidden) timer = setTimeout(refresh, failed ? RETRY_MS : (tab === 'live' || tab === 'queue') ? OPEN_MS : BACK_MS);
  }
  try {
    const kept = JSON.parse(localStorage.getItem(KEEP) || 'null');
    if (kept && kept.body && Date.now() - kept.at < 86400000) { last = kept.body; lastOk = kept.at; draw(kept.body); }
  } catch (e) { /* nothing kept */ }
  document.addEventListener('fletchery-refresh', () => setTimeout(refresh, 1500));
  document.addEventListener('fletchery-tab', e => { tab = e.detail; if (tab === 'live' || tab === 'queue') refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
  window.addEventListener('online', refresh);
  setInterval(tick, 1000);
  refresh();
})();
