"""Is work moving? The Flow strip at the top of Live: merges in the last hour and day, time since the last merge into each
base branch, how long a change takes from PR opened to merged, how long checks wait for a machine, how often a base
branch goes red, and one plain verdict (moving, slow, stopped, idle) with its reason.

Everything is read from the history store (store.py), so a backend restart does not reset it. Computed per repository
and for all of them together, so the page's project chips filter it like every other card.

Profile, under `workers.portal.live.flow` (all optional):

    "flow": {"slowMinutes": 90, "stopHours": 3}

Standard library only."""
from __future__ import annotations

import math
import statistics
import time

SLOW_MINUTES = 90      # waiting work and no merge for this long: "slow"
STOP_HOURS = 3         # ... and for this long: "stopped" (an alert, alerts.py)
RED = ("failure", "error", "timeout")


def _median(xs):
    return statistics.median(xs) if xs else None


def _p90(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]


class Flow:
    def __init__(self, store, conf: dict | None = None):
        conf = conf if isinstance(conf, dict) else {}
        self.store = store
        try:
            self.slow = 60 * float(conf.get("slowMinutes", SLOW_MINUTES))
            self.stop = 3600 * float(conf.get("stopHours", STOP_HOURS))
        except (TypeError, ValueError):
            self.slow, self.stop = 60 * SLOW_MINUTES, 3600 * STOP_HOURS

    def compute(self, repos: list, waiting: dict, now: float | None = None, projects: dict | None = None,
                build_queues: dict | None = None) -> dict:
        """{"all": {...}, "repos": {repo: {...}}, "projects": {name: {...}}}. `waiting` = {repo: open PRs on their way to
        merging (not drafts, checks not failed)}; `projects` = {name: [repos]} for projects of several repositories."""
        now = now or time.time()
        out = {"all": self._one(None, sum(waiting.values()), now), "repos": {}, "projects": {}}
        for r in repos:
            out["repos"][r] = self._one(r, waiting.get(r, 0), now)
        for name, rs in (projects or {}).items():
            out["projects"][name] = self._one(list(rs), sum(waiting.get(r, 0) for r in rs), now)
        # Keep build attempts separate from the all-jobs tile, even when the page spans projects.
        samples = {}
        for repo, wait in self.store.query("""SELECT repo, started-pr_build_created FROM jobs
            WHERE started>? AND started<=? AND pr_build_created IS NOT NULL AND started>=pr_build_created
              AND runner<>'' AND finished>=started AND result IN ('success','failure') ORDER BY started-pr_build_created""",
                                           (now - 86400, now)):
            samples.setdefault(repo, []).append(wait)
        builds = {}
        for repo in repos:
            waits = samples.get(repo, [])
            current = (build_queues or {}).get(repo)
            if not waits and not (current and current["times"]) and repo.rsplit("/", 1)[-1].lower() != "lifeshare":
                continue
            times = current["times"] if current else []
            oldest = now - min(times) if times and all(t is not None and t <= now for t in times) else None
            builds[repo] = {"repo": repo, "samples": len(waits), "median": _median(waits),
                            "p90": waits[math.ceil(0.9 * len(waits)) - 1] if waits else None,
                            "waiting": len(times) if current else None, "oldest": oldest}
            out["repos"][repo]["build_queues"] = [builds[repo]]
        out["all"]["build_queues"] = list(builds.values())
        for name, rs in (projects or {}).items():
            out["projects"][name]["build_queues"] = [builds[r] for r in rs if r in builds]
        return out

    def _one(self, repo, waiting: int, now: float) -> dict:
        """One repository (a name), several (a list), or all (None)."""
        if isinstance(repo, list):
            repo = repo or [""]
            where, args = (f"WHERE repo IN ({','.join('?' * len(repo))})", tuple(repo))
        else:
            where, args = ("WHERE repo=?", (repo,)) if repo else ("", ())
        merges = self.store.query(f"SELECT repo, number, base, title, created, merged, agent FROM merges {where}"
                                  + (" AND" if where else " WHERE") + " merged > ? ORDER BY merged DESC", args + (now - 7 * 86400,))
        hours = [0] * 24
        for m in merges:
            age = now - m[5]
            if 0 <= age < 86400:
                hours[23 - int(age // 3600)] += 1
        last: dict = {}
        for m in merges:                         # newest first: the first per (repo, base) is its last merge
            last.setdefault((m[0], m[2]), {"repo": m[0], "base": m[2], "number": m[1], "title": m[3], "at": m[5],
                                           "ago": round(now - m[5])})
        if not last:                             # nothing in 7 days: the newest ever, if any
            for m in self.store.query(f"SELECT repo, number, base, title, created, merged FROM merges {where} "
                                      "ORDER BY merged DESC LIMIT 5", args):
                last.setdefault((m[0], m[2]), {"repo": m[0], "base": m[2], "number": m[1], "title": m[3], "at": m[5],
                                               "ago": round(now - m[5])})
        lead = [m[5] - m[4] for m in merges if m[4] and m[5] >= m[4]]
        jw, ja = (("AND " + where[len("WHERE "):]), args) if where else ("", ())
        waits = [r[0] - r[1] for r in self.store.query(
            f"SELECT started, created FROM jobs WHERE started > ? AND created IS NOT NULL {jw}", (now - 86400,) + ja)
            if r[0] and r[1] and r[0] >= r[1]]
        bases = {m[2] for m in merges if m[2]} | {b[1] for b in self.store.query(
            f"SELECT repo, base FROM merges {where} GROUP BY repo, base", args)}
        pushes = [r[0] for r in self.store.query(
            f"SELECT result, branch FROM runs WHERE event='push' AND finished > ? {jw}", (now - 7 * 86400,) + ja)
            if r[1] in bases and r[0] not in ("cancellation", "skip", "")]
        newest = min((x["ago"] for x in last.values()), default=None)
        return {
            "merges_hour": sum(1 for m in merges if now - m[5] < 3600),
            "merges_day": sum(hours),
            "merges_week": len(merges),
            "hours": hours,
            "last": sorted(last.values(), key=lambda x: x["ago"])[:6],
            "since_merge": newest,
            "lead_p50": _median(lead),
            "wait_p50": _median(waits), "wait_p90": _p90(waits), "wait_n": len(waits),
            "red_share": (sum(1 for r in pushes if r in RED) / len(pushes)) if pushes else None, "red_n": len(pushes),
            "waiting": waiting,
            "verdict": self.verdict(newest, waiting, sum(1 for m in merges if now - m[5] < 3600), sum(hours)),
        }

    def verdict(self, since_merge, waiting: int, hour: int, day: int) -> dict:
        def ago(s):
            return f"{int(s // 60)} min" if s < 5400 else f"{s / 3600:.1f} h".replace(".0 h", " h")
        if since_merge is not None and since_merge < self.slow:
            return {"state": "moving", "why": f"Last merge {ago(since_merge)} ago; {hour} in the last hour, {day} today."}
        if not waiting:
            return {"state": "idle", "why": "Nothing is waiting to merge." + (f" Last merge {ago(since_merge)} ago." if since_merge is not None else "")}
        plural = "s" if waiting != 1 else ""
        if since_merge is None:
            return {"state": "slow", "why": f"{waiting} change{plural} on the way; no merge seen yet."}
        if since_merge < self.stop:
            return {"state": "slow", "why": f"No merge for {ago(since_merge)} while {waiting} change{plural} wait."}
        return {"state": "stopped", "why": f"Nothing merged for {ago(since_merge)} while {waiting} change{plural} wait."}
