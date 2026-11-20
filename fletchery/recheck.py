"""Re-run checks that failed because of the machine, not the change.

Why: a PR check that ends "No result" (the machine stayed busy), or fails because its build folder was locked or the
runner was lost, says nothing about the PR. Nobody is told to re-run it, so the PR sits red until someone notices
(2026-11-20: #2320 and #2329, fixes for a crash, sat for two days behind such a check).

What it does, every EVERY seconds, from the Live snapshot (devwatch.py already sorts failures by cause):

  - takes the failed checks whose cause is "machine";
  - leaves a check alone while the machine it ran on is itself in trouble (a hold whose process is gone, a machine that
    stopped reporting, a restart under way): re-running there fails the same way and wastes the queue;
  - re-runs the check's job on GitHub (the same call as the "Re-run" button), at most PER_TICK a round, and at most
    PER_PR times a day for one PR, so a machine that keeps failing cannot spin the queue;
  - never touches a cancelled check (someone cancelled it on purpose) or a check that failed on the PR's own code.

Turn it off with `"autoRerun": false` in the profile's workers.portal.live block. Standard library only."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

EVERY = 300          # seconds between rounds
PER_TICK = 3         # checks re-run in one round
PER_PR = 2           # automatic re-runs of one PR's checks in a day
DAY = 86400
TROUBLE = ("deadhold:", "idlehold:", "machine:", "restart:")   # blocker keys that mean "this machine is not fit"


class Recheck:
    def __init__(self, gh, log, state_dir=None, snapshot=None, clock=time.time, enabled=True):
        self.gh, self.log, self.snapshot, self.clock = gh, log, snapshot, clock
        self.enabled = bool(enabled)
        self.path = Path(state_dir) / "recheck.json" if state_dir else None
        self._lock = threading.Lock()
        self.last = 0.0
        self.error = ""
        self.done: list[dict] = []       # {"at", "repo", "pr", "job", "check", "why"}: the re-runs of the last day
        self.waiting: list[dict] = []    # what the last round left alone, and why (for the page)
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
            tmp.write_text(json.dumps({"done": self.done}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def tick(self) -> None:
        at = self.clock()
        if not self.enabled or self.snapshot is None or at - self.last < EVERY:
            return
        self.last = at
        self.round(at)

    def round(self, at: float | None = None) -> list[dict]:
        """One pass. Returns what it re-ran."""
        at = self.clock() if at is None else at
        snap = self.snapshot() or {}
        if (snap.get("pr_count_status") or {}).get("partial") or (snap.get("fresh") or {}).get("error"):
            return []          # GitHub was not fully read: decide next time
        with self._lock:
            self.done = [d for d in self.done if at - d.get("at", 0) < DAY]
            tried_jobs = {(d["repo"], d["job"]) for d in self.done}
            per_pr: dict = {}
            for d in self.done:
                per_pr[(d["repo"], d["pr"])] = per_pr.get((d["repo"], d["pr"]), 0) + 1
            unfit = self._unfit(snap)
            out, waiting = [], []
            for g in snap.get("problems") or []:
                if g.get("cause") != "machine":
                    continue
                machine = self._machine(snap, g.get("runner") or "")
                for p in g.get("prs") or []:
                    repo, job, number = str(p.get("repo") or g.get("repo") or ""), p.get("job"), p.get("number")
                    if not repo or not isinstance(job, int) or job <= 0 or (repo, job) in tried_jobs:
                        continue
                    row = {"repo": repo, "pr": number, "job": job, "check": str(p.get("check") or g.get("check") or "")[:80]}
                    if machine and machine.lower() in unfit:
                        waiting.append({**row, "why": f"{machine} is in trouble: {unfit[machine.lower()]}"})
                        continue
                    if per_pr.get((repo, number), 0) >= PER_PR:
                        waiting.append({**row, "why": f"already re-run {PER_PR} times today; it needs a look"})
                        continue
                    if len(out) >= PER_TICK:
                        waiting.append({**row, "why": "next round"})
                        continue
                    try:
                        self.gh.api(f"repos/{repo}/actions/jobs/{job}/rerun", "POST", {})
                    except Exception as e:
                        waiting.append({**row, "why": f"GitHub refused the re-run ({type(e).__name__})"})
                        tried_jobs.add((repo, job))
                        self.done.append({**row, "at": at, "why": "refused"})     # asked once: a refused job is not asked again
                        continue
                    why = "; ".join(str(t)[:90] for t in (g.get("tests") or [])[:1]) or "machine problem"
                    rec = {**row, "at": at, "why": why}
                    self.done.append(rec)
                    tried_jobs.add((repo, job))
                    per_pr[(repo, number)] = per_pr.get((repo, number), 0) + 1
                    out.append(rec)
                    self.log(f"re-run: #{number} {row['check']} in {repo.split('/')[-1]} failed for the machine's reasons ({why}); re-run started")
            self.waiting = waiting[:40]
            if out:
                self._save()
            self.error = ""
            return out

    @staticmethod
    def _unfit(snap: dict) -> dict:
        """machine name (lower) -> why it should not be given a re-run now."""
        names = {}
        for m in snap.get("machines") or []:
            for n in (m.get("name"), m.get("host"), m.get("key")):
                if n:
                    names[str(n).lower()] = str(m.get("name") or n).lower()
        out = {}
        for m in snap.get("machines") or []:
            r = m.get("restart") or {}
            if r.get("state") in ("requested", "sent"):
                out[str(m.get("name")).lower()] = "it is restarting"
            elif m.get("online") is False:
                out[str(m.get("name")).lower()] = "it is not reporting"
        for b in snap.get("blockers") or []:
            key = str(b.get("key") or "")
            if not key.startswith(TROUBLE):
                continue
            host = key.split(":", 2)[1].lower() if ":" in key else ""
            if host in names:
                out.setdefault(names[host], str(b.get("title") or "")[:140])
        return out

    @staticmethod
    def _machine(snap: dict, runner: str) -> str:
        for r in snap.get("runners") or []:
            if str(r.get("name") or "").lower() == runner.lower() and r.get("machine"):
                return str(r["machine"])
        best = ""
        for m in snap.get("machines") or []:      # "<COMPUTERNAME>-<label>"
            for n in (m.get("host"), m.get("name")):
                if n and runner.lower().startswith(str(n).lower() + "-") and len(str(n)) > len(best):
                    best = str(m.get("name") or n)
        return best

    def view(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "every": EVERY, "error": self.error,
                    "done": [dict(d) for d in self.done if d.get("why") != "refused"][-20:], "waiting": list(self.waiting)}
