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
  const CAP = {liveBuilding: 6, liveChanges: 5, liveAgents: 6, liveBlockers: 4, liveMachines: 4, liveRunners: 6, liveRuns: 5, livePrs: 6, liveIssues: 6};
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
    document.title = (c.blockers ? '(' + c.blockers + ') ' : '') + 'Fletchery';
  }
  // ---- Flow: is work moving? (flow.py) ----
  const VERDICT = {moving: ['run', 'Moving'], idle: ['', 'Idle'], slow: ['wait', 'Slow'], stopped: ['bad', 'Stopped']};
  const dur = s => s == null ? '–' : s < 90 ? Math.round(s) + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : s < 172800 ? (s / 3600).toFixed(1).replace('.0', '') + ' h' : Math.round(s / 86400) + ' d';
  function drawFlow(full) {
    const box = $('liveFlow'), all = full.flow || {};
    const f = (repo ? (all.repos || {})[repo] : all.all) || null;
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
        num('Wait for a machine', dur(f.wait_p50), f.wait_p90 != null ? 'p90 ' + dur(f.wait_p90) : 'today'),
        num('Base went red', f.red_share == null ? '–' : Math.round(100 * f.red_share) + '%', f.red_n ? 'of ' + f.red_n + ' runs, 7 days' : '')),
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
        first ? el('p', null, el('b', {text: 'First bad: '}), prLink(first), ' ' + (first.title || '') + (first.agent ? ' (' + first.agent + ')' : ''),
          sus.length > 1 ? [' · also merged since the last green: ', ...sep(sus.filter(x => x !== first).map(prLink))] : '') : null,
        el('p', null, el('b', {text: 'Fix in line: '}), (h.fixes || []).length ? sep(h.fixes.map(f => [a(f.url, '', '#' + f.number), ' ' + (f.title || '') +
          ' (' + (CHK[f.state] || f.state || 'waiting') + (f.auto_merge ? ', merges by itself' : '') + ')'])) : 'none yet',
          (h.issues || []).length ? [' · tracked in ', ...sep(h.issues.map(i => a(i.url, '', '#' + i.number)))] : ''),
        h.held ? el('p', {class: 'sub', text: h.held + ' open PR' + (h.held > 1 ? 's fail' : ' fails') + ' the same check' + (h.held > 1 ? 's' : '') + ' because of this; fixing ' + h.branch + ' clears ' + (h.held > 1 ? 'them' : 'it') + '.'}) : null);
    }));
  }
  function blockerRow(b) {
    const label = {base: 'Base red', feed: 'Feed', machine: 'Silent', stuck: 'Stuck', check: 'Failed', runner: 'Offline', label: 'Blocked', disk: 'Low disk'}[b.kind] || 'Look';
    return el('li', {class: 'blk'}, el('span', {class: 'chip needs', text: label}),
      el('div', {class: 'what'}, a(b.url, 't', b.title),
        b.detail || b.since ? el('p', {class: 'sub'}, ...sep(join(b.detail, b.since ? since(b.since) : ''))) : null));
  }
  function actRow(x) {
    const k = KIND[x.kind] ? x.kind : 'working';
    return el('li', null, el('span', {class: 'chip k-' + k, text: KIND[k]}),
      el('div', {class: 'what'}, web(x.url) ? a(x.url, '', x.title) : el('span', {class: 't', text: x.title}),
        el('p', {class: 'sub'}, ...sep(join(x.detail, x.since ? since(x.since) : '')))));
  }
  function diskRow(g) {
    const full = g.state === 'full' || (g.runner_stopped || []).length;
    const chip = full ? ['bad', 'PR runner offline: disk full'] : g.low ? ['wait', 'Under ' + Math.round(g.warn_gb) + ' GB'] : ['run', 'Disk OK'];
    return el('div', {class: 'm-disk'},
      el('p', {class: 'sub'}, el('span', {class: 'chip ' + chip[0], text: chip[1]}), ' ',
        (g.drive || 'Build drive') + ' ' + Math.round(g.free_gb) + (g.total_gb ? '/' + Math.round(g.total_gb) : '') + ' GB free',
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
      here = el('p', {class: 'sub', text: 'This step: ' + mins(step.elapsed) + ' so far (no usual time yet)'});
    }
    return [overall, here];
  }
  function basis(c, onMachine) {
    const step = c.step || {}, parts = step.parts || [], part = parts[step.part];
    return el('p', {class: 'basis'}, ...sep(join(
      onMachine ? '' : step.machine || '',
      part && !onMachine ? 'now: ' + part.name + ' (' + (step.part + 1) + ' of ' + parts.length + ')' : '',
      step.avg ? 'usually ' + mins(step.avg) + (step.own ? ' for this change' : c.kind && c.kind !== 'Change' && c.stage !== 'staging' ? ' for a ' + c.kind.toLowerCase() : '') + ' (' + step.samples + ' run' + (step.samples > 1 ? 's' : '') + ')' : '',
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
        .map(x => x.name + ' ' + Math.round(x.free_gb) + (x.total_gb ? '/' + Math.round(x.total_gb) : '') + ' GB free'));
    const state = !m.online ? ['off', 'Not reporting'] : m.busy ? ['busy', 'Busy'] : ['on', 'Idle'];
    const tools = Object.entries(m.procs || {}).filter(([k]) => k !== 'claude').map(([k, n]) => k + (n > 1 ? ' ×' + n : ''));
    return el('li', null,
      el('div', {class: 'm-head'}, el('span', {class: 'm-name'}, dot(state[0], state[1]), m.name),
        el('span', {class: 'm-stats', text: bits.join(' · ')})),
      g ? diskRow(g) : null,
      !m.online ? el('p', {class: 'sub'}, m.last_report ? ['Last report ', ago(m.last_report)] : 'No reporter on this machine yet') : null,
      m.activities && m.activities.length ? el('ul', {class: 'acts'}, (seen => m.activities.map(x => { const c = changeByKey[x.change]; if (c && seen.has(c.key)) return null; if (c) seen.add(c.key); return c ? machineAct(x, c) : actRow(x); }))(new Set())) :
        el('p', {class: 'sub', text: m.online ? 'Idle' : ''}),
      tools.length ? el('p', {class: 'sub', text: 'Running: ' + tools.join(', ')}) : null,
      m.runners && m.runners.length ? el('p', {class: 'sub', text: 'Runners: ' + m.runners.join(', ')}) : null);
  }
  function runnerRow(r) {
    const j = r.job, stepFor = j && j.step_started ? (Date.now() - Date.parse(j.step_started)) / 60000 : 0;
    const chip = !r.online ? el('span', {class: 'chip needs', text: 'Offline'}) :
      j && j.pr_closed ? el('span', {class: 'chip needs', text: 'Not needed'}) :
      stepFor > 60 ? el('span', {class: 'chip needs', text: 'Step too long'}) :
      r.busy || j ? el('span', {class: 'chip wait', text: 'Busy'}) : el('span', {class: 'chip run', text: 'Idle'});
    return el('li', null, el('div', {class: 'r-row'},
      el('div', null, el('div', {class: 'title', text: r.name}), el('p', {class: 'sub', text: join(r.machine, (r.labels || []).join(', ')).join(' · ')}),
        j ? el('p', {class: 'sub'}, a(j.url, '', j.title), ...sep(['', ...join(j.step ? 'step "' + j.step + '"' + (j.step_started ? ' for ' : '') : '',
          j.step_started ? since(j.step_started) : j.started ? since(j.started) : '')])) : null,
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
    const where = join(r.repo.split('/')[1], r.runner, going && r.step ? 'step ' + (r.steps_done + 1) + '/' + r.steps_total + ': ' + r.step : '');
    const when = going ? ['for ', since(r.started)] : waiting ? ['queued ', since(r.created)] : [ago(r.updated)];
    return el('li', null, el('div', {class: 'run-row'}, el('span', {class: 'ico ' + ico[0], role: 'img', 'aria-label': ico[2], text: ico[1]}),
      el('div', null, a(r.url, 'title', r.name + (r.pr ? ' · PR #' + r.pr : '')),
        el('p', {class: 'sub'}, ...sep(where), where.length ? ' · ' : '', ...when),
        going && r.steps_total ? bar(r.steps_done / r.steps_total) : null, runControls(r))));
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
  function repoChips(d) {
    const names = [...new Set([...(d.runs || []), ...(d.prs || []), ...(d.issues || [])].map(x => x.repo).filter(Boolean))].sort();
    if (repo && !names.includes(repo)) repo = '';
    const box = $('liveRepos');
    box.hidden = names.length < 2;
    const chip = (value, label) => {
      const b = el('button', {type: 'button', 'aria-pressed': String(repo === value), text: label});
      b.addEventListener('click', () => { repo = value; try { localStorage.setItem('fletchery-live-repo', repo); } catch (e) { /* ignore */ } if (last) draw(last); });
      return b;
    };
    box.replaceChildren(chip('', 'All projects'), ...names.map(n => chip(n, n.split('/')[1] || n)));
  }
  const mine = x => !repo || !x.repo || x.repo === repo;
  function draw(full) {
    repoChips(full);
    const d = repo ? {...full, blockers: (full.blockers || []).filter(mine), parked: (full.parked || []).filter(mine), runs: (full.runs || []).filter(mine),
                      prs: (full.prs || []).filter(mine), issues: (full.issues || []).filter(mine)} : full;
    stats(full);
    drawBases(full);
    drawFlow(full);
    const blk = d.blockers || [];
    $('liveBlockersCard').classList.toggle('calm', !blk.length);
    $('n-lblock').textContent = blk.length ? '(' + blk.length + ')' : '';
    fill($('liveBlockers'), blk.map(blockerRow), 'Nothing stuck. Everything is moving or idle.');
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
    fill($('liveBuilding'), building.map(x => actRow({...x, detail: join(x.machine, x.detail).join(' · ')})), 'Nothing is building right now.');
    changeByKey = Object.fromEntries((full.changes || []).map(c => [c.key, c]));
    const changes = (full.changes || []).filter(mine);
    $('n-lchg').textContent = changes.length ? '(' + changes.length + ')' : '';
    $('liveChangesCard').classList.toggle('calm', !changes.length);
    fill($('liveChanges'), changes.map(changeRow), 'No open or recently merged changes.');
    const agents = (full.agents || []).filter(x => !repo || x.repo === repo);
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
    $('n-lpr').textContent = (d.prs || []).length ? '(' + (repo ? d.prs.length : (full.counts || {}).prs || d.prs.length) + ')' : '';
    const c = full.counts || {};
    fill($('livePrs'), (d.prs || []).map(prRow), 'No open pull requests.', repo ? 0 : (c.prs || 0) - (d.prs || []).length);
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
    if (!document.hidden) timer = setTimeout(refresh, failed ? RETRY_MS : tab === 'live' ? OPEN_MS : BACK_MS);
  }
  try {
    const kept = JSON.parse(localStorage.getItem(KEEP) || 'null');
    if (kept && kept.body && Date.now() - kept.at < 86400000) { last = kept.body; lastOk = kept.at; draw(kept.body); }
  } catch (e) { /* nothing kept */ }
  document.addEventListener('fletchery-refresh', () => setTimeout(refresh, 1500));
  document.addEventListener('fletchery-tab', e => { tab = e.detail; if (tab === 'live') refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
  window.addEventListener('online', refresh);
  setInterval(tick, 1000);
  refresh();
})();
