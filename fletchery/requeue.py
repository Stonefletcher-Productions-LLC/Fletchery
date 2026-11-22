"""Put back a PR check that was cancelled while it was still waiting for a machine.

Why: a queued check can be cancelled by something that means to re-queue it and then does not. On 2026-11-22 the
"Tip fix first" step moved nine queued builds behind a tip fix, died on one dropped connection, and re-queued none of
them (LifeShare #2542). GitHub starts nothing by itself after a cancel, so both build machines sat idle for 48 minutes
with nine PRs waiting to merge, until a person noticed.

What it does, every EVERY seconds, for each repository on the Live page:

  - lists the PR checks GitHub shows as cancelled (one call);
  - keeps a check only when all of this is true: it was cancelled QUIET or more seconds ago; it is the newest check on
    the current commit of an open, non-draft PR that has auto-merge on; and its build job never got a machine
    (a build that was running when it was cancelled was stopped by someone on purpose and is never restarted here);
  - waits while a "Tip fix first" run is under way: that run re-queues what it moved when the fix is building;
  - re-runs the check (the same call as the "Re-run" button), at most PER_TICK a round and PER_RUN times a day for one
    check, so something that keeps cancelling a check cannot make this spin.

To keep a PR's check from coming back, turn the PR's auto-merge off or make it a draft. Turn the whole thing off with
`"autoRequeue": false` in the profile's workers.portal.live block. Standard library only."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

EVERY = 300          # seconds between rounds
QUIET = 600          # a cancelled check is left alone this long: whatever cancelled it may still re-queue it
AGE = 3 * 86400      # older cancelled checks are history, not a waiting PR
PER_TICK = 5         # checks re-queued in one round
PER_RUN = 2          # automatic re-queues of one check in a day
DAY = 86400
WORKFLOW = "pr-check.yml"
MOVER = "tip-fix-first.yml"
BUILD_JOB = "Compile and test"


def _ts(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


class Requeue:
    def __init__(self, gh, log, state_dir=None, repos=None, clock=time.time, enabled=True):
        """`repos()` lists the "owner/name" repositories to look at."""
        self.gh, self.log, self.repos, self.clock = gh, log, repos, clock
        self.enabled = bool(enabled)
        self.path = Path(state_dir) / "requeue.json" if state_dir else None
        self._lock = threading.Lock()
        self.last = 0.0
        self.error = ""
        self.note = ""
        self.done: list[dict] = []       # {"at", "repo", "pr", "run"}: the re-queues of the last day
        self.waiting: list[dict] = []    # stranded checks the last round left alone, and why
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.done = [d for d in data.get("done", []) if isinstance(d, dict)][-400:]
            except (OSError, ValueError):
                self.done = []

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"done": self.done, "at": self.clock(), "note": self.note, "error": self.error,
                                       "waiting": self.waiting}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _say(self, note: str) -> None:
        if note != self.note:
            self.log(f"re-queue: {note}")
        self.note = note
        self._save()

    def tick(self) -> None:
        at = self.clock()
        if not self.enabled or at - self.last < EVERY:
            return
        self.last = at
        try:
            self.round(at)
        except Exception as e:
            self._say(f"stopped by an error: {type(e).__name__}: {e}"[:300])
            raise

    def _get(self, path: str):
        return self.gh.api(path, fresh=True)

    def _moving(self, repo: str) -> bool:
        """True while a Tip fix first run is queued or under way: it re-queues what it moved."""
        for status in ("in_progress", "queued"):
            try:
                got = self._get(f"repos/{repo}/actions/workflows/{MOVER}/runs?status={status}&per_page=5")
            except Exception:
                return False              # the repository has no such workflow
            if isinstance(got, dict) and got.get("workflow_runs"):
                return True
        return False

    def stranded(self, repo: str, at: float) -> tuple[list[dict], str]:
        """([{"repo", "pr", "run"}], why nothing can be done now) for one repository."""
        try:
            got = self._get(f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?status=cancelled&event=pull_request&per_page=50")
        except Exception:
            return [], ""                 # the repository has no PR check workflow
        runs = [r for r in (got or {}).get("workflow_runs") or []
                if r.get("conclusion") == "cancelled" and QUIET <= at - _ts(r.get("updated_at")) <= AGE]
        if not runs:
            return [], ""
        pulls = self.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True, fresh=True) or []
        heads = {str((p.get("head") or {}).get("sha")): p for p in pulls
                 if isinstance(p, dict) and p.get("auto_merge") and not p.get("draft")}
        out = []
        for run in sorted(runs, key=lambda r: r.get("id") or 0):
            sha = str(run.get("head_sha"))
            pr = heads.get(sha)
            if not pr:
                continue
            same = self._get(f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?head_sha={sha}&event=pull_request&per_page=30")
            if max((r.get("id") or 0 for r in (same or {}).get("workflow_runs") or []), default=0) != run.get("id"):
                continue                  # a newer check on this commit exists
            jobs = (self._get(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=50") or {}).get("jobs") or []
            job = next((j for j in jobs if BUILD_JOB in str(j.get("name") or "")), None)
            if job and job.get("runner_name"):
                continue                  # it was building when it was cancelled: on purpose, never restarted here
            out.append({"repo": repo, "pr": pr.get("number"), "run": run["id"]})
        if out and self._moving(repo):
            return [], f"{len(out)} cancelled check(s) in {repo.split('/')[-1]} wait for the tip fix that moved them"
        return out, ""

    def round(self, at: float | None = None) -> list[dict]:
        """One pass. Returns what it re-queued."""
        at = self.clock() if at is None else at
        out, waiting, notes = [], [], []
        with self._lock:
            self.done = [d for d in self.done if at - d.get("at", 0) < DAY]
            count: dict = {}
            for d in self.done:
                count[(d["repo"], d["run"])] = count.get((d["repo"], d["run"]), 0) + 1
            for repo in (self.repos() if self.repos else []) or []:
                found, why = self.stranded(repo, at)
                if why:
                    notes.append(why)
                for row in found:
                    if count.get((repo, row["run"]), 0) >= PER_RUN:
                        waiting.append({**row, "why": f"already re-queued {PER_RUN} times today and cancelled again; it needs a look"})
                        continue
                    if len(out) >= PER_TICK:
                        waiting.append({**row, "why": "next round"})
                        continue
                    self.done.append({**row, "at": at})      # counted even when refused: a refused check is not asked forever
                    count[(repo, row["run"])] = count.get((repo, row["run"]), 0) + 1
                    try:
                        self.gh.api(f"repos/{repo}/actions/runs/{row['run']}/rerun-failed-jobs", "POST", {})
                    except Exception as e:
                        self.log(f"re-queue: GitHub refused #{row['pr']} run {row['run']}: {type(e).__name__}: {e}"[:300])
                        waiting.append({**row, "why": f"GitHub refused the re-run ({type(e).__name__})"})
                        continue
                    out.append(row)
                    self.log(f"re-queue: #{row['pr']} in {repo.split('/')[-1]} had its check cancelled before it got a machine "
                             f"and nothing put it back; re-queued (run {row['run']})")
            self.waiting = waiting[:40]
            self.error = ""
            self._say("; ".join(notes) or (f"{len(out)} re-queued, {len(waiting)} left alone" if out or waiting
                                           else "no waiting PR has a cancelled check"))
        return out

    def view(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "every": EVERY, "error": self.error, "note": self.note,
                    "done": [dict(d) for d in self.done][-20:], "waiting": list(self.waiting)}
