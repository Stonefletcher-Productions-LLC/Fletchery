"""Notice a build machine that a finished check still holds, and clear it.

Why: a cancelled PR check can leave an editor that Windows accepts the kill for but that never exits. It keeps the
checkout's DLLs locked, so every later check on that machine fails at cleanup within a minute, or waits. Nothing said
so: the machine looked busy with "PR check #2399" for nine hours after that check had ended (2026-11-21, Gaming PC),
and the only cure was a person at the keyboard. A restart clears it (SF-NODE-01, same night, back in six minutes).

What it does, every EVERY seconds, from the Live snapshot:

  - takes each machine whose reservation holder is "PR check #N (GitHub run R)";
  - asks GitHub whether run R is finished. A finished run that still holds the machine QUIET seconds later is a jam:
    the job's own cleanup has run by then and could not end what the job left behind;
  - shows the jam under "Stuck or blocked" (devwatch.py reads `blockers()`), with what clears it;
  - restarts the machine when that is safe to do without a person: the machine signs in by itself after a restart
    (restart.py reports this), the jam was seen on two rounds in a row, no restart is already under way, and this
    machine was not restarted for a jam in the last AGAIN seconds (a restart that did not help must not loop).
    The run that holds the machine is finished, so the restart ends no build.

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
        self._finished: dict[str, dict] = {}   # run id -> {"repo", "at"} once GitHub says it is finished (final)
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

    def _run_finished(self, run: str) -> dict | None:
        """{"repo", "at"} when GitHub says the run is finished, None while it runs or cannot be found."""
        if run in self._finished:
            return self._finished[run]
        for repo in (self.repos() if self.repos else []) or []:
            try:
                data = self.gh.api(f"repos/{repo}/actions/runs/{run}", fresh=True)
            except Exception:
                continue                      # not this repository's run, or GitHub is unreachable: decide next round
            if not isinstance(data, dict) or not data.get("status"):
                continue
            if data.get("status") != "completed":
                return None                   # running again (a re-run reuses the id), so never cached
            self._finished[run] = {"repo": repo, "at": _ts(data.get("updated_at")) or self.clock()}
            if len(self._finished) > 200:
                self._finished.pop(next(iter(self._finished)))
            return self._finished[run]
        return None

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
            done = self._run_finished(hit.group(2))
            if not done or at - done["at"] < QUIET:
                continue
            signs_in = (m.get("after_restart") or {}).get("signs_in")
            seen[name.lower()] = {"machine": name, "pr": int(hit.group(1)), "run": hit.group(2), "repo": done["repo"],
                                  "finished": done["at"], "signs_in": signs_in, "restart": (m.get("restart") or {}).get("state") or "",
                                  "since": str(holder.get("since") or ""), "rounds": 1, "action": ""}
        with self._lock:
            for key, jam in seen.items():
                jam["rounds"] = self.jams.get(key, {}).get("rounds", 0) + 1
                jam["action"] = self._act(key, jam, at)
            cleared = [j["machine"] for k, j in self.jams.items() if k not in seen]
            self.jams = seen
            self.error = ""
            self._save()
        for name in cleared:
            self.log(f"unjam: {name} is no longer held by a finished check")
        return list(seen.values())

    def _act(self, key: str, jam: dict, at: float) -> str:
        """What happens to this jam now, as words for the page."""
        waited = at - self.restarted.get(key, 0)
        if jam["rounds"] == 1:
            self.log(f"unjam: {jam['machine']} is still held by PR check #{jam['pr']}, which finished "
                     f"{int((at - jam['finished']) // 60)} min ago")
        if jam["restart"] in ("requested", "sent"):
            return "It is restarting."
        if jam["signs_in"] is not True:
            return ("It does not sign in by itself after a restart, so Fletchery will not restart it. End the leftover "
                    "UnrealEditor-Cmd in an elevated Task Manager, or restart it and sign in.")
        if not self.enabled or self.restart is None:
            return "Automatic restart is turned off. Use Restart now on this machine's card."
        if waited < AGAIN:
            return (f"Fletchery restarted it {int(waited // 60)} min ago for the same reason and it is held again, "
                    "so it will not restart it a second time. It needs a look.")
        if jam["rounds"] < 2:
            return "Fletchery restarts it on the next check if it is still held."
        try:
            said = self.restart(jam["machine"], f"PR check #{jam['pr']} finished but still holds the machine")
        except Exception as e:
            self.log(f"unjam: could not restart {jam['machine']}: {type(e).__name__}: {e}"[:300])
            return f"Fletchery could not restart it ({type(e).__name__}: {e}). Use Restart now on this machine's card."[:300]
        self.restarted[key] = at
        self.log(f"unjam: restarting {jam['machine']} to clear what PR check #{jam['pr']} left behind ({said})"[:300])
        return "Fletchery is restarting it to clear this."

    def blockers(self) -> list[dict]:
        """Rows for Live's "Stuck or blocked" (keyword arguments of devwatch's block())."""
        with self._lock:
            jams = [dict(j) for j in self.jams.values()]
        at = self.clock()
        return [{"kind": "stuck",
                 "title": f"{j['machine']}: PR check #{j['pr']} finished {int((at - j['finished']) // 60)} min ago but still holds the machine",
                 "detail": "Something it left behind will not end, so every other check there fails at cleanup or waits. " + j["action"],
                 "url": f"https://github.com/{j['repo']}/actions/runs/{j['run']}", "since": j["since"],
                 "key": f"jam:{j['machine'].lower()}:{j['run']}", "repo": j["repo"]} for j in jams]

    def view(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "every": EVERY, "error": self.error, "jams": [dict(j) for j in self.jams.values()],
                    "restarted": dict(self.restarted)}
