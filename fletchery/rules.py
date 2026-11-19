"""The rules: what counts as red, broken, stuck or late. Small pure functions over the live feed (devwatch.py) and the
history store (store.py), each with tests (test_rules.py). Every project-specific word (fix labels, issue labels) comes
from the profile, so a new project needs no change here.

Base health (Horde's "build health", for a base branch such as Test-Staging or main):
- A commit on the base branch is **red** when one of its checks ended failure, error or timeout (only the branch's
  required checks when GitHub says which those are), **green** when none did and at least one passed, else unknown
  (still running, or only cancelled).
- **Red since** is when the first commit of the current red streak was first seen red, walking back to the last green
  one, from the store, so a backend restart does not reset it.
- **Suspects** are the pull requests merged after the last green commit and up to the first red one.
- **Fix in line** is every open PR into that branch labelled with a fix label, or naming an open base-red issue.
- **Held red** counts open PRs into the branch whose failing checks include one that fails on the branch itself.

Standard library only."""
from __future__ import annotations

import re
import time

RED = ("failure", "error", "timeout")
OK = ("success", "skip")
FIX_LABELS = ("tip-fix", "fix-base", "base-fix", "hotfix")
RED_ISSUE_LABELS = ("tip-red", "base-red", "broken-base", "red-base")
WINDOW_DAYS = 14


def _iso(t):
    from store import iso
    return iso(t)


def commit_states(store, repo: str, branch: str, required=None, now: float | None = None) -> list:
    """The branch's commits, newest first: [{"sha", "at", "first", "state", "red": [names], "results": {name: result}}]."""
    now = now or time.time()
    rows = store.query("SELECT sha, name, result, at FROM base_checks WHERE repo=? AND branch=? AND at > ? ORDER BY at DESC",
                       (repo, branch, now - WINDOW_DAYS * 86400))
    commits: dict = {}
    for sha, name, result, at in rows:
        c = commits.setdefault(sha, {"sha": sha, "at": at, "first": at, "results": {}, "ats": {}})
        c["first"] = min(c["first"], at)
        if name not in c["results"]:                   # newest result per check (a re-run replaces the failure)
            c["results"][name], c["ats"][name] = result, at
    want = {r.lower() for r in required or ()}
    out = []
    for c in sorted(commits.values(), key=lambda c: -c["at"]):
        res = {n: r for n, r in c["results"].items() if not want or n.lower() in want}
        red = sorted(n for n, r in res.items() if r in RED)
        c["red"] = red
        c["red_at"] = min((c["ats"][n] for n in red), default=c["first"])
        c["state"] = "red" if red else "green" if any(r == "success" for r in res.values()) else "unknown"
        out.append(c)
    return out


def _mentions(text: str, numbers: set) -> bool:
    return any(int(n) in numbers for n in re.findall(r"#(\d{1,9})\b", text or ""))


def base_health(store, repo: str, branch: str, *, required=None, prs=(), issues=(), notes=None, checking: bool = False,
                fix_labels=FIX_LABELS, red_issue_labels=RED_ISSUE_LABELS, now: float | None = None) -> dict | None:
    """One base branch's health, or None when nothing is known yet.

    `prs` are the feed's PR rows ({"number", "title", "url", "base", "labels", "body", "auto_merge", "checks"}) and
    `issues` its issue rows, both for this repository. `notes` maps a failing check's name to the test names its
    annotations gave (shown as "which tests"). `checking`: the newest commit's checks are still running."""
    now = now or time.time()
    commits = commit_states(store, repo, branch, required, now)
    known = [c for c in commits if c["state"] != "unknown"]
    if not known:
        return None
    head = known[0]
    out = {"repo": repo, "branch": branch, "state": head["state"], "sha": head["sha"][:12], "checking": bool(checking),
           "required": bool(required)}
    if head["state"] != "red":
        out["green_at"] = _iso(head["at"])
        return out
    first, green = head, None
    for c in known[1:]:
        if c["state"] == "green":
            green = c
            break
        first = c
    out["since"] = _iso(first["red_at"])
    out["for"] = round(now - first["red_at"])
    out["failing"] = [{"name": n, "tests": list((notes or {}).get(n) or [])[:6]} for n in head["red"]][:8]
    out["first_bad"] = first["sha"][:12]
    out["last_green"] = green["sha"][:12] if green else ""
    out["suspects"] = suspects(store, repo, branch, first, green)
    red_labels = {x.lower() for x in red_issue_labels}
    branch_in_title = re.compile(r"(?<![\w./-])" + re.escape(branch) + r"(?![\w./-])")
    red_issues = [i for i in issues if any(str(lab).lower() in red_labels for lab in i.get("labels") or [])
                  and branch_in_title.search(str(i.get("title") or ""))]
    nums = {int(i["number"]) for i in red_issues if isinstance(i.get("number"), int)}
    fixes = []
    for p in prs:
        if p.get("base") != branch or p.get("draft"):
            continue
        labels = {str(x).lower() for x in p.get("labels") or []}
        if labels & {x.lower() for x in fix_labels} or (nums and _mentions(str(p.get("title") or "") + " " + str(p.get("body") or ""), nums)):
            fixes.append({"number": p.get("number"), "title": p.get("title"), "url": p.get("url"),
                          "state": (p.get("checks") or {}).get("state", ""), "auto_merge": bool(p.get("auto_merge"))})
    out["fixes"] = fixes[:5]
    out["issues"] = [{"number": i.get("number"), "title": i.get("title"), "url": i.get("url")} for i in red_issues][:3]
    failing = {n.lower() for n in head["red"]}
    out["held"] = sum(1 for p in prs if p.get("base") == branch and not p.get("draft")
                      and failing & {str(n).lower() for n in (p.get("checks") or {}).get("failed") or []})
    return out


def suspects(store, repo: str, branch: str, first: dict, green: dict | None) -> list:
    """PRs merged into `branch` after the last green commit, up to and including the first red one (Horde's suspects).
    Merge commits place them exactly; without them, merges up to the first red result after the last green one."""
    rows = store.query("SELECT number, title, author, agent, merged, sha FROM merges WHERE repo=? AND base=? AND merged > ? "
                       "ORDER BY merged DESC", (repo, branch, (green["first"] if green else first["first"]) - 7 * 86400))
    by_sha = {r[5]: r for r in rows if r[5]}
    hi = by_sha[first["sha"]][4] if first["sha"] in by_sha else first["first"]
    lo = by_sha[green["sha"]][4] if green and green["sha"] in by_sha else (green["first"] if green else hi - 6 * 3600)
    pick = [r for r in rows if lo < r[4] <= hi]
    if not pick and first["sha"] in by_sha:
        pick = [by_sha[first["sha"]]]
    return [{"number": r[0], "title": r[1], "author": r[2], "agent": r[3], "merged": _iso(r[4]),
             "first_bad": r[5] == first["sha"]} for r in pick][:6]


GENERIC_NOTE = re.compile(r"^(process completed with exit code|the job (was|has)|the operation was canceled|"
                          r"node\.js \d+ actions are deprecated|the `set-output`|##\[)", re.I)


def test_names(annotations: list) -> list:
    """The test or spec names a failed check's annotations give (title, else the message's first line), without
    GitHub's own generic lines ("Process completed with exit code 1")."""
    out = []
    for n in annotations or []:
        if not isinstance(n, dict) or n.get("annotation_level") not in (None, "failure"):
            continue
        lines = str(n.get("message") or "").strip().splitlines()
        text = re.sub(r"\s+", " ", str(n.get("title") or "").strip() or (lines[0] if lines else ""))[:100]
        if text and not GENERIC_NOTE.search(text) and text not in out:
            out.append(text)
    return out[:10]


def required_checks(branch_rules, protection) -> list:
    """The required checks of a branch, from repository rulesets (`rules/branches/<b>`, needs only read access) and
    classic branch protection (`protection/required_status_checks`, needs admin: often refused, then skipped)."""
    names = []
    for r in branch_rules if isinstance(branch_rules, list) else []:
        if isinstance(r, dict) and r.get("type") == "required_status_checks":
            for c in (r.get("parameters") or {}).get("required_status_checks") or []:
                if isinstance(c, dict) and c.get("context"):
                    names.append(str(c["context"]))
    if isinstance(protection, dict):
        names += [str(c) for c in protection.get("contexts") or [] if c]
        names += [str(c.get("context")) for c in protection.get("checks") or [] if isinstance(c, dict) and c.get("context")]
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen[:30]


# ---- Failure causes (proposal 2): why a PR's check is red, and which red checks share one problem ----
#
# Checked in this order; the first that fits wins:
#   cancelled  the check was cancelled (by a newer run, a person or an agent): it needs a re-run, not a fix
#   machine    the system broke, not the code (OpenTelemetry "error"): no verdict ("No result"), a startup failure,
#              a timeout with no failed step, a failed setup step (checkout, LFS, disk), or a known machine message
#   base       the same check fails on the PR's base branch right now (and, where both name tests, the same tests)
#   flaky      the same check failed and then passed on the same commit (Trunk's pass-on-retry)
#   own        anything else: a real bug in this PR
CAUSES = ("own", "base", "machine", "flaky", "cancelled")
CAUSE_WORDS = {"own": "Own bug", "base": "Base is red", "machine": "Machine problem", "flaky": "Flaky", "cancelled": "Cancelled"}
SETUP_STEP = re.compile(r"^(set up|check ?out|git |fetch|lfs|clean|free|disk|download|prepare|restore|cache|install|"
                        r"wait for (a )?(machine|lease|slot)|reserve|acquire)", re.I)
MACHINE_TEXT = (r"no result", r"runner .*(lost|shut ?down|offline|stopped)", r"lost communication", r"no space left",
                r"disk (is )?full", r"not enough (disk )?space", r"out of memory", r"git-lfs|lfs .*(fail|error)",
                r"the hosted runner", r"received a shutdown signal", r"machine (was )?busy")


def _norm(name: str) -> str:
    """'PR check / Compile and test' and 'Compile and test' are the same check."""
    return re.sub(r"\s+", " ", str(name or "").split(" / ")[-1]).strip().lower()


def _fp(text: str) -> str:
    """A fingerprint of a test or message: numbers, hashes and paths stripped, so the same failure matches itself."""
    t = re.sub(r"[0-9a-f]{7,40}", "#", str(text or "").lower())
    t = re.sub(r"\d+(\.\d+)?", "#", t)
    t = re.sub(r"[a-z]:\\[^\s]+|/[^\s]+/", "<path>", t)
    return re.sub(r"\s+", " ", t).strip()[:80]


def classify(check: dict, *, failed_step: str = "", tests=(), text: str = "", base_failing=None, retried_ok: bool = False,
             machine_patterns=MACHINE_TEXT) -> str:
    """One failed (or cancelled, or no-verdict) check's cause. `base_failing` maps the PR's base branch's failing
    checks (normalised name) to their test names, or is None when the base is not red."""
    concl = check.get("conclusion")
    if concl == "cancelled":
        return "cancelled"
    blob = " ".join([failed_step or "", text or "", str(check.get("why") or "")])
    if check.get("no_result") or concl == "startup_failure" or (failed_step and SETUP_STEP.search(failed_step)) \
            or (concl == "timed_out" and not failed_step) or any(re.search(p, blob, re.I) for p in machine_patterns):
        return "machine"
    if base_failing and _norm(check.get("name")) in base_failing:
        theirs = {_fp(t) for t in base_failing[_norm(check.get("name"))] or ()}
        mine = {_fp(t) for t in tests or ()}
        if not theirs or not mine or theirs & mine:
            return "base"
    if retried_ok:
        return "flaky"
    return "own"


def problems(failures: list) -> list:
    """Group failures into problems (Horde's issues): one per cause + check + first failing test (fingerprinted), or
    per base branch for "base", or per runner for "machine". Each failure is {"repo", "number", "title", "url", "base",
    "check", "cause", "tests", "since", "runner", "job"}. Own bugs first, then the rest, biggest first."""
    groups: dict = {}
    for f in failures:
        cause = f["cause"]
        if cause == "base":
            key = f"base:{f['repo']}:{f['base']}:{_norm(f['check'])}"
        elif cause == "machine":
            key = f"machine:{f.get('runner') or 'unknown'}:{_norm(f['check'])}"
        elif cause == "cancelled":
            key = f"cancelled:{f['repo']}"
        else:
            key = f"{cause}:{f['repo']}:{_norm(f['check'])}:{_fp((f.get('tests') or [''])[0])}"
        g = groups.setdefault(key, {"key": key, "cause": cause, "words": CAUSE_WORDS[cause], "check": f["check"], "repo": f["repo"],
                                    "base": f.get("base") or "", "runner": f.get("runner") or "", "tests": [], "prs": [], "since": f.get("since") or ""})
        for t in f.get("tests") or []:
            if t not in g["tests"]:
                g["tests"].append(t)
        if not any(p["number"] == f["number"] and p["repo"] == f["repo"] for p in g["prs"]):
            g["prs"].append({"repo": f["repo"], "number": f["number"], "title": f["title"], "url": f["url"], "job": f.get("job") or 0,
                             "check": f["check"]})
        if f.get("since") and (not g["since"] or f["since"] < g["since"]):
            g["since"] = f["since"]
    out = list(groups.values())
    for g in out:
        g["tests"] = g["tests"][:6]
        g["title"] = _problem_title(g)
    order = {c: i for i, c in enumerate(CAUSES)}
    out.sort(key=lambda g: (order.get(g["cause"], 9), -len(g["prs"]), g["since"]))
    return out


def _problem_title(g: dict) -> str:
    n = len(g["prs"])
    who = f"PR #{g['prs'][0]['number']}" if n == 1 else f"{n} PRs"
    test = f" ({', '.join(g['tests'][:2])})" if g["tests"] else ""
    if g["cause"] == "base":
        return f"{who} fail{'s' if n == 1 else ''} {g['check']}{test} because {g['base']} is red"
    if g["cause"] == "machine":
        return f"{who}: {g['check']} gave no verdict on {g['runner'] or 'a machine'}; re-run"
    if g["cause"] == "cancelled":
        return f"{who}: a check was cancelled; re-run"
    if g["cause"] == "flaky":
        return f"{who}: {g['check']} failed, then passed on the same commit"
    return f"{who} fail{'s' if n == 1 else ''} {g['check']}{test}"


def summary(problems_: list) -> dict:
    """{"own": PRs with an own bug, ..., "red": distinct red PRs}: the line "13 red PRs: 4 own bugs, 6 base, 3 machine"."""
    out = {c: 0 for c in CAUSES}
    seen = {}
    for g in problems_:
        for p in g["prs"]:
            k = (p["repo"], p["number"])
            if k not in seen or CAUSES.index(g["cause"]) < CAUSES.index(seen[k]):
                seen[k] = g["cause"]
    for c in seen.values():
        out[c] += 1
    out["red"] = len(seen)
    out["problems_own"] = sum(1 for g in problems_ if g["cause"] == "own")
    return out


# ---- Queue (proposal 3): what waits for which machines, when it should start and finish, and who cancelled what ----
CANCEL_BY = re.compile(r"cancel+ed by @?([A-Za-z0-9][A-Za-z0-9-]{0,38})", re.I)
SUPERSEDED = re.compile(r"higher priority waiting request|canceling since a|cancel+ed because a newer|concurrency", re.I)


def cancel_reason(annotations: list) -> tuple[str, str]:
    """(how, who) from a cancelled job's annotations: ("person", "octocat") for "The run was canceled by @octocat.",
    ("replaced", "") for a concurrency group's newer run, ("", "") when they don't say."""
    for n in annotations or []:
        text = f"{(n or {}).get('title') or ''} {(n or {}).get('message') or ''}" if isinstance(n, dict) else str(n)
        m = CANCEL_BY.search(text)
        if m:
            return "person", m.group(1)
        if SUPERSEDED.search(text):
            return "replaced", ""
    return "", ""


def schedule(free_in: list, waiting: list) -> list:
    """When each waiting job should start and finish. `free_in` = seconds until each worker of the pool is free (0 when
    idle); `waiting` = each job's expected seconds (None = not learned yet), oldest first, the order GitHub roughly
    hands them out. Returns [(start_in, finish_in)] with None once an unknown duration makes later guesses meaningless."""
    slots = sorted(max(0.0, float(x)) for x in free_in if x is not None)
    out = []
    blind = not slots
    for est in waiting:
        if blind:
            out.append((None, None))
            continue
        start = slots.pop(0)
        if est is None:
            out.append((start, None))
            blind = True           # this worker's free time is unknown from here on, so every later guess is too
            continue
        finish = start + float(est)
        out.append((start, finish))
        slots.append(finish)
        slots.sort()
    return out


def pool_of(labels, generic) -> str:
    """A pool's name: its custom labels, sorted ("unreal-pr")."""
    gen = {str(g).lower() for g in generic}
    return " + ".join(sorted({str(x) for x in labels or () if str(x).lower() not in gen})) or "any"


# ---- disk forecast ----
FORECAST_WINDOW = 2 * 3600    # readings looked at
FORECAST_MIN_SPAN = 15 * 60   # ... spanning at least this long
FORECAST_MIN_N = 5
FORECAST_FLOOR_GB = 5.0       # "full" means under this much free
FORECAST_HORIZON = 6 * 3600   # a forecast inside this is a signal


def disk_forecast(readings: list, now: float | None = None, floor_gb: float = FORECAST_FLOOR_GB):
    """Seconds until a drive has under `floor_gb` free at the rate it has been filling (least squares over the last
    two hours of (at, free_gb) readings), or None when it is not filling or there is too little to go on."""
    now = now or time.time()
    pts = [(float(t), float(f)) for t, f in readings or () if t is not None and f is not None and now - FORECAST_WINDOW <= t <= now]
    if len(pts) < FORECAST_MIN_N or max(p[0] for p in pts) - min(p[0] for p in pts) < FORECAST_MIN_SPAN:
        return None
    n = len(pts)
    mt, mf = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    var = sum((p[0] - mt) ** 2 for p in pts)
    if not var:
        return None
    slope = sum((p[0] - mt) * (p[1] - mf) for p in pts) / var     # GB per second; negative while filling
    if slope >= -1e-6:                                              # under ~3.6 MB an hour: not filling
        return None
    latest = max(pts)[1]
    if latest <= floor_gb:
        return 0.0
    return (latest - floor_gb) / -slope
