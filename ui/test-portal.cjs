// Browser checks for the phone portal (ui/portal) against a local fixture; never talks to a real Fletchery.
// Run with Playwright installed: node ui/test-portal.cjs   (FLETCHERY_BROWSER=<chromium> off Windows;
// FLETCHERY_PORTAL_SHOTS=<folder> also saves screenshots)
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, devices } = require(process.env.FLETCHERY_PLAYWRIGHT || 'playwright');

const ago = minutes => new Date(Date.now() - minutes * 60000).toISOString();
const gh = 'https://github.com/example/harbor';
const data = {
  project: 'Harbor', repo: 'example/harbor', repo_url: gh, at: ago(0), last_pass: ago(3), next_pass_at: ago(-7), passing: false,
  pauses: [], max_parallel: 4, spent_today: 27.08, daily_budget: 400, auth: 'subscription', pc: 'the PC is free',
  needs: [{number: 900, title: 'Missing <asset> access', why: 'Needs you: allow the Blender tool or pick another route', url: gh + '/issues/900', pr: 0, pr_url: '', agent: '', depends: []}],
  workers: [
    {task: 891, title: 'Shower interactions', agent: 'sonnet', stage: 'Reviewing', since: ago(18), last: 'Read src/shower.cpp', last_at: ago(1), emergency: false},
    {task: 899, title: 'Preview reactions', agent: 'codex-sol', stage: 'Writing', since: ago(6), last: 'Edit ui/reactions.ts', last_at: ago(0), emergency: false},
  ],
  running: [{number: 891, title: 'Shower interactions', why: '', url: gh + '/issues/891', pr: 0, pr_url: '', agent: 'sonnet', depends: []},
            {number: 899, title: 'Preview reactions', why: '', url: gh + '/issues/899', pr: 0, pr_url: '', agent: 'codex-sol', depends: []}],
  queued: [{number: 840, title: 'Bikes and parcels', why: 'ready', url: gh + '/issues/840', pr: 0, pr_url: '', agent: '', depends: []},
           {number: 841, title: 'Parcel lockers', why: 'waits for #840', url: gh + '/issues/841', pr: 0, pr_url: '', agent: '', depends: [840]}],
  in_pr: [{number: 1036, title: 'Save purchased bike details', why: '', url: gh + '/issues/1036', pr: 1041, pr_url: gh + '/pull/1041', agent: 'opus', depends: []}],
  merge: {on: true, held: false, active: true, state: 'running', round: '16', headline: 'Game tests running · 47 min elapsed', why: '',
    started: ago(47), updated: ago(1), stale: false, prs: [1041, 1043], agent: 'opus', backlog: 3,
    steps: [{name: 'Compile', state: 'done', message: '', seconds: 372}, {name: 'Imports', state: 'done', message: '', seconds: 95},
            {name: 'Game tests', state: 'running', message: 'Verify the rebuilt game before landing.', seconds: null}, {name: 'Land', state: 'todo', message: '', seconds: null}],
    previous: {round: '15', result: 'landed 4 PRs', ended: ago(190)},
    waiting: [{pr: 1041, title: 'Save purchased bike details', url: gh + '/pull/1041', ready: true, flags: []},
              {pr: 1050, title: 'Move the cafe counter', url: gh + '/pull/1050', ready: false, flags: ['strict NavCheck on the PC']}]},
  builds: [
    {id: 'b3', version: '0.4.6', state: 'running', ref: 'main', sha: 'a1b2c3d4e5', at: ago(30), started: ago(25), finished: '', machine: 'NODE-01', reason: '', publish: '', tests: 'full', repeat: '', links: []},
    {id: 'b2', version: '0.4.5', state: 'done', ref: 'main', sha: '326ed7bfd3', at: ago(900), started: ago(890), finished: ago(800), machine: 'this PC', reason: 'Download ready', publish: 'published', tests: '', repeat: '',
      links: [{label: 'GitHub download', url: gh + '/releases/tag/build-0.4.5'}, {label: 'Google Drive', url: 'https://drive.google.com/file/d/example'}]},
    {id: 'b1', version: '0.4.4', state: 'failed', ref: 'main', sha: '99887766aa', at: ago(2000), started: ago(1990), finished: ago(1950), machine: 'NODE-01', reason: 'Compile failed in <Game>', publish: '', tests: '', repeat: '', links: [{label: 'bad', url: 'javascript:alert(1)'}]},
  ],
  machines: [{name: 'NODE-01', online: true, last_seen: ago(0), job: 'package', job_ref: 'main', paused: []}],
  log: [{at: ago(5), line: 'Merge round 16 started'}, {at: ago(1), line: '<script>window.hacked = 1</script>'}],
};
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
  if (req.method !== 'GET') { posts.push(url); return send(404, '{}', 'application/json'); }
  if (url === '/api/summary') return down ? req.socket.destroy() : send(200, JSON.stringify(data), 'application/json');
  if (url === '/api/live') return down ? req.socket.destroy() : send(200, JSON.stringify({...live, at: liveAt}), 'application/json');
  if (url === '/api/push') return send(200, JSON.stringify(push), 'application/json');
  const file = path.join(__dirname, 'portal', url === '/' ? 'index.html' : path.basename(url));
  if (!fs.existsSync(file)) return send(404, '{}', 'application/json');
  send(200, fs.readFileSync(file), types[path.extname(file)] || 'application/octet-stream');
});

(async () => {
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const base = 'http://127.0.0.1:' + server.address().port;
  const shots = process.env.FLETCHERY_PORTAL_SHOTS;
  const executablePath = process.env.FLETCHERY_BROWSER;
  const browser = await chromium.launch(executablePath ? {executablePath} : {channel: 'msedge'});
  const errors = [];
  try {
    const phone = devices['iPhone 14'];
    const context = await browser.newContext({viewport: phone.viewport, deviceScaleFactor: shots ? phone.deviceScaleFactor : 1, isMobile: true, hasTouch: true, colorScheme: 'light'});
    const page = await context.newPage();
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', e => errors.push(String(e)));
    // Live opens first: machines, runners, Actions, PRs, issues and what is stuck, from one feed.
    await page.goto(base + '/');
    await page.waitForSelector('#liveMachines .m-name');
    assert.equal(await page.isVisible('#view-live'), true);
    assert.equal(await page.isVisible('#liveStale'), false);
    assert.deepEqual(await page.$$eval('#liveStats b', list => list.map(b => b.textContent)), ['2/3', '2', '2', '6']);
    assert.equal(await page.textContent('#b-live'), '6');
    assert.match(await page.textContent('#liveBlockers'), /Build editor" has run 75 min/);
    assert.match(await page.textContent('#liveMachines'), /Gaming PC[\s\S]*PR check #12[\s\S]*SF-NODE-01[\s\S]*Hand merge batch 7/);
    assert.match(await page.textContent('#liveRunners'), /ALEX-PC-unreal-pr[\s\S]*Busy[\s\S]*NODE-unreal-light[\s\S]*Offline/);
    assert.match(await page.textContent('#liveRuns'), /PR check · PR #12/);
    assert.match(await page.textContent('#livePrs'), /#13\s*Weather[\s\S]*Failing/);
    assert.match(await page.textContent('#liveFoot'), /^1 repositories watched · GitHub read [0-9]/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    if (shots) await page.screenshot({path: path.join(shots, 'portal-live.png'), fullPage: true});
    // A feed whose data is old says so at the top, in words.
    liveAt = new Date(Date.now() - 5 * 60000).toISOString();
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('fletchery-tab', {detail: 'live'})));
    await page.waitForSelector('#liveStale:not([hidden])');
    assert.match(await page.textContent('#liveStale'), /has not had fresh data for 5 min/);
    liveAt = new Date().toISOString();

    await page.goto(base + '/#now');
    await page.reload();
    await page.waitForSelector('#needs .title');

    // Now: the numbers, what needs you, the workers and the round in short.
    assert.equal(await page.textContent('#project'), 'Harbor');
    assert.deepEqual(await page.$$eval('#stats b', list => list.map(b => b.textContent)), ['1', '2', '2', '2']);
    assert.match(await page.textContent('#needs'), /#900\s*Missing <asset> access/);
    assert.equal(await page.getAttribute('#needs a.title', 'href'), gh + '/issues/900');
    assert.equal(await page.getAttribute('#needs a.title', 'rel'), 'noopener noreferrer');
    assert.equal(await page.$$eval('#workers > li', l => l.length), 2);
    assert.match(await page.textContent('#mergeMini'), /Game tests running/);
    assert.match(await page.textContent('#freshText'), /^Live/);
    assert.equal(await page.textContent('#b-now'), '1');
    assert.equal(await page.isVisible('#machinesCard'), true);

    // Text from Fletchery is shown as text, never run or turned into markup.
    await page.click('.log-card summary');
    assert.match(await page.textContent('#log'), /<script>window\.hacked = 1<\/script>/);
    assert.equal(await page.evaluate(() => window.hacked), undefined);
    assert.equal(await page.$$eval('#needs asset', l => l.length), 0);

    // Nothing is wider than the phone, and every control is big enough for a thumb.
    const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.ok(await fits(), 'Now fits the phone');
    for (const box of await page.$$eval('.tabs button, #bell', list => list.map(b => b.getBoundingClientRect().toJSON()))) {
      assert.ok(box.width >= 44 && box.height >= 44, 'touch target ' + JSON.stringify(box));
    }
    if (shots) { fs.mkdirSync(shots, {recursive: true}); await page.click('.log-card summary'); await page.screenshot({path: path.join(shots, 'portal-now.png')}); }

    // Tasks.
    await page.click('#tab-tasks');
    assert.equal(await page.isVisible('#view-now'), false);
    assert.equal(await page.getAttribute('#tab-tasks', 'aria-current'), 'page');
    assert.match(await page.textContent('#t-queued'), /Parcel lockers[\s\S]*waits for #840/);
    assert.equal(await page.getAttribute('#t-pr .sub a', 'href'), gh + '/pull/1041');
    assert.ok(await fits(), 'Tasks fits the phone');
    assert.match(page.url(), /#tasks$/);

    // Merges: every step with its state, and the PRs waiting.
    await page.click('#tab-merges');
    assert.deepEqual(await page.$$eval('#mergeFull .steps li', l => l.map(x => x.className)), ['done', 'done', 'running', 'todo']);
    assert.match(await page.textContent('#mergeFull'), /Last round 15: landed 4 PRs/);
    assert.match(await page.textContent('#waiting'), /strict NavCheck on the PC/);
    if (shots) await page.screenshot({path: path.join(shots, 'portal-merges.png'), fullPage: true});

    // Builds: download links, only real web links.
    await page.click('#tab-builds');
    assert.deepEqual(await page.$$eval('#builds .links a', l => l.map(a => a.textContent + ' ' + a.href)),
      ['GitHub download ' + gh + '/releases/tag/build-0.4.5', 'Google Drive https://drive.google.com/file/d/example']);
    assert.match(await page.textContent('#builds'), /Build 0\.4\.4[\s\S]*Failed[\s\S]*Compile failed in <Game>/);
    assert.ok(await fits(), 'Builds fits the phone');
    if (shots) await page.screenshot({path: path.join(shots, 'portal-builds.png'), fullPage: true});

    // A reload on a tab comes back to that tab.
    await page.reload();
    await page.waitForSelector('#builds .title');
    assert.equal(await page.isVisible('#view-builds'), true);

    // Notifications: says what the PC said when it is not set up; a browser without web-app push says so.
    push = {available: false, why: 'Notifications are off for this portal (workers.portal.push).'};
    await page.click('#bell');
    await page.waitForFunction(() => /workers\.portal\.push/.test(document.getElementById('notifyText').textContent));
    assert.equal(await page.isVisible('#notifyOn'), false);
    await page.click('#notifyClose');

    // The PC stops answering: the page says so and keeps what it last showed.
    down = true;
    await page.click('#tab-now');
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForFunction(() => /reach the PC/.test(document.getElementById('freshText').textContent));
    assert.match(await page.textContent('#needs'), /#900/);

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
    assert.match(await page.textContent('#needs'), /#900/);
    await page.waitForFunction(() => /reach the PC/.test(document.getElementById('freshText').textContent));
    off = false;
    down = false;

    // An iPhone in Safari (not yet on the Home Screen) is told how to add it.
    push = {available: true, key: push.key || 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', events: [], phones: 0};
    const safari = await browser.newContext({...phone, deviceScaleFactor: 1, colorScheme: 'dark'});
    const tab = await safari.newPage();
    await tab.goto(base + '/#merges');
    await tab.waitForSelector('#mergeFull .steps');
    assert.equal(await tab.isVisible('#view-merges'), true);
    await tab.click('#bell');
    await tab.waitForSelector('#installSteps:not([hidden])');
    assert.match(await tab.textContent('#installSteps'), /Add to Home Screen/);
    if (shots) { await tab.click('#notifyClose'); await tab.click('#tab-now'); await tab.screenshot({path: path.join(shots, 'portal-now-dark.png')}); }

    // Read-only: the page never sent anything but reads.
    assert.deepEqual(posts, []);
    assert.deepEqual(errors.filter(e => !/Failed to load resource|ERR_|net::/.test(e)), []);
    console.log('portal browser checks passed');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
