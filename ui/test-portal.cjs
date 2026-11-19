// Browser checks for Fletchery's page (ui/portal) against a local fixture; never talks to a real Fletchery.
// Run with Playwright installed: node ui/test-portal.cjs   (FLETCHERY_BROWSER=<chromium> off Windows;
// FLETCHERY_PORTAL_SHOTS=<folder> also saves screenshots)
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, devices } = require(process.env.FLETCHERY_PLAYWRIGHT || 'playwright');

const ago = minutes => new Date(Date.now() - minutes * 60000).toISOString();
const gh = 'https://github.com/example/harbor';
const job = (id, state, extra) => ({id, recipe: 'package', state, machine: 'NODE-01', ref: 'main', sha: 'a1b2c3d4e5', version: '0.4.6',
  requested_by: 'build 0.4.6', created: ago(30), started: state === 'queued' ? '' : ago(25), finished: '', summary: '', cancel: false,
  log_bytes: 2048, steps: [{name: 'Build the editor', state: 'done', start: ago(25), end: ago(12)}, {name: 'Package 0.4.6', state: 'running', start: ago(12), end: ''}], ...extra});
const data = {
  github_login: 'demo-user', project: 'Harbor', repo: 'example/harbor', at: ago(0), started: ago(185), version: 'abc12345', base: 'main',
  build_machines: ['NODE-01'], machines_on: true, held: false, errors: [], restarting: '',
  builds: [
    {id: 'b3', version: '0.4.6', state: 'running', ref: 'main', sha: 'a1b2c3d4e5', at: ago(30), started: ago(25), finished: '', machine: 'NODE-01', job: 7, reason: 'Building on NODE-01.', publish: '', publish_reason: '', tests: 'full', repeat: '', links: []},
    {id: 'b4', version: '0.4.7', state: 'scheduled', ref: 'main', sha: '', at: ago(-600), started: '', finished: '', machine: '', job: 0, reason: '', publish: '', publish_reason: '', tests: '', repeat: 'daily', links: []},
    {id: 'b2', version: '0.4.5', state: 'done', ref: 'main', sha: '326ed7bfd3', at: ago(900), started: ago(890), finished: ago(800), machine: 'NODE-01', job: 5, reason: 'Download ready', publish: 'published', publish_reason: '', tests: '', repeat: '',
      links: [{label: 'GitHub download', url: gh + '/releases/tag/build-0.4.5'}, {label: 'Google Drive', url: 'https://drive.google.com/file/d/example'}]},
    {id: 'b1', version: '0.4.4', state: 'failed', ref: 'main', sha: '99887766aa', at: ago(2000), started: ago(1990), finished: ago(1950), machine: 'NODE-01', job: 3, reason: 'Compile failed in <Game>', publish: '', publish_reason: '', tests: '', repeat: '', links: [{label: 'bad', url: 'javascript:alert(1)'}]},
  ],
  machines: [{name: 'NODE-01', online: true, last_seen: ago(0), labels: ['os:windows', 'role:cook', 'ram:24'], builds: true, paused: [], job: job(7, 'running')},
             {name: 'MY-PC', online: true, last_seen: ago(0), labels: ['os:windows', 'role:cook'], builds: false, paused: ['builds'], job: null}],
  jobs: [job(7, 'running'), job(6, 'failed', {recipe: 'checkout-only', finished: ago(40), summary: 'git fetch failed', steps: []})],
  scratch: {checked_at: ago(20), mode: 'trim', freed_gb: 3.2},
  log: [{at: ago(5), line: 'builds: 0.4.6: job 7 queued'}, {at: ago(1), line: '<script>window.hacked = 1</script>'}],
};
let mode = 'phone';   // phone | readonly | desktop: what the backend tells this screen it may do
let push = {available: true, key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', events: [], phones: 0};
let down = false;
const live = JSON.parse(fs.readFileSync(path.join(__dirname, 'test-live-fixture.json'), 'utf8'));   // devwatch.py's own output
Object.assign(live.building[0], {change: 'Acme/Game#12', job_name: 'Compile and test', step: 'Build editor',
  step_started: new Date(Date.now() - 60000).toISOString(), steps_done: 2, steps_total: 5,
  progress: {phase: {label: 'Importing environments', since: new Date().toISOString()}, eta: {seconds: 720, samples: 3, same_pr: true}}});
let liveAt = new Date().toISOString();
let off = false;   // the whole PC is unreachable: not even the page's own files answer
const types = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.webmanifest': 'application/manifest+json', '.png': 'image/png'};
const csp = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const posts = [];
let layoutFile = null;   // what the backend keeps in <Name>.layout.json
const server = http.createServer((req, res) => {
  const url = req.url.split('?')[0];
  const send = (code, body, type) => { res.writeHead(code, {'Content-Type': type, 'Content-Security-Policy': csp, 'Cache-Control': 'no-store'}); res.end(body); };
  if (off) return req.socket.destroy();
  if (req.method === 'POST') {
    let body = '';
    req.on('data', c => { body += c; });
    req.on('end', () => {
      const parsed = JSON.parse(body || '{}');
      if (url.startsWith('/api/push')) return send(200, '{}', 'application/json');
      if (url.startsWith('/api/layout')) {
        if (mode === 'readonly') return send(403, JSON.stringify({error: 'read-only'}), 'application/json');
        layoutFile = url === '/api/layout/reset' ? null : parsed.layout;
        return send(200, JSON.stringify({layout: layoutFile}), 'application/json');
      }
      posts.push([url, parsed]);
      if (mode === 'readonly') return send(403, JSON.stringify({error: 'Do this in the Fletchery window on the PC.'}), 'application/json');
      if (parsed.id === 'nope') return send(400, JSON.stringify({error: 'Build not found.'}), 'application/json');
      send(200, JSON.stringify(url === '/api/builds/add' ? {...parsed, id: 'b9'} : {ok: true}), 'application/json');
    });
    return;
  }
  if (url === '/api/summary') return down ? req.socket.destroy() : send(200, JSON.stringify({...data, desktop: mode === 'desktop', controls: mode !== 'readonly'}), 'application/json');
  if (url === '/api/live') return down ? req.socket.destroy() : send(200, JSON.stringify({...live, at: liveAt, fresh: {...live.fresh, github: liveAt}}), 'application/json');
  if (url === '/api/github/accounts') return send(200, JSON.stringify({logins: ['demo-user', 'second-user']}), 'application/json');
  if (url === '/api/push') return send(200, JSON.stringify(push), 'application/json');
  if (url === '/api/layout') return send(200, JSON.stringify({layout: layoutFile, can_save: mode !== 'readonly'}), 'application/json');
  if (url === '/api/jobs/log') return send(200, 'step 1\nstep 2', 'text/plain');
  const file = path.join(__dirname, 'portal', url === '/' ? 'index.html' : path.basename(url));
  if (!fs.existsSync(file)) return send(404, '{}', 'application/json');
  send(200, fs.readFileSync(file), types[path.extname(file)] || 'application/octet-stream');
});

(async () => {
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const base = 'http://127.0.0.1:' + server.address().port;
  const shots = process.env.FLETCHERY_PORTAL_SHOTS;
  if (shots) fs.mkdirSync(shots, {recursive: true});
  const executablePath = process.env.FLETCHERY_BROWSER;
  const browser = await chromium.launch(executablePath ? {executablePath} : {channel: 'msedge'});
  const errors = [];
  const watch = page => {
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', e => errors.push(String(e)));
    page.on('dialog', d => d.accept());   // the confirms on stop, cancel, pause all and restart
  };
  const lastPost = () => posts[posts.length - 1];
  try {
    const phone = devices['iPhone 14'];
    const context = await browser.newContext({viewport: phone.viewport, deviceScaleFactor: shots ? phone.deviceScaleFactor : 1, isMobile: true, hasTouch: true, colorScheme: 'light'});
    const page = await context.newPage();
    watch(page);
    const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

    // Live opens first: machines, runners, Actions, PRs, issues and what is stuck, from one feed.
    await page.goto(base + '/');
    await page.waitForSelector('#liveMachines .m-name');
    assert.equal(await page.isVisible('#view-live'), true);
    assert.equal(await page.isVisible('#liveStale'), false);
    assert.deepEqual(await page.$$eval('#liveStats b', list => list.map(b => b.textContent)), ['2/3', '1', '2', '6']);
    assert.match(await page.textContent('#liveStats'), /running now · 1 queued/);
    assert.equal(await page.textContent('#b-live'), '6');
    assert.match(await page.textContent('#liveBlockers'), /Build editor" has run 75 min/);
    // B: a machine says in plain words what it does for which change, with its parts, badges, two bars and why
    const machinesText = await page.textContent('#liveMachines');
    assert.match(machinesText, /Gaming PC[\s\S]*Testing #12 Bus lines before it can merge[\s\S]*Specs[\s\S]*Running[\s\S]*~12 min left[\s\S]*Town change[\s\S]*Overall[\s\S]*48%[\s\S]*This step[\s\S]*31 min of ~43 min[\s\S]*Why:/);
    assert.match(machinesText, /SF-NODE-01[\s\S]*Building Test-Staging with #9 Folk greetings and everything merged before it[\s\S]*Hand merge batch 7/);
    assert.doesNotMatch(machinesText, /PR check #12 \(GitHub run 7\)/);          // the same work is not listed twice
    // A: every change with its six steps, the current one marked, and the bars
    const changesText = await page.textContent('#liveChanges');
    assert.match(changesText, /#12[\s\S]*Bus lines[\s\S]*Written[\s\S]*Quick checks[\s\S]*Build & test[\s\S]*Into Test-Staging[\s\S]*Staging build[\s\S]*In a release/);
    assert.match(changesText, /#13[\s\S]*Weather[\s\S]*Failed[\s\S]*Compile and test/);
    // A failed step's clock stops: it says how long it ran and since when it waits, not "2 h 20 min of ~1 min".
    assert.match(changesText, /#13[\s\S]*This step failed after 19 min[\s\S]*Waiting for a fix since/);
    assert.doesNotMatch(changesText, /#13 Weather[^#]*Taking longer than usual/);
    assert.match(changesText, /#14[\s\S]*Shop signs[\s\S]*Queued[\s\S]*still learning/);
    assert.equal(await page.getAttribute('#liveChanges li.chg:nth-child(2) .st.now', 'aria-current'), 'step');
    assert.equal(await page.getAttribute('#liveChanges li.chg:nth-child(2) .pb .meter', 'aria-valuenow'), '48');
    // Agents: only those working now. Heartbeats show the step, where it runs and the PR's checks; a GitHub-only row is marked a guess.
    const agents = await page.textContent('#liveAgents');
    assert.match(agents, /Claude[\s\S]*#12[\s\S]*Bus lines[\s\S]*Working[\s\S]*Fixing the bus stop importer[\s\S]*Gaming PC[\s\S]*checks running[\s\S]*checked in/);
    assert.match(agents, /Codex[\s\S]*#13[\s\S]*Weather[\s\S]*NODE-01[\s\S]*checks failing/);
    assert.match(agents, /#40[\s\S]*Hair import[\s\S]*Waiting[\s\S]*Waiting on PR checks[\s\S]*cloud/);
    assert.match(agents, /claude\/town-benches[\s\S]*Coding, no PR yet[\s\S]*seen on GitHub[\s\S]*active/);
    assert.equal(await page.$$eval('#liveAgents .guess', list => list.length), 1);
    assert.doesNotMatch(await page.textContent('#liveMachines'), /Claude: \d+ session/);
    assert.match(await page.textContent('#liveAgentsCard'), /12 more agents were active earlier today, but not in the last 15 minutes/);
    // the disk guard's drive: free/total, a warning under warnGB, the biggest folders; not listed twice in the stats
    assert.match(await page.textContent('#liveMachines'), /SF-NODE-01[\s\S]*C: 88\/230 GB free[\s\S]*Under 50 GB[\s\S]*E: 31\/250 GB free[\s\S]*Biggest: lifeshare-scratch\\wt-bus-lines 61 GB/);
    assert.doesNotMatch(await page.textContent('#liveMachines'), /E: 31\/250 GB free[\s\S]*E: 31\/250 GB free/);
    assert.match(await page.textContent('#liveRunners'), /MY-PC-unreal-pr[\s\S]*Busy[\s\S]*NODE-unreal-light[\s\S]*Offline/);
    assert.match(await page.textContent('#liveRuns'), /PR check · PR #12/);
    assert.match(await page.textContent('#livePrs'), /#13\s*Weather[\s\S]*Failing/);
    assert.match(await page.textContent('#liveParked'), /1 issue parked until other work lands \(not stuck\): #42/);
    assert.match(await page.textContent('#liveFoot'), /^1 repositories watched · GitHub read [0-9]/);
    // Base health: a red base branch on top, with what fails, the first bad merge, the fix in line and what it holds red.
    assert.equal(await page.$$eval('#baseBanner .base-red', l => l.length), 1, 'only the red base shows');
    assert.match(await page.textContent('#baseBanner'), /Test-Staging is red · for \d+ (h|d)[\s\S]*a new commit is being checked[\s\S]*Failing: PR check \/ Compile and test \(LifeShare.Town.Fishing, RouteScale\)[\s\S]*First bad: #9 Folk greetings \(Claude\)[\s\S]*Fix in line: #12 Bus lines \(checks running, merges by itself\) · tracked in #42[\s\S]*3 open PRs fail the same checks because of this; fixing Test-Staging clears them/);
    assert.equal(await page.getAttribute('#baseBanner a[href$="/pull/9"]', 'href'), 'https://github.com/Acme/Game/pull/9');
    // Why PRs are red: problems by cause; only own bugs count as stuck; machine problems can be re-run.
    assert.match(await page.textContent('#liveCauses'), /^4 red PRs: 1 own bug · 2 inherited from a red base · 1 machine problem\. Only own bugs count as stuck\.$/);
    assert.match(await page.textContent('#liveProblems'), /Own bug\s*PR #13 fails Compile and test \(LifeShare.Folk.Hair LOD 3\)[\s\S]*Base is red\s*2 PRs fail[\s\S]*#20 Lots · #21 Roads[\s\S]*Machine\s*PR #22/);
    assert.match(await page.textContent('#livePrs'), /#13\s*Weather[\s\S]*Failing\s*Own bug/);
    // Queue tab: pools with what runs and waits in order, estimated start and finish, and who cancelled what.
    await page.click('#tab-queue');
    await page.waitForSelector('#queuePools .pool');
    assert.equal(await page.textContent('#b-queue'), '2');
    assert.match(await page.textContent('#queuePools'), /unreal-prGaming PC, SF-NODE-01[\s\S]*2\/2 busy[\s\S]*2 waiting[\s\S]*wait today 7 min \(p90 55 min\)[\s\S]*in use 71% of the last day[\s\S]*▶PR check · PR #12 · Compile and test[\s\S]*~25 min left[\s\S]*1PR check · PR #13 · Compile and test[\s\S]*starts in 25 min, done in 75 min[\s\S]*2PR check · PR #20[\s\S]*starts in 40 min/);
    assert.match(await page.textContent('#queuePools'), /unreal-light[\s\S]*No machine of this kind is online\./);
    assert.match(await page.textContent('#queueCancelled'), /PR #12[\s\S]*cancelled by @ProjektPayne[\s\S]*PR #21[\s\S]*replaced by a newer run of the same check/);
    assert.ok(await fits(), 'Queue fits the phone');
    if (shots) { await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(200); await page.screenshot({path: path.join(shots, 'portal-queue.png')}); }
    await page.click('#tab-live');
    await page.waitForSelector('#liveFlow:not([hidden])');
    // Flow: is work moving? A verdict with its reason, the numbers, 24 hourly bars.
    assert.match(await page.textContent('#liveFlow'), /Moving\s*Last merge 21 min ago[\s\S]*Merges2\s*last hour · 9 today[\s\S]*Since last merge21 min[\s\S]*PR to merge4.4 h\s*median, 7 days[\s\S]*Wait for a machine7 min\s*p90 55 min[\s\S]*Base went red18%\s*of 22 runs/);
    assert.equal(await page.$$eval('#liveFlow .hours i', l => l.length), 24);
    assert.match(await page.getAttribute('#liveFlow .hours', 'aria-label'), /^Merges per hour, last 24 hours: 0, 0, 1/);
    assert.ok(await fits(), 'Live fits the phone');
    for (const box of await page.$$eval('.tabs button, #bell, #refreshTop', list => list.map(b => b.getBoundingClientRect().toJSON()))) {
      assert.ok(box.width >= 44 && box.height >= 44, 'touch target ' + JSON.stringify(box));
    }
    // Every card folds from its heading, and the page remembers it; long lists show a few and "Show N more".
    assert.equal(await page.getAttribute('#h-liss .fold', 'aria-expanded'), 'false', 'issues start folded');
    await page.click('#h-lblock .fold');
    assert.equal(await page.isVisible('#liveBlockers'), false);
    assert.ok(await page.evaluate(() => JSON.parse(localStorage.getItem('fletchery-folded')).includes('h-lblock')));
    await page.click('#h-lblock .fold');
    assert.equal(await page.isVisible('#liveBlockers'), true);
    assert.equal(await page.$$eval('#liveBlockers > li.blk:not([hidden])', l => l.length), 4);
    assert.match(await page.textContent('#liveBlockers .more-btn'), /Show 2 more/);
    await page.click('#liveBlockers .more-btn');
    assert.equal(await page.$$eval('#liveBlockers > li.blk:not([hidden])', l => l.length), 6);
    await page.click('#liveBlockers .more-btn');
    if (shots) { await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(200); } if (shots) await page.screenshot({path: path.join(shots, 'portal-live.png')});
    if (shots) {   // the Agents card alone, without the fixed header and tab bar over it
      const bars = 'header.top, nav.tabs';
      await page.evaluate(b => document.querySelectorAll(b).forEach(n => { n.style.visibility = 'hidden'; }), bars);
      await page.locator('#liveAgentsCard').screenshot({path: path.join(shots, 'portal-agents.png')});
      await page.locator('#liveChangesCard').screenshot({path: path.join(shots, 'portal-changes.png')});
      await page.locator('#liveMachinesCard').screenshot({path: path.join(shots, 'portal-machines.png')});
      await page.locator('#liveFlow').screenshot({path: path.join(shots, 'portal-flow.png')});
      await page.locator('#baseBanner').screenshot({path: path.join(shots, 'portal-base.png')});
      await page.locator('#liveProblemsCard').screenshot({path: path.join(shots, 'portal-problems.png')});
      await page.evaluate(b => document.querySelectorAll(b).forEach(n => { n.style.visibility = ''; }), bars);
    }

    // GitHub Actions controls on Live: cancel a run that is going, after a confirm.
    await page.waitForSelector('#liveRuns button:has-text("Cancel run")');
    await page.click('#liveRuns button:has-text("Cancel run") >> nth=0');
    await page.waitForSelector('#toast:not([hidden])');
    assert.deepEqual(lastPost(), ['/api/actions/cancel', {repo: 'Acme/Game', id: 7}]);
    assert.match(await page.textContent('#toast'), /Sent to GitHub/);
    // A check that gave no verdict (cancelled, or "No result") is re-run from its PR, one job.
    await page.click('#livePrs button:has-text("Re-run Cheap checks")');
    await page.waitForFunction(() => document.querySelector('#toast') && !document.querySelector('#toast').hidden);
    assert.deepEqual(lastPost(), ['/api/actions/rerun-job', {repo: 'Acme/Game', id: 41}]);

    // Alerts: which blockers reached the phone, which wait their turn, which are snoozed; snooze from the row.
    const blkText = await page.textContent('#liveBlockers');
    assert.match(blkText, /Not yet alerted|Not alerted yet/);
    assert.match(blkText, /alerts in 4 min if it lasts/);
    assert.match(blkText, /Snoozed until/);
    assert.match(blkText, /Shown here only: no alert for this kind/);
    assert.match(await page.textContent('#liveCleared'), /Cleared recently: Gaming PC stopped reporting/);
    assert.match(await page.textContent('#liveMachines'), /E: 31\/250 GB free \(full in ~2\.5 h\)/);
    if (shots) {
      await page.evaluate(() => document.querySelectorAll('header.top, nav.tabs').forEach(n => { n.style.visibility = 'hidden'; }));
      await page.locator('#liveBlockersCard').screenshot({path: path.join(shots, 'portal-alerts.png')});
      await page.evaluate(() => document.querySelectorAll('header.top, nav.tabs').forEach(n => { n.style.visibility = ''; }));
    }
    await page.click('#liveBlockers li.blk >> nth=0 >> button:has-text("4 h")');
    await page.waitForFunction(() => /Snoozed for 4 h/.test(document.getElementById('toast').textContent));
    assert.deepEqual(lastPost(), ['/api/alerts/snooze', {key: 'queued:Acme/Game:13', hours: 4}]);

    // A feed whose data is old says so at the top, in words.
    liveAt = new Date(Date.now() - 5 * 60000).toISOString();
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('fletchery-tab', {detail: 'live'})));
    await page.waitForSelector('#liveStale:not([hidden])');
    assert.match(await page.textContent('#liveStale'), /has not had fresh data for 5 min/);
    liveAt = new Date().toISOString();

    // Live shows every build going on, on any machine, at the top.
    assert.match(await page.textContent('#liveBuilding'), /#12 Bus lines[\s\S]*Gaming PC[\s\S]*Compile and test[\s\S]*2 of 5 complete[\s\S]*Build editor/);
    assert.equal(await page.locator('#liveBuilding [role=progressbar]').getAttribute('aria-valuenow'), '40');
    assert.match(await page.textContent('#liveBuilding'), /Importing environments[\s\S]*About 12 min left in this job · 3 past checks on this PR/);

    // Builds on the phone: running and coming up first (with the running one's steps), start a build, finished ones.
    await page.click('#tab-builds');
    await page.waitForSelector('#buildsOpen .title');
    assert.equal(await page.textContent('#project'), 'demo-user // Fletchery // All projects');
    assert.match(await page.textContent('#buildsOpen'), /0\.4\.6[\s\S]*Building[\s\S]*Package 0\.4\.6[\s\S]*0\.4\.7[\s\S]*Scheduled/);
    assert.equal(await page.isVisible('#newBuildCard'), true);
    assert.equal(await page.isVisible('#bAt'), false, 'later, daily and tests fold away');
    assert.deepEqual(await page.$$eval('#buildsDone .links a', l => l.map(a => a.textContent + ' ' + a.href)),
      ['GitHub download ' + gh + '/releases/tag/build-0.4.5', 'Google Drive https://drive.google.com/file/d/example']);
    assert.match(await page.textContent('#buildsDone'), /0\.4\.4[\s\S]*Failed[\s\S]*Compile failed in <Game>/);
    const buttons = sel => page.$$eval(sel + ' button', l => l.map(b => b.textContent));
    assert.deepEqual(await buttons('#buildsOpen'), ['Stop', 'Run once now', 'Stop repeating']);
    assert.deepEqual(await buttons('#buildsDone'), ['Build again']);
    await page.fill('#bVersion', '0.4.9');
    await page.click('#newBuild button[type=submit]');
    await page.waitForFunction(() => /0\.4\.9 started/.test(document.getElementById('bMsg').textContent));
    assert.deepEqual(lastPost(), ['/api/builds/add', {version: '0.4.9', ref: '', at: '', repeat: '', tests: ''}]);
    await page.click('#buildsDone button:has-text("Build again")');
    await page.waitForFunction(() => /queued again/.test(document.getElementById('toast').textContent));
    assert.deepEqual(lastPost(), ['/api/builds/retry', {id: 'b1'}]);
    await page.click('#buildsOpen button:has-text("Stop") >> nth=0');
    await page.waitForFunction(() => /Done/.test(document.getElementById('toast').textContent));
    assert.deepEqual(lastPost(), ['/api/builds/cancel', {id: 'b3'}]);
    // Machines: pause one, pause all, and machine jobs: stop, run again, log.
    assert.deepEqual(await buttons('#buildMachines'), ['Pause', 'Resume']);
    assert.match(await page.textContent('#buildMachines'), /NODE-01[\s\S]*Building 0\.4\.6[\s\S]*MY-PC[\s\S]*Paused/);
    await page.click('#buildMachines button:has-text("Resume")');
    await page.waitForFunction(() => /takes builds again/.test(document.getElementById('toast').textContent));
    assert.deepEqual(lastPost(), ['/api/machines/pause', {name: 'MY-PC', kind: 'builds', on: false}]);
    assert.equal(await page.isVisible('#holdAll'), true);
    await page.click('#holdAll');
    await page.waitForFunction(() => /All machines paused/.test(document.getElementById('toast').textContent));
    assert.deepEqual(lastPost(), ['/api/machines/hold', {on: true}]);
    assert.equal(await page.getAttribute('#buildsOpen a.small-link', 'href'), '/api/jobs/log?id=7');
    assert.deepEqual(await buttons('#jobs'), ['Run again']);   // only jobs that are not builds have their own list
    await page.click('#jobs button:has-text("Run again")');
    await page.waitForFunction(() => /Queued again/.test(document.getElementById('toast').textContent));
    assert.deepEqual(lastPost(), ['/api/jobs/retry', {id: 6}]);
    assert.ok(await fits(), 'Builds fits the phone');
    if (shots) { await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(200); } if (shots) await page.screenshot({path: path.join(shots, 'portal-builds.png')});

    // A control that fails says why, in red.
    await page.evaluate(() => window.fletcheryAct('/api/builds/now', {id: 'nope'}, 'x'));
    assert.match(await page.textContent('#toast'), /Build not found/);
    assert.equal(await page.getAttribute('#toast', 'class'), 'toast bad');

    // Activity: the app itself, its controls, and text shown as text.
    await page.click('#tab-activity');
    await page.waitForSelector('#health .title');
    assert.match(await page.textContent('#health'), /Backend[\s\S]*Running[\s\S]*Up 3 h[\s\S]*code abc12345/);
    assert.match(await page.textContent('#health'), /Build machines[\s\S]*Taking jobs[\s\S]*2 of 2 online · 1 running, 0 queued/);
    assert.match(await page.textContent('#log'), /<script>window\.hacked = 1<\/script>/);
    assert.equal(await page.evaluate(() => window.hacked), undefined);
    await page.click('#checkNow');
    await page.waitForFunction(() => /Reading GitHub now/.test(document.getElementById('toast').textContent));
    assert.deepEqual(lastPost(), ['/api/live/refresh', {}]);
    await page.click('#restart');
    await page.waitForFunction(() => /Restarting/.test(document.getElementById('rMsg').textContent));
    assert.deepEqual(lastPost(), ['/api/restart', {}]);
    assert.ok(await fits(), 'Activity fits the phone');
    if (shots) { await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(200); } if (shots) await page.screenshot({path: path.join(shots, 'portal-activity.png')});

    // A reload on a tab comes back to that tab.
    await page.reload();
    await page.waitForSelector('#health .title');
    assert.equal(await page.isVisible('#view-activity'), true);

    // Notifications: says what the PC said when it is not set up.
    push = {available: false, why: 'Notifications are off for this portal (workers.portal.push).'};
    await page.click('#bell');
    await page.waitForFunction(() => /workers\.portal\.push/.test(document.getElementById('notifyText').textContent));
    assert.equal(await page.isVisible('#notifyOn'), false);
    await page.click('#notifyClose');

    // The PC stops answering: the page says so and keeps what it last showed.
    down = true;
    await page.click('#tab-builds');
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForFunction(() => /reach the PC/.test(document.getElementById('freshText').textContent));
    assert.match(await page.textContent('#buildsOpen'), /0\.4\.6/);

    // Opening with the PC out of reach (Tailscale still connecting): the page comes from the copy saved on the phone
    // at once, with what it last showed, and says it cannot reach the PC.
    await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      for (let i = 0; i < 50 && !(await caches.match('/portal.js')); i++) await new Promise(r => setTimeout(r, 100));
    });
    assert.ok(await page.evaluate(() => caches.match('/').then(Boolean)), 'the page is saved on the phone');
    off = true;
    await page.reload();
    assert.match(await page.textContent('#freshText'), /Showing what it said/);
    assert.match(await page.textContent('#buildsOpen'), /0\.4\.6/);
    await page.waitForFunction(() => /reach the PC/.test(document.getElementById('freshText').textContent));
    off = false;
    down = false;

    // Controls off (workers.portal.controls false): the phone only reads.
    mode = 'readonly';
    const before = posts.length;
    const ro = await context.newPage();
    watch(ro);
    await ro.goto(base + '/#builds');
    await ro.waitForSelector('#buildsOpen .title');
    assert.equal(await ro.isVisible('#newBuildCard'), false);
    assert.equal(await ro.$$eval('#view-builds button', l => l.filter(b => b.offsetParent && !b.classList.contains('fold') && !b.classList.contains('grip')).length), 0);
    await ro.click('#tab-activity');
    assert.equal(await ro.isVisible('#restart'), false);
    assert.equal(await ro.isVisible('#reload'), true);
    await ro.click('#tab-live');
    await ro.waitForSelector('#liveRuns li');
    assert.equal(await ro.$$eval('#liveRuns button', l => l.length), 0);
    assert.equal(posts.length, before);
    await ro.close();

    // An iPhone in Safari (not yet on the Home Screen) is told how to add it.
    mode = 'phone';
    push = {available: true, key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', events: [], phones: 0};
    const safari = await browser.newContext({...phone, deviceScaleFactor: shots ? phone.deviceScaleFactor : 1, colorScheme: 'dark'});
    const tab = await safari.newPage();
    watch(tab);
    await tab.goto(base + '/#builds');
    await tab.waitForSelector('#buildsOpen .title');
    await tab.click('#bell');
    await tab.waitForSelector('#installSteps:not([hidden])');
    assert.match(await tab.textContent('#installSteps'), /Add to Home Screen/);
    if (shots) { await tab.click('#notifyClose'); await tab.click('#tab-live'); await tab.waitForSelector('#liveMachines .m-name'); await tab.screenshot({path: path.join(shots, 'portal-live-dark.png')}); }

    // The desktop window: one row of tabs at the top, never the phone's bar at the bottom, at any width.
    mode = 'desktop';
    const desk = await browser.newContext({viewport: {width: 1400, height: 1000}, deviceScaleFactor: shots ? 2 : 1, colorScheme: 'light'});
    const win = await desk.newPage();
    watch(win);
    await win.goto(base + '/#builds');
    await win.waitForSelector('#newBuildCard:not([hidden])');
    assert.equal(await win.evaluate(() => document.body.classList.contains('desktop')), true);
    assert.equal(await win.isVisible('#bell'), false);
    const tabsTop = () => win.evaluate(() => { const r = document.querySelector('.tabs').getBoundingClientRect(); return r.top < 200 && r.bottom < innerHeight - 100; });
    assert.ok(await tabsTop(), 'tabs at the top');
    await win.setViewportSize({width: 700, height: 900});
    assert.ok(await tabsTop(), 'tabs at the top when narrow too');
    await win.setViewportSize({width: 1400, height: 1000});
    await win.fill('#bVersion', '0.4.8');
    await win.click('#newBuild summary');
    await win.check('#bDaily');
    await win.click('#newBuild button[type=submit]');
    await win.waitForFunction(() => /0\.4\.8 started/.test(document.getElementById('bMsg').textContent));
    assert.deepEqual(lastPost(), ['/api/builds/add', {version: '0.4.8', ref: '', at: '', repeat: 'daily', tests: ''}]);
    if (shots) {
      await win.reload();
      await win.waitForSelector('#buildsOpen .title');
      await win.screenshot({path: path.join(shots, 'desktop-builds.png')});
      await win.click('#tab-live');
      await win.waitForSelector('#liveMachines .m-name');
      await win.screenshot({path: path.join(shots, 'desktop-live.png')});
      await win.click('#tab-activity');
      await win.waitForSelector('#health .title');
      await win.screenshot({path: path.join(shots, 'desktop-activity.png')});
    }

    // Cards move: drag one by its grip, or arrow keys on the grip; the layout is saved on the PC and comes back.
    const until = async (test, what) => { for (let i = 0; i < 50 && !test(); i++) await new Promise(r => setTimeout(r, 100)); assert.ok(test(), what); };
    const where = id => win.evaluate(id => { const c = document.getElementById(id), col = c.parentElement;
      return [[...col.parentElement.children].indexOf(col), [...col.children].indexOf(c)]; }, id);
    await win.click('#tab-live');
    await win.waitForSelector('#liveMachines .m-name');
    assert.equal(await win.$$eval('#view-live .cols > .col', l => l.filter(c => c.offsetWidth > 0).length), 2, 'two columns on a wide window');
    assert.deepEqual(await where('liveBuildingCard'), [0, 0]);
    assert.deepEqual(await where('liveMachinesCard'), [1, 0]);
    const g = await win.$('#liveMachinesCard .grip');
    const gb = await g.boundingBox(), tb = await (await win.$('#liveBuildingCard')).boundingBox();
    await win.mouse.move(gb.x + gb.width / 2, gb.y + gb.height / 2);
    await win.mouse.down();
    await win.mouse.move(tb.x + 40, tb.y + 30, {steps: 8});
    await win.mouse.move(tb.x + 40, tb.y + 10, {steps: 4});
    assert.equal(await win.evaluate(() => document.body.classList.contains('arranging')), true);
    await win.mouse.up();
    assert.deepEqual(await where('liveMachinesCard'), [0, 0], 'dropped above Building now');
    assert.deepEqual(await where('liveBuildingCard'), [0, 1]);
    await until(() => layoutFile && layoutFile.views.live[0][0] === 'liveMachinesCard', 'the drag is saved on the PC');
    await win.focus('#liveMachinesCard .grip');
    await win.keyboard.press('ArrowRight');
    await win.keyboard.press('ArrowDown');   // already last in that column: stays
    assert.deepEqual(await where('liveMachinesCard'), [1, (await win.$$eval('#view-live .col:nth-child(2) > .card', l => l.length)) - 1]);
    assert.equal(await win.evaluate(() => document.activeElement.classList.contains('grip')), true, 'focus stays on the grip');
    await until(() => layoutFile.views.live[1].includes('liveMachinesCard'), 'the key move is saved');
    await win.evaluate(() => localStorage.clear());   // the file on the PC is what brings it back
    await win.reload();
    await win.waitForSelector('#liveMachines .m-name');
    await win.waitForFunction(() => document.getElementById('liveMachinesCard').parentElement === document.querySelector('#view-live .col:nth-child(2)'));
    await win.click('#tab-activity');
    await win.click('.layout-cols button[data-cols="3"]');
    await until(() => layoutFile.columns === 3, 'three columns saved');
    assert.equal(await win.getAttribute('.layout-cols button[data-cols="3"]', 'aria-pressed'), 'true');
    await win.click('#tab-live');
    assert.equal(await win.$$eval('#view-live .cols > .col', l => l.length), 3);
    await win.setViewportSize({width: 700, height: 900});
    assert.equal(await win.$$eval('#view-live .cols > .col', l => new Set(l.filter(c => c.offsetWidth).map(c => c.getBoundingClientRect().left)).size), 1, 'one column when narrow');
    await win.setViewportSize({width: 1400, height: 1000});
    await win.click('#tab-activity');
    await win.click('#layoutReset');
    await until(() => layoutFile === null, 'reset clears the file');
    await win.click('#tab-live');
    assert.deepEqual(await where('liveBuildingCard'), [0, 0]);
    assert.deepEqual(await where('liveMachinesCard'), [1, 0]);

    // Several projects on Live: a chip shows one at a time.
    mode = 'phone';
    live.prs.push({repo: 'Acme/Town', number: 7, title: 'Road decals', url: 'https://github.com/Acme/Town/pull/7', draft: false, author: 'dev',
                   branch: 'roads', base: 'main', updated: new Date().toISOString(), created: '', labels: [], checks: {state: 'passed', total: 1, done: 1, failed: [], running: [], url: ''}});
    live.open_pr_counts = {'Acme/Town': 81, 'Acme/Game': 2};
    const solo = await context.newPage();
    watch(solo);
    await solo.goto(base + '/#tasks');   // an old link to a tab that is gone opens Live
    await solo.waitForSelector('#liveRepos button');
    assert.deepEqual(await solo.$$eval('.tabs button:not([hidden])', l => l.map(b => b.dataset.tab)), ['live', 'queue', 'builds', 'activity']);
    assert.equal(await solo.isVisible('#view-live'), true);
    assert.equal(await solo.$$eval('#livePrs > li', l => l.length), 3);
    assert.equal(await solo.textContent('#accountPick'), 'demo-user');
    assert.equal(await solo.textContent('#scopeName'), 'All projects');
    await solo.click('#accountPick');
    await solo.waitForFunction(() => document.querySelectorAll('#accountList option').length === 2);
    await solo.selectOption('#accountList', 'second-user');
    await solo.click('#accountApply');
    await solo.waitForSelector('#accountDialog[data-pending="second-user"]');
    assert.deepEqual(lastPost(), ['/api/github/account', {login: 'second-user'}]);
    await solo.click('#accountClose');
    await solo.click('#liveRepos button:has-text("Town")');
    assert.equal(await solo.textContent('#scopeName'), 'Town');
    assert.equal(await solo.locator('#liveStats b').nth(2).textContent(), '81');
    assert.equal(await solo.$$eval('#livePrs > li', l => l.length), 1);
    assert.match(await solo.textContent('#livePrs'), /Road decals/);
    // A project of several repositories (workers.portal.live.projects) is one chip.
    live.projects = [{name: 'Acme world', repos: ['Acme/Game', 'Acme/Town']}];
    await solo.reload();
    await solo.waitForSelector('#liveRepos button:has-text("Acme world")', {state: 'attached'});
    assert.deepEqual(await solo.$$eval('#liveRepos button', l => l.map(b => b.textContent)), ['All projects', 'Acme world']);
    assert.equal(await solo.isVisible('#liveRepos'), false);      // one project: nothing to choose
    delete live.projects;

    assert.deepEqual(errors.filter(e => !/Failed to load resource|ERR_|net::/.test(e)), []);
    console.log('portal browser checks passed');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
