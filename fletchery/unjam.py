"""Notice a build machine that a finished check still holds, and clear it.

Why: a cancelled PR check can leave an editor that Windows accepts the kill for but that never exits. It keeps the
checkout's DLLs locked, so every later check on that machine fails at cleanup within a minute, or waits. Nothing said
so: the machine looked busy with "PR check #2399" for nine hours after that check had ended (2026-11-21, Gaming PC),
and the only cure was a person at the keyboard. A restart clears it (SF-NODE-01, same night, back in six minutes).

What it does, every EVERY seconds, from the Live snapshot:

  - takes each machine whose reservation holder is "PR check #N (GitHub run R)";
  - asks GitHub, every round, for the latest attempt of run R and that attempt's jobs. When every job of it is
    finished and the machine is still held QUIET seconds after the last one ended, that is a jam: the job's own cleanup
    has run by then and could not end what the job left behind;
  - shows the jam under "Stuck or blocked" (devwatch.py reads `blockers()`), with what clears it;
  - restarts the machine when that is safe to do without a person: the machine signs in by itself after a restart
    (restart.py reports this), no restart is already under way, this machine was not restarted for a jam in the last
    AGAIN seconds (a restart that did not help must not loop), and on two rounds in a row the jam was there and
    nothing was working on the machine (`_working`): none of its GitHub runners busy, no Runner.Worker, no compiler.

A re-run keeps the run's id, so what GitHub said about a run is never kept from one round to the next. 2026-11-22:
attempt 16 of a run ended on one machine and was remembered as "the run is finished"; two hours later attempt 17 was
compiling on the Gaming PC under the same id, was taken for a jam "finished 125 min ago", and the PC was restarted
in the middle of that build (#284). Whatever cannot be read or proven counts as "not a jam" or "do not restart".

A second jam, LifeShare #2530 (2026-11-23, Gaming PC): a killed UnrealEditor-Cmd became unkillable, kept the PR checkout's
Binaries/Win64/UnrealEditor-LSBoot.dll mapped, and the next check sat 25 minutes in its checkout step. The rule above never
saw it: that check was running, so its run was not finished. LifeShare's job now moves such files to
<checkout>/Saved/StuckFiles and goes on; a file there that still cannot be opened for writing means the process that
cannot be ended is alive. The reporter counts those files (reporter.py stuck_files). When a machine has shown any for
QUIET seconds it is a jam too ("stuck-files"), handled by the same rules.

A restart is always asked for "when idle": it never ends a running build. A machine whose reporter says it is someone's
PC (presence "attended", the Gaming PC) is restarted only while nobody uses it: no keyboard or mouse input for AWAY
seconds and none of the games LifeShare's game throttle watches running. Otherwise the jam is flagged and the card says
it waits until they are away.

A machine that does not sign in by itself is never restarted from here: it is flagged, and the text says what a person
must do. Turn the restart off with `"autoUnjam": false` in the profile's workers.portal.live block; the flag stays.
Standard library only."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

EVERY = 120          # seconds between rounds
QUIET = 600          # a finished run may hold the machine this long (its own cleanup) before it counts as a jam
AGAIN = 2 * 3600     # one automatic restart per machine in this time
STALE = 300          # a Live snapshot whose last good GitHub read is older than this says nothing about runners now
AWAY = 1800          # an attended PC is restarted only after this long without keyboard or mouse input
_RUN = re.compile(r"PR check #(\d+) \(GitHub run (\d+)\)", re.I)


def _ts(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


class Unjam:
    def __init__(self, gh, log, state_dir=None, snapshot=None, restart=None, repos=None, clock=time.time, enabled=True):
        """`restart(machine_name, reason)` restarts a machine now and returns its message; `repos()` lists the
        "owner/name" repositories a run may belong to."""
        self.gh, self.log, self.snapshot, self.restart, self.repos, self.clock = gh, log, snapshot, restart, repos, clock
        self.enabled = bool(enabled)
        self.path = Path(state_dir) / "unjam.json" if state_dir else None
        self._lock = threading.Lock()
        self.last = 0.0
        self.error = ""
        self.jams: dict[str, dict] = {}        # machine name (lower) -> the jam seen last round
        self.restarted: dict[str, float] = {}  # machine name (lower) -> when it was last restarted for a jam
        self._repo: dict[str, str] = {}        # run id -> its repository (that never changes; its state does)
        self.first: dict[str, float] = {}      # machine name (lower) -> since when it has shown files still held
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.restarted = {str(k): float(v) for k, v in (data.get("restarted") or {}).items()}
            except (OSError, ValueError, TypeError):
                self.restarted = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"restarted": self.restarted, "jams": list(self.jams.values()), "at": self.clock(),
                                       "error": self.error}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def tick(self) -> None:
        at = self.clock()
        if self.snapshot is None or at - self.last < EVERY:
            return
        self.last = at
        self.round(at)

    def _latest_attempt(self, run: str) -> dict | None:
        """{"repo", "attempt", "at"} when every job of the run's latest attempt is finished (`at`: when the last one
        ended). None while any of them waits or runs, and when GitHub cannot be read or the run cannot be found.
        Asked again every round: a re-run starts a new attempt under the same id at any time."""
        for repo in [self._repo[run]] if run in self._repo else (self.repos() if self.repos else []) or []:
            try:
                data = self.gh.api(f"repos/{repo}/actions/runs/{run}", fresh=True)
            except Exception:
                continue                      # not this repository's run, or GitHub is unreachable: decide next round
            if not isinstance(data, dict) or not data.get("status"):
                continue
            self._repo[run] = repo
            if len(self._repo) > 200:
                self._repo.pop(next(iter(self._repo)))
            if data.get("status") != "completed":
                return None
            try:
                attempt = int(data.get("run_attempt") or 1)
                listed = self.gh.api(f"repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100", fresh=True)
            except Exception:
                return None
            jobs = listed.get("jobs") if isinstance(listed, dict) else None
            if not isinstance(jobs, list) or not jobs or int(listed.get("total_count") or 0) > len(jobs):
                return None                   # its jobs cannot all be seen, so nothing is proven
            if any(not isinstance(j, dict) or j.get("status") != "completed" for j in jobs):
                return None
            ended = max(_ts(j.get("completed_at")) for j in jobs)
            return {"repo": repo, "attempt": attempt, "at": ended} if ended else None
        return None

    @staticmethod
    def _working(m: dict, snap: dict, at: float) -> str:
        """What shows that this machine is working, as words for the page; "" when nothing does. An Unreal editor
        alone is not counted: the editor a cancelled check left behind is the very thing a restart clears."""
        procs = m.get("procs") or {}           # the machine's own report, at most a minute old
        if procs.get("runner"):
            return "A runner job is running on it (Runner.Worker)."
        if procs.get("compiler"):
            return "A compiler is running on it."
        mine = {str(n).lower() for n in m.get("runners") or []}
        for r in snap.get("runners") or []:
            if str(r.get("name") or "").lower() in mine and (r.get("busy") or r.get("job")):
                return f"Its runner {r.get('name')} is running a job."
        fresh = snap.get("fresh") or {}
        if fresh.get("error") or at - _ts(fresh.get("github")) > STALE:
            return "GitHub cannot be read just now, so the state of its runners is not known."
        return ""

    def round(self, at: float | None = None) -> list[dict]:
        """One pass. Returns the jams it sees now; restarts what it may."""
        at = self.clock() if at is None else at
        snap = self.snapshot() or {}
        seen: dict[str, dict] = {}
        for m in snap.get("machines") or []:
            name = str(m.get("name") or "")
            holder = next((a for a in m.get("activities") or [] if a.get("reservation_holder")), None)
            hit = _RUN.search(str((holder or {}).get("title") or ""))
            if not name or not hit or m.get("online") is False:
                continue
            done = self._latest_attempt(hit.group(2))
            if not done or at - done["at"] < QUIET:
                continue
            if _ts(holder.get("since")) > done["at"]:
                continue                      # held since after that attempt ended: a newer one has the machine
            signs_in = (m.get("after_restart") or {}).get("signs_in")
            seen[name.lower()] = {"machine": name, "kind": "finished-check", "pr": int(hit.group(1)), "run": hit.group(2),
                                  "repo": done["repo"], "attempt": done["attempt"], "finished": done["at"], "signs_in": signs_in,
                                  "restart": (m.get("restart") or {}).get("state") or "", "working": self._working(m, snap, at),
                                  "presence": m.get("presence"), "since": str(holder.get("since") or ""), "rounds": 1, "idle": 0,
                                  "action": ""}
        held = set()
        for m in snap.get("machines") or []:
            name = str(m.get("name") or "")
            stuck = m.get("stuck") if isinstance(m.get("stuck"), dict) else {}
            if not name or m.get("online") is False or not int(stuck.get("locked") or 0):
                continue
            key = name.lower()
            held.add(key)
            first = self.first.setdefault(key, at)
            if key in seen or at - first < QUIET:
                continue
            seen[key] = {"machine": name, "kind": "stuck-files", "pr": 0, "run": "", "repo": "", "attempt": 0,
                         "finished": first, "locked": int(stuck["locked"]), "sample": str(stuck.get("sample") or "")[:200],
                         "signs_in": (m.get("after_restart") or {}).get("signs_in"),
                         "restart": (m.get("restart") or {}).get("state") or "", "working": self._working(m, snap, at),
                         "presence": m.get("presence"), "since": datetime.fromtimestamp(first, timezone.utc).isoformat(),
                         "rounds": 1, "idle": 0, "action": ""}
        for key in [k for k in self.first if k not in held]:
            del self.first[key]
        with self._lock:
            for key, jam in seen.items():
                was = self.jams.get(key, {})
                jam["rounds"] = was.get("rounds", 0) + 1
                jam["idle"] = 0 if jam["working"] else was.get("idle", 0) + 1
                jam["action"] = self._act(key, jam, at, was.get("working"), was.get("in_use"))
            cleared = [j["machine"] for k, j in self.jams.items() if k not in seen]
            self.jams = seen
            self.error = ""
            self._save()
        for name in cleared:
            self.log(f"unjam: {name} is no longer held by a finished check or a process that cannot be ended")
        return list(seen.values())

    @staticmethod
    def _in_use(jam: dict) -> str:
        """Why an attended PC (the Gaming PC) must not be restarted now, as words for its card; "" when nobody uses it.
        What cannot be read counts as "in use"."""
        p = jam.get("presence")
        if not isinstance(p, dict) or not p.get("attended"):
            return ""
        if p.get("games"):
            return f"Waiting until you are away: {', '.join(p['games'][:3])} is running."
        idle = p.get("input_idle")
        if not isinstance(idle, (int, float)) or isinstance(idle, bool):
            return "Waiting until you are away: this PC cannot tell when it was last used."
        if idle < AWAY:
            return (f"Waiting until you are away: this PC was used {int(idle // 60)} min ago. It restarts after "
                    f"{AWAY // 60} min without keyboard or mouse input and with no game running.")
        return ""

    @staticmethod
    def _why(jam: dict) -> str:
        if jam.get("kind") == "stuck-files":
            return (f"{jam['locked']} file(s) of its PR checkout are still held by a process that cannot be ended "
                    "(Saved\\StuckFiles)")
        return f"PR check #{jam['pr']} finished but still holds the machine"

    def _act(self, key: str, jam: dict, at: float, before: str | None = None, was_in_use: str | None = None) -> str:
        """What happens to this jam now, as words for the page. `before`: what was working there last round;
        `was_in_use`: why an attended PC waited last round."""
        waited = at - self.restarted.get(key, 0)
        jam["in_use"] = ""
        if jam["rounds"] == 1:
            if jam.get("kind") == "stuck-files":
                self.log(f"unjam: {jam['machine']}: {self._why(jam)}, for {int((at - jam['finished']) // 60)} min")
            else:
                self.log(f"unjam: {jam['machine']} is still held by PR check #{jam['pr']}, which finished "
                         f"{int((at - jam['finished']) // 60)} min ago")
        if jam["restart"] in ("requested", "sent"):
            return "It is restarting."
        if jam["restart"] == "waiting":
            return "It restarts as soon as nothing runs there."
        if jam["signs_in"] is not True:
            return ("It does not sign in by itself after a restart, so Fletchery will not restart it. End the leftover "
                    "UnrealEditor-Cmd in an elevated Task Manager, or restart it and sign in.")
        if not self.enabled or self.restart is None:
            return "Automatic restart is turned off. Use Restart now on this machine's card."
        if waited < AGAIN:
            return (f"Fletchery restarted it {int(waited // 60)} min ago for the same reason and it is held again, "
                    "so it will not restart it a second time. It needs a look.")
        if jam["working"]:
            if jam["working"] != before:
                self.log(f"unjam: not restarting {jam['machine']}: {jam['working']}"[:300])
            return (f"{jam['working']} Fletchery will not restart it until nothing runs there. "
                    "Restart now on this machine's card ends what runs.")
        in_use = self._in_use(jam)
        jam["in_use"] = in_use
        if in_use:
            if not was_in_use:
                self.log(f"unjam: not restarting {jam['machine']} yet: {in_use}"[:300])
            return in_use
        if jam["idle"] < 2:
            return "Fletchery restarts it on the next check if it is still jammed and nothing runs there."
        try:
            said = self.restart(jam["machine"], self._why(jam))
        except Exception as e:
            self.log(f"unjam: could not restart {jam['machine']}: {type(e).__name__}: {e}"[:300])
            return f"Fletchery could not restart it ({type(e).__name__}: {e}). Use Restart now on this machine's card."[:300]
        self.restarted[key] = at
        self.log(f"unjam: restarting {jam['machine']} when idle: {self._why(jam)} ({said})"[:300])
        return "Fletchery is restarting it to clear this."

    def blockers(self) -> list[dict]:
        """Rows for Live's "Stuck or blocked" (keyword arguments of devwatch's block())."""
        with self._lock:
            jams = [dict(j) for j in self.jams.values()]
        at = self.clock()
        out = []
        for j in jams:
            if j.get("kind") == "stuck-files":
                out.append({"kind": "stuck", "since": j["since"], "key": f"jam:{j['machine'].lower()}:stuck", "repo": "", "url": "",
                            "title": f"{j['machine']}: a process that cannot be ended has held {j['locked']} file(s) of its PR "
                                     f"checkout for {int((at - j['finished']) // 60)} min",
                            "detail": "The PR check moved them aside (Saved\\StuckFiles) and went on, but that process stays until "
                                      "a restart and may lock the next build. " + j["action"]})
            else:
                out.append({"kind": "stuck",
                            "title": f"{j['machine']}: PR check #{j['pr']} finished {int((at - j['finished']) // 60)} min ago but still holds the machine",
                            "detail": "Something it left behind will not end, so every other check there fails at cleanup or waits. " + j["action"],
                            "url": f"https://github.com/{j['repo']}/actions/runs/{j['run']}", "since": j["since"],
                            "key": f"jam:{j['machine'].lower()}:{j['run']}", "repo": j["repo"]})
        return out

    def notes(self) -> dict[str, str]:
        """machine name (lower) -> what unjam says about it, for that machine's card ("waiting until you are away")."""
        with self._lock:
            return {k: f"Jammed: {self._why(j)}. {j['action']}" for k, j in self.jams.items()}

    def view(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "every": EVERY, "error": self.error, "jams": [dict(j) for j in self.jams.values()],
                    "restarted": dict(self.restarted)}
