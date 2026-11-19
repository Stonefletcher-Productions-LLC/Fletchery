"""Live: everything happening in development, across every repository and machine, for the phone portal's Live tab.

Three sources, merged into one snapshot (`DevWatch.snapshot()`), which portal.py serves at /api/live:

1. GitHub, read by this PC. Every repository the token can see is found by itself (`/user/repos`), so a new project
   shows up with no code change. For each repository that saw a push in the last `activeDays`: Actions runs (and the
   jobs and current step of those still going), open pull requests with their check runs, and open issues. Self-hosted
   runners come from each organisation (and each personal repository). Every GET carries the ETag of the last answer,
   and GitHub does not count a "304 Not Modified" against the 5,000-an-hour limit, so asking every 15 seconds costs
   almost nothing while little changes. It runs on this PC, so it spends no Actions minutes. It asks every `fastSeconds`
   while anything is queued or running, else every `slowSeconds`, and backs off when the limit runs low or GitHub
   asks it to wait.

   Why not webhooks: GitHub would need a public address into this PC, and the portal is deliberately tailnet-only.

2. The machines themselves. reporter.py, on each machine, posts what LaneGuard's lease says the machine is doing ("PR
   check #2290", "Hand merge batch 7", proofs, builds), who queues for it, which tools run (GitHub job, Unreal, Claude,
   Blender) and its CPU, memory and disk, every ~10 seconds, to the machines worker API (/v1/report), signed with the
   machine's existing key. That covers work GitHub never sees.

3. The coding agents themselves (presence.py, docs/AGENT-PRESENCE.md): a heartbeat from an agent on a machine
   (/v1/agent), or an edited comment on a repository's status issue from a cloud agent. The Agents card lists only
   agents working now, and falls back to pushes and PR updates for an agent that sends nothing.

Nothing goes stale quietly: every item carries its age, and `blockers` names what looks stuck (a feed that stopped, a
machine that stopped reporting, a lease whose process is gone, a lease held with the CPU idle, a step or queue that ran
far too long, an offline runner, a failed check, an issue labelled blocked).

Profile, under `workers.portal` (all optional):

    "live": {"owners": ["My-Org", "me"], "exclude": ["me/old-repo"], "activeDays": 21,
             "fastSeconds": 15, "slowSeconds": 90, "names": {"MY-PC": "Gaming PC"},
             "agentIssues": {"My-Org/game": 123}, "agentActiveMinutes": 15}

Standard library only."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import flow as flowmod
import pipeline  # beside this file, like every Fletchery module
import presence
import store as storemod

API = "https://api.github.com"
ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
# A label that says the item is blocked: the whole label, after an optional "status:"-style prefix. "sev: blocker" (how
# bad a bug is) is not one (2026-11-18: it put a crash report under Stuck or blocked).
BLOCKED_LABEL = re.compile(r"^(?:[\w .-]{1,20}[:/]\s*)?(?:blocked(?:[\s_:-].*)?|needs[\s_:-]*(?:alex|you|human|person|owner|decision)"
                           r"|waiting[\s_:-]*(?:on|for)(?:[\s_:-].*)?)$", re.I)
# A blocked issue that waits on a person (not on other work) still counts as blocked (2026-11-18: triage labels parked
# nine issues "blocked" on other issues and Stuck jumped from 9 to 23).
NEEDS_PERSON_LABEL = re.compile(r"^(?:[\w .-]{1,20}[:/]\s*)?(?:needs[\s_:-]*(?:alex|you|human|person|owner|decision)"
                                r"|(?:blocked|waiting)[\s_:-]*(?:on|for)[\s_:-]*(?:alex|you|human|person|owner|decision))$", re.I)
FAILED = ("failure", "timed_out", "action_required", "startup_failure")
# A check that gave no verdict on the PR: cancelled, or a run whose machine stayed busy (LifeShare's PrCheckJob exits 2
# with "No result: ... Re-run this check"). It needs a re-run, not a fix, so it is not a blocker.
NO_RESULT = re.compile(r"no result", re.I)
# Coding agents' branches (and PR authors): one Agents row per branch with recent activity.
AGENTS = (("claude/", "Claude"), ("codex/", "Codex"))
AGENT_HOURS = 24      # a branch quiet this long drops off the Agents card (workers.portal.live.agentHours)
AGENT_ACTIVE_MINUTES = 15   # working now: a heartbeat this recent (or, for an agent that sends none, a push or PR update)
CODING_FRESH = 30 * 60   # a push this recent counts as "coding"
GENERIC_LABELS = {"self-hosted", "windows", "linux", "macos", "x64", "arm64"}
REPORT_STALE = 60          # a machine that has not reported for this long is shown as silent
RUNNER_REFUSED_RETRY = 3600   # a runner list GitHub refused (no admin rights) is asked again after an hour
LIMITS = {                 # seconds before something counts as stuck
    "queued_run": 20 * 60,      # an Actions run nobody picked up
    "step": 150 * 60,           # one job step (LifeShare's PR check "Build and test" runs 60-90 min)
    "job": 3 * 3600,            # one job
    "lease": 4 * 3600,          # one hold on a machine
    "idle_lease": 10 * 60,      # a hold while the machine's CPU stays idle
    "queue_wait": 90 * 60,      # a run waiting in a machine's queue
    "check": 3 * 3600,          # one PR check run (as long as a job)
    "auto_merge": 2 * 3600,     # a PR set to merge automatically, checks green, not merged
}
DISK_LOW_GB = 30               # a machine's drive with less free space than this is flagged (workers.portal.live.diskFreeGb)


def _agent_of(branch: str, author: str) -> str:
    """Which coding agent a branch (or PR author) belongs to: "Claude", "Codex" or ""."""
    b, a = str(branch or "").lower(), str(author or "").lower()
    for prefix, name in AGENTS:
        if b.startswith(prefix):
            return name
    return "Codex" if "codex" in a else ""


def _latest_checks(cr: list) -> list:
    """The newest check run per name on a commit: a re-run (queued, running or passed) replaces the failure before it."""
    best = {}
    for c in cr:
        if not isinstance(c, dict):
            continue
        k, b = str(c.get("name") or ""), best.get(str(c.get("name") or ""))
        if b is None or (c.get("id") or 0) > (b.get("id") or 0):
            best[k] = c
    return list(best.values())


def _ago_text(seconds: float) -> str:
    s = int(seconds)
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h"


def now_iso_at(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _age(iso, at: float | None = None) -> float:
    t = _ts(iso)
    return max(0.0, (at or time.time()) - t) if t else 0.0


def _text(v, limit: int = 300) -> str:
    return str(v if v is not None else "")[:limit]


def _web(v) -> str:
    s = str(v or "").strip()
    return s if s.startswith("https://") and " " not in s else ""


PATHS = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|]*")


def _scrub(v, limit: int = 300) -> str:
    """Folder paths on a machine stay on that machine."""
    return PATHS.sub("[a folder]", _text(v, limit * 2))[:limit]


DISK_WARN_GB = 50   # the disk guard's warnGB when its report has none


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _holder_name(path) -> str:
    """A folder's last two parts ("lifeshare-scratch\\wt-bus-lines"): enough to know it, without the machine's path."""
    parts = [p for p in re.split(r"[\\/]+", str(path or "")) if p and not re.fullmatch(r"[A-Za-z]:", p)]
    return _text("\\".join(parts[-2:]) or "?", 80)


def _disk_guard(g) -> dict | None:
    """LifeShare's disk guard report (disk.json, sent by reporter.py 2+), cleaned for the page; None without one."""
    if not isinstance(g, dict) or _num(g.get("freeGB")) is None:
        return None
    free, warn = float(g["freeGB"]), float(_num(g.get("warnGB")) or DISK_WARN_GB)
    state = _text(g.get("state"), 10).lower()
    return {"drive": _text(g.get("drive"), 8), "free_gb": round(free, 1), "total_gb": _num(g.get("totalGB")),
            "state": state if state in ("ok", "low", "full") else "", "warn_gb": warn, "min_gb": _num(g.get("minGB")),
            "low": free < warn or state in ("low", "full"),
            "runner_stopped": [_text(x, 80) for x in (g.get("runnerStopped") or []) if x][:10],
            "holders": [{"name": _holder_name(h.get("path")), "gb": round(float(h["gb"]), 1)} for h in (g.get("holders") or [])[:8]
                        if isinstance(h, dict) and _num(h.get("gb")) is not None],
            "updated": _text(g.get("updatedUtc"), 40), "last_clean": _text(g.get("lastCleanUtc"), 40),
            "last_freed_gb": _num(g.get("lastFreedGB"))}


BUILD_KINDS = ("building", "check", "proof", "merging", "importing")   # what "Building now" lists


def kind_of(purpose: str) -> str:
    """What a machine's hold is for, from the name its run gave itself (LaneGuard's LS_LANE_PURPOSE)."""
    p = (purpose or "").lower()
    for kind, words in (("check", ("pr check", "pr-check", "check #")), ("merging", ("merge", "hand-land", "hand land", "landing", "batch")),
                        ("proof", ("proof", "navcheck", "spec", "test")), ("building", ("build", "package", "cook", "release")),
                        ("importing", ("import",)), ("coding", ("claude", "coding"))):
        if any(w in p for w in words):
            return kind
    return "working"


# ---- GitHub ----
class GitHub:
    """GETs with ETags. A 304 is free and answers with the copy kept from last time."""

    def __init__(self, token_fn, log=None, timeout: float = 20):
        self.token_fn = token_fn
        self.log = log or (lambda line: None)
        self.timeout = timeout
        self.cache: dict[str, tuple[str, object]] = {}
        self.remaining = -1
        self.reset_at = 0.0
        self.wait_until = 0.0
        self.calls = 0          # requests that counted against the limit
        self.free = 0           # 304s
        self._token = ""
        self._token_at = 0.0
        self._lock = threading.Lock()
        self.via = "https"          # or "gh" once direct HTTPS fails on this machine
        self.gh_cmd = "gh"          # None: never fall back (tests)

    def token(self) -> str:
        """Read again every 10 minutes: a `gh auth refresh` (new scopes) must reach the feed without a restart."""
        if not self._token or time.time() - self._token_at > 600:
            self._token = self.token_fn() or self._token
            self._token_at = time.time()
        return self._token

    def get(self, path: str):
        """The JSON at `path` (a list or dict), or raises GitHubError."""
        if time.time() < self.wait_until:
            raise GitHubError(0, "GitHub asked to wait")
        url = path if path.startswith("https://") else API + "/" + path.lstrip("/")
        with self._lock:
            etag, kept = self.cache.get(url, ("", None))
        if self.via == "gh":
            code, headers, body = self._gh(url, etag)
        else:
            try:
                code, headers, body = self._urllib(url, etag)
            except GitHubError as e:
                if e.code or self.gh_cmd is None:
                    raise
                # Some processes on a PC can't open sockets of their own (a sandbox, a firewall rule for one program)
                # while `gh` can: 2026-11-18 the Gaming PC backend got URLError on every call. Use gh from then on.
                self.via = "gh"
                self.log(f"live: direct HTTPS failed ({e}); reading GitHub through gh from now on")
                code, headers, body = self._gh(url, etag)
        self._limits(headers)
        if code == 304 and kept is not None:
            with self._lock:
                self.free += 1
            return kept
        with self._lock:
            self.calls += 1
        if 200 <= code < 300:
            data = json.loads(body or b"null")
            if headers.get("ETag"):
                with self._lock:
                    if len(self.cache) > 4000 and url not in self.cache:   # bounded: old URLs (finished runs) drop out
                        for old in list(self.cache)[:1000]:
                            self.cache.pop(old, None)
                    self.cache[url] = (headers["ETag"], data)
            return data
        if code == 401:
            self._token = ""        # read the token again next time (gh may have refreshed it)
        if code in (403, 429) and (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"):
            wait = float(headers.get("Retry-After") or 0) or max(60.0, float(headers.get("X-RateLimit-Reset") or 0) - time.time())
            self.wait_until = time.time() + min(wait, 3600)
        try:
            msg = json.loads(body or b"{}").get("message", "")
        except (ValueError, AttributeError):
            msg = ""
        raise GitHubError(code, msg or f"HTTP {code}")

    def _urllib(self, url: str, etag: str):
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Fletchery-Live",
            **({"Authorization": "Bearer " + self.token()} if self.token() else {}),
            **({"If-None-Match": etag} if etag else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, _Headers(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            return e.code, _Headers((e.headers or {}).items()), e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(0, f"GitHub could not be reached: {getattr(e, 'reason', None) or e}"[:300])

    def _gh(self, url: str, etag: str):
        """The same GET through `gh api -i` (no window): status line, headers, then the body."""
        path = url[len(API) + 1:] if url.startswith(API + "/") else url
        cmd = [self.gh_cmd or "gh", "api", "-i", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
        if etag:
            cmd += ["-H", f"If-None-Match: {etag}"]
        try:
            out = subprocess.run(cmd + [path], capture_output=True, timeout=self.timeout + 10,
                                 creationflags=0x08000000 if os.name == "nt" else 0)
        except (OSError, subprocess.SubprocessError) as e:
            raise GitHubError(0, f"gh could not be run: {e}"[:300])
        return parse_gh_output(out.stdout, out.stderr)

    def _limits(self, headers) -> None:
        if headers is None:
            return
        try:
            if headers.get("X-RateLimit-Remaining") is not None:
                self.remaining = int(headers["X-RateLimit-Remaining"])
                self.reset_at = float(headers.get("X-RateLimit-Reset") or 0)
        except ValueError:
            pass


class _Headers(dict):
    """Case-insensitive header lookup."""

    def __init__(self, items=()):
        super().__init__((str(k).lower(), str(v)) for k, v in items)

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())


def parse_gh_output(stdout: bytes, stderr: bytes = b""):
    """`gh api -i` prints "HTTP/2.0 200 OK", the headers, a blank line, then the body; on an error status it still prints
    them, and stderr says "HTTP 404"."""
    text = stdout or b""
    head, _, body = text.replace(b"\r\n", b"\n").partition(b"\n\n")
    lines = head.decode("utf-8", "replace").split("\n")
    m = re.match(r"HTTP/[\d.]+\s+(\d{3})", lines[0] if lines else "")
    if not m:
        e = re.search(r"HTTP (\d{3})", (stderr or b"").decode("utf-8", "replace"))
        if not e:
            raise GitHubError(0, ("gh: " + (stderr or b"").decode("utf-8", "replace").strip())[:300] or "gh gave no answer")
        return int(e.group(1)), _Headers(), b""
    headers = _Headers(tuple(x.split(":", 1)) for x in lines[1:] if ":" in x)
    return int(m.group(1)), _Headers((k, v.strip()) for k, v in headers.items()), body


class GitHubError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"{code} {message}" if code else message)
        self.code = code


def gh_token() -> str:
    """GH_TOKEN / GITHUB_TOKEN, else the token `gh` is signed in with (read without opening a window)."""
    for name in ("FLETCHERY_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name].strip()
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20,
                             creationflags=0x08000000 if os.name == "nt" else 0)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ---- the watcher ----
def _runner_job(run: dict) -> dict:
    """What a runner is doing, from its run row: enough to see a step that runs too long or a run nobody needs."""
    return {"title": run["name"] + (f" · PR #{run['pr']}" if run["pr"] else ""), "repo": run["repo"], "id": run["id"],
            "step": run["step"], "step_started": run["step_started"], "url": run["url"], "started": run["started"],
            "status": run["status"], "pr_closed": bool(run.get("pr_closed")), "pr": run["pr"]}


class DevWatch:
    def __init__(self, conf: dict | None = None, log=None, token_fn=gh_token, github: GitHub | None = None,
                 state_dir=None):
        conf = conf or {}
        self.log = log or (lambda line: None)
        self.owners = {str(o).lower() for o in conf.get("owners") or []}
        self.exclude = {str(r).lower() for r in conf.get("exclude") or []}
        self.active_days = float(conf.get("activeDays") or 21)
        self.fast = max(10.0, float(conf.get("fastSeconds") or 15))
        self.slow = max(self.fast, float(conf.get("slowSeconds") or 90))
        self.names = {str(k).lower(): _text(v, 60) for k, v in (conf.get("names") or {}).items()}
        # runner name suffixes to strip when GitHub doesn't send a runner's labels (InstallPrRunner.ps1 labels)
        self.runner_labels = [str(x) for x in conf.get("runnerLabels") or ("unreal-pr", "unreal-light")]
        self.gh = github or GitHub(token_fn, self.log)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.repos: list[dict] = []
        self.repos_at = 0.0
        self.runners: dict[str, list] = {}          # owner or repo -> runners
        self.runner_note = ""
        self.runs: dict[str, list] = {}             # repo -> recent runs
        self.jobs: dict[int, list] = {}             # run id -> jobs
        self.prs: dict[str, list] = {}              # repo -> open PRs
        self.checks: dict[tuple, list] = {}         # (repo, sha) -> check runs
        self.pushes: dict[str, list] = {}           # repo -> recent pushes to agent branches (from repository events)
        self.closed: dict[str, list] = {}           # repo -> recently merged or closed PRs' branches (they leave Agents)
        self.no_result: dict[int, bool] = {}        # failed check run id -> its annotations say "No result" (asked once)
        self.issues: dict[str, list] = {}           # repo -> open issues
        self.reports: dict[str, dict] = {}          # machine -> its last report
        self.skip: dict[str, float] = {}            # path -> don't ask again before
        self.last_ok = ""
        self.last_try = ""
        self.error = ""
        self.interval = self.fast
        self.started = now_iso()
        # The backend adds what only it knows (app.py): build machines whose worker stopped calling in, builds no
        # machine can take. A callable returning [{"kind", "title", "detail", "since", "key"}].
        self.extra = None
        # Builds the backend runs on its machines (app.py): [{"machine", "title", "since", "detail", "kind"}], shown on
        # each machine and under "Building now" like everything else a machine does.
        self.own_builds = None
        try:
            self.disk_low = float(conf.get("diskFreeGb", DISK_LOW_GB)) if isinstance(conf, dict) else DISK_LOW_GB
        except (TypeError, ValueError):
            self.disk_low = DISK_LOW_GB
        try:
            self.agent_hours = float(conf.get("agentHours", AGENT_HOURS)) if isinstance(conf, dict) else AGENT_HOURS
            self.agent_active = 60 * float(conf.get("agentActiveMinutes", AGENT_ACTIVE_MINUTES)) if isinstance(conf, dict) else 60 * AGENT_ACTIVE_MINUTES
        except (TypeError, ValueError):
            self.agent_hours, self.agent_active = AGENT_HOURS, 60 * AGENT_ACTIVE_MINUTES
        # Cloud agents' status issues (docs/AGENT-PRESENCE.md): {"Owner/Repo": 123}. Anything else is ignored, with a note.
        self.agent_issues: dict[str, int] = {}
        for repo, number in ((conf.get("agentIssues") or {}) if isinstance(conf.get("agentIssues"), dict) else {}).items():
            if re.match(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$", str(repo)) and isinstance(number, int) and number > 0:
                self.agent_issues[str(repo).lower()] = number
            else:
                self.log(f"live: agentIssues entry {str(repo)[:80]!r} ignored: give \"Owner/Repo\": <issue number>")
        # Where each change is in the whole process, and how long its steps usually take (pipeline.py).
        self.rules = pipeline.Rules(conf.get("pipeline"))
        self.history = pipeline.History(Path(state_dir) / "step-times.json" if state_dir else None, log=self.log)
        self.merged: dict[str, list] = {}           # repo -> PRs merged in the last day (followed through staging)
        self.releases: dict[str, list] = {}         # repo -> its latest releases
        self.presence = presence.Presence(Path(state_dir) / "agent-presence.json" if state_dir else None, clean=_scrub,
                                          active_seconds=self.agent_active, log=self.log)
        # What happened, kept (store.py): merges, runs, jobs. The Flow strip reads it (flow.py).
        self.store = storemod.Store(Path(state_dir) / "fletchery.db" if state_dir else None, log=self.log)
        self.flow = flowmod.Flow(self.store, conf.get("flow"))
        self._flow_cache: tuple[float, dict] = (0.0, {})

    # ---- machines ----
    def report(self, machine: str, data: dict, ip: str = "") -> dict:
        """A machine's report (machines.py /v1/report). Kept whole but bounded; cleaned when shown."""
        if not isinstance(data, dict):
            raise ValueError("report must be an object")
        body = json.dumps(data)
        if len(body) > 64 * 1024:
            raise ValueError("report too large")
        with self._lock:
            was = (self.reports.get(str(machine)) or {}).get("data") or {}
            self.reports[str(machine)] = {"data": data, "at": now_iso(), "ip": ip}
        if data.get("busy") and not was.get("busy"):
            self._wake.set()      # a machine just picked something up: read GitHub soon (once, not on every report)
        return {"ok": True, "every": 10}

    def agent(self, machine: str, data: dict) -> dict:
        """An agent's heartbeat from a registered machine (machines.py /v1/agent). Raises ValueError (400) or
        presence.TooMany (429)."""
        return self.presence.beat(machine, data)

    # ---- the loop ----
    def start(self) -> None:
        threading.Thread(target=self._run, name="fletchery-live", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:          # never let the feed die: say why and try again
                self.error = f"{type(e).__name__}: {e}"[:300]
                self.log(f"live: {self.error}")
            self._wake.wait(self.interval)
            self._wake.clear()

    def poll(self) -> None:
        self.last_try = now_iso()
        gh = self.gh
        if time.time() - self.repos_at > 600 or not self.repos:
            self.repos = self._discover()
            self.repos_at = time.time()
        names = [r["full_name"] for r in self.repos]
        orgs = sorted({r["owner"] for r in self.repos if r.get("org")})
        with ThreadPoolExecutor(max_workers=6) as pool:
            got = dict(zip(names, pool.map(self._poll_repo, names)))
            # Organisation runners need admin:org; runners registered on one repository are listed there, so every
            # repository is asked too (an ETag answer costs nothing; a refusal is asked again after an hour).
            places = [("orgs", o) for o in orgs] + [("repos", r["full_name"]) for r in self.repos]
            found = dict(zip([p[1] for p in places], pool.map(self._poll_runners, places)))
        runs, jobs, prs, checks, issues, pushes, closed, merged, releases = {}, {}, {}, {}, {}, {}, {}, {}, {}
        errors = [g["error"] for g in got.values() if g.get("error")]
        for repo, g in got.items():
            runs[repo], prs[repo], issues[repo], pushes[repo] = g["runs"], g["prs"], g["issues"], g.get("pushes") or []
            closed[repo], merged[repo], releases[repo] = g.get("closed") or [], g.get("merged") or [], g.get("releases") or []
            jobs.update(g["jobs"])
            checks.update(g["checks"])
        with self._lock:   # swapped whole, so a snapshot never sees half a poll
            self.runs, self.jobs, self.prs, self.checks, self.issues, self.pushes = runs, jobs, prs, checks, issues, pushes
            self.closed, self.merged, self.releases = closed, merged, releases
            self.runners = {k: v for k, v in found.items() if v is not None}
        try:
            self._learn()
        except Exception as e:      # step times are a nicety: never lose the feed over them
            self.log(f"live: step times: {type(e).__name__}: {e}")
        try:
            self._record(got)
        except Exception as e:      # history is a nicety too: the live feed comes first
            self.log(f"live: history: {type(e).__name__}: {e}")
        busy = any(r.get("status") in ACTIVE for rs in runs.values() for r in rs) or any(
            (rep["data"] or {}).get("busy") for rep in list(self.reports.values()))
        self.interval = self.fast if busy else self.slow
        if 0 <= gh.remaining < 300:      # running low: stretch until the hour resets
            self.interval = max(self.interval, min(600.0, max(60.0, gh.reset_at - time.time())))
        if errors and len(errors) == len(got):
            self.error = errors[0][:300]
            raise GitHubError(0, errors[0])
        self.error = ("; ".join(errors))[:300] if errors else ""
        self.last_ok = now_iso()

    def _discover(self) -> list[dict]:
        out, page = [], 1
        cutoff = time.time() - self.active_days * 86400
        while page <= 5:
            batch = self.gh.get(f"user/repos?affiliation=owner,collaborator,organization_member&sort=pushed&per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            for r in batch:
                full = str(r.get("full_name") or "")
                owner = str((r.get("owner") or {}).get("login") or "")
                if (not full or r.get("archived") or full.lower() in self.exclude
                        or (self.owners and owner.lower() not in self.owners)):
                    continue
                if _ts(r.get("pushed_at")) < cutoff:
                    continue
                out.append({"full_name": full, "owner": owner, "org": (r.get("owner") or {}).get("type") == "Organization",
                            "url": _web(r.get("html_url")), "private": bool(r.get("private"))})
            if len(batch) < 100 or _ts(batch[-1].get("pushed_at")) < cutoff:
                break
            page += 1
        return out

    def _statuses(self, repo: str, sha: str) -> list:
        """A commit's statuses, shaped like check runs (newest per context)."""
        state = {"success": ("completed", "success"), "failure": ("completed", "failure"), "error": ("completed", "failure"),
                 "pending": ("in_progress", None)}
        out = []
        for st in (self._try(f"repos/{repo}/commits/{sha}/status?per_page=100") or {}).get("statuses") or []:
            if not isinstance(st, dict) or str(st.get("state")) not in state:
                continue
            status, conclusion = state[str(st["state"])]
            row = {"id": st.get("id"), "name": st.get("context"), "status": status, "conclusion": conclusion,
                   "html_url": st.get("target_url"), "started_at": st.get("created_at"),
                   "completed_at": st.get("updated_at") if status == "completed" else None, "source": "status"}
            if conclusion == "failure" and isinstance(st.get("id"), int):
                self.no_result[st["id"]] = bool(NO_RESULT.search(str(st.get("description") or "")))
            out.append(row)
        return out

    def _try(self, path: str, quiet_for: float = 1800):
        """GET that remembers a 403/404 (no access, Actions off) and stops asking for a while."""
        if self.skip.get(path, 0) > time.time():
            return None
        try:
            return self.gh.get(path)
        except GitHubError as e:
            if e.code in (403, 404, 410) and "rate limit" not in str(e).lower():
                now_s = time.time()
                if len(self.skip) > 500:
                    self.skip = {k: v for k, v in self.skip.items() if v > now_s}
                self.skip[path] = now_s + quiet_for
                return None
            raise

    def _poll_repo(self, repo: str) -> dict:
        """One repository's runs, jobs, PRs, checks and issues. One repository GitHub fails on keeps its last answers
        (and says so) instead of blanking the rest."""
        try:
            return self._read_repo(repo)
        except GitHubError as e:
            with self._lock:
                old = {"runs": self.runs.get(repo, []), "prs": self.prs.get(repo, []), "issues": self.issues.get(repo, []),
                       "pushes": self.pushes.get(repo, []), "closed": self.closed.get(repo, []),
                       "merged": self.merged.get(repo, []), "releases": self.releases.get(repo, []),
                       "jobs": {k: v for k, v in self.jobs.items() if any(r["id"] == k for r in self.runs.get(repo, []))},
                       "checks": {k: v for k, v in self.checks.items() if k[0] == repo}}
            old["error"] = f"{repo}: {e}"
            return old

    def _read_repo(self, repo: str) -> dict:
        out = {"runs": [], "jobs": {}, "prs": [], "checks": {}, "issues": [], "pushes": [], "closed": [], "merged": [], "releases": [],
               "merged_all": []}
        runs = (self._try(f"repos/{repo}/actions/runs?per_page=20") or {}).get("workflow_runs") or []
        out["runs"] = runs
        for run in runs:
            before = self.jobs.get(run["id"])
            if run.get("status") in ACTIVE or before is None and run.get("status") != "completed" or \
                    before and any(j.get("status") in ACTIVE for j in before):
                out["jobs"][run["id"]] = (self._try(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=30") or {}).get("jobs") or []
            elif before is not None:
                out["jobs"][run["id"]] = before
        prs = self._try(f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=50") or []
        out["prs"] = prs
        for pr in prs:
            sha = (pr.get("head") or {}).get("sha")
            if not sha:
                continue
            kept = self.checks.get((repo, sha))
            if kept and all(c.get("status") == "completed" for c in kept):
                out["checks"][(repo, sha)] = kept    # finished checks on an unchanged commit do not change
                continue
            out["checks"][(repo, sha)] = (self._try(f"repos/{repo}/commits/{sha}/check-runs?per_page=50") or {}).get("check_runs") or []
            if not out["checks"][(repo, sha)]:
                # A commit pushed with a workflow's token starts no check run; the job posts its result as a commit status
                # instead (LifeShare's PR check on generated content). Without this such a PR looked unchecked, not failed.
                out["checks"][(repo, sha)] = self._statuses(repo, sha)
            for c in out["checks"][(repo, sha)]:
                # A failed check's annotations, once per check run (they never change): "No result" = needs a re-run.
                if c.get("conclusion") in FAILED and c.get("id") and c["id"] not in self.no_result \
                        and (c.get("output") or {}).get("annotations_count"):
                    notes = self._try(f"repos/{repo}/check-runs/{c['id']}/annotations?per_page=30") or []
                    self.no_result[c["id"]] = any(NO_RESULT.search(str(n.get("message") or "") + " " + str(n.get("title") or ""))
                                                  for n in notes if isinstance(n, dict))
        # Pushes to agent branches (claude/..., codex/...): what the Agents card shows before a PR exists. One call, ETag.
        for e in self._try(f"repos/{repo}/events?per_page=100") or []:
            if not isinstance(e, dict) or e.get("type") != "PushEvent":
                continue
            ref = str((e.get("payload") or {}).get("ref") or "")
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
            if _agent_of(branch, ""):
                out["pushes"].append({"branch": _text(branch, 120), "at": _text(e.get("created_at"), 30),
                                      "actor": _text((e.get("actor") or {}).get("login"), 60)})
        # Agent branches whose PR was merged or closed: their work is done, so they leave the Agents card.
        for p in self._try(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=50") or []:
            branch = str((p.get("head") or {}).get("ref") or "")
            if isinstance(p, dict) and p.get("closed_at") and _agent_of(branch, ""):
                out["closed"].append({"branch": _text(branch, 120), "at": _text(p.get("closed_at"), 30), "number": p.get("number")})
            if isinstance(p, dict) and p.get("merged_at"):
                out["merged_all"].append(p)
            if isinstance(p, dict) and p.get("merged_at") and _age(p["merged_at"]) < pipeline.MERGED_HOURS * 3600:
                out["merged"].append({k: p.get(k) for k in ("number", "title", "html_url", "merged_at", "base", "head", "labels")})
        # Releases: the last step of a change's road. One call, ETag; only asked while something merged recently.
        if out["merged"]:
            out["releases"] = [r for r in self._try(f"repos/{repo}/releases?per_page=5") or []
                               if isinstance(r, dict) and not r.get("draft")]
        issues = self._try(f"repos/{repo}/issues?state=open&sort=updated&direction=desc&per_page=50") or []
        out["issues"] = [i for i in issues if "pull_request" not in i]
        self._read_status_issue(repo)
        return out

    def _read_status_issue(self, repo: str) -> None:
        """Cloud agents' heartbeats: comments on the repository's status issue edited in the last day. `since` is
        rounded down to the hour, so the same question (and its ETag) holds for an hour and an unchanged answer is a
        free 304."""
        number = self.agent_issues.get(repo.lower())
        if not number:
            return
        since = now_iso_at(int((time.time() - presence.KEEP_HOURS * 3600) // 3600 * 3600))
        comments: list = []
        for page in range(1, presence.MAX_COMMENTS // 100 + 1):
            batch = self._try(f"repos/{repo}/issues/{number}/comments?since={since}&per_page=100&page={page}")
            if not isinstance(batch, list):
                break
            comments += batch
            if len(batch) < 100:
                break
        self.presence.relay(repo, comments)

    def _poll_runners(self, where: tuple):
        kind, name = where
        try:
            data = self._try(f"{kind}/{name}/actions/runners?per_page=100", quiet_for=RUNNER_REFUSED_RETRY)
        except GitHubError:
            return self.runners.get(name)     # keep the last answer through a hiccup
        if data is None:
            if kind == "orgs":
                self.runner_note = ("The GitHub sign-in on this PC cannot list the organisation's own runners (that needs "
                                    "`gh auth refresh -h github.com -s admin:org`). Runners added to a repository are listed; "
                                    "others show up through their jobs.")
            return None
        return data.get("runners") or []

    # ---- the snapshot ----
    def machine_of(self, runner_name: str, labels=()) -> str:
        """Runners are named <COMPUTERNAME>-<label> (InstallPrRunner.ps1): the machine is the name without the label."""
        n = str(runner_name or "")
        for lab in sorted({str(x) for x in labels or ()} | set(self.runner_labels), key=len, reverse=True):
            if lab and n.lower().endswith("-" + lab.lower()):
                return n[: -len(lab) - 1]
        return n

    def display(self, machine: str) -> str:
        return self.names.get(str(machine).lower(), str(machine))

    def snapshot(self) -> dict:
        at = time.time()
        blockers: list[dict] = []
        with self._lock:
            runs_by, jobs_by, prs_by, checks_by, issues_by, runners_by = (self.runs, self.jobs, self.prs, self.checks,
                                                                           self.issues, self.runners)

        def block(kind, title, detail="", url="", since="", key="", repo=""):
            """`key` stays the same while the same thing is stuck (titles carry minutes that change), so a phone is
            told once; `repo` lets the page filter by project."""
            blockers.append({"kind": kind, "key": _text(key or f"{kind}:{title}", 200), "title": _text(title, 200), "detail": _text(detail),
                             "url": _web(url), "since": since, "repo": _text(repo, 140)})

        # GitHub feed itself
        feed_age = _age(self.last_ok, at) if self.last_ok else _age(self.started, at)
        if self.error:
            block("feed", "GitHub can't be read right now", self.error, since=self.last_ok, key="feed")
        elif feed_age > max(180, self.interval * 4):
            block("feed", "The GitHub feed stopped updating", f"Last good read {int(feed_age // 60)} min ago.", since=self.last_ok, key="feed")

        # jobs per runner (from runs still going)
        repo_of_run = {}
        runs_out = []
        waiting = []     # runs queued past the limit: stuck only if a runner that could take them is idle, or none is online
        open_heads = {repo: {str((p.get("head") or {}).get("ref") or "") for p in ps} | {f"#{p.get('number')}" for p in ps}
                      for repo, ps in prs_by.items()}
        for repo, runs in runs_by.items():
            for r in runs:
                repo_of_run[r["id"]] = repo
                prn = ((r.get("pull_requests") or [{}])[0] or {}).get("number") or 0
                jobs = jobs_by.get(r["id"]) or []
                live = [j for j in jobs if j.get("status") in ACTIVE] or jobs[:1]
                job = live[0] if live else {}
                steps = job.get("steps") or []
                cur = next((s for s in steps if s.get("status") == "in_progress"), None)
                done = sum(1 for s in steps if s.get("status") == "completed")
                row = {"repo": repo, "id": r["id"], "name": _text(r.get("name"), 120), "title": _text(r.get("display_title"), 200),
                       "event": _text(r.get("event"), 30), "status": _text(r.get("status"), 20), "conclusion": _text(r.get("conclusion"), 20),
                       "branch": _text(r.get("head_branch"), 120), "pr": prn, "url": _web(r.get("html_url")),
                       "created": _text(r.get("created_at"), 30), "started": _text(r.get("run_started_at") or r.get("created_at"), 30),
                       "updated": _text(r.get("updated_at"), 30), "runner": _text(job.get("runner_name"), 80),
                       "job": _text(job.get("name"), 120), "step": _text((cur or {}).get("name"), 120),
                       "step_started": _text((cur or {}).get("started_at"), 30), "steps_done": done, "steps_total": len(steps),
                       "labels": [_text(x, 60) for x in job.get("labels") or [] if isinstance(x, str)][:8]}
                heads = open_heads.get(repo)
                row["pr_closed"] = bool(row["status"] in ACTIVE and row["event"].startswith("pull_request") and heads is not None
                                        and (f"#{prn}" not in heads if prn else row["branch"] and row["branch"] not in heads))
                runs_out.append(row)
                label = f"{row['name']}" + (f" · PR #{prn}" if prn else "")
                if row["pr_closed"]:
                    block("stuck", f"{label} is still {'running' if row['status'] == 'in_progress' else 'queued'}, but its pull "
                          "request is closed", f"{repo}{' on ' + row['runner'] if row['runner'] else ''}: nothing needs this run. "
                          "Cancel it to free the runner.", row["url"], row["started"], f"closed:{repo}:{row['id']}", repo)
                if row["status"] == "queued" and _age(row["created"], at) > LIMITS["queued_run"]:
                    waiting.append((row, label))
                if row["step"] and _age(row["step_started"], at) > LIMITS["step"]:
                    block("stuck", f"{label}: step \"{row['step']}\" has run {int(_age(row['step_started'], at) // 60)} min",
                          f"{repo} on {row['runner'] or 'a runner'}", row["url"], row["step_started"], f"step:{repo}:{row['id']}:{row['step']}", repo)
                elif row["status"] == "in_progress" and _age(row["started"], at) > LIMITS["job"]:
                    block("stuck", f"{label} has run {int(_age(row['started'], at) // 3600)} h", repo, row["url"], row["started"], f"long:{repo}:{row['id']}", repo)
        runs_out.sort(key=lambda x: (x["status"] not in ACTIVE, -_ts(x["updated"])))

        running_by_runner = {r["runner"].lower(): r for r in runs_out if r["status"] in ACTIVE and r["runner"]}
        runners_out = []
        seen = set()
        for owner, rs in runners_by.items():
            for x in rs:
                if x.get("id") in seen:
                    continue
                seen.add(x.get("id"))
                labels = [str(lab.get("name")) for lab in x.get("labels") or [] if lab.get("type") == "custom" or
                          str(lab.get("name")) not in ("self-hosted", "Windows", "Linux", "macOS", "X64", "ARM64")]
                job = running_by_runner.get(str(x.get("name") or "").lower())
                row = {"name": _text(x.get("name"), 80), "owner": _text(owner, 80), "labels": labels[:8],
                       "machine": self.display(self.machine_of(x.get("name"), labels)),
                       "online": x.get("status") == "online", "busy": bool(x.get("busy")),
                       "job": _runner_job(job) if job else None}
                runners_out.append(row)
                if not row["online"]:
                    block("runner", f"Runner {row['name']} is offline", f"{row['machine']} · {', '.join(labels)}", key=f"runner:{row['name']}")
        known = {r["name"].lower() for r in runners_out}
        seen_labels = {lab for r in runners_out for lab in r["labels"]}
        for name, job in running_by_runner.items():   # runners seen only through their jobs (no admin:org)
            if name not in known:
                labels = sorted(seen_labels | set(job.get("labels") or []))
                runners_out.append({"name": job["runner"], "owner": "", "labels": job.get("labels") or [],
                                    "machine": self.display(self.machine_of(job["runner"], labels)),
                                    "online": True, "busy": True, "job": _runner_job(job)})
        runners_out.sort(key=lambda r: (r["machine"].lower(), r["name"].lower()))

        for row, label in waiting:
            repo, mins = row["repo"], int(_age(row["created"], at) // 60)
            want = {x.lower() for x in row["labels"]} - GENERIC_LABELS
            fits = [r for r in runners_out if want and want <= {x.lower() for x in r["labels"]}]
            if not runners_out or not want:      # nothing to compare with: the old rule
                block("stuck", f"{label} has waited {mins} min for a runner", f"{repo}: no runner picked it up.",
                      row["url"], row["created"], f"queued:{repo}:{row['id']}", repo)
            elif not any(r["online"] for r in fits):
                block("stuck", f"{label} has waited {mins} min: no runner with its labels is online",
                      f"{repo} · needs {', '.join(sorted(want))}", row["url"], row["created"], f"queued:{repo}:{row['id']}", repo)
            elif any(r["online"] and not r["busy"] and not r["job"] for r in fits):
                block("stuck", f"{label} has waited {mins} min although a runner with its labels is idle",
                      f"{repo} · idle: {', '.join(r['name'] for r in fits if r['online'] and not r['busy'] and not r['job'])}",
                      row["url"], row["created"], f"queued:{repo}:{row['id']}", repo)
            # else every runner that could take it is busy: a queue, shown with the runs, not stuck

        machines_out = self._machines(at, runners_out, block)

        prs_out = []
        for repo, prs in prs_by.items():
            for p in prs:
                sha = (p.get("head") or {}).get("sha")
                cr = _latest_checks(checks_by.get((repo, sha)) or [])
                done_ = [c for c in cr if c.get("status") == "completed"]
                rerun = [c for c in done_ if c.get("conclusion") == "cancelled" or
                         (c.get("conclusion") in FAILED and self.no_result.get(c.get("id")))]
                failed = [c for c in done_ if c.get("conclusion") in FAILED and c not in rerun]
                going = [c for c in cr if c.get("status") != "completed"]
                state = "failed" if failed else "running" if any(c.get("status") == "in_progress" for c in going) else \
                        "queued" if going else "rerun" if rerun else "passed" if cr else "none"
                labels = [str(lab.get("name")) for lab in p.get("labels") or []][:10]
                row = {"repo": repo, "number": p.get("number"), "title": _text(p.get("title"), 200), "url": _web(p.get("html_url")),
                       "draft": bool(p.get("draft")), "auto_merge": bool(p.get("auto_merge")), "author": _text((p.get("user") or {}).get("login"), 60),
                       "branch": _text((p.get("head") or {}).get("ref"), 120), "base": _text((p.get("base") or {}).get("ref"), 80),
                       "updated": _text(p.get("updated_at"), 30), "created": _text(p.get("created_at"), 30), "labels": labels,
                       "checks": {"state": state, "total": len(cr), "done": len(cr) - len(going),
                                  "failed": [_text(c.get("name"), 80) for c in failed][:4],
                                  "running": [_text(c.get("name"), 80) for c in going][:4],
                                  "rerun": [{"name": _text(c.get("name"), 80), "job": c.get("id") if isinstance(c.get("id"), int) else 0,
                                             "why": "cancelled" if c.get("conclusion") == "cancelled" else "no result"} for c in rerun][:4],
                                  "url": _web((failed or rerun or going or cr or [{}])[0].get("html_url"))}}
                prs_out.append(row)
                if failed and not row["draft"]:
                    block("check", f"PR #{row['number']} check failed: {', '.join(row['checks']['failed'][:2])}",
                          f"{repo} · {row['title']}", row["checks"]["url"] or row["url"], max((c.get("completed_at") or "") for c in failed),
                          f"check:{repo}:{row['number']}:{sha}", repo)
                for c in going:
                    if c.get("status") == "in_progress" and _age(c.get("started_at"), at) > LIMITS["check"]:
                        block("stuck", f"PR #{row['number']} check \"{_text(c.get('name'), 60)}\" has run {int(_age(c.get('started_at'), at) // 60)} min",
                              repo, _web(c.get("html_url")) or row["url"], c.get("started_at") or "", f"checklong:{repo}:{c.get('id')}", repo)
                if row["auto_merge"] and state == "passed" and _age(row["updated"], at) > LIMITS["auto_merge"]:
                    block("stuck", f"PR #{row['number']} is set to merge automatically but has waited "
                          f"{int(_age(row['updated'], at) // 3600)} h", f"{repo} · checks pass; a required review, a conflict or "
                          "the merge queue may be holding it", row["url"], row["updated"], f"automerge:{repo}:{row['number']}", repo)
                if any(BLOCKED_LABEL.match(lab) for lab in labels):
                    block("label", f"PR #{row['number']} is marked blocked", f"{repo} · {', '.join(labels[:3])}", row["url"], row["updated"],
                          f"label:{repo}:{row['number']}", repo)
        order = {"failed": 0, "rerun": 1, "running": 2, "queued": 3, "none": 4, "passed": 5}
        prs_out.sort(key=lambda r: (r["draft"], order.get(r["checks"]["state"], 5), -_ts(r["updated"])))

        issues_out = []
        parked: list[dict] = []     # issues marked blocked on other work: shown apart, not counted as stuck
        for repo, issues in issues_by.items():
            for i in issues:
                labels = [str(lab.get("name")) for lab in i.get("labels") or []][:10]
                row = {"repo": repo, "number": i.get("number"), "title": _text(i.get("title"), 200), "url": _web(i.get("html_url")),
                       "labels": labels, "created": _text(i.get("created_at"), 30), "updated": _text(i.get("updated_at"), 30),
                       "blocked": any(BLOCKED_LABEL.match(lab) for lab in labels)}
                issues_out.append(row)
                if row["blocked"] and any(NEEDS_PERSON_LABEL.match(lab) for lab in labels):
                    block("label", f"#{row['number']} needs you: {row['title']}", f"{repo} · {', '.join(labels[:3])}", row["url"], row["updated"],
                          f"label:{repo}:{row['number']}", repo)
                elif row["blocked"]:
                    # Parked on purpose until other work lands (a triage label): listed, but nothing is stuck.
                    parked.append({"kind": "parked", "key": f"parked:{repo}:{row['number']}", "title": f"#{row['number']} {row['title']}",
                                    "detail": f"{repo} · {', '.join(labels[:3])}", "url": row["url"], "since": row["updated"], "repo": _text(repo, 140)})
        issues_out.sort(key=lambda r: -_ts(r["updated"]))

        if self.extra is not None:
            try:
                for b in self.extra() or []:
                    block(b.get("kind") or "stuck", b.get("title") or "", b.get("detail") or "", b.get("url") or "",
                          b.get("since") or "", b.get("key") or "")
            except Exception as e:   # never lose the whole feed over it
                block("feed", "Fletchery could not check its own machines", f"{type(e).__name__}: {e}"[:200], key="extra")
        rank = {"feed": 0, "machine": 1, "stuck": 2, "check": 3, "runner": 4, "label": 5}
        blockers.sort(key=lambda b: (rank.get(b["kind"], 9), -_ts(b["since"])))
        agents = self._agents(at, prs_out, runs_out, issues_out)
        changes = self._changes(at, prs_by, checks_by, runs_by, jobs_by)
        self._link_activities(machines_out, changes)
        active_runs = [r for r in runs_out if r["status"] in ACTIVE]
        # Everything building, checking, testing, merging or importing right now, on any machine, in one list.
        building = [{**a, "machine": m["name"]} for m in machines_out for a in m["activities"] if a["kind"] in BUILD_KINDS]
        building.sort(key=lambda a: _ts(a.get("since")) or 0)
        return {
            "at": now_iso(), "fresh": {"github": self.last_ok, "tried": self.last_try, "every": round(self.interval),
                                       "error": _text(self.error), "remaining": self.gh.remaining,
                                       "free": self.gh.free, "calls": self.gh.calls, "runner_note": _text(self.runner_note, 400)},
            "repos": [{"name": r["full_name"], "url": r["url"]} for r in self.repos],
            "counts": {"machines": len(machines_out), "busy": sum(1 for m in machines_out if m["busy"]),
                       "runs": len(active_runs), "queued": sum(1 for r in active_runs if r["status"] != "in_progress"),
                       "prs": len(prs_out), "issues": len(issues_out), "blockers": len(blockers), "parked": len(parked), "building": len(building),
                       "agents": sum(1 for a in agents if a["active"]), "agents_idle": sum(1 for a in agents if not a["active"]),
                       "changes": len(changes), "changes_moving": sum(1 for c in changes if c["status"] in ("running", "queued"))},
            "flow": self.flow_now(prs_out, at),
            "agents": [a for a in agents if a["active"]][:40],     # working now; the rest is only counted
            "changes": changes[:60],
            "building": building[:30],
            "blockers": blockers[:40], "parked": parked[:40], "machines": machines_out, "runners": runners_out,
            "runs": runs_out[:40], "prs": prs_out[:80], "issues": issues_out[:60],
        }

    def _record(self, got: dict) -> None:
        """Keep what this poll saw (store.py): merged PRs, finished runs, started and finished jobs."""
        for repo, g in got.items():
            if g.get("error"):
                continue
            self.store.merges(repo, g.get("merged_all") or [], _agent_of)
            self.store.runs(repo, g.get("runs") or [])
            for run in g.get("runs") or []:
                self.store.jobs(repo, run, (g.get("jobs") or {}).get(run.get("id")) or [])
        self.store.prune()
        self._flow_cache = (0.0, {})

    def flow_now(self, prs_out: list, at: float) -> dict:
        """The Flow strip (flow.py), worked out at most every 15 s."""
        when, kept = self._flow_cache
        if kept and at - when < 15:
            return kept
        waiting: dict = {}
        for p in prs_out:
            if not p["draft"] and p["checks"]["state"] not in ("failed",):
                waiting[p["repo"]] = waiting.get(p["repo"], 0) + 1
        try:
            kept = self.flow.compute([r["full_name"] for r in self.repos], waiting, at)
        except Exception as e:
            self.log(f"live: flow: {type(e).__name__}: {e}")
            kept = {}
        self._flow_cache = (at, kept)
        return kept

    def _runner_machine(self, runner_name: str) -> str:
        return self.display(self.machine_of(runner_name)) if runner_name else ""

    def _learn(self) -> None:
        """Add what finished since the last poll to the step times (pipeline.History), then save them."""
        with self._lock:
            prs_by, checks_by, runs_by, jobs_by, merged_by = self.prs, self.checks, self.runs, self.jobs, self.merged
        jobs = {j.get("id"): j for js in jobs_by.values() for j in js if isinstance(j, dict)}
        for repo, prs in prs_by.items():
            for p in prs:
                sha = (p.get("head") or {}).get("sha")
                cr = _latest_checks(checks_by.get((repo, sha)) or [])
                if not cr:
                    continue
                kind = self.rules.kind(p.get("title"), [x.get("name") for x in p.get("labels") or [] if isinstance(x, dict)],
                                       (p.get("head") or {}).get("ref"))
                pipeline.learn_checks(self.history, repo, kind, sha, [c for c in cr if self.rules.is_quick(c.get("name"))],
                                      [c for c in cr if not self.rules.is_quick(c.get("name"))], jobs, self._runner_machine)
            bases = {str((p.get("base") or {}).get("ref") or "") for p in list(prs) + list(merged_by.get(repo, []))} - {""}
            for r in runs_by.get(repo, []):
                if r.get("event") == "push" and r.get("head_branch") in bases:
                    pipeline.learn_staging(self.history, repo, r, jobs_by.get(r.get("id")) or [])
        self.history.save()

    def _changes(self, at: float, prs_by: dict, checks_by: dict, runs_by: dict, jobs_by: dict) -> list:
        """Every open PR, and every PR merged in the last day until its staging build and release, with its step."""
        with self._lock:
            merged_by, releases_by = self.merged, self.releases
        jobs = {j.get("id"): j for js in jobs_by.values() for j in js if isinstance(j, dict)}
        out = []
        for repo, prs in prs_by.items():
            for p in prs:
                cr = _latest_checks(checks_by.get((repo, (p.get("head") or {}).get("sha"))) or [])
                out.append(pipeline.open_change(self.history, self.rules, repo, p, cr, jobs, self._runner_machine, at))
        for repo, ms in merged_by.items():
            for p in ms:
                c = pipeline.merged_change(self.history, self.rules, repo, p, runs_by.get(repo, []), jobs_by,
                                           releases_by.get(repo, []), self._runner_machine, at)
                if c:
                    out.append(c)
        order = {"failed": 0, "rerun": 1, "running": 2, "queued": 3, "waiting": 4, "done": 5}
        out.sort(key=lambda c: (order.get(c["status"], 4), c["stage"] == "written", -_ts(c["updated"])))
        return out

    @staticmethod
    def _link_activities(machines: list, changes: list) -> None:
        """Tie what a machine does to the change it is for: its runner's PR or staging run, or "#123" in a lease's name."""
        by_key = {c["key"]: c for c in changes}
        by_run = {c["run"]: c for c in changes if c.get("run")}
        by_num: dict = {}
        for c in changes:
            by_num.setdefault(c["number"], []).append(c)
        for m in machines:
            for a in m["activities"]:
                c = None
                if a.get("repo") and a.get("pr"):
                    c = by_key.get(f"{a['repo']}#{a['pr']}")
                if c is None and a.get("run"):
                    c = by_run.get(a["run"])
                if c is None and a.get("kind") != "queued":
                    found = re.search(r"#(\d{1,9})\b", a.get("title") or "")
                    hits = by_num.get(int(found.group(1)), []) if found else []
                    c = hits[0] if len(hits) == 1 else None
                if c is not None:
                    a["change"] = c["key"]

    def _agents(self, at: float, prs: list, runs: list, issues: list = ()) -> list:
        """The coding agents (Claude, Codex), one row each, newest first; `active` marks those working now.

        1. What agents say (presence.py): one row per session whose last heartbeat is not `done`. Its work is linked
           to the PR or issue when the repository is watched.
        2. What GitHub shows, for agents that send no heartbeat: an agent branch (claude/..., codex/... or a codex
           author) with a push, or its PR updated, recently. Marked `source: github`, a guess. A PR or branch a
           heartbeat covers is left to the heartbeat; after it only a newer push brings the guess back.
        Gone: done, quiet past the window, or its PR merged or closed after the last sign of work."""
        with self._lock:
            pushes = {repo: list(ps) for repo, ps in self.pushes.items()}
            closed_at = {}
            for repo, cs in self.closed.items():
                for c in cs:
                    for k in ((repo.lower(), c["branch"]), (repo.lower(), f"#{c.get('number')}")):
                        if _ts(c["at"]) > _ts(closed_at.get(k, "")):
                            closed_at[k] = c["at"]
        watched = {r["full_name"].lower(): r["full_name"] for r in self.repos}
        pr_by = {}
        for p in prs:
            pr_by[(p["repo"].lower(), f"#{p['number']}")] = p
            pr_by.setdefault((p["repo"].lower(), p["branch"]), p)
        issue_by = {(i["repo"].lower(), f"#{i['number']}"): i for i in issues}
        out, covered = [], {}

        for r in self.presence.current():
            m = presence.WORK.match(r["work"]) if r["work"] else None
            repo = f"{m.group(1)}/{m.group(2)}" if m and m.group(1) else r["repo"]
            num = f"#{m.group(3)}" if m else ""
            key = repo.lower()
            for k in ((key, num), (key, r["branch"])):
                if key and k[1]:
                    covered[k] = max(covered.get(k, 0.0), r["at"])
            if r["state"] == "done":
                continue
            if key and max(_ts(closed_at.get((key, num), "")) if num else 0, _ts(closed_at.get((key, r["branch"]), "")) if r["branch"] else 0) >= r["at"]:
                continue      # its PR was merged or closed after the last heartbeat
            pr = pr_by.get((key, num)) if num else None
            pr = pr or (pr_by.get((key, r["branch"])) if r["branch"] else None)
            issue = issue_by.get((key, num)) if num and not pr else None
            linked = key in watched
            title = (pr or issue or {}).get("title") or (f"{repo}{num}" if repo and num else r["work"] or r["branch"] or "No issue or PR named")
            url = ""      # linked only inside the repositories Live watches
            if linked:
                url = (pr or issue or {}).get("url") or (f"https://github.com/{watched[key]}/issues/{num[1:]}" if num else "")
            waiting = r["state"] == "waiting"
            out.append({"agent": presence.AGENTS[r["agent"]], "source": "heartbeat", "repo": watched.get(key, repo),
                        "number": int(num[1:]) if num else 0, "title": _text(title, 200), "url": _web(url),
                        "branch": _text(r["branch"], 120), "state": "Waiting" if waiting else "Working",
                        "tone": "wait" if waiting else "run", "step": _text(r["step"], presence.STEP_MAX),
                        "where": self.display(r["machine"]) if r["machine"] else "cloud",
                        "checks": (pr or {}).get("checks", {}).get("state", ""), "last": now_iso_at(r["at"]),
                        "active": at - r["at"] < self.agent_active})

        last_push = {}
        for repo, ps in pushes.items():
            for p in ps:
                k = (repo, p["branch"])
                if _ts(p["at"]) > _ts(last_push.get(k, "")):
                    last_push[k] = p["at"]
        building = {(r["repo"], r["branch"]) for r in runs if r["status"] in ACTIVE}

        def guess(last, pushed, cover):
            """When the GitHub guess last saw work, or "" when a heartbeat already tells more."""
            if not cover:
                return last
            return pushed if pushed and _ts(pushed) > cover + 60 else ""

        seen = set()
        for p in prs:
            who = _agent_of(p["branch"], p["author"])
            if not who:
                continue
            k = (p["repo"], p["branch"])
            seen.add(k)
            pushed = last_push.get(k, "")
            cover = max(covered.get((p["repo"].lower(), f"#{p['number']}"), 0.0), covered.get((p["repo"].lower(), p["branch"]), 0.0))
            last = guess(max((p["updated"], pushed), key=lambda x: _ts(x) or 0), pushed, cover)
            if not last or _age(last, at) > self.agent_hours * 3600:
                continue
            c = p["checks"]["state"]
            if p.get("blocked"):
                state = ("needs", "Blocked")
            elif c == "failed":
                state = ("bad", "Checks failing: " + ", ".join(p["checks"]["failed"][:2]))
            elif c == "rerun":
                state = ("wait", "Needs re-run")
            elif c in ("running", "queued") or k in building:
                state = ("wait", "Waiting on checks")
            elif pushed and _age(pushed, at) < CODING_FRESH:
                state = ("run", "Coding")
            elif p["draft"]:
                state = ("", "Draft")
            elif c == "passed":
                state = ("run", "Merging when green" if p["auto_merge"] else "Checks pass, ready to merge")
            else:
                state = ("", "No checks yet")
            out.append({"agent": who, "source": "github", "repo": p["repo"], "number": p["number"], "title": p["title"],
                        "url": p["url"], "branch": p["branch"], "state": state[1], "tone": state[0], "step": "", "where": "",
                        "checks": c, "last": last, "active": _age(last, at) < self.agent_active})
        for (repo, branch), pushed in last_push.items():
            if (repo, branch) in seen or _age(pushed, at) > self.agent_hours * 3600:
                continue
            if _ts(closed_at.get((repo.lower(), branch), "")) >= _ts(pushed):   # its PR was merged or closed after the push
                continue
            if not guess(pushed, pushed, covered.get((repo.lower(), branch), 0.0)):
                continue
            fresh = _age(pushed, at) < CODING_FRESH
            out.append({"agent": _agent_of(branch, ""), "source": "github", "repo": repo, "number": 0, "title": branch,
                        "url": _web(f"https://github.com/{repo}/tree/{branch}"), "branch": branch,
                        "state": "Coding, no PR yet" if fresh else "Pushed, no PR yet", "tone": "run" if fresh else "",
                        "step": "", "where": "", "checks": "", "last": pushed, "active": _age(pushed, at) < self.agent_active})
        out.sort(key=lambda x: (not x["active"], x["source"] != "heartbeat", -_ts(x["last"])))
        return out

    def _machines(self, at: float, runners: list, block) -> list:
        with self._lock:
            reports = dict(self.reports)
        by = {}
        for name, rep in reports.items():
            d = rep["data"] or {}
            host = _text(d.get("host") or name, 60)
            age = _age(rep["at"], at)
            if age > 7 * 86400:
                continue
            stats = d.get("stats") if isinstance(d.get("stats"), dict) else {}
            acts = []
            lease = d.get("lease") if isinstance(d.get("lease"), dict) else None
            if lease and lease.get("purpose"):
                purpose = _scrub(lease.get("purpose"), 200)
                acts.append({"kind": kind_of(purpose), "title": purpose, "since": _text(lease.get("since"), 30),
                             "detail": "holds the machine" + ("" if lease.get("alive", True) else " (its process is gone)")})
                held = _age(lease.get("since"), at)
                if lease.get("alive") is False:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" holds the machine but its process is gone",
                          "Nothing else can start there until the stale hold is cleared.", since=lease.get("since") or "", key=f"deadhold:{host}:{lease.get('since')}")
                elif held > LIMITS["lease"]:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" has held the machine {int(held // 3600)} h", "", since=lease.get("since") or "", key=f"longhold:{host}:{lease.get('since')}")
                idle = float(d.get("idle_seconds") or 0)
                if idle > LIMITS["idle_lease"]:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" holds the machine but the CPU has been idle {int(idle // 60)} min",
                          "It may be stuck waiting on something.", since=lease.get("since") or "", key=f"idlehold:{host}:{lease.get('since')}")
            for q in (d.get("queue") or [])[:10]:
                if not isinstance(q, dict):
                    continue
                purpose = _scrub(q.get("purpose"), 200)
                acts.append({"kind": "queued", "title": purpose, "since": _text(q.get("since"), 30), "detail": "waiting for the machine"})
                if _age(q.get("since"), at) > LIMITS["queue_wait"]:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" has waited {int(_age(q.get('since'), at) // 60)} min in the queue", since=q.get("since") or "", key=f"queue:{host}:{q.get('since')}:{purpose}")
            for a in (d.get("activities") or [])[:10]:
                if isinstance(a, dict) and a.get("title"):
                    acts.append({"kind": _text(a.get("kind") or kind_of(a.get("title")), 20), "title": _scrub(a.get("title"), 200),
                                 "since": _text(a.get("since"), 30), "detail": _scrub(a.get("detail"), 200)})
            procs = d.get("procs") if isinstance(d.get("procs"), dict) else {}
            guard = _disk_guard(d.get("disk_guard"))
            if guard:
                drive, who = guard["drive"] or "The build drive", self.display(host)
                if guard["state"] == "full" or guard["runner_stopped"]:
                    block("stuck", f"{who}: PR runner offline: disk full",
                          f"{drive} has {guard['free_gb']:.0f} GB free." + (f" Stopped: {', '.join(guard['runner_stopped'])}." if guard["runner_stopped"] else "")
                          + " The disk guard starts it again once it has cleared enough space.",
                          since=guard["updated"], key=f"diskfull:{host}")
                elif guard["low"]:
                    block("disk", f"{who}: {drive} has {guard['free_gb']:.0f} GB free",
                          f"Under {guard['warn_gb']:.0f} GB. The disk guard clears old build output; the biggest folders are on the Machines card.",
                          since=guard["updated"], key=f"disklow:{host}")
            for x in (stats.get("disks") or [])[:4]:
                if guard and isinstance(x, dict) and _text(x.get("name"), 8).rstrip(":").upper() == (guard["drive"] or "").rstrip(":\\/").upper():
                    continue   # the disk guard's drive is flagged from its own report, above
                if isinstance(x, dict) and isinstance(x.get("free_gb"), (int, float)) and x["free_gb"] < self.disk_low:
                    block("stuck", f"{self.display(host)}: {_text(x.get('name'), 8)} has {x['free_gb']:.0f} GB free",
                          f"Below {self.disk_low:.0f} GB: builds and checks there may fail. Clear old worktrees or build output.",
                          key=f"disk:{host}:{_text(x.get('name'), 8)}")
            silent = age > REPORT_STALE
            if silent:   # stays listed (and flagged) until it reports again or a week has passed
                block("machine", f"{self.display(host)} stopped reporting",
                      f"Last report {_ago_text(age)} ago. Its reporter task may have stopped, or the machine is off.",
                      since=rep["at"], key=f"machine:{host}")
            by[host.lower()] = {"name": self.display(host), "host": host, "online": not silent, "last_report": rep["at"],
                                "busy": bool(acts and any(a["kind"] not in ("queued",) for a in acts)) or bool(procs.get("runner")),
                                "stats": {k: stats.get(k) for k in ("cpu", "ram_used_gb", "ram_total_gb") if isinstance(stats.get(k), (int, float))},
                                "disks": [{"name": _text(x.get("name"), 8), "free_gb": x.get("free_gb"),
                                           **({"total_gb": x["total_gb"]} if isinstance(x.get("total_gb"), (int, float)) else {})}
                                          for x in (stats.get("disks") or [])[:4]
                                          if isinstance(x, dict) and isinstance(x.get("free_gb"), (int, float))],
                                "disk_guard": guard,
                                "procs": {k: int(v) for k, v in procs.items() if isinstance(v, (int, float)) and v}, "activities": acts,
                                "runners": [], "version": _text(d.get("version"), 20)}
        for r in runners:
            key = r["machine"].lower()
            # A runner belongs to the reporting machine whose name it starts with (<COMPUTERNAME>-<label>), even when
            # GitHub did not list the label that would have told us where the machine name ends.
            m = next((v for k, v in by.items() if k == key or v["name"].lower() == key), None) or next(
                (v for k, v in sorted(by.items(), key=lambda kv: -len(kv[0])) if r["name"].lower().startswith(k + "-")), None)
            if m is not None:
                r["machine"] = m["name"]
            if m is None:
                m = by.setdefault(key, {"name": r["machine"], "host": r["machine"], "online": r["online"], "last_report": "",
                                        "busy": False, "stats": {}, "disks": [], "disk_guard": None, "procs": {}, "activities": [], "runners": [], "version": ""})
            m["runners"].append(r["name"])
            if r["job"]:
                m["busy"] = True
                if not any(r["job"]["title"].split(" · ")[0].lower() in a["title"].lower() for a in m["activities"]):
                    m["activities"].insert(0, {"kind": "check" if "check" in r["job"]["title"].lower() else "working",
                                               "title": r["job"]["title"], "since": r["job"]["started"],
                                               "detail": (f"step: {r['job']['step']} · " if r["job"]["step"] else "") + r["job"]["repo"],
                                               "url": r["job"]["url"], "repo": r["job"]["repo"], "pr": r["job"]["pr"],
                                               "run": r["job"]["id"]})
        if self.own_builds is not None:
            try:
                own = list(self.own_builds() or [])
            except Exception:
                own = []
            for j in own:
                key = str(j.get("machine") or "").lower()
                if not key:
                    continue
                m = next((v for k, v in by.items() if k == key or v["name"].lower() == key), None)
                if m is None:
                    m = by.setdefault(key, {"name": self.display(j["machine"]), "host": j["machine"], "online": True, "last_report": "",
                                            "busy": False, "stats": {}, "disks": [], "disk_guard": None, "procs": {}, "activities": [], "runners": [], "version": ""})
                m["activities"].insert(0, {"kind": _text(j.get("kind") or "building", 20), "title": _text(j.get("title"), 200),
                                           "since": _text(j.get("since"), 30), "detail": _text(j.get("detail"), 200), "fletchery": True})
                m["busy"] = m["busy"] or j.get("kind") != "queued"
        return sorted(by.values(), key=lambda m: m["name"].lower())
