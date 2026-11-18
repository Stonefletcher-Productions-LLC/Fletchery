"""Live: everything happening in development, across every repository and machine, for the phone portal's Live tab.

Two sources, merged into one snapshot (`DevWatch.snapshot()`), which portal.py serves at /api/live:

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

Nothing goes stale quietly: every item carries its age, and `blockers` names what looks stuck (a feed that stopped, a
machine that stopped reporting, a lease whose process is gone, a lease held with the CPU idle, a step or queue that ran
far too long, an offline runner, a failed check, an issue labelled blocked).

Profile, under `workers.portal` (all optional):

    "live": {"owners": ["My-Org", "me"], "exclude": ["me/old-repo"], "activeDays": 21,
             "fastSeconds": 15, "slowSeconds": 90, "names": {"ALEX-GAMING-PC": "Gaming PC"}}

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

API = "https://api.github.com"
ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
BLOCKED_LABEL = re.compile(r"block|needs[\s_:-]*(alex|you|human|person|owner|decision)|waiting[\s_:-]*on", re.I)
REPORT_STALE = 60          # a machine that has not reported for this long is shown as silent
REPORT_GONE = 15 * 60      # ... and as offline (no blocker) after this long
LIMITS = {                 # seconds before something counts as stuck
    "queued_run": 20 * 60,      # an Actions run nobody picked up
    "step": 60 * 60,            # one job step
    "job": 3 * 3600,            # one job
    "lease": 4 * 3600,          # one hold on a machine
    "idle_lease": 10 * 60,      # a hold while the machine's CPU stays idle
    "queue_wait": 90 * 60,      # a run waiting in a machine's queue
    "check": 90 * 60,           # one PR check run
}


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
        self._lock = threading.Lock()

    def token(self) -> str:
        if not self._token:
            self._token = self.token_fn() or ""
        return self._token

    def get(self, path: str):
        """The JSON at `path` (a list or dict), or raises GitHubError."""
        if time.time() < self.wait_until:
            raise GitHubError(0, "GitHub asked to wait")
        url = path if path.startswith("https://") else API + "/" + path.lstrip("/")
        with self._lock:
            etag, kept = self.cache.get(url, ("", None))
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Fletchery-Live",
            **({"Authorization": "Bearer " + self.token()} if self.token() else {}),
            **({"If-None-Match": etag} if etag else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self._limits(r.headers)
                data = json.loads(r.read() or b"null")
                with self._lock:
                    self.calls += 1
                    if r.headers.get("ETag"):
                        self.cache[url] = (r.headers["ETag"], data)
                return data
        except urllib.error.HTTPError as e:
            self._limits(e.headers)
            if e.code == 304 and kept is not None:
                with self._lock:
                    self.free += 1
                return kept
            with self._lock:
                self.calls += 1
            if e.code == 401:
                self._token = ""        # read the token again next time (gh may have refreshed it)
            if e.code in (403, 429) and (e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Remaining") == "0"):
                wait = float(e.headers.get("Retry-After") or 0) or max(60.0, float(e.headers.get("X-RateLimit-Reset") or 0) - time.time())
                self.wait_until = time.time() + min(wait, 3600)
            try:
                msg = json.loads(e.read() or b"{}").get("message", "")
            except (ValueError, AttributeError):
                msg = ""
            raise GitHubError(e.code, msg or e.reason)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(0, f"GitHub could not be reached ({type(e).__name__})")

    def _limits(self, headers) -> None:
        if headers is None:
            return
        try:
            if headers.get("X-RateLimit-Remaining") is not None:
                self.remaining = int(headers["X-RateLimit-Remaining"])
                self.reset_at = float(headers.get("X-RateLimit-Reset") or 0)
        except ValueError:
            pass


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
class DevWatch:
    def __init__(self, conf: dict | None = None, log=None, token_fn=gh_token, github: GitHub | None = None):
        conf = conf or {}
        self.log = log or (lambda line: None)
        self.owners = {str(o).lower() for o in conf.get("owners") or []}
        self.exclude = {str(r).lower() for r in conf.get("exclude") or []}
        self.active_days = float(conf.get("activeDays") or 21)
        self.fast = max(10.0, float(conf.get("fastSeconds") or 15))
        self.slow = max(self.fast, float(conf.get("slowSeconds") or 90))
        self.names = {str(k).lower(): _text(v, 60) for k, v in (conf.get("names") or {}).items()}
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
        self.issues: dict[str, list] = {}           # repo -> open issues
        self.reports: dict[str, dict] = {}          # machine -> its last report
        self.skip: dict[str, float] = {}            # path -> don't ask again before
        self.last_ok = ""
        self.last_try = ""
        self.error = ""
        self.interval = self.fast
        self.started = now_iso()

    # ---- machines ----
    def report(self, machine: str, data: dict, ip: str = "") -> dict:
        """A machine's report (machines.py /v1/report). Kept whole but bounded; cleaned when shown."""
        if not isinstance(data, dict):
            raise ValueError("report must be an object")
        body = json.dumps(data)
        if len(body) > 64 * 1024:
            raise ValueError("report too large")
        with self._lock:
            self.reports[str(machine)] = {"data": data, "at": now_iso(), "ip": ip}
        if data.get("busy"):
            self._wake.set()      # a machine just picked something up: read GitHub soon
        return {"ok": True, "every": 10}

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
            places = [("orgs", o) for o in orgs] + [("repos", r["full_name"]) for r in self.repos if not r.get("org")]
            found = dict(zip([p[1] for p in places], pool.map(self._poll_runners, places)))
        runs, jobs, prs, checks, issues = {}, {}, {}, {}, {}
        errors = [g["error"] for g in got.values() if g.get("error")]
        for repo, g in got.items():
            runs[repo], prs[repo], issues[repo] = g["runs"], g["prs"], g["issues"]
            jobs.update(g["jobs"])
            checks.update(g["checks"])
        with self._lock:   # swapped whole, so a snapshot never sees half a poll
            self.runs, self.jobs, self.prs, self.checks, self.issues = runs, jobs, prs, checks, issues
            self.runners = {k: v for k, v in found.items() if v is not None}
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

    def _try(self, path: str, quiet_for: float = 1800):
        """GET that remembers a 403/404 (no access, Actions off) and stops asking for a while."""
        if self.skip.get(path, 0) > time.time():
            return None
        try:
            return self.gh.get(path)
        except GitHubError as e:
            if e.code in (403, 404, 410) and "rate limit" not in str(e).lower():
                self.skip[path] = time.time() + quiet_for
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
                       "jobs": {k: v for k, v in self.jobs.items() if any(r["id"] == k for r in self.runs.get(repo, []))},
                       "checks": {k: v for k, v in self.checks.items() if k[0] == repo}}
            old["error"] = f"{repo}: {e}"
            return old

    def _read_repo(self, repo: str) -> dict:
        out = {"runs": [], "jobs": {}, "prs": [], "checks": {}, "issues": []}
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
        issues = self._try(f"repos/{repo}/issues?state=open&sort=updated&direction=desc&per_page=50") or []
        out["issues"] = [i for i in issues if "pull_request" not in i]
        return out

    def _poll_runners(self, where: tuple):
        kind, name = where
        try:
            data = self._try(f"{kind}/{name}/actions/runners?per_page=100", quiet_for=3600)
        except GitHubError:
            return self.runners.get(name)     # keep the last answer through a hiccup
        if data is None:
            if kind == "orgs":
                self.runner_note = ("The GitHub sign-in on this PC cannot list the organisation's runners (it needs the "
                                    "admin:org scope: run `gh auth refresh -h github.com -s admin:org`). Runners are shown "
                                    "from their jobs and the machines' own reports until then.")
            return None
        return data.get("runners") or []

    # ---- the snapshot ----
    def machine_of(self, runner_name: str, labels=()) -> str:
        """Runners are named <COMPUTERNAME>-<label> (InstallPrRunner.ps1): the machine is the name without the label."""
        n = str(runner_name or "")
        for lab in sorted((str(x) for x in labels or ()), key=len, reverse=True):
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

        def block(kind, title, detail="", url="", since=""):
            blockers.append({"kind": kind, "title": _text(title, 200), "detail": _text(detail), "url": _web(url), "since": since})

        # GitHub feed itself
        feed_age = _age(self.last_ok, at) if self.last_ok else _age(self.started, at)
        if self.error:
            block("feed", "GitHub can't be read right now", self.error, since=self.last_ok)
        elif feed_age > max(180, self.interval * 4):
            block("feed", "The GitHub feed stopped updating", f"Last good read {int(feed_age // 60)} min ago.", since=self.last_ok)

        # jobs per runner (from runs still going)
        repo_of_run = {}
        runs_out = []
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
                       "step_started": _text((cur or {}).get("started_at"), 30), "steps_done": done, "steps_total": len(steps)}
                runs_out.append(row)
                label = f"{row['name']}" + (f" · PR #{prn}" if prn else "")
                if row["status"] == "queued" and _age(row["created"], at) > LIMITS["queued_run"]:
                    block("stuck", f"{label} has waited {int(_age(row['created'], at) // 60)} min for a runner",
                          f"{repo}: no free runner with its labels picked it up.", row["url"], row["created"])
                if row["step"] and _age(row["step_started"], at) > LIMITS["step"]:
                    block("stuck", f"{label}: step \"{row['step']}\" has run {int(_age(row['step_started'], at) // 60)} min",
                          f"{repo} on {row['runner'] or 'a runner'}", row["url"], row["step_started"])
                elif row["status"] == "in_progress" and _age(row["started"], at) > LIMITS["job"]:
                    block("stuck", f"{label} has run {int(_age(row['started'], at) // 3600)} h", repo, row["url"], row["started"])
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
                       "job": ({"title": job["name"] + (f" · PR #{job['pr']}" if job["pr"] else ""), "repo": job["repo"],
                                "step": job["step"], "url": job["url"], "started": job["started"]} if job else None)}
                runners_out.append(row)
                if not row["online"]:
                    block("runner", f"Runner {row['name']} is offline", f"{row['machine']} · {', '.join(labels)}")
        known = {r["name"].lower() for r in runners_out}
        for name, job in running_by_runner.items():   # runners seen only through their jobs (no admin:org)
            if name not in known:
                runners_out.append({"name": job["runner"], "owner": "", "labels": [], "machine": self.display(self.machine_of(job["runner"])),
                                    "online": True, "busy": True, "job": {"title": job["name"], "repo": job["repo"], "step": job["step"],
                                                                          "url": job["url"], "started": job["started"]}})
        runners_out.sort(key=lambda r: (r["machine"].lower(), r["name"].lower()))

        machines_out = self._machines(at, runners_out, block)

        prs_out = []
        for repo, prs in prs_by.items():
            for p in prs:
                sha = (p.get("head") or {}).get("sha")
                cr = checks_by.get((repo, sha)) or []
                failed = [c for c in cr if c.get("status") == "completed" and c.get("conclusion") in ("failure", "timed_out", "action_required", "startup_failure")]
                going = [c for c in cr if c.get("status") != "completed"]
                state = "failed" if failed else "running" if any(c.get("status") == "in_progress" for c in going) else \
                        "queued" if going else "passed" if cr else "none"
                labels = [str(lab.get("name")) for lab in p.get("labels") or []][:10]
                row = {"repo": repo, "number": p.get("number"), "title": _text(p.get("title"), 200), "url": _web(p.get("html_url")),
                       "draft": bool(p.get("draft")), "author": _text((p.get("user") or {}).get("login"), 60),
                       "branch": _text((p.get("head") or {}).get("ref"), 120), "base": _text((p.get("base") or {}).get("ref"), 80),
                       "updated": _text(p.get("updated_at"), 30), "created": _text(p.get("created_at"), 30), "labels": labels,
                       "checks": {"state": state, "total": len(cr), "done": len(cr) - len(going),
                                  "failed": [_text(c.get("name"), 80) for c in failed][:4],
                                  "running": [_text(c.get("name"), 80) for c in going][:4],
                                  "url": _web((failed or going or cr or [{}])[0].get("html_url"))}}
                prs_out.append(row)
                if failed and not row["draft"]:
                    block("check", f"PR #{row['number']} check failed: {', '.join(row['checks']['failed'][:2])}",
                          f"{repo} · {row['title']}", row["checks"]["url"] or row["url"], max((c.get("completed_at") or "") for c in failed))
                for c in going:
                    if c.get("status") == "in_progress" and _age(c.get("started_at"), at) > LIMITS["check"]:
                        block("stuck", f"PR #{row['number']} check \"{_text(c.get('name'), 60)}\" has run {int(_age(c.get('started_at'), at) // 60)} min",
                              repo, _web(c.get("html_url")) or row["url"], c.get("started_at") or "")
                if any(BLOCKED_LABEL.search(lab) for lab in labels):
                    block("label", f"PR #{row['number']} is marked blocked", f"{repo} · {', '.join(labels[:3])}", row["url"], row["updated"])
        order = {"failed": 0, "running": 1, "queued": 2, "none": 3, "passed": 4}
        prs_out.sort(key=lambda r: (r["draft"], order.get(r["checks"]["state"], 5), -_ts(r["updated"])))

        issues_out = []
        for repo, issues in issues_by.items():
            for i in issues:
                labels = [str(lab.get("name")) for lab in i.get("labels") or []][:10]
                row = {"repo": repo, "number": i.get("number"), "title": _text(i.get("title"), 200), "url": _web(i.get("html_url")),
                       "labels": labels, "created": _text(i.get("created_at"), 30), "updated": _text(i.get("updated_at"), 30),
                       "blocked": any(BLOCKED_LABEL.search(lab) for lab in labels)}
                issues_out.append(row)
                if row["blocked"]:
                    block("label", f"#{row['number']} is blocked: {row['title']}", f"{repo} · {', '.join(labels[:3])}", row["url"], row["updated"])
        issues_out.sort(key=lambda r: -_ts(r["updated"]))

        rank = {"feed": 0, "machine": 1, "stuck": 2, "check": 3, "runner": 4, "label": 5}
        blockers.sort(key=lambda b: (rank.get(b["kind"], 9), -_ts(b["since"])))
        active_runs = [r for r in runs_out if r["status"] in ACTIVE]
        return {
            "at": now_iso(), "fresh": {"github": self.last_ok, "tried": self.last_try, "every": round(self.interval),
                                       "error": _text(self.error), "remaining": self.gh.remaining,
                                       "free": self.gh.free, "calls": self.gh.calls, "runner_note": _text(self.runner_note, 400)},
            "repos": [{"name": r["full_name"], "url": r["url"]} for r in self.repos],
            "counts": {"machines": len(machines_out), "busy": sum(1 for m in machines_out if m["busy"]),
                       "runs": len(active_runs), "queued": sum(1 for r in active_runs if r["status"] != "in_progress"),
                       "prs": len(prs_out), "issues": len(issues_out), "blockers": len(blockers)},
            "blockers": blockers[:40], "machines": machines_out, "runners": runners_out,
            "runs": runs_out[:40], "prs": prs_out[:80], "issues": issues_out[:60],
        }

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
                          "Nothing else can start there until the stale hold is cleared.", since=lease.get("since") or "")
                elif held > LIMITS["lease"]:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" has held the machine {int(held // 3600)} h", "", since=lease.get("since") or "")
                idle = float(d.get("idle_seconds") or 0)
                if idle > LIMITS["idle_lease"]:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" holds the machine but the CPU has been idle {int(idle // 60)} min",
                          "It may be stuck waiting on something.", since=lease.get("since") or "")
            for q in (d.get("queue") or [])[:10]:
                if not isinstance(q, dict):
                    continue
                purpose = _scrub(q.get("purpose"), 200)
                acts.append({"kind": "queued", "title": purpose, "since": _text(q.get("since"), 30), "detail": "waiting for the machine"})
                if _age(q.get("since"), at) > LIMITS["queue_wait"]:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" has waited {int(_age(q.get('since'), at) // 60)} min in the queue", since=q.get("since") or "")
            for a in (d.get("activities") or [])[:10]:
                if isinstance(a, dict) and a.get("title"):
                    acts.append({"kind": _text(a.get("kind") or kind_of(a.get("title")), 20), "title": _scrub(a.get("title"), 200),
                                 "since": _text(a.get("since"), 30), "detail": _scrub(a.get("detail"), 200)})
            procs = d.get("procs") if isinstance(d.get("procs"), dict) else {}
            if procs.get("claude"):
                acts.append({"kind": "coding", "title": f"Claude: {int(procs['claude'])} session" + ("s" if int(procs["claude"]) > 1 else ""),
                             "since": "", "detail": ""})
            silent = age > REPORT_STALE
            if silent and age < REPORT_GONE:
                block("machine", f"{self.display(host)} stopped reporting", f"Last report {int(age)} s ago.", since=rep["at"])
            by[host.lower()] = {"name": self.display(host), "host": host, "online": not silent, "last_report": rep["at"],
                                "busy": bool(acts and any(a["kind"] not in ("queued",) for a in acts)) or bool(procs.get("runner")),
                                "stats": {k: stats.get(k) for k in ("cpu", "ram_used_gb", "ram_total_gb") if isinstance(stats.get(k), (int, float))},
                                "disks": [{"name": _text(x.get("name"), 8), "free_gb": x.get("free_gb")} for x in (stats.get("disks") or [])[:4]
                                          if isinstance(x, dict) and isinstance(x.get("free_gb"), (int, float))],
                                "procs": {k: int(v) for k, v in procs.items() if isinstance(v, (int, float)) and v}, "activities": acts,
                                "runners": [], "version": _text(d.get("version"), 20)}
        for r in runners:
            key = r["machine"].lower()
            m = next((v for k, v in by.items() if k == key or v["name"].lower() == key), None)
            if m is None:
                m = by.setdefault(key, {"name": r["machine"], "host": r["machine"], "online": r["online"], "last_report": "",
                                        "busy": False, "stats": {}, "disks": [], "procs": {}, "activities": [], "runners": [], "version": ""})
            m["runners"].append(r["name"])
            if r["job"]:
                m["busy"] = True
                if not any(r["job"]["title"].split(" · ")[0].lower() in a["title"].lower() for a in m["activities"]):
                    m["activities"].insert(0, {"kind": "check" if "check" in r["job"]["title"].lower() else "working",
                                               "title": r["job"]["title"], "since": r["job"]["started"],
                                               "detail": (f"step: {r['job']['step']} · " if r["job"]["step"] else "") + r["job"]["repo"],
                                               "url": r["job"]["url"]})
        return sorted(by.values(), key=lambda m: m["name"].lower())
