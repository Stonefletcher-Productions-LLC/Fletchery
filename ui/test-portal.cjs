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
  project: 'Harbor', repo: 'example/harbor', at: ago(0), started: ago(185), version: 'abc12345', base: 'main',
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
let liveAt = new Date().toISOString();
let off = false;   // the whole PC is unreachable: not even the page's own files answer
const types = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.webmanifest': 'application/manifest+json', '.png': 'image/png'};
const csp = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const posts = [];
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
      posts.push([url, parsed]);
      if (mode === 'readonly') return send(403, JSON.stringify({error: 'Do this in the Fletchery window on the PC.'}), 'application/json');
      if (parsed.id === 'nope') return send(400, JSON.stringify({error: 'Build not found.'}), 'application/json');
      send(200, JSON.stringify(url === '/api/builds/add' ? {...parsed, id: 'b9'} : {ok: true}), 'application/json');
    });
    return;
  }
  if (url === '/api/summary') return down ? req.socket.destroy() : send(200, JSON.stringify({...data, desktop: mode === 'desktop', controls: mode !== 'readonly'}), 'application/json');
  if (url === '/api/live') return down ? req.socket.destroy() : send(200, JSON.stringify({...live, at: liveAt, fresh: {...live.fresh, github: liveAt}}), 'application/json');
  if (url === '/api/push') return send(200, JSON.stringify(push), 'application/json');
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
    assert.deepEqual(await page.$$eval('#liveStats b', list => list.map(b => b.textContent)), ['2/3', '2', '2', '6']);
    assert.equal(await page.textContent('#b-live'), '6');
    assert.match(await page.textContent('#liveBlockers'), /Build editor" has run 75 min/);
    assert.match(await page.textContent('#liveMachines'), /Gaming PC[\s\S]*PR check #12[\s\S]*SF-NODE-01[\s\S]*Hand merge batch 7/);
    // the disk guard's drive: free/total, a warning under warnGB, the biggest folders; not listed twice in the stats
    assert.match(await page.textContent('#liveMachines'), /SF-NODE-01[\s\S]*C: 88\/230 GB free[\s\S]*Under 50 GB[\s\S]*E: 31\/250 GB free[\s\S]*Biggest: lifeshare-scratch\\wt-bus-lines 61 GB/);
    assert.doesNotMatch(await page.textContent('#liveMachines'), /E: 31\/250 GB free[\s\S]*E: 31\/250 GB free/);
    assert.match(await page.textContent('#liveRunners'), /MY-PC-unreal-pr[\s\S]*Busy[\s\S]*NODE-unreal-light[\s\S]*Offline/);
    assert.match(await page.textContent('#liveRuns'), /PR check · PR #12/);
    assert.match(await page.textContent('#livePrs'), /#13\s*Weather[\s\S]*Failing/);
    assert.match(await page.textContent('#liveFoot'), /^1 repositories watched · GitHub read [0-9]/);
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

    // A feed whose data is old says so at the top, in words.
    liveAt = new Date(Date.now() - 5 * 60000).toISOString();
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('fletchery-tab', {detail: 'live'})));
    await page.waitForSelector('#liveStale:not([hidden])');
    assert.match(await page.textContent('#liveStale'), /has not had fresh data for 5 min/);
    liveAt = new Date().toISOString();

    // Live shows every build going on, on any machine, at the top.
    assert.match(await page.textContent('#liveBuilding'), /PR check · PR #12[\s\S]*Gaming PC/);

    // Builds on the phone: running and coming up first (with the running one's steps), start a build, finished ones.
    await page.click('#tab-builds');
    await page.waitForSelector('#buildsOpen .title');
    assert.equal(await page.textContent('#project'), 'Harbor');
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
    assert.equal(await ro.$$eval('#view-builds button', l => l.filter(b => b.offsetParent && !b.classList.contains('fold')).length), 0);
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

    // Several projects on Live: a chip shows one at a time.
    mode = 'phone';
    live.prs.push({repo: 'Acme/Town', number: 7, title: 'Road decals', url: 'https://github.com/Acme/Town/pull/7', draft: false, author: 'dev',
                   branch: 'roads', base: 'main', updated: new Date().toISOString(), created: '', labels: [], checks: {state: 'passed', total: 1, done: 1, failed: [], running: [], url: ''}});
    const solo = await context.newPage();
    watch(solo);
    await solo.goto(base + '/#tasks');   // an old link to a tab that is gone opens Live
    await solo.waitForSelector('#liveRepos button');
    assert.deepEqual(await solo.$$eval('.tabs button:not([hidden])', l => l.map(b => b.dataset.tab)), ['live', 'builds', 'activity']);
    assert.equal(await solo.isVisible('#view-live'), true);
    assert.equal(await solo.$$eval('#livePrs > li', l => l.length), 3);
    await solo.click('#liveRepos button:has-text("Town")');
    assert.equal(await solo.$$eval('#livePrs > li', l => l.length), 1);
    assert.match(await solo.textContent('#livePrs'), /Road decals/);

    assert.deepEqual(errors.filter(e => !/Failed to load resource|ERR_|net::/.test(e)), []);
    console.log('portal browser checks passed');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
