# Machines: the build machines Fletchery runs

Fletchery on the PC is the one queue and history for every build. Each build machine (a test node such as NODE-01,
or this PC itself) runs a small worker, `fletchery/node_worker.py`, that connects **out** to Fletchery over Tailscale,
asks for work, runs it and reports back. The **Builds** tab lists the machines (Offline, Building, Paused or Ready),
the builds running and coming up, and the finished ones with their downloads and logs. **Live > Building now** shows
every build on every machine.

## How it is kept safe

- **Private network only.** The worker API listens on the PC's Tailscale address and port
  (`workers.machines.listen`, e.g. `100.64.0.10:8771`). Fletchery refuses `0.0.0.0`, `::` and public addresses.
  The tailnet ACL lets only `tag:node` reach `tag:coordinator:8771`. The window stays on `127.0.0.1:8770`.
- **One key per machine, revocable.** Adding a machine prints its key once; Fletchery keeps only its SHA-256 hash.
  Revoking (`machines.py revoke`) stops the key at once and queues the machine's running job again. Ten bad sign-ins
  from one address in a minute are refused for a minute.
- **Jobs are recipes, not commands.** A job names one of the machine's recipes (`package`, `checkout-only`) with
  checked parameters and a full commit SHA. The machine has its own copy of the recipes and its own allow-list
  (`recipes` in its config), so a compromised PC still cannot make it run anything else.
- **Read-only code access.** The machine fetches the exact commit with its own read-only deploy key. It holds no
  GitHub token.

## Jobs

| Recipe | What the machine runs |
|---|---|
| `package` | a build from Builds: Stop-JobProcesses, ResetContentWorkspace, `Build.ps1` and `BuildContent.ps1` (or `NodeTests.ps1` Full when Full tests is ticked), `Package.ps1 -Version <v> -Out <buildsDir>\<Name>-<v>`, then the same clean-up |
| `checkout-only` | fetch and check out the commit, print it (a connection test) |

A `package` job needs the labels `os:windows` and `role:cook`, any in `workers.builds.nodeLabels` (for example
`["unreal:5.8.4"]`, so a machine with another engine never builds), and 16 GB of RAM; `checkout-only` needs nothing.
A machine runs one job at a time, oldest first. A job whose machine stops answering for 3 minutes is queued again once,
then marked lost. **Stop** on a running job ends its whole process tree at the machine's next heartbeat (within a
minute). Logs are kept in `<state>\job-logs` (50 MB per job); the **Log** link on a build opens it.

PR checks, test runs and merges are GitHub Actions jobs on your self-hosted runners now; Fletchery shows them on Live
but does not run them.

## Builds

**Builds > Start a build**: a version (`0.4.5`) and a branch (the base branch if left empty). Under **Later, daily or
with Full tests**: a time, every day at that time, and Full tests first. At its time Fletchery reads the branch once
and builds that commit, whatever lands after, on the first machine that can take it.

A folder that already exists is never overwritten: the commit is added to the name. A daily build starts a one-off
copy each day; **Stop repeating** ends it. Builds are kept in `builds.json` in the state folder, so a restart keeps
them.

Profile (optional, `workers` block): `"builds": {"outDir": "D:\\Builds", "engine": "E:\\UE_5.8", "timeoutMinutes":
300, "nodeLabels": ["unreal:5.8.4"], "publish": {"to": "github", "keep": 3, "driveRemote": "gdrive:Builds",
"driveKeep": 2}}`. `publish` uploads each finished build to a pre-release on the project repository (keeping the newest
`keep`) and, with `driveRemote`, to Google Drive with `rclone` (keeping `driveKeep`).

## Pausing machines

**Pause** on a machine's row stops it taking new builds; **Resume** undoes it. **Pause all machines** holds the whole
queue. Running work always finishes, unless you **Stop** it. Pauses live in `machines.db` and survive restarts.

A machine that also runs other heavy work watches its heavy lease (`heavyLeaseFile` in its config, or LaneGuard's
default lease): while a local run holds it, the machine shows "busy with a local run: <what>" and takes no build.
`"yieldTo": ["Wow.exe"]` makes it take nothing while one of those programs runs.

## Asset folders every node mirrors

Some project imports read files that are not in git: LifeShare's character and wardrobe imports read
`E:\\lifeshare-scratch\\meshy_export\\<delivery>` by absolute path (Tools/CI/V8CharacterDelivery.json). A node that does
not have them fails mid-import (LifeShare #1681). So the PC shares those folders with every node through the worker
API, on the same Tailscale address and with the same per-node keys. There is no SMB share or firewall rule.

- **Profile:** `"assetRoots": ["E:\\lifeshare-scratch\\meshy_export"]` inside `workers.machines`. Fletchery copies the
  list to `<state>\\asset_roots.json` and logs a line at start for any folder
  that is missing or empty.
- **API:** `/v1/assets/list` gives each folder's files with their size and SHA-256 (hashes are cached by size and
  modified time). `/v1/assets/read` gives one 4 MiB chunk of one file. It only serves files inside a shared folder,
  with no `..` and no other paths.
- **Worker:** `node_worker.py` (version 2) mirrors each shared folder to the **same path** on the node, or to a path set
  with `assetMap` in its config. It runs first in
  every job that needs it, as the step "Mirror the PC's asset folders", and also when the worker starts and every 15 minutes while it is idle (checking in every 30 s while it copies, so the node never looks offline), so a newly added node fills itself before
  its first import. Only missing or changed files are copied. Each file goes to `<name>.fletchery-part`, has its SHA-256
  checked, and then replaces the old copy. Nothing on the node is deleted. A worker running on the PC itself skips this.
  `python node_worker.py sync --config <file>` mirrors on demand.
- **Always used:** the worker reports the label `cap:asset-sync`. When `assetRoots` is set, every import job needs that
  label, so an older worker never gets an import. If the mirror fails, the job fails before importing, with "Asset
  folders not mirrored, imports not started: <reason>".
- **New asset folder:** add it to `assetRoots` . Nodes pick it up within 15
  minutes, or at their next import.
## Builds come back to the PC

After a `package` job passes, the node zips the build and sends it to Fletchery in 4 MiB chunks over the worker API
(`/v1/upload`, only for the node's own package job). Fletchery checks the SHA-256, keeps `<state>\\builds\\<Name>-<version>.zip`
and unpacks it beside it, and the Builds tab points at that folder. If the copy fails, the build stays on the node and
the job still passes. `"uploadBuilds": false` in a node's config turns it off. For a build packaged before this existed:
`python node_worker.py upload --config <file> --job <package job id> --folder <build folder>`.
## Keeping a machine's disk free

A node has no Fletchery backend, so nothing cleaned the worktrees that sessions left on it (2026-11-16: SF-NODE-01's
work drive reached 0 GB). Worker version 8 runs the same scratch retention as the PC (`scratch.py`, README "Session
folders"). Copy `fletchery\scratch.py` beside `node_worker.py` and add to the worker's config:

    "scratchRoots": ["C:\\StoneNode", "E:\\lifeshare-scratch"],
    "scratchMode": "remove",                      # off, report, trim (default) or remove
    "scratchKeep": ["LifeShareArt", "*rescue*"],  # folder names never touched
    "scratchDisposable": ["*-builds"],            # plain folders listed once idle scratchRemoveDays (never removed)
    "scratchTrimHours": 24, "scratchRemoveDays": 7, "scratchMinFreeGB": 30

The idle worker runs a pass when it starts and every hour, never during a job and never while a person's pause file
exists. Under `scratchMinFreeGB` free on a root's drive, build output is trimmed after 2 idle hours instead of
`scratchTrimHours`; nothing is ever removed sooner. In `remove` mode a clean, pushed worktree is first listed
(`<workRoot>\fletchery\scratch-plan.json`), a day later moved to `lifeshare-quarantine` beside the root, and a week
after that removed; a folder that is not a git checkout is never removed (README "Session folders"). The worker's own
`workRoot`, `buildsDir` and mirrored asset folders, `build-slots` and the quarantine are never entered. Receipts are
in `<workRoot>\fletchery\scratch-retention.json`, and its `attention` list names the
idle folders that still hold uncommitted or unpushed work. `python node_worker.py scratch --config <file>` runs one
pass now. Without `scratch.py` beside it, or without `scratchRoots`, the worker cleans nothing.

The worker (version 9) also compacts its own checkout's git store. Every shallow fetch adds a pack, a killed fetch
leaves `tmp_pack_*` files, and a leftover `fetch-pack <pid>` `.keep` marker makes `git gc` skip its pack for good, so
the store only grew (81 GB here, 103 GB in a CI checkout, 2026-11-16). Every six idle hours, when the packs are over
`compactMinGB` (default 20; 0 turns it off) no git lock exists and no other worktree is attached to the checkout (a shared store is never compacted, because
a session could be writing to it), the worker removes `tmp_pack_*` files older than a
day and the `.keep` markers of fetches that are no longer running, then runs `git gc --prune=now` at below-normal
priority. Other `.keep` files, LFS storage and worktrees are never touched.
## Setting it up

1. **Profile** (`workers` block):

   ```json
   "machines": {"listen": "100.64.0.10:8771"}
   ```

   Fletchery restarts with it; the log says `machines: worker API on 100.64.0.10:8771`.
2. **Add the node** on the PC:
   `python <install>\fletchery\machines.py --state-dir <state> add NODE-01`. Copy the key it prints.
3. **On the node**, put the key in a file only the worker's account can read (e.g. `C:\FletcheryNode\keys\fletchery_node_key`),
   copy `fletchery\node_worker.py` anywhere, and write its config:

   ```json
   {"coordinator": "http://100.64.0.10:8771", "machine": "NODE-01",
    "keyFile": "C:\\FletcheryNode\\keys\\fletchery_node_key",
    "labels": ["os:windows", "ram:24", "unreal:5.8.4", "role:tests", "role:cook"],
    "workRoot": "E:\\buildlane-node\\_work",
    "repoUrl": "git@github.com:Stonefletcher-Productions-LLC/LifeShare.git",
    "gitSshKey": "C:\\FletcheryNode\\keys\\lifeshare_deploy",
    "engine": "E:\\UE_5.8",
    "pauseFile": "E:\\GitHubRunner\\PAUSED-NODE",
    "recipes": ["checkout-only", "package"]}
   ```

   The checkout is `<workRoot>\fletchery\LifeShare` (its own, so it never fights a GitHub runner's checkout).
   `python node_worker.py check --config <file>` signs in once and prints what the coordinator answers.
   `python node_worker.py install --config <file>` adds the logon task **Fletchery node worker** (current user,
   limited rights, below-normal priority); `schtasks /Run /TN "Fletchery node worker"` starts it now.
   The worker's own log is `<workRoot>\fletchery\worker.log`.
4. **First job:** queue a `checkout-only` (or start a build on Builds) and watch the machine on the Builds tab and on
   Live. Then install `reporter.py` on it too ([PORTAL.md](PORTAL.md#the-live-tab)).

While `pauseFile` exists the node still checks in (it shows "paused on the node") but takes no new job.

## Command line

```text
python machines.py --state-dir <state> add <name>       # prints the key once
python machines.py --state-dir <state> revoke <name>
python machines.py --state-dir <state> list
python machines.py --state-dir <state> cancel <id>
```

## The worker API

Send a JSON object by POST with `Authorization: Bearer <key>` and `X-Fletchery-Machine: <name>`.
The worker checks the machine key before reading the body. Machine reports and control requests share the portal's
8 KiB body limit and 30-second socket inactivity timeout. Log batches and structured job results allow up to 1 MiB
per request. Build upload requests allow 8 MiB for the encoded chunk. Limits apply to the request body in bytes.

| Path | Body | Answer |
|---|---|---|
| `/v1/hello` | `labels, ram_gb, version, stats` | `poll_seconds` |
| `/v1/claim` | `stats, paused` | `job` (`id, recipe, sha, ref, params, timeout_minutes`) or `null` |
| `/v1/heartbeat` | `job_id, stats` (every 30 s) | `cancel` |
| `/v1/log` | `job_id, text` (every 5 s) | `ok` |
| `/v1/result` | `job_id, exit_code, summary, result` (the result JSON) | `ok` |
| `/v1/upload` | a finished build, in 4 MiB chunks (only for the machine's own `package` job) | `ok` |
| `/v1/assets/list`, `/v1/assets/read` | the shared asset folders (above) | files, or one chunk |
| `/v1/report` | what the machine is doing, from `reporter.py` | `ok` |

## Restarting a machine

Each machine's card on the Live tab has a **Restart** button (the window, and the phone when controls are on). A script
or an agent does the same with `POST /api/machines/restart {"name": "<machine>", "force": false, "reason": "..."}` on
the dashboard port (header `Origin: http://127.0.0.1:<port>`). Without `force` the request is refused while a live
run holds the machine, so nobody ends a build by accident; the button asks first and then sends `force`.

How it reaches the machine (`fletchery/restart.py`):

1. The backend records the request. The machine's reporter collects it with its next report (about 10 seconds) and
   runs `shutdown /r /f`. No admin rights, no new port, no new secret.
2. The **restart guard** asks the backend the same question every 15 seconds. It is a startup task that runs as
   SYSTEM, so it works with nobody signed in, and if Windows is still up 10 minutes after the polite restart it
   restarts the machine the hard way. Install it once per machine from an elevated prompt:
   `python fletchery\restart.py install-guard --config <reporter.json>`
3. A request belongs to the boot it was made in. A machine that started after the request never gets it, so a restart
   cannot loop. A request nobody collects in 10 minutes is dropped.
4. The first report after the boot marks the restart "back" with how long it took. A machine that is not back after
   15 minutes is flagged on the Live tab.

The card says before you press whether the machine **signs in by itself** after a restart. A machine that does not
comes back with its services (GitHub runners, the restart guard) but without anything that starts at sign-in: the
reporter, the Fletchery backend, Claude. Turn on automatic sign-in (Sysinternals Autologon) on any machine that must
come back with nobody at it. `python fletchery\restart.py status` prints what this machine will do.

| Path | Body | Answer |
| --- | --- | --- |
| `/v1/restart` | `{boot, ack}` from the restart guard | `{restart}`: the request to act on, or null |

### Restart when idle

A machine that is running something offers two buttons. **Restart when idle** stops nothing: the request waits (up to 12 hours) until the machine reports that no build, runner job or slot hold is alive, and restarts it then. It can be withdrawn with **Cancel restart** while it waits. **Restart now** ends what is running. From a script: `POST /api/machines/restart {"name": "SF-NODE-01", "when_idle": true}`, and `{"name": ..., "cancel": true}` to withdraw.

## A finished check that still holds a machine

A cancelled PR check can leave an editor that Windows accepts the kill for but that never exits. The machine then
looks busy with a check that ended long ago, and every other check there fails at cleanup or waits.

`unjam.py` looks every two minutes. When the reservation holder is "PR check #N (GitHub run R)", it asks GitHub for
the **latest attempt** of run R and that attempt's jobs. A re-run keeps the run's id, so nothing GitHub said about a
run is kept from one look to the next. If every job of the latest attempt ended more than ten minutes ago, and the
machine has been held since before that, Live shows it under "Stuck or blocked" with what clears it. A job that waits
or runs, or jobs that cannot be read, mean it is not flagged.

If the machine signs in by itself after a restart, Fletchery restarts it, at most once in two hours per machine, and
only when nothing is working on it on two looks in a row. Any of these counts as working and stops the restart:

- GitHub shows one of the machine's runners as busy, or a job on it;
- the machine reports a `Runner.Worker` process (a runner job, and everything that job started), or a compiler
  (`cl.exe`, `link.exe`, `clang-cl.exe`);
- the Live feed could not read GitHub in the last five minutes, so the runners' state is not known.

An Unreal editor alone does not count: the editor a cancelled check left behind is what the restart clears. While
something works there the machine stays flagged, the text says what was seen, and **Restart now** on its card still
restarts it by hand. A machine that does not sign in by itself is flagged and never restarted from here.
`"autoUnjam": false` in the profile's `workers.portal.live` block turns the restart off; the flag stays. Automatic
re-runs wait for a machine in this state.

### A process that cannot be ended still holds files (LifeShare #2530)

The rule above misses a jam a *running* check hits: on 2026-11-23 a killed UnrealEditor-Cmd on the Gaming PC kept
`Binaries\Win64\UnrealEditor-LSBoot.dll` of the PR checkout mapped and the next check sat 25 minutes in its checkout
step. LifeShare's job now moves such a file to `<PR checkout>\Saved\StuckFiles\<utc stamp>\...` and goes on. The
reporter looks there every minute and counts the files it cannot open exclusively for writing: each one is held by a
process that is still alive, and only a restart ends it. A machine that shows any for ten minutes is jammed, with the
same rules as above (signs in by itself, once in two hours, nothing working on two looks in a row).

The reporter finds the PR checkouts from `prCheckouts` in reporter.json, else from `PR_CHECKOUT_ROOT` in each PR
runner's `.env` (written by LifeShare's `Tools\CI\InstallPrRunner.ps1`).

### How a restart is asked for

Automatic restarts are always **when idle**: a running build is never ended. A lease whose process is gone does not
count as running.

**Someone's PC** (the reporter says `attended`: `"attended": true` in reporter.json, or LifeShare's game throttle runs
there) is restarted only when nobody uses it: no keyboard or mouse input for 30 minutes in the signed-in session and
none of the games the game throttle watches (`yieldTo` in the lease folder's machine.json, else its defaults) running.
Input that cannot be read counts as in use. Until then the jam stays flagged and the machine's card says
"Waiting until you are away" and why. Other machines (SF-NODE-01) restart when idle as before.

## A waiting check that was cancelled and never put back

`requeue.py` looks every five minutes. GitHub starts nothing by itself after a cancel, so a check that something
cancelled while it waited for a machine, meaning to re-queue it, stays gone if that something fails (LifeShare #2542:
nine builds, both machines idle for 48 minutes). A check is re-queued when it was cancelled at least ten minutes ago,
is the newest check on the current commit of an open PR with auto-merge on, and its build never got a machine. A build
that was running when it was cancelled is never restarted. While a "Tip fix first" run is under way nothing is
re-queued: that run puts back what it moved. One check is re-queued at most twice a day, five checks a round; what it
did and what it left alone is in `requeue.json` in the state folder and in the log ("re-queue: ...").

To keep one PR's check from coming back, turn its auto-merge off or make it a draft. `"autoRequeue": false` in the
profile's `workers.portal.live` block turns the whole thing off.
