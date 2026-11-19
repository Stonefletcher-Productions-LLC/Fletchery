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
    red_issues = [i for i in issues if any(str(lab).lower() in {x.lower() for x in red_issue_labels} for lab in i.get("labels") or [])]
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
        if not isinstance(n, dict) or n.get("annotation_level") not in (None, "failure", "warning"):
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
