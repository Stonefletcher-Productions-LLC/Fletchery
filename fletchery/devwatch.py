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

import http.client
import json
import os
import re
import ssl
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import alerts as alertsmod
import flow as flowmod
import pipeline  # beside this file, like every Fletchery module
import presence
import restart as restartmod
import rules
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
RETIRED_STATUS = re.compile(r"(?:(?:Running|Passed|Failed) on the build PC|Cancelled|Skipped) \(run 900[0-9]{9}\)")
NO_RESULT = re.compile(r"no result", re.I)
# Coding agents' branches (and PR authors): one Agents row per branch with recent activity.
AGENTS = (("claude/", "Claude"), ("codex/", "Codex"))     # branch prefix -> agent (workers.portal.live.agents)
AGENT_LOGINS = (("codex", "Codex"),)                      # PR author login containing this -> agent
SCHEMA = 2            # the snapshot's shape (docs/API.md); a field is only ever added, never renamed or removed
AGENT_HOURS = 24      # a branch quiet this long drops off the Agents card (workers.portal.live.agentHours)
AGENT_ACTIVE_MINUTES = 15   # working now: a heartbeat this recent (or, for an agent that sends none, a push or PR update)
CODING_FRESH = 30 * 60   # a push this recent counts as "coding"
GENERIC_LABELS = {"self-hosted", "windows", "linux", "macos", "x64", "arm64"}
FLOW_FRESH_DAYS = 3         # an open PR untouched for longer does not count as waiting to merge
REPORT_STALE = 60          # a machine that has not reported for this long is shown as silent
BACKFILL_RUNS = 30         # past runs read once at start when a repository has no step times yet
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
    for login, name in AGENT_LOGINS:
        if login and login in a:
            return name
    return ""


def set_agents(conf) -> list:
    """The coding agents this install knows, from the profile: [{"name": "Claude", "branches": ["claude/"],
    "logins": ["claude[bot]"]}]. Replaces the defaults (Claude, Codex) when given; returns notes on what was ignored."""
    global AGENTS, AGENT_LOGINS
    if not isinstance(conf, list) or not conf:
        return []
    notes, prefixes, logins = [], [], []
    for a in conf[:20]:
        name = _text((a or {}).get("name"), 30) if isinstance(a, dict) else ""
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,29}$", name):
            notes.append(f"live: agents entry {str(a)[:60]!r} ignored: give a name (letters, digits)")
            continue
        for b in a.get("branches") or [name.lower() + "/"]:
            if isinstance(b, str) and re.match(r"^[A-Za-z0-9._/-]{1,60}$", b):
                prefixes.append((b.lower(), name))
        for x in a.get("logins") or []:
            if isinstance(x, str) and x.strip():
                logins.append((x.strip().lower()[:60], name))
    if prefixes or logins:
        AGENTS, AGENT_LOGINS = tuple(prefixes), tuple(logins)
        presence.AGENTS.update({n.lower(): n for _, n in prefixes + logins})
    return notes


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


def _base_evidence(runs, statuses, sources):
    """Keep the latest result per producer. A required context can have both a check run and a commit status."""
    required = {name.casefold(): name for name in sources}
    latest = {}
    for row in [*runs, *statuses]:
        name = str(row.get("name") or "").casefold()
        if not name or (required and name not in required):
            continue
        app = (row.get("app") or {}).get("id")
        context = required.get(name, row["name"])
        pinned = sources.get(context, set()) - {None}
        if pinned and app not in pinned:
            continue
        if row.get("source") == "status" and not required and not row.get("html_url"):
            continue
        key = (context.casefold(), row.get("source", "check"), app)
        if key not in latest or (row.get("id") or 0) > (latest[key].get("id") or 0):
            latest[key] = dict(row, name=context)
    grouped = {}
    for (name, _, _), row in latest.items():
        grouped.setdefault(name, []).append(row)
    result = []
    for rows in grouped.values():
        context = rows[0]["name"]
        pinned = sources.get(context, set()) - {None}
        observed = {(r.get("app") or {}).get("id") for r in rows}
        # Show a genuine failure immediately. Otherwise all required producers must give a passing result.
        failed = next((r for r in rows if r.get("status") == "completed" and r.get("conclusion") in FAILED), None)
        unfinished = next((r for r in rows if r.get("status") != "completed" or r.get("conclusion") != "success"), None)
        if failed:
            result.append(failed)
        elif pinned - observed:
            result.append({"name": context, "status": "queued"})
        else:
            result.append(unfinished or rows[0])
    return result


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


def _upload_progress(raw, age: float) -> dict | None:
    if not isinstance(raw, dict):
        return None
    done, total = raw.get("done"), raw.get("total")
    if type(done) is not int or type(total) is not int or not (0 <= done <= total and total > 0):
        return None
    eta = raw.get("eta_seconds")
    if type(eta) is not int or eta < 0 or age > 120 or done in (0, total):
        eta = None
    return {"done": done, "total": total, "eta_seconds": eta}


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
def tls_context() -> ssl.SSLContext:
    """The TLS context for direct GitHub calls.

    Python's default context only reads the roots that are already in the Windows store; it cannot make Windows fetch
    a missing root the way Windows' own TLS does. GitHub's certificate since 2026-10-28 chains to Sectigo Public Server
    Authentication Root E46, which Windows downloads on first use, so on a PC where nothing had fetched it yet every
    direct call failed with CERTIFICATE_VERIFY_FAILED ("unable to get issuer certificate"; Gaming PC, 2026-11-20..22).
    truststore verifies through the OS (Windows CertGetCertificateChain, which fetches the root); without it, certifi's
    bundle; without either, the default context."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


HTTPS_RETRY = 3600      # after falling back to gh, try direct HTTPS again this often (a fault can clear itself)
TIMEOUT_BACKOFF = 2.0   # a timed-out direct request is tried once more after this many seconds
TIMEOUTS_BEFORE_GH = 3  # this many requests in a row timing out twice each: treat it as blocked and use gh


_TRANSIENT = (TimeoutError,                 # socket.timeout is TimeoutError since 3.10
              http.client.IncompleteRead,   # the answer was cut short
              ConnectionResetError)         # includes http.client.RemoteDisconnected: GitHub closed a kept-alive
                                            # connection without answering (Gaming PC, 2026-11-22 22:46Z)


def _timed_out(e: BaseException) -> bool:
    """A timeout, a response cut short or a dropped connection: worth one more try, not a sign that this process
    cannot reach GitHub at all."""
    reason = getattr(e, "reason", None)
    if isinstance(e, _TRANSIENT) or isinstance(reason, _TRANSIENT):
        return True
    text = str(reason or e).lower()
    return "timed out" in text or "remote end closed connection" in text


class GitHub:
    """GETs with ETags. A 304 is free and answers with the copy kept from last time."""

    def __init__(self, token_fn, log=None, timeout: float = 20, cli_env=None):
        self.token_fn = token_fn
        self.cli_env = cli_env or (lambda: None)
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
        self.via_since = 0.0        # when it fell back to gh; direct HTTPS is tried again HTTPS_RETRY later
        self.ssl_context = None     # built on the first direct call (tls_context())
        self.sleep = time.sleep     # the backoff before retrying a timed-out request (tests replace it)
        self.timeouts = 0           # requests in a row whose direct tries both timed out
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
        if self.via == "gh" and self.gh_cmd is not None and time.time() - self.via_since >= HTTPS_RETRY:
            self.via = "https"
            self.log("live: trying direct HTTPS to GitHub again")
        if self.via == "gh":
            code, headers, body = self._gh(url, etag)
        else:
            try:
                code, headers, body = self._urllib_retrying(url, etag)
                self.timeouts = 0
            except GitHubError as e:
                if e.code or self.gh_cmd is None:
                    raise
                if e.timeout:
                    self.timeouts += 1
                if e.timeout and self.timeouts < TIMEOUTS_BEFORE_GH:
                    # Slow, not blocked: a busy backend or a slow GitHub answer (2026-11-22, Gaming PC: one 20 s
                    # timeout sent every read through gh for an hour). This one request goes through gh; the feed
                    # stays on direct HTTPS.
                    code, headers, body = self._gh(url, etag)
                else:
                    # Some processes on a PC can't open sockets of their own (a sandbox, a firewall rule for one
                    # program) or can't verify GitHub's certificate, while `gh` can: 2026-11-18 the Gaming PC backend
                    # got URLError on every call. Use gh from then on (tried again HTTPS_RETRY later). Timeouts that
                    # keep coming (TIMEOUTS_BEFORE_GH requests in a row) are read as blocked too.
                    self.via, self.via_since, self.timeouts = "gh", time.time(), 0
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
            try:
                data = json.loads(body or b"null")
            except ValueError as e:
                raise GitHubError(0, "GitHub returned an incomplete JSON response") from e
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

    def _urllib_retrying(self, url: str, etag: str):
        """_urllib, with one more try after TIMEOUT_BACKOFF when the first one timed out."""
        try:
            return self._urllib(url, etag)
        except GitHubError as e:
            if e.code or not e.timeout:
                raise
        self.sleep(TIMEOUT_BACKOFF)
        return self._urllib(url, etag)

    def _urllib(self, url: str, etag: str):
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Fletchery-Live",
            **({"Authorization": "Bearer " + self.token()} if self.token() else {}),
            **({"If-None-Match": etag} if etag else {})})
        try:
            if self.ssl_context is None:
                self.ssl_context = tls_context()
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as r:
                return r.status, _Headers(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            return e.code, _Headers((e.headers or {}).items()), e.read()
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(0, f"GitHub could not be reached: {getattr(e, 'reason', None) or e}"[:300],
                              timeout=_timed_out(e))

    def _gh(self, url: str, etag: str):
        """The same GET through `gh api -i` (no window): status line, headers, then the body."""
        path = url[len(API) + 1:] if url.startswith(API + "/") else url
        cmd = [self.gh_cmd or "gh", "api", "-i", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
        if etag:
            cmd += ["-H", f"If-None-Match: {etag}"]
        try:
            out = subprocess.run(cmd + [path], capture_output=True, env=self.cli_env(), timeout=self.timeout + 10,
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
    def __init__(self, code: int, message: str, timeout: bool = False):
        super().__init__(f"{code} {message}" if code else message)
        self.code = code
        self.timeout = timeout      # the request timed out (or was cut short): slow, not unreachable


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
_MISSING_REF = object()


def _iso_after(iso: str, seconds: float) -> str:
    """`iso` plus `seconds`, as an ISO string that compares with GitHub's."""
    t = _ts(iso)
    return now_iso_at(t + seconds) if t else "~"


def _is_pr_build(run: dict) -> bool:
    """A pull request's PR check run. Actions can put the PR title in name; the workflow path survives re-runs and
    custom run-name expressions."""
    path = str(run.get("path") or "").split("@")[0]
    return run.get("event") == "pull_request" and (
        path == ".github/workflows/pr-check.yml" or not path and run.get("name") == "PR check")


def _workload(title: str) -> str:
    """Recognize a dispatch's named task across checkout revisions (e.g. 'Node checks <sha> CityPictures')."""
    suffix = re.split(r"\b[0-9a-fA-F]{7,40}\b\s*", title)[-1]
    words = re.findall(r"[a-z0-9]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", suffix).lower())
    return " ".join(words) if len(words) >= 2 else ""


def _names_holder(workload: str, purpose: str) -> bool:
    description = " ".join(re.findall(r"[a-z0-9]+", purpose.lower()))
    return bool(workload and f" {workload} " in f" {description} ")


def _reservation_key(host: str, purpose: str) -> tuple:
    name = re.sub(r"\s*\(github run \d+\)", "", _scrub(purpose).lower()).removeprefix("local run ")
    return "reservation", host.lower(), name


def _runner_job(run: dict) -> dict:
    """What a runner is doing, from its run row: enough to see a step that runs too long or a run nobody needs."""
    return {"title": run["name"] + (f" · PR #{run['pr']}" if run["pr"] else ""), "repo": run["repo"], "id": run["id"],
            "step": run["step"], "step_started": run["step_started"], "url": run["url"], "started": run["started"],
            "status": run["status"], "pr_closed": bool(run.get("pr_closed")), "pr": run["pr"],
            "steps_done": run["steps_done"], "steps_total": run["steps_total"], "name": run["job"],
            "attempt": run.get("attempt", 1), **({"progress": run["progress"]} if "progress" in run else {})}


class DevWatch:
    def __init__(self, conf: dict | None = None, log=None, token_fn=gh_token, github: GitHub | None = None,
                 state_dir=None, count_account=""):
        conf = conf or {}
        self.log = log or (lambda line: None)
        self.owners = {str(o).lower() for o in conf.get("owners") or []}
        self.exclude = {str(r).lower() for r in conf.get("exclude") or []}
        self.active_days = float(conf.get("activeDays") or 21)
        self.fast = max(10.0, float(conf.get("fastSeconds") or 15))
        self.slow = max(self.fast, float(conf.get("slowSeconds") or 90))
        self.names = {str(k).lower(): _text(v, 60) for k, v in (conf.get("names") or {}).items()}
        for note in set_agents(conf.get("agents")):
            self.log(note)
        # Projects (workers.portal.live.projects): {"LifeShare": ["Owner/LifeShare", "Owner/LifeShare-Art"]}. A watched
        # repository in none of them is a project of its own.
        self.projects: dict[str, list] = {}
        for name, repos in ((conf.get("projects") or {}) if isinstance(conf.get("projects"), dict) else {}).items():
            good = [str(r) for r in repos if isinstance(r, str) and re.match(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$", r)] \
                if isinstance(repos, list) else []
            if good and re.match(r"^[^<>\x00-\x1f]{1,40}$", str(name)):
                self.projects[str(name)] = good[:50]
            else:
                self.log(f"live: projects entry {str(name)[:40]!r} ignored: give \"Name\": [\"Owner/Repo\", ...]")
        # runner name suffixes to strip when GitHub doesn't send a runner's labels (InstallPrRunner.ps1 labels)
        self.runner_labels = [str(x) for x in conf.get("runnerLabels") or ("unreal-pr", "unreal-light")]
        self.gh = github or GitHub(token_fn, self.log)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.repos: list[dict] = []
        self.repos_at = 0.0
        self.count_account = count_account
        self.pr_counts = {}                  # watched repo -> (last complete PR count, confirmation time)
        self.stale_counts = {}               # affected watched repo -> confirmation time, or "" if unknown
        self.inventory_complete = False
        self.runners: dict[str, list] = {}          # owner or repo -> runners
        self.runner_note = ""
        self.runs: dict[str, list] = {}             # repo -> recent runs
        self.jobs: dict[int, list] = {}             # run id -> jobs
        self.pr_build_queues = {}                   # repository -> queued PR compile jobs from a complete live read
        self.partial_jobs = {}                      # repo -> run attempts whose job pages still need a complete read
        self.prs: dict[str, list] = {}              # repo -> open PRs
        self.checks: dict[tuple, list] = {}         # (repo, sha) -> check runs
        self.pushes: dict[str, list] = {}           # repo -> recent pushes to agent branches (from repository events)
        self.closed: dict[str, list] = {}           # repo -> recently merged or closed PRs' branches (they leave Agents)
        self.no_result: dict[int, bool] = {}        # failed check run id -> its annotations say "No result" (asked once)
        self.fail_notes: dict[int, tuple] = {}      # failed check run id -> (test names, annotation text) (asked once)
        self.fail_jobs: dict[int, dict] = {}        # failed check run id -> {"step", "runner"} of its job (asked once)
        self.cancels: dict[tuple, dict] = {}        # (repo, run id) -> {"how", "who"}: who or what cancelled a run
        self.issues: dict[str, list] = {}           # repo -> open issues
        self.reports: dict[str, dict] = {}          # machine -> its last report
        self.restarts = restartmod.Restarts(Path(state_dir) / "restarts.json" if state_dir else None)   # the Restart button
        self.extra_blockers = None                  # callable -> [block() keyword dicts] from helpers (unjam.py)
        self.skip: dict[str, tuple[float, bool]] = {}  # path -> retry time, GitHub explicitly reported a missing ref
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
        self._backfilled: set = set()               # repositories whose past runs were read for step times
        self._holder_times = {}                    # bounded historical reads, outside the snapshot/HTTP path
        self.merged: dict[str, list] = {}           # repo -> PRs merged in the last day (followed through staging)
        self.releases: dict[str, list] = {}         # repo -> its latest releases
        self.presence = presence.Presence(Path(state_dir) / "agent-presence.json" if state_dir else None, clean=_scrub,
                                          active_seconds=self.agent_active, log=self.log)
        # What happened, kept (store.py): merges, runs, jobs. The Flow strip reads it (flow.py).
        self.store = storemod.Store(Path(state_dir) / "fletchery.db" if state_dir else None, log=self.log)
        self.flow = flowmod.Flow(self.store, conf.get("flow"))
        self._flow_cache: tuple[float, dict] = (0.0, {})
        # Base branches (rules.py): which to watch besides every PR's base, and the project's fix and red-issue labels.
        self.bases_conf: dict[str, list] = {str(k).lower(): [str(b) for b in v][:10] for k, v in
                                            ((conf.get("bases") or {}) if isinstance(conf.get("bases"), dict) else {}).items()
                                            if isinstance(v, list)}
        self.fix_labels = [str(x) for x in conf.get("fixLabels") or rules.FIX_LABELS][:20]
        # Extra "this is the machine, not the code" messages (regular expressions), added to rules.MACHINE_TEXT.
        self.machine_patterns = list(rules.MACHINE_TEXT)
        for x in ((conf.get("causes") or {}).get("machine") or []) if isinstance(conf.get("causes"), dict) else []:
            try:
                re.compile(str(x))
                self.machine_patterns.append(str(x)[:200])
            except re.error:
                self.log(f"live: causes.machine pattern {str(x)[:60]!r} ignored: not a regular expression")
        self.red_issue_labels = [str(x) for x in conf.get("redIssueLabels") or rules.RED_ISSUE_LABELS][:20]
        self.bases: dict[str, dict] = {}            # branch tip, effective requirements and observations by repository
        self.notes: dict[int, list] = {}            # completed base check run id -> test names
        # Which blockers reach a person, and when (alerts.py): a wait before firing, a hold after, groups and snoozes.
        self.alerts = alertsmod.Alerts(Path(state_dir) / "alerts.json" if state_dir else None, conf.get("alerts"), self.store, self.log)

    # ---- machines ----
    def report(self, machine: str, data: dict, ip: str = "") -> dict:
        """A machine's report (machines.py /v1/report). Kept whole but bounded; cleaned when shown."""
        if not isinstance(data, dict):
            raise ValueError("report must be an object")
        body = json.dumps(data)
        if len(body) > 64 * 1024:
            raise ValueError("report too large")
        with self._lock:
            previous = self.reports.get(str(machine)) or {}
            was = previous.get("data") or {}
            self.reports[str(machine)] = {"data": data, "at": now_iso(), "ip": ip}
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        host = _text(data.get("host") or machine, 60)
        old = was.get("lease")
        current = data.get("lease")
        if (isinstance(old, dict) and old.get("purpose") and old.get("alive") and _ts(old.get("since"))
                and _age(previous.get("at")) <= REPORT_STALE
                and (current is None or isinstance(current, dict) and current.get("since") != old["since"])):
            self.history.add(_reservation_key(host, old["purpose"]), _age(old["since"]), old["since"])
        for x in (stats.get("disks") or [])[:4]:      # kept, so a drive that is filling is seen before it is full
            if isinstance(x, dict) and isinstance(x.get("free_gb"), (int, float)) and x.get("name"):
                try:
                    self.store.disk(host, _text(x.get("name"), 8), float(x["free_gb"]))
                except Exception as e:      # history is a nicety
                    self.log(f"live: disk history: {type(e).__name__}: {e}")
        if data.get("busy") and not was.get("busy"):
            self._wake.set()      # a machine just picked something up: read GitHub soon (once, not on every report)
        lease = data.get("lease") if isinstance(data.get("lease"), dict) else None
        idle = not data.get("busy") and not (lease and lease.get("alive") is not False)
        cmd = self.restarts.poll(str(machine), data.get("boot"), _text(data.get("restart_ack"), 60), idle=idle)
        return {"ok": True, "every": 10, **({"restart": cmd} if cmd else {})}

    def restart_machine(self, name: str, by: str = "", reason: str = "", force: bool = False, when_idle: bool = False,
                        cancel: bool = False) -> dict:
        """Tell one machine to restart (restart.py). `name` is its registered name, host name or display name.
        Refused with ValueError when the machine cannot take the request, or is working and neither `force` nor
        `when_idle` is set. `when_idle` waits for the machine to finish what it runs and restarts it then.
        `cancel` withdraws a request the machine has not collected yet."""
        want = str(name or "").strip().lower()
        with self._lock:
            reports = dict(self.reports)
        hit = next(((k, r) for k, r in reports.items() if want and want in (
            k.lower(), _text((r["data"] or {}).get("host") or k, 60).lower(),
            self.display(_text((r["data"] or {}).get("host") or k, 60)).lower())), None)
        if hit is None:
            raise ValueError(f"No reporting machine is called {name!r}.")
        key, rep = hit
        d = rep["data"] or {}
        shown = self.display(_text(d.get("host") or key, 60))
        if _age(rep["at"]) > REPORT_STALE and not (d.get("after_restart") or {}).get("guard"):
            raise ValueError(f"{shown} is not reporting, so it cannot be told to restart from here.")
        if not d.get("boot"):
            raise ValueError(f"{shown} runs an older reporter that cannot restart it. Update Fletchery on that machine first.")
        lease = d.get("lease") if isinstance(d.get("lease"), dict) else None
        doing = _scrub(lease.get("purpose"), 120) if lease and lease.get("alive") is not False else ""
        if cancel:
            if not self.restarts.cancel(key):
                raise ValueError(f"{shown} has no restart request that can still be withdrawn.")
            self.log(f"live: restart of {shown} withdrawn by {by or 'someone'}")
            return {"ok": True, "machine": shown, "message": f"The restart of {shown} was withdrawn."}
        busy = bool(doing or d.get("busy"))
        if busy and when_idle and not force:
            rec = self.restarts.request(key, by, reason, wait=True)
            self.log(f"live: restart of {shown} when idle requested by {by or 'someone'}")
            return {"ok": True, "id": rec["id"], "machine": shown, "waiting": True,
                    "message": f"{shown} restarts as soon as it has finished what it is running."}
        if doing and not force:
            raise ValueError(f"{shown} is running \"{doing}\". A restart ends it. Send when_idle to restart after it, or force to restart now.")
        rec = self.restarts.request(key, by, reason)
        self.log(f"live: restart of {shown} requested by {by or 'someone'}" + (f" (ends \"{doing}\")" if doing else ""))
        return {"ok": True, "id": rec["id"], "machine": shown,
                "message": f"{shown} restarts in under a minute." + ("" if (d.get("after_restart") or {}).get("signs_in") is not False
                            else " It does not sign in by itself, so someone must sign in before Fletchery and Claude are back.")}

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
        repos, discovered_at, known = self.repos, self.repos_at, self.pr_counts
        if not self.inventory_complete or time.time() - discovered_at > 600:
            try:
                repos = self._discover()
            except GitHubError as e:
                with self._lock:
                    self.inventory_complete = False
                    self.stale_counts = {r["full_name"]: known.get(r["full_name"], (None, ""))[1] for r in repos}
                    self.error = str(e)[:300]
                raise
            discovered_at = time.time()
            # Current discovery authorizes which saved values can be published after a restart.
            known = {**self.store.saved_pr_counts(self.count_account), **known}
        names = [r["full_name"] for r in repos]
        orgs = sorted({r["owner"] for r in repos if r.get("org")})
        with ThreadPoolExecutor(max_workers=6) as pool:
            inventories = dict(zip(names, pool.map(self._poll_open_prs, names)))
            got = dict(zip(names, pool.map(self._poll_repo, names, (inventories[repo][0] for repo in names))))
            # Organisation runners need admin:org; runners registered on one repository are listed there, so every
            # repository is asked too (an ETag answer costs nothing; a refusal is asked again after an hour).
            places = [("orgs", o) for o in orgs] + [("repos", repo) for repo in names]
            found = dict(zip([p[1] for p in places], pool.map(self._poll_runners, places)))
        runs, jobs, prs, checks, issues, pushes, closed, merged, releases, bases = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}
        refreshed, counts, stale, errors = {}, {}, {}, []
        for repo, g in got.items():
            rows, confirmed, pr_error = inventories[repo]
            if confirmed:
                counts[repo] = refreshed[repo] = (len(rows), confirmed)
            elif repo in known:
                counts[repo] = known[repo]
            error = pr_error or g.get("error") or found[repo][1]
            if error:
                stale[repo] = counts.get(repo, (None, ""))[1]
                errors.append(error)
                g["error"] = error
            runs[repo], prs[repo], issues[repo], pushes[repo] = g["runs"], g["prs"], g["issues"], g.get("pushes") or []
            closed[repo], merged[repo], releases[repo] = g.get("closed") or [], g.get("merged") or [], g.get("releases") or []
            bases[repo] = g.get("bases", self.bases.get(repo, {}))
            jobs.update(g["jobs"])
            checks.update(g["checks"])
        with self._lock:   # swapped whole, so a snapshot never sees half a poll
            self.repos, self.repos_at, self.inventory_complete = repos, discovered_at, True
            self.pr_counts, self.stale_counts = counts, stale
            self.error = ("; ".join(errors))[:300]
            if not errors or len(errors) < len(got):
                self.last_ok = now_iso()
            self.runs, self.jobs, self.prs, self.checks, self.issues, self.pushes = runs, jobs, prs, checks, issues, pushes
            self.pr_build_queues = {repo: g.get("pr_build_queue") for repo, g in got.items()}
            self.partial_jobs = {repo: g["partial_jobs"] for repo, g in got.items()}
            self.closed, self.merged, self.releases = closed, merged, releases
            self.bases = bases
            self.runners = {k: rows for k, (rows, _) in found.items() if rows is not None}
        if refreshed:
            self.store.save_pr_counts(self.count_account, refreshed)
        for repo in names:          # nothing known about a repository's times yet: learn from its past runs, once
            if self.history.path and repo not in self._backfilled and not self.history.estimate(("build", repo))[1]:
                self._backfilled.add(repo)
                threading.Thread(target=self._backfill, args=(repo,), name="fletchery-backfill", daemon=True).start()
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
            raise GitHubError(0, errors[0])

    def _discover(self) -> list[dict]:
        out, page = [], 1
        cutoff = time.time() - self.active_days * 86400
        seen = set()
        while True:
            batch = self.gh.get(f"user/repos?affiliation=owner,collaborator,organization_member&sort=pushed&per_page=100&page={page}")
            if not isinstance(batch, list) or any(not isinstance(r, dict) or not r.get("full_name") for r in batch):
                raise GitHubError(0, "GitHub returned an incomplete repository list")
            for r in batch:
                full = str(r.get("full_name") or "")
                if full.lower() in seen:
                    raise GitHubError(0, "The repository list changed during pagination. Retrying discovery.")
                seen.add(full.lower())
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

    def _statuses(self, repo: str, sha: str, response=None) -> list:
        """A commit's statuses, shaped like check runs (newest per context)."""
        state = {"success": ("completed", "success"), "failure": ("completed", "failure"), "error": ("completed", "failure"),
                 "pending": ("in_progress", None)}
        out = []
        if response is None:
            response = self._try(f"repos/{repo}/commits/{sha}/status?per_page=100") or {}
        for st in response.get("statuses") or []:
            if not isinstance(st, dict) or str(st.get("state")) not in state:
                continue
            status, conclusion = state[str(st["state"])]
            row = {"id": st.get("id"), "name": st.get("context"), "status": status, "conclusion": conclusion,
                   "app": st.get("performed_via_github_app") or st.get("app"),
                   "html_url": st.get("target_url"), "started_at": st.get("created_at"),
                   "completed_at": st.get("updated_at") if status == "completed" else None, "source": "status"}
            if conclusion == "failure" and isinstance(st.get("id"), int):
                self.no_result[st["id"]] = bool(NO_RESULT.search(str(st.get("description") or "")))
            out.append(row)
        return out

    def _try(self, path: str, quiet_for: float = 1800, *, missing_ref=None):
        """GET that remembers a 403/404 (no access, Actions off) or a 422 (a branch deleted since: "No commit found for
        SHA: <branch>") and stops asking for a while."""
        retry_at, missing = self.skip.get(path, (0, False))
        if retry_at > time.time():
            return missing_ref if missing else None
        try:
            return self.gh.get(path)
        except GitHubError as e:
            if e.code in (403, 404, 410, 422) and "rate limit" not in str(e).lower():
                now_s = time.time()
                if len(self.skip) > 500:
                    self.skip = {k: v for k, v in self.skip.items() if v[0] > now_s}
                missing = (e.code == 422 and "no commit found for sha:" in str(e).lower()) or str(e).lower() in (
                    "404 ref not found", "404 branch not found")
                self.skip[path] = (now_s + quiet_for, missing)
                return missing_ref if missing else None
            message = str(e).removeprefix(f"{e.code} ")
            raise GitHubError(e.code, f"{path}: {message}") from e

    def _poll_open_prs(self, repo: str) -> tuple:
        """Publish a repository's PR rows only after every page succeeds, including the terminal empty page."""
        path = f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=50"
        pulls, page = {}, 1
        try:
            while True:
                batch = self.gh.get(path if page == 1 else f"{path}&page={page}")
                if not isinstance(batch, list):
                    raise GitHubError(0, "GitHub returned an incomplete open PR list")
                for pr in batch:
                    number = pr.get("number") if isinstance(pr, dict) else None
                    if type(number) is not int or number <= 0 or number in pulls:
                        raise GitHubError(0, "GitHub returned an inconsistent open PR list")
                    pulls[number] = pr
                if len(batch) < 50:
                    return list(pulls.values()), now_iso(), ""
                page += 1
        except GitHubError as e:
            return self.prs.get(repo, []), "", f"{repo}: {e}"

    def _poll_repo(self, repo: str, prs: list) -> dict:
        """Stage the repository privately; each successful read replaces only the data it owns."""
        with self._lock:
            runs = self.runs.get(repo, [])
            heads = {(repo, (p.get("head") or {}).get("sha")) for p in prs}
            out = {"runs": runs, "prs": prs, "issues": self.issues.get(repo, []), "pushes": self.pushes.get(repo, []),
                   "closed": self.closed.get(repo, []), "merged": self.merged.get(repo, []), "merged_all": [],
                   "releases": self.releases.get(repo, []), "jobs": {r["id"]: self.jobs[r["id"]] for r in runs if r["id"] in self.jobs},
                   "checks": {k: self.checks[k] for k in heads if k in self.checks},
                   "partial_jobs": set(self.partial_jobs.get(repo, ()))}
        try:
            self._read_repo(repo, prs, out)
        except GitHubError as exc:
            out.setdefault("error", f"{repo}: {exc}")
        return out

    def _read_holder_times(self, repo, runs, jobs):
        """Look up the same runner, dispatch workload, job and step, only while a reported reservation has waiters."""
        with self._lock:
            reports = [r["data"] for r in self.reports.values() if _age(r["at"]) <= REPORT_STALE and r["data"].get("queue")]
        for run in runs:
            if run.get("event") != "workflow_dispatch" or run.get("status") != "in_progress" or not run.get("workflow_id"):
                continue
            workload = _workload(run.get("display_title") or "")
            for job in jobs.get(run["id"], []):
                runner = str(job.get("runner_name") or "")
                if job.get("status") != "in_progress" or not any(
                        isinstance(d.get("lease"), dict) and d["lease"].get("alive") and isinstance(d.get("host"), str) and
                        (runner.lower() == d["host"].lower() or runner.lower().startswith(d["host"].lower() + "-")) and
                        _names_holder(workload, str(d["lease"].get("purpose") or "")) for d in reports):
                    continue
                step = next((s.get("name") for s in job.get("steps") or [] if s.get("status") == "in_progress"), None)
                if not step:
                    continue
                key = (repo, run["id"], runner, step)
                now = time.time()
                with self._lock:
                    if self._holder_times.get(key, (0, ()))[0] > now:
                        continue
                samples = []
                ttl = 3600
                try:
                    samples = self._past_holder_steps(repo, run, workload, job, step)
                except GitHubError:
                    ttl = 180
                with self._lock:
                    self._holder_times = {k: v for k, v in self._holder_times.items() if v[0] > now}
                    self._holder_times[key] = (now + ttl, samples)

    def _past_holder_steps(self, repo, run, workload, job, step):
        path = f"repos/{repo}/actions/workflows/{run['workflow_id']}/runs?status=completed&per_page=50"
        past = (self._try(path) or {}).get("workflow_runs") or []
        matches = [r for r in past if r.get("id") != run["id"] and r.get("conclusion") in ("success", "failure")
                   and _workload(r.get("display_title") or "") == workload]
        samples = []
        for r in matches[:5]:
            jobs = (self._try(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=50") or {}).get("jobs") or []
            for j in jobs:
                if j.get("runner_name") != job.get("runner_name") or j.get("name") != job.get("name"):
                    continue
                for s in j.get("steps") or []:
                    start, end = _ts(s.get("started_at")), _ts(s.get("completed_at"))
                    if s.get("name") == step and s.get("conclusion") in ("success", "failure") and end > start > 0:
                        samples.append((end - start, s["conclusion"]))
        return samples

    def _action_pages(self, repo, path, field, size, out):
        """Keep pages already received on timeout and tell the caller when absence is authoritative."""
        rows, seen = [], set()
        page = 1
        try:
            while True:
                page_path = f"{path}&page={page}"
                response = self._try(page_path)
                if response is None:  # endpoint unavailable (e.g. Actions disabled)
                    if rows:
                        raise GitHubError(0, f"GitHub did not return {page_path}")
                    return rows, False
                batch = response.get(field) if isinstance(response, dict) else None
                if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                    raise GitHubError(0, f"Incomplete {field} response for {path}")
                ids = [row["id"] for row in batch if row.get("id") is not None]
                if (field == "workflow_runs" or len(batch) >= size) and len(ids) != len(batch):
                    raise GitHubError(0, f"Missing IDs in {field} for {path}")
                if len(ids) != len(set(ids)):
                    raise GitHubError(0, f"Duplicate IDs in {field} for {path}")
                rows.extend(row for row in batch if row.get("id") not in seen)
                if seen.intersection(ids):
                    raise GitHubError(0, f"{field} changed while paging {path}")
                seen.update(ids)
                total = response.get("total_count")
                if len(batch) < size or len(rows) == total:
                    if total is not None and len(rows) != total:
                        raise GitHubError(0, f"Incomplete {field} list for {path}")
                    return rows, True
                if field == "workflow_runs" and page == 10:
                    raise GitHubError(0, f"Active run list exceeds GitHub's 1,000-result limit for {path}")
                page += 1
        except GitHubError as exc:
            out.setdefault("error", f"{repo}: {exc}")
            return rows, False

    def _read_actions(self, repo, out):
        previous = {r["id"]: r for r in out["runs"]}
        recent = self._try(f"repos/{repo}/actions/runs?per_page=20")
        out["pr_build_queue"] = None  # set below only from a complete read of every active PR build
        if recent is None:
            if previous:
                out["error"] = f"{repo}: GitHub did not return the recent Actions runs"
            return
        if not isinstance(recent, dict) or not isinstance(recent.get("workflow_runs"), list):
            raise GitHubError(0, f"Incomplete recent Actions runs for {repo}")
        recent_runs = recent["workflow_runs"]
        if any(not isinstance(r, dict) or type(r.get("id")) is not int for r in recent_runs):
            raise GitHubError(0, f"Invalid recent Actions run for {repo}")
        observed = {r["id"]: r for r in recent_runs}
        complete = True
        if len(recent_runs) == 20 or recent.get("total_count", 0) > len(recent_runs):
            # A long-running job can fall outside the recent window. A partial active list cannot retire unseen jobs.
            for status in ("in_progress", "queued"):
                path = f"repos/{repo}/actions/runs?status={status}&per_page=100"
                batch, finished = self._action_pages(repo, path, "workflow_runs", 100, out)
                observed.update((r["id"], r) for r in batch)
                complete = complete and finished
            if not complete:
                out.setdefault("error", f"{repo}: GitHub did not return all active runs")
        runs = dict(observed)
        if not complete:
            runs.update((rid, r) for rid, r in previous.items() if rid not in runs and r.get("status") in ACTIVE)
        out["runs"] = list(runs.values())
        out["partial_jobs"].intersection_update((rid, r.get("run_attempt") or 1) for rid, r in runs.items())
        jobs = {}
        for rid, run in runs.items():
            old = previous.get(rid, {})
            attempt = run.get("run_attempt") or 1
            same_attempt = (old.get("run_attempt") or 1) == attempt
            before = out["jobs"].get(rid) if same_attempt else None
            if before is not None:
                jobs[rid] = before
            if rid not in observed:
                continue
            key = (rid, attempt)
            # A PR build seen finished for the first time (or on a new attempt) is read once, so its wait is recorded.
            newly_finished = _is_pr_build(run) and run.get("conclusion") in ("success", "failure") and (
                before is None or old.get("status") != "completed")
            if (newly_finished or run.get("status") in ACTIVE or old.get("status") in ACTIVE or key in out["partial_jobs"]
                    or any(j.get("status") in ACTIVE for j in before or [])):
                path = f"repos/{repo}/actions/runs/{rid}/jobs?per_page=30"
                batch, finished = self._action_pages(repo, path, "jobs", 30, out)
                # /jobs defaults to the latest attempt, which may advance during this poll.
                if any((j.get("run_attempt") or attempt) > attempt for j in batch):
                    finished = False
                    out.setdefault("error", f"{repo}: run {rid} changed attempts during its job refresh")
                batch = [j for j in batch if (j.get("run_attempt") or attempt) == attempt]
                if finished:
                    jobs[rid] = batch
                    out["partial_jobs"].discard(key)
                else:
                    out["partial_jobs"].add(key)
                    out.setdefault("error", f"{repo}: GitHub did not return all jobs for run {rid}")
                    if batch:
                        received = {j.get("id") for j in batch}
                        jobs[rid] = batch + [j for j in before or [] if j.get("id") not in received]
        out["jobs"] = jobs
        # Queued PR compile jobs, only when every active run and every active PR build's jobs were read in full:
        # a partial read must not report a shorter line than the real one.
        queued, queue_complete = {}, complete
        for rid, run in runs.items():
            if not _is_pr_build(run) or run.get("status") not in ACTIVE:
                continue
            attempt = run.get("run_attempt") or 1
            if rid not in observed or (rid, attempt) in out["partial_jobs"] or rid not in jobs:
                queue_complete = False
                continue
            for job in jobs[rid]:
                if job.get("name") != storemod.PR_BUILD or job.get("status") != "queued" or job.get("runner_name"):
                    continue
                if not isinstance(job.get("id"), int):
                    queue_complete = False
                    continue
                queued[job["id"]] = storemod.ts(job.get("created_at"))
        out["pr_build_queue"] = {"times": list(queued.values()), "at": time.time()} if queue_complete else None

    def _read_repo(self, repo: str, prs: list, out: dict) -> None:
        self._read_actions(repo, out)
        runs = out["runs"]
        self._read_holder_times(repo, runs, out["jobs"])
        for run in runs:
            # A cancelled run in the last day: who or what cancelled it, from its job's annotations, asked once.
            if (run["id"], run.get("run_attempt") or 1) in out["partial_jobs"]:
                continue
            if run.get("conclusion") == "cancelled" and (repo, run["id"]) not in self.cancels and _age(run.get("updated_at")) < 86400:
                jobs = out["jobs"].get(run["id"])
                if jobs is None:
                    jobs = (self._try(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=30") or {}).get("jobs") or []
                    out["jobs"][run["id"]] = jobs
                job = next((j for j in jobs if j.get("conclusion") == "cancelled"), None)
                notes = self._try(f"repos/{repo}/check-runs/{job['id']}/annotations?per_page=20") or [] if job and job.get("id") else []
                how, who = rules.cancel_reason(notes)
                self.cancels[(repo, run["id"])] = {"how": how, "who": who}
        checked = set()
        for pr in prs:
            sha = (pr.get("head") or {}).get("sha")
            if not sha or sha in checked:
                continue
            # A rerun can add a check to the same commit. Revalidate each open head once per poll using GitHub's ETag.
            checks = (self._try(f"repos/{repo}/commits/{sha}/check-runs?per_page=50") or {}).get("check_runs") or []
            if not checks:
                # A commit pushed with a workflow's token starts no check run; the job posts its result as a commit status
                # instead (LifeShare's PR check on generated content). Without this such a PR looked unchecked, not failed.
                checks = self._statuses(repo, sha)
            out["checks"][(repo, sha)] = checks
            checked.add(sha)
            for c in checks:
                # A failed check's annotations, once per check run (they never change): "No result" = needs a re-run.
                if c.get("conclusion") in FAILED and c.get("id") and c["id"] not in self.no_result \
                        and (c.get("output") or {}).get("annotations_count"):
                    notes = self._try(f"repos/{repo}/check-runs/{c['id']}/annotations?per_page=30") or []
                    self.no_result[c["id"]] = any(NO_RESULT.search(str(n.get("message") or "") + " " + str(n.get("title") or ""))
                                                  for n in notes if isinstance(n, dict))
                    # what the failure says, for its cause and its problem (rules.classify): test names and a little text
                    self.fail_notes[c["id"]] = (rules.test_names(notes), " ".join(
                        str(n.get("message") or "")[:300] for n in notes if isinstance(n, dict))[:1500])
                if c.get("conclusion") in FAILED + ("cancelled",) and isinstance(c.get("id"), int) and c.get("source") != "status" \
                        and c["id"] not in self.fail_jobs and "/actions/" in str(c.get("details_url") or c.get("html_url") or "/actions/"):
                    # the job behind a failed check, once (a finished job never changes): its failed step and machine
                    j = self._try(f"repos/{repo}/actions/jobs/{c['id']}", quiet_for=86400) or {}
                    step = next((st.get("name") for st in j.get("steps") or [] if st.get("conclusion") in FAILED), "")
                    self.fail_jobs[c["id"]] = {"step": _text(step, 120), "runner": _text(j.get("runner_name"), 80)}
        # Pushes to agent branches (claude/..., codex/...): what the Agents card shows before a PR exists. One call, ETag.
        events = self._try(f"repos/{repo}/events?per_page=100") or []
        out["pushes"] = []
        for e in events:
            if not isinstance(e, dict) or e.get("type") != "PushEvent":
                continue
            ref = str((e.get("payload") or {}).get("ref") or "")
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
            if _agent_of(branch, ""):
                out["pushes"].append({"branch": _text(branch, 120), "at": _text(e.get("created_at"), 30),
                                      "actor": _text((e.get("actor") or {}).get("login"), 60)})
        # Agent branches whose PR was merged or closed: their work is done, so they leave the Agents card.
        closed = self._try(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=50") or []
        out["closed"], out["merged"] = [], []
        for p in closed:
            branch = str((p.get("head") or {}).get("ref") or "")
            if isinstance(p, dict) and p.get("closed_at") and _agent_of(branch, ""):
                out["closed"].append({"branch": _text(branch, 120), "at": _text(p.get("closed_at"), 30), "number": p.get("number")})
            if isinstance(p, dict) and p.get("merged_at"):
                out["merged_all"].append(p)
            if isinstance(p, dict) and p.get("merged_at") and _age(p["merged_at"]) < pipeline.MERGED_HOURS * 3600:
                out["merged"].append({k: p.get(k) for k in ("number", "title", "html_url", "merged_at", "base", "head", "labels")})
        # Releases: the last step of a change's road. One call, ETag; only asked while something merged recently.
        releases = self._try(f"repos/{repo}/releases?per_page=5") if out["merged"] else []
        out["releases"] = [r for r in releases or [] if isinstance(r, dict) and not r.get("draft")]
        issues = self._try(f"repos/{repo}/issues?state=open&sort=updated&direction=desc&per_page=50") or []
        out["issues"] = [i for i in issues if "pull_request" not in i]
        week = time.time() - 7 * 86400
        names = ({str((p.get("base") or {}).get("ref") or "") for p in prs if isinstance(p, dict)}
                 | {str((p.get("base") or {}).get("ref") or "") for p in out["merged_all"] if _ts(p.get("merged_at")) > week}
                 | set(self.bases_conf.get(repo.lower(), []))) - {""}
        out["bases"] = {b: info for b in sorted(names)[:8] if (info := self._read_base(repo, b)) is not None}
        self._read_status_issue(repo)

    def _base_get(self, path, **kwargs):
        try:
            return self._try(path, **kwargs)
        except GitHubError as exc:
            self.log(f"live: incomplete branch health while reading {path}: {exc}")
            return None

    def _base_pages(self, repo, sha, endpoint, field):
        """Read check runs or combined statuses pinned to the tip SHA. Never certify a truncated collection."""
        rows = []
        ids = set()
        path = f"repos/{repo}/commits/{sha}/{endpoint}?per_page=100"
        for page in range(1, 11):
            data = self._base_get(path if page == 1 else f"{path}&page={page}")
            if not isinstance(data, dict) or not isinstance(data.get(field), list):
                return rows, False
            if field == "statuses" and data.get("sha") != sha:
                return rows, False
            batch = data[field]
            if any(not isinstance(c, dict) for c in batch):
                return rows, False
            if field == "check_runs" and any(c.get("head_sha") != sha for c in batch):
                return rows, False
            batch_ids = [c["id"] for c in batch if c.get("id") is not None]
            if ids.intersection(batch_ids) or len(batch_ids) != len(set(batch_ids)):
                return rows, False
            ids.update(batch_ids)
            rows.extend(batch)
            total = data.get("total_count")
            if len(batch) < 100 or (total is not None and len(rows) >= total):
                return rows, total is None or len(rows) == total
        return rows, False

    def _read_base(self, repo: str, branch: str) -> dict | None:
        """Resolve the tip once. Both sources and every page must describe that exact revision."""
        ref = urllib.parse.quote(branch, safe="")
        head = self._base_get(f"repos/{repo}/branches/{ref}", missing_ref=_MISSING_REF)
        if head is _MISSING_REF or (head and head.get("name", branch) != branch):
            return None
        sha = ((head or {}).get("commit") or {}).get("sha")
        if not sha:
            if branch in self.bases.get(repo, {}):
                return {"sha": "", "checks": [], "required": [], "reason": "GitHub could not confirm the current branch commit."}
            return None
        ruleset = self._base_get(f"repos/{repo}/rules/branches/{ref}", quiet_for=3600)
        classic = (head.get("protection") or {}).get("required_status_checks")
        if classic is None and head.get("protected") is not False:
            classic = self._base_get(f"repos/{repo}/branches/{ref}/protection/required_status_checks", quiet_for=3600)
        sources = rules.required_sources(ruleset, classic)
        known_rules = head.get("protected") is False or (isinstance(classic, dict) and isinstance(ruleset, list))
        runs, full_runs = self._base_pages(repo, sha, "check-runs", "check_runs")
        statuses, full_statuses = self._base_pages(repo, sha, "status", "statuses")
        # The retired Buildlane runner emitted this exact description with its private run-ID range.
        # Its optional target_url could make an obsolete result look like a live check of the same name.
        statuses = [st for st in statuses if not RETIRED_STATUS.fullmatch(str(st.get("description") or ""))]
        status_rows = self._statuses(repo, sha, {"statuses": statuses})
        checks = _base_evidence(runs, status_rows, sources)
        reason = ""
        if not known_rules:
            reason = "GitHub has not returned the full set of branch rules."
        elif not (full_runs and full_statuses):
            reason = "GitHub has not returned all check results for this commit."
        if not reason:
            self.store.base_checks(repo, branch, sha, checks, complete=True, required_sources=sources)
        notes = {}
        for c in checks:
            if c.get("conclusion") in FAILED and c.get("source") != "status" and isinstance(c.get("id"), int) \
                    and (c.get("output") or {}).get("annotations_count"):
                if c["id"] not in self.notes:
                    found = self._base_get(f"repos/{repo}/check-runs/{c['id']}/annotations?per_page=50")
                    if found is not None:
                        self.notes[c["id"]] = rules.test_names(found)
                notes[c.get("name")] = self.notes.get(c["id"], [])
        return {"sha": sha, "checks": checks, "required": list(sources), "sources": sources, "known_rules": known_rules, "reason": reason,
                "checking": any(c.get("status") != "completed" for c in checks), "notes": notes}

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
        except GitHubError as e:
            return self.runners.get(name), f"{name}: {e}"     # keep the last answer through a hiccup
        if data is None:
            if kind == "orgs":
                self.runner_note = ("The GitHub sign-in on this PC cannot list the organisation's own runners (that needs "
                                    "`gh auth refresh -h github.com -s admin:org`). Runners added to a repository are listed; "
                                    "others show up through their jobs.")
            return None, ""
        return data.get("runners") or [], ""

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
            repos, inventory_complete, stale = self.repos, self.inventory_complete, self.stale_counts
            counts = {repo: count for repo, (count, _) in self.pr_counts.items()}
            last_ok, error = self.last_ok, self.error
        projects = self.projects_now(repos)

        def block(kind, title, detail="", url="", since="", key="", repo=""):
            """`key` stays the same while the same thing is stuck (titles carry minutes that change), so a phone is
            told once; `repo` lets the page filter by project."""
            blockers.append({"kind": kind, "key": _text(key or f"{kind}:{title}", 200), "title": _text(title, 200), "detail": _text(detail),
                             "url": _web(url), "since": since, "repo": _text(repo, 140)})

        # GitHub feed itself
        feed_age = _age(last_ok, at) if last_ok else _age(self.started, at)
        if error:
            block("feed", "GitHub can't be read right now", error, since=last_ok, key="feed")
        elif feed_age > self.feed_stale_after():
            block("feed", "The GitHub feed stopped updating", f"Last good read {int(feed_age // 60)} min ago.", since=last_ok, key="feed")

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
                job = next((j for j in jobs if j.get("status") == "in_progress"), None)
                if job is None:
                    job = next((j for j in jobs if j.get("status") in ACTIVE), jobs[0] if jobs else {})
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
                if job.get("status") == "in_progress" and re.match(r"^Build and test(?:\s*\(|$)", row["step"], re.I):
                    row["attempt"] = r.get("run_attempt") or 1
                    start = _ts(job.get("started_at"))
                    row["progress"] = {"eta": self.store.job_remaining(repo, row["job"], prn, max(0, at - start))
                                       if start else {}}
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
        self._reservations(at, machines_out, runs_out)

        prs_out = []
        red_checks: list = []
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
                if not row["draft"]:
                    for c in failed + rerun:      # why each is red is decided below, once base health is known
                        red_checks.append((row, c, cr_all := checks_by.get((repo, sha)) or []))
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
        bases_out = self.base_now(prs_out, prs_by, issues_out, at)
        problems = self._problems(red_checks, bases_out, prs_out)
        for g in problems:
            if g["cause"] == "own":       # only a real bug in a PR needs someone; the rest is shown, not counted
                first = g["prs"][0]
                block("check", g["title"], f"{g['repo']} · " + "; ".join(f"#{p['number']} {p['title']}" for p in g["prs"][:3]),
                      first["url"], g["since"], f"problem:{g['key']}", g["repo"])
        for h in bases_out:
            if h.get("state") == "red":
                fix = ", ".join(f"#{f['number']}" for f in h.get("fixes") or [])
                block("base", f"{h['branch']} is red: {', '.join(x['name'] for x in h.get('failing') or [])[:120]}",
                      f"{h['repo']} · " + (f"fix in line: {fix}" if fix else "no fix in line yet") +
                      (f" · {h['held']} PRs held red by it" if h.get("held") else ""), "", h.get("since") or "",
                      f"base:{h['repo']}:{h['branch']}", h["repo"])
        flow_out = self.flow_now(prs_out, at)
        for repo, f in ((flow_out or {}).get("repos") or {}).items():
            v = f.get("verdict") or {}
            if v.get("state") == "stopped":
                last = (f.get("last") or [{}])[0]
                block("flow", f"Nothing has merged in {repo.split('/')[-1]} for {_ago_text(f.get('since_merge') or 0)}",
                      f"{repo} · {v.get('why') or ''}", since=now_iso_at(last["at"]) if last.get("at") else "", key=f"flow:{repo}", repo=repo)
        rank = {"feed": 0, "base": 1, "machine": 2, "disk": 3, "stuck": 3, "check": 4, "runner": 5, "flow": 6, "label": 7}
        try:
            for extra in (self.extra_blockers() if self.extra_blockers else []):
                block(**extra)
        except Exception as e:              # a helper must never take the page down
            self.log(f"live: extra blockers failed: {type(e).__name__}: {e}"[:300])
        blockers.sort(key=lambda b: (rank.get(b["kind"], 9), -_ts(b["since"])))
        project_of = {r.lower(): p["name"] for p in projects if len(p["repos"]) > 1 for r in p["repos"]}
        for b in blockers:      # alerts group by project: one notification for a project's repositories
            if project_of.get(b["repo"].lower()):
                b["project"] = project_of[b["repo"].lower()]
        try:
            alerts_out = self.alerts.evaluate(blockers, at)
        except Exception as e:      # the page still lists every blocker
            self.log(f"live: alerts: {type(e).__name__}: {e}")
            alerts_out = {"firing": [], "pending": [], "cleared": []}
        state = {a["key"]: ("firing", a) for a in alerts_out["firing"]}
        state.update({a["key"]: ("pending", a) for a in alerts_out["pending"]})
        for b in blockers:      # what the page shows beside each: waiting to fire, fired, snoozed
            st, a = state.get(b["key"], ("", {}))
            b["alert"] = {"state": st or "off", "group": a.get("group") or alertsmod.group_of(b),
                          **({"fires_in": a["fires_in"]} if "fires_in" in a else {}),
                          **({"snoozed_until": a["snoozed_until"]} if a.get("snoozed_until") else {})}
        agents = self._agents(at, prs_out, runs_out, issues_out)
        changes = self._changes(at, prs_by, checks_by, runs_by, jobs_by)
        self._link_activities(machines_out, changes)
        active_runs = [r for r in runs_out if r["status"] in ACTIVE]
        # Everything building, checking, testing, merging or importing right now, on any machine, in one list.
        building = [{**a, "machine": m["name"]} for m in machines_out for a in m["activities"]
                    if a["kind"] in BUILD_KINDS or "progress" in a]
        building.sort(key=lambda a: _ts(a.get("since")) or 0)
        return {
            "at": now_iso(), "fresh": {"github": last_ok, "tried": self.last_try, "every": round(self.interval),
                                       "error": _text(error), "remaining": self.gh.remaining,
                                       "free": self.gh.free, "calls": self.gh.calls, "runner_note": _text(self.runner_note, 400)},
            "schema": SCHEMA,
            "repos": [{"name": r["full_name"], "url": r["url"]} for r in repos],
            "projects": projects,
            "open_pr_counts": counts,
            "pr_count_status": {"partial": bool(stale) or not inventory_complete,
                                "inventory_complete": inventory_complete, "stale": dict(stale)},
            "counts": {"machines": len(machines_out), "busy": sum(1 for m in machines_out if m["busy"]),
                       "runs": len(active_runs), "queued": sum(1 for r in active_runs if r["status"] != "in_progress"),
                       "prs": sum(counts.values()) if counts or (inventory_complete and not repos) else None,
                       "issues": len(issues_out), "blockers": len(blockers), "parked": len(parked), "building": len(building),
                       "agents": sum(1 for a in agents if a["active"]), "agents_idle": sum(1 for a in agents if not a["active"]),
                       "changes": len(changes), "changes_moving": sum(1 for c in changes if c["status"] in ("running", "queued"))},
            "flow": flow_out,
            "alerts": {k: v[:60] for k, v in alerts_out.items()},
            "bases": bases_out,
            "problems": problems[:40], "causes": rules.summary(problems),
            "queue": self._queue(at, runners_out, runs_out, jobs_by, machines_out),
            "agents": [a for a in agents if a["active"]][:40],     # working now; the rest is only counted
            "changes": changes[:60],
            "building": building[:30],
            "blockers": blockers[:40], "parked": parked[:40], "machines": machines_out, "runners": runners_out,
            "runs": runs_out[:40], "prs": prs_out[:80], "issues": issues_out[:60],
        }

    def _record(self, got: dict) -> None:
        """Keep what this poll saw (store.py): merged PRs, finished runs, started and finished jobs."""
        for repo, g in got.items():
            self.store.merges(repo, g.get("merged_all") or [], _agent_of)
            self.store.runs(repo, g.get("runs") or [])
            for run in g.get("runs") or []:
                self.store.jobs(repo, run, (g.get("jobs") or {}).get(run.get("id")) or [])
        self.store.prune()
        for cache in (self.no_result, self.fail_notes, self.fail_jobs, self.notes):
            while len(cache) > 4000:      # oldest first (insertion order): finished checks never change, so re-asking is rare
                cache.pop(next(iter(cache)))
        self._flow_cache = (0.0, {})
        self._base_cache = (0.0, None)

    def note_cancel(self, repo: str, run: int, who: str) -> None:
        """A cancel pressed in Fletchery (app.py): the surest "who cancelled"."""
        self.cancels[(repo, int(run))] = {"how": "fletchery", "who": _text(who, 80)}

    def _queue(self, at: float, runners_out: list, runs_out: list, jobs_by: dict, machines_out: list) -> dict:
        """The Queue tab (rules.py): one row per pool of runners (same custom labels) with its workers, what runs and
        what waits in order with an estimated start and finish, waits and use over the last day; every run cancelled in
        the last day with who or what cancelled it; and what waits for a machine's hold."""
        run_by_id = {r["id"]: r for r in runs_out}
        pools: dict = {}
        for r in runners_out:
            name = rules.pool_of(r["labels"], GENERIC_LABELS)
            p = pools.setdefault(name, {"name": name, "labels": sorted(set(r["labels"])), "runners": [], "running": [], "waiting": []})
            p["runners"].append(r)
        waiting = []
        for rid, jobs in jobs_by.items():
            run = run_by_id.get(rid)
            if not run or run["status"] not in ACTIVE:
                continue
            for j in jobs:
                if j.get("status") in ("queued", "waiting", "pending", "requested"):
                    waiting.append((run, j))
        for run in runs_out:          # a queued run whose jobs were not read yet
            if run["status"] in ACTIVE and run["status"] != "in_progress" and not jobs_by.get(run["id"]):
                waiting.append((run, {"name": run["name"], "labels": run.get("labels") or [], "created_at": run["created"]}))
        waiting.sort(key=lambda x: _ts(x[1].get("created_at") or x[0]["created"]))
        for run, j in waiting:
            want = {str(x).lower() for x in j.get("labels") or []} - GENERIC_LABELS
            fits = [p for p in pools.values() if want and want <= {x.lower() for x in p["labels"]}]
            pool = min(fits, key=lambda p: len(p["labels"])) if fits else pools.setdefault(
                rules.pool_of(j.get("labels") or [], GENERIC_LABELS), {"name": rules.pool_of(j.get("labels") or [], GENERIC_LABELS),
                                                                       "labels": sorted(want), "runners": [], "running": [], "waiting": []})
            est = self.store.job_seconds(run["repo"], run["name"], str(j.get("name") or ""))
            pool["waiting"].append({"repo": run["repo"], "run": run["id"], "name": run["name"], "job": _text(j.get("name"), 120),
                                    "pr": run["pr"], "branch": run["branch"], "url": run["url"],
                                    "since": _text(j.get("created_at") or run["created"], 30), "est": est})
        out = []
        for p in pools.values():
            free = []
            for r in p["runners"]:
                if not r["online"]:
                    continue
                job = r.get("job")
                if not job:
                    free.append(0.0)
                    continue
                run = run_by_id.get(job.get("id")) or {}
                est = self.store.job_seconds(run.get("repo", ""), run.get("name", ""), run.get("job", ""))
                left = None if est is None else max(0.0, est - _age(run.get("started"), at))
                free.append(left)
                p["running"].append({"repo": run.get("repo", ""), "run": run.get("id"), "name": run.get("name", ""),
                                     "job": run.get("job", ""), "pr": run.get("pr"), "url": run.get("url", ""), "runner": r["name"],
                                     "since": run.get("started", ""), "left": None if left is None else round(left),
                                     "late": bool(est and _age(run.get("started"), at) > est * 1.5)})
            known_free = [x for x in free if x is not None]
            times = rules.schedule(known_free if len(known_free) == len(free) else [], [w["est"] for w in p["waiting"]])
            for w, (start, finish) in zip(p["waiting"], times):
                w["start_in"] = None if start is None else round(start)
                w["finish_in"] = None if finish is None else round(finish)
                w["waited"] = round(_age(w["since"], at))
            online = [r for r in p["runners"] if r["online"]]
            stats = self.store.pool_stats(set(p["labels"]), len(online) or len(p["runners"]), at)
            out.append({"name": p["name"], "labels": p["labels"], "workers": len(p["runners"]), "online": len(online),
                        "busy": sum(1 for r in online if r["busy"] or r.get("job")), "offline": len(p["runners"]) - len(online),
                        "machines": sorted({r["machine"] for r in p["runners"]}), "running": p["running"][:10],
                        "waiting": p["waiting"][:30], "waiting_n": len(p["waiting"]), **stats})
        out.sort(key=lambda p: (-p["waiting_n"], -p["busy"], p["name"]))
        cancelled = []
        by_flow: dict = {}
        for r in runs_out:
            by_flow.setdefault((r["repo"], r["name"], r["pr"] or r["branch"]), []).append(r)
        for r in runs_out:
            if r["conclusion"] != "cancelled" or _age(r["updated"], at) > 86400:
                continue
            why = dict(self.cancels.get((r["repo"], r["id"])) or {})
            if not why.get("how"):
                newer = [o for o in by_flow.get((r["repo"], r["name"], r["pr"] or r["branch"]), [])
                         if o["id"] != r["id"] and r["created"] < o["created"] <= _iso_after(r["updated"], 120)]
                why = {"how": "replaced", "who": ""} if newer else {"how": "unknown", "who": ""}
            cancelled.append({"repo": r["repo"], "run": r["id"], "name": r["name"], "pr": r["pr"], "branch": r["branch"],
                              "url": r["url"], "at": r["updated"], "how": why["how"], "who": why.get("who", "")})
        holds = [{**a, "machine": m["name"]} for m in machines_out for a in m["activities"] if a.get("kind") == "queued"]
        return {"pools": out, "cancelled": cancelled[:30], "holds": holds[:20],
                "waiting": sum(p["waiting_n"] for p in out) + len(holds)}

    def _problems(self, red_checks: list, bases_out: list, prs_out: list) -> list:
        """Why each red PR check is red (rules.classify), grouped into problems (rules.problems); each PR row gets its
        cause too."""
        base_failing = {(h["repo"], h["branch"]): {rules._norm(f["name"]): f.get("tests") or [] for f in h.get("failing") or []}
                        for h in bases_out if h.get("state") == "red"}
        failures = []
        for row, c, cr_all in red_checks:
            cid = c.get("id") if isinstance(c.get("id"), int) else 0
            tests, text = self.fail_notes.get(cid, ([], ""))
            job = self.fail_jobs.get(cid, {})
            retried_ok = any(o.get("name") == c.get("name") and o.get("conclusion") == "success"
                             and str(o.get("completed_at") or "") < str(c.get("completed_at") or "~") for o in cr_all)
            cause = rules.classify({"name": c.get("name"), "conclusion": c.get("conclusion"), "no_result": self.no_result.get(cid)},
                                   failed_step=job.get("step", ""), tests=tests, text=text, retried_ok=retried_ok,
                                   base_failing=base_failing.get((row["repo"], row["base"])), machine_patterns=self.machine_patterns)
            failures.append({"repo": row["repo"], "number": row["number"], "title": row["title"], "url": _web(c.get("html_url")) or row["url"],
                             "base": row["base"], "check": _text(c.get("name"), 80), "cause": cause, "tests": tests,
                             "since": c.get("completed_at") or "", "runner": job.get("runner", ""), "job": cid})
        by_pr: dict = {}
        for f in failures:
            by_pr.setdefault((f["repo"], f["number"]), []).append(f["cause"])
        for p in prs_out:
            causes = by_pr.get((p["repo"], p["number"]))
            if causes:
                p["checks"]["cause"] = min(causes, key=rules.CAUSES.index)
        return rules.problems(failures)

    def base_now(self, prs_out: list, prs_by: dict, issues_out: list, at: float) -> list:
        """Every watched base branch's health (rules.base_health), red ones first; worked out at most every 15 s."""
        when, kept = getattr(self, "_base_cache", (0.0, None))
        if kept is not None and at - when < 15:
            return kept
        with self._lock:
            bases = {r: dict(b) for r, b in self.bases.items()}
        bodies = {(repo, p.get("number")): str(p.get("body") or "")[:4000] for repo, ps in prs_by.items() for p in ps}
        out = []
        for repo, branches in bases.items():
            prs = [{**p, "body": bodies.get((repo, p["number"]), "")} for p in prs_out if p["repo"] == repo]
            issues = [i for i in issues_out if i["repo"] == repo]
            for branch, info in branches.items():
                try:
                    h = rules.base_health(self.store, repo, branch, required=info.get("required"), prs=prs, issues=issues,
                                          notes=info.get("notes"), checking=info.get("checking"), fix_labels=self.fix_labels,
                                          tip_sha=info.get("sha"), tip_checks=info.get("checks"), required_sources=info.get("sources"),
                                          red_issue_labels=self.red_issue_labels, now=at)
                except Exception as e:      # one branch's history never costs the feed
                    self.log(f"live: base health {repo} {branch}: {type(e).__name__}: {e}")
                    h = None
                if info.get("reason") and (not h or h["state"] != "red" or not (info.get("known_rules") or info["required"])):
                    h = {"repo": repo, "branch": branch, "sha": info["sha"][:12], "state": "checking", "checking": True,
                         "required": bool(info["required"]), "waiting": (h or {}).get("waiting", []), "reason": info["reason"]}
                if h:
                    out.append(h)
        out.sort(key=lambda h: (h["state"] != "red", -(h.get("for") or 0), h["repo"], h["branch"]))
        self._base_cache = (at, out[:20])
        return out[:20]

    def projects_now(self, repos=None) -> list:
        """[{"name", "repos"}]: the profile's projects (only repositories being watched), then one per other repository."""
        watched = {r["full_name"].lower(): r["full_name"] for r in (self.repos if repos is None else repos)}
        out, used = [], set()
        for name, repos in self.projects.items():
            mine = [watched[r.lower()] for r in repos if r.lower() in watched]
            if mine:
                out.append({"name": name, "repos": mine})
                used |= {r.lower() for r in mine}
        out += [{"name": full.split("/")[-1], "repos": [full]} for low, full in sorted(watched.items()) if low not in used]
        return out

    def feed_stale_after(self) -> float:
        # A multi-repository poll can itself take longer than the delay between polls.
        return max(180, self.interval * 4)

    def flow_now(self, prs_out: list, at: float) -> dict:
        """The Flow strip (flow.py), worked out at most every 15 s."""
        when, kept = self._flow_cache
        if kept and at - when < 15:
            return kept
        waiting: dict = {}
        for p in prs_out:
            # On its way: not a draft, not failed, and touched in the last FLOW_FRESH_DAYS. A PR left open for weeks in
            # a quiet repository is not a stopped pipeline (2026-11-20: four of sixteen blockers were such repositories).
            stale = _ts(p.get("updated")) and at - _ts(p.get("updated")) > FLOW_FRESH_DAYS * 86400
            if not p["draft"] and p["checks"]["state"] not in ("failed",) and not stale:
                waiting[p["repo"]] = waiting.get(p["repo"], 0) + 1
        try:
            kept = self.flow.compute([r["full_name"] for r in self.repos], waiting, at,
                                     projects={p["name"]: p["repos"] for p in self.projects_now() if len(p["repos"]) > 1},
                                     build_queues={r: q for r, q in self.pr_build_queues.items()
                                                   if q and at - q["at"] <= self.feed_stale_after()})
        except Exception as e:
            self.log(f"live: flow: {type(e).__name__}: {e}")
            kept = {}
        self._flow_cache = (at, kept)
        return kept

    def _runner_machine(self, runner_name: str) -> str:
        return self.display(self.machine_of(runner_name)) if runner_name else ""

    def _backfill(self, repo: str) -> None:
        """Teach the step times (pipeline.History) from a repository's last successful pull-request runs, once per
        start, when it knows nothing for that repository yet: a fresh install, or samples dropped as unreliable
        (#235), would otherwise show "no usual time yet" until enough checks finish again. One list call plus one
        jobs call per run, at most BACKFILL_RUNS."""
        try:
            # Green and red runs: a build that failed in its tests took as long as a green one, and on a busy project
            # most long builds end red (pipeline._build_span). Cancelled and skipped runs teach nothing.
            got = self.gh.get(f"repos/{repo}/actions/runs?status=completed&per_page=100")
            runs = [r for r in (got or {}).get("workflow_runs") or [] if isinstance(r, dict) and isinstance(r.get("id"), int)
                    and r.get("conclusion") in ("success", "failure")]
            bases = set((self.bases.get(repo) or {}).keys())
            learned = 0
            for r in runs:
                pr_run = str(r.get("event") or "").startswith("pull_request")
                staging = r.get("event") == "push" and r.get("head_branch") in bases
                if not (pr_run or staging) or learned >= BACKFILL_RUNS:
                    continue
                jobs = [j for j in (self.gh.get(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=50") or {}).get("jobs") or []
                        if isinstance(j, dict)]
                learned += 1
                self.store.runs(repo, [r])
                self.store.jobs(repo, r, jobs)
                if staging:
                    pipeline.learn_staging(self.history, repo, r, jobs)
                    continue
                kind = self.rules.kind(r.get("display_title"), (), r.get("head_branch"))
                number = ((r.get("pull_requests") or [{}])[0] or {}).get("number")
                pipeline.learn_checks(self.history, repo, kind, f"run{r['id']}",
                                      [j for j in jobs if self.rules.is_quick(j.get("name"))],
                                      [j for j in jobs if not self.rules.is_quick(j.get("name"))],
                                      {j.get("id"): j for j in jobs}, self._runner_machine, number)
            self.history.save()
            if learned:
                self.log(f"live: learned step times for {repo} from {learned} past runs")
        except Exception as e:      # a nicety: the feed goes on, and times are learned as checks finish
            self.log(f"live: could not learn past step times for {repo}: {type(e).__name__}: {e}"[:300])

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
                                      [c for c in cr if not self.rules.is_quick(c.get("name"))], jobs, self._runner_machine,
                                      p.get("number"))
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

    def _full_in(self, host: str, drive: str, at: float):
        """Seconds until this drive is full at its recent rate (rules.disk_forecast), or None."""
        try:
            rows = self.store.query("SELECT at, free_gb FROM disks WHERE machine=? AND drive=? AND at > ?",
                                    (host, drive, at - rules.FORECAST_WINDOW))
            return rules.disk_forecast(rows, at)
        except Exception as e:
            self.log(f"live: disk forecast: {type(e).__name__}: {e}")
            return None

    def _release_estimate(self, at, machine, holder, runs):
        """Prefer observed release times. Otherwise label a matching job or step as a proxy for release."""
        seconds, n = self.history.remaining_exact(_reservation_key(machine["host"], holder["title"]), _age(holder["since"], at))
        if seconds is not None:
            return {"seconds": seconds, "samples": n, "basis": "reservation"}
        eta = (holder.get("progress") or {}).get("eta") or {}
        if eta.get("seconds") is not None:
            return {**eta, "basis": "job", "url": holder.get("url")}
        candidates = [r for r in runs if r["runner"] in machine["runners"] and r["status"] == "in_progress"
                      and r["event"] == "workflow_dispatch" and r["step_started"] and r["step"]
                      and (r["id"] == holder["run"] if holder.get("run") else
                           _names_holder(_workload(r["title"]), holder["title"]))
                      and 0 < _ts(r["step_started"]) <= _ts(holder["since"]) + 5]
        if len(candidates) != 1:
            return {}
        run = candidates[0]
        key = (run["repo"], run["id"], run["runner"], run["step"])
        elapsed = _age(run["step_started"], at)
        with self._lock:
            _, samples = self._holder_times.get(key, (0, ()))
        longer = [(duration, result) for duration, result in samples if duration > elapsed]
        detail = {"url": run["url"], "step": run["step"]}
        if longer:
            detail.update(seconds=round(statistics.median(d for d, _ in longer) - elapsed), samples=len(longer),
                          basis="step", failed=sum(result == "failure" for _, result in longer))
        return detail

    def _reservations(self, at, machines, runs):
        for m in machines:
            tickets = sorted((a for a in m["activities"] if "position" in a), key=lambda a: a["position"])
            if not m["online"]:
                continue
            # The reporter knows who is waiting even during the first GitHub poll after startup.
            for ticket in tickets:
                if (not ticket.get("run") and _ts(ticket["since"]) and
                        re.match(r"^(?:local run )?PR check #\d+ \(GitHub run \d+\)", ticket["title"], re.I)):
                    ticket["progress"] = {"waiting_machine": True, "phase": {
                        "label": "Waiting for the machine reservation", "since": ticket["since"]}}
            if not any((a.get("progress") or {}).get("waiting_machine") for a in tickets):
                continue
            holder = next((a for a in m["activities"] if a.get("reservation_holder")), None)
            release = self._release_estimate(at, m, holder, runs) if holder and holder.get("alive") and _ts(holder["since"]) else {}
            delay = release.get("seconds")
            for ticket in tickets:
                progress = ticket.get("progress") or {}
                if progress.get("waiting_machine"):
                    info = {"machine": m["name"], "ahead": ticket["position"] - 1, "start_in": delay, "release": release}
                    if holder:
                        info.update(holder=holder["title"], since=holder["since"], dead=holder.get("alive") is False)
                    progress["reservation"] = info
                if delay is not None:
                    duration, _ = self.history.remaining_exact(_reservation_key(m["host"], ticket["title"]), 0)
                    delay = delay + duration if duration is not None else None

    def _machines(self, at: float, runners: list, block) -> list:
        with self._lock:
            reports = dict(self.reports)
        by = {}
        phases = {}
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
                acts.append({"kind": kind_of(purpose), "title": purpose, "since": _text(lease.get("since"), 40),
                             "reservation_holder": True, "alive": lease.get("alive"),
                             "detail": "holds the machine" + ("" if lease.get("alive", True) else " (its process is gone)")})
                output = lease.get("last_output")
                if (age <= REPORT_STALE and lease.get("alive") and isinstance(output, dict) and output.get("label")
                        and 0 < _ts(lease.get("since")) <= _ts(output.get("at")) <= at):
                    acts[-1]["last_output"] = {"label": _scrub(output.get("label"), 80), "at": _text(output.get("at"), 40)}
                    upload = _upload_progress(output.get("upload"), at - _ts(output["at"]))
                    if upload is not None:
                        acts[-1]["last_output"]["upload"] = upload
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
            for position, q in enumerate((d.get("queue") or [])[:10], 1):
                if not isinstance(q, dict):
                    continue
                purpose = _scrub(q.get("purpose"), 200)
                acts.append({"kind": "queued", "title": purpose, "since": _text(q.get("since"), 40),
                             "position": position, "detail": "waiting for the machine"})
                if _age(q.get("since"), at) > LIMITS["queue_wait"]:
                    block("stuck", f"{self.display(host)}: \"{purpose}\" has waited {int(_age(q.get('since'), at) // 60)} min in the queue", since=q.get("since") or "", key=f"queue:{host}:{q.get('since')}:{purpose}")
            for a in (d.get("activities") or [])[:10]:
                if isinstance(a, dict) and a.get("kind") == "pr-phase":
                    if age <= REPORT_STALE and a.get("detail"):
                        phases[host.lower(), _text(a.get("title")).lower()] = {
                            "label": _scrub(a["detail"], 220), "since": _text(a.get("since"), 40)}
                    continue
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
            forecasts = {}
            for x in (stats.get("disks") or [])[:4]:
                if not (isinstance(x, dict) and isinstance(x.get("free_gb"), (int, float))):
                    continue
                drive = _text(x.get("name"), 8)
                full_in = self._full_in(host, drive, at)
                if full_in is None:
                    continue
                forecasts[drive] = round(full_in)
                if full_in < rules.FORECAST_HORIZON and x["free_gb"] >= self.disk_low:   # already low: flagged above
                    block("disk", f"{self.display(host)}: {drive} will be full in about {_ago_text(full_in)}",
                          f"{x['free_gb']:.0f} GB free and falling. Clear old build output or worktrees before it runs out.",
                          key=f"diskfc:{host}:{drive}")
            restart = self.restarts.view(name)
            if restart and restart["overdue"]:
                block("machine", f"{self.display(host)} was told to restart {_ago_text(restart['age'])} ago and is not back",
                      "It may be stuck shutting down or waiting at the sign-in screen. It needs someone at the machine.",
                      since=restart["requested"], key=f"restart:{host}:{restart['id']}")
            ready = d.get("after_restart") if isinstance(d.get("after_restart"), dict) else {}
            silent = age > REPORT_STALE
            if silent:   # stays listed (and flagged) until it reports again or a week has passed
                block("machine", f"{self.display(host)} stopped reporting",
                      f"Last report {_ago_text(age)} ago. Its reporter task may have stopped, or the machine is off.",
                      since=rep["at"], key=f"machine:{host}")
            by[host.lower()] = {"name": self.display(host), "host": host, "online": not silent, "last_report": rep["at"],
                                "busy": bool(acts and any(a["kind"] not in ("queued",) for a in acts)) or bool(procs.get("runner")),
                                "stats": {k: stats.get(k) for k in ("cpu", "ram_used_gb", "ram_total_gb") if isinstance(stats.get(k), (int, float))},
                                "disks": [{"name": _text(x.get("name"), 8), "free_gb": x.get("free_gb"),
                                           **({"total_gb": x["total_gb"]} if isinstance(x.get("total_gb"), (int, float)) else {}),
                                           **({"full_in": forecasts[_text(x.get("name"), 8)]} if _text(x.get("name"), 8) in forecasts else {})}
                                          for x in (stats.get("disks") or [])[:4]
                                          if isinstance(x, dict) and isinstance(x.get("free_gb"), (int, float))],
                                "disk_guard": guard,
                                "procs": {k: int(v) for k, v in procs.items() if isinstance(v, (int, float)) and v}, "activities": acts,
                                "runners": [], "version": _text(d.get("version"), 20),
                                "key": _text(name, 63), "boot": _text(d.get("boot"), 30), "can_restart": bool(d.get("boot")),
                                "after_restart": {k: ready.get(k) for k in ("signs_in", "guard") if isinstance(ready.get(k), bool)},
                                "restart": restart}
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
                job = r["job"]
                lease = next((a for a in m["activities"] if f"(github run {job['id']})" in a["title"].lower()), None)
                if lease is not None:
                    lease.update(repo=job["repo"], pr=job["pr"], run=job["id"], url=job["url"])
                else:
                    m["activities"].insert(0, {"kind": "check" if "progress" in job or "check" in job["title"].lower() else "working",
                                               "title": r["job"]["title"], "since": r["job"]["started"],
                                               "detail": (f"step: {r['job']['step']} · " if r["job"]["step"] else "") + r["job"]["repo"],
                                               "url": r["job"]["url"], "repo": r["job"]["repo"], "pr": r["job"]["pr"],
                                               "run": r["job"]["id"]})
                    lease = m["activities"][0]
                lease.update(job_name=job["name"], step=job["step"], step_started=job["step_started"],
                             steps_done=job["steps_done"], steps_total=job["steps_total"])
                if "progress" in job:
                    key = f"{job['repo']}@{job['id']}/{job['attempt']}".lower()
                    phase = phases.get((m["host"].lower(), key))
                    if lease["kind"] == "queued" and m["online"] and _ts(job["step_started"]) <= _ts(lease["since"]):
                        job["progress"].pop("eta", None)
                        job["progress"]["waiting_machine"] = True
                        job["progress"]["phase"] = {"label": "Waiting for the machine reservation", "since": lease["since"]}
                    else:
                        observed_since = _ts(job["step_started"])
                        if phase and 0 < observed_since <= _ts(phase["since"]) <= at + 5:
                            job["progress"]["phase"] = phase
                            observed_since = _ts(phase["since"])
                        if lease.get("last_output") and 0 < observed_since <= _ts(lease["last_output"]["at"]):
                            job["progress"]["last_output"] = lease["last_output"]
                    lease["progress"] = job["progress"]
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
