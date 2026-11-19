"""Agent presence: which Claude and Codex agents are working right now, from what they say themselves.

Design and reasons: docs/AGENT-PRESENCE.md. Two ways in, one record:

- `Presence.beat(machine, data)`: a heartbeat from an agent on a registered machine (machines.py `POST /v1/agent`,
  signed with that machine's key). The time is when Fletchery received it.
- `Presence.relay(repo, comments)`: the comments on a repository's status issue, where cloud agents keep one comment
  per session up to date. Only comments by the repository's owner, members and collaborators count, and the time is
  the comment's `updated_at`, GitHub's own clock.

Every field is checked against a fixed shape; anything else is refused (`ValueError`, a 400) or skipped. Records expire
`KEEP_HOURS` after their last heartbeat and are saved, atomically, to `<state dir>/agent-presence.json`, so a restart
keeps who is working. No secret is ever stored.

Standard library only."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

AGENTS = {"claude": "Claude", "codex": "Codex"}
STATES = ("working", "waiting", "done")
SESSION = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
WORK = re.compile(r"^(?:([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}))?#([0-9]{1,9})$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
STEP_MAX = 200
TITLE_MAX = 120
KEEP_HOURS = 24              # a record with no heartbeat for this long is forgotten
RATE_PER_MINUTE = 60         # heartbeats one machine may send in a minute
MAX_SESSIONS = 50            # live sessions one machine (or one repository's status issue) may hold
MAX_COMMENTS = 300           # status-issue comments read in one poll
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")
MARKER = re.compile(r"^\s*<!--\s*fletchery-agent\s+(\{.*?\})\s*-->", re.S)


class TooMany(Exception):
    """Over a limit (heartbeats a minute, or live sessions): the endpoint answers 429."""


def now_iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def validate(data, clean=None) -> dict:
    """One heartbeat's fields, checked. Raises ValueError naming the first bad field."""
    if not isinstance(data, dict):
        raise ValueError("a heartbeat is a JSON object")
    agent = data.get("agent")
    if not isinstance(agent, str) or agent.lower() not in AGENTS:
        raise ValueError("agent: one of " + ", ".join(AGENTS))
    session = data.get("session")
    if not isinstance(session, str) or not SESSION.match(session):
        raise ValueError("session: 1-80 of A-Z a-z 0-9 . _ : -")
    work = data.get("work", "")
    if work is None:
        work = ""
    if not isinstance(work, str) or (work and not WORK.match(work)):
        raise ValueError("work: #123 or owner/repo#123")
    branch = data.get("branch", "")
    if branch is None:
        branch = ""
    if not isinstance(branch, str) or (branch and (not BRANCH.match(branch) or ".." in branch)):
        raise ValueError("branch: a git branch name")
    step = data.get("step", "")
    if step is None:
        step = ""
    if not isinstance(step, str) or len(step) > STEP_MAX:
        raise ValueError(f"step: text of at most {STEP_MAX} characters")
    step = " ".join(step.split())       # one line
    if clean:
        step = clean(step, STEP_MAX)
    state = data.get("state", "working")
    if state not in STATES:
        raise ValueError("state: one of " + ", ".join(STATES))
    title = data.get("title", "")
    if title is None:
        title = ""
    if not isinstance(title, str) or len(title) > TITLE_MAX:
        raise ValueError(f"title: text of at most {TITLE_MAX} characters")
    title = " ".join(title.split())
    if clean:
        title = clean(title, TITLE_MAX)
    return {"agent": agent.lower(), "session": session, "work": work, "branch": branch, "step": step, "state": state,
            "title": title}


class Presence:
    def __init__(self, path: str | Path | None = None, clean=None, clock=time.time, active_seconds: float = 15 * 60,
                 log=None):
        self.path = Path(path) if path else None
        self.clean = clean
        self.clock = clock
        self.active_seconds = active_seconds
        self.log = log or (lambda line: None)
        self._lock = threading.Lock()
        self._beats: dict[str, list[float]] = {}     # machine -> receive times in the last minute
        self.records: dict[str, dict] = {}           # "<source>|<agent>|<session>" -> record
        self._load()

    # ---- in ----
    def beat(self, machine: str, data) -> dict:
        """A heartbeat from an agent on `machine` (already signed in). Returns what the agent is told."""
        rec = validate(data, self.clean)
        t = self.clock()
        source = "machine:" + str(machine)
        with self._lock:
            recent = [x for x in self._beats.get(machine, []) if t - x < 60]
            if len(recent) >= RATE_PER_MINUTE:
                self._beats[machine] = recent
                raise TooMany(f"at most {RATE_PER_MINUTE} heartbeats a minute per machine")
            recent.append(t)
            self._beats[machine] = recent
            self._expire(t)
            key = f"{source}|{rec['agent']}|{rec['session']}"
            if key not in self.records:
                self._make_room(source, t)
            self.records[key] = {**rec, "source": source, "machine": str(machine), "repo": "", "url": "", "at": t}
            self._save()
        return {"ok": True, "state": rec["state"]}

    def relay(self, repo: str, comments) -> int:
        """The status issue's comments for `repo` (newest edits matter). Returns how many heartbeats were taken."""
        if not isinstance(comments, list):
            return 0
        source = "github:" + str(repo).lower()
        took, changed = 0, False
        with self._lock:
            t_now = self.clock()
            self._expire(t_now)
            for c in comments[:MAX_COMMENTS]:
                rec = self._from_comment(c)
                if rec is None:
                    continue
                at = ts(c.get("updated_at"))
                if not at or t_now - at > KEEP_HOURS * 3600:
                    continue
                at = min(at, t_now)       # GitHub's clock a little ahead of ours is not "in the future"
                key = f"{source}|{rec['agent']}|{rec['session']}"
                old = self.records.get(key)
                if old and old["at"] >= at:
                    continue
                if old is None and not self._make_room(source, t_now, strict=False):
                    continue
                self.records[key] = {**rec, "source": source, "machine": "", "repo": str(repo),
                                     "url": _web(c.get("html_url")), "at": at}
                took += 1
                changed = True
            if changed:
                self._save()
        return took

    def _from_comment(self, c) -> dict | None:
        if not isinstance(c, dict) or str(c.get("author_association") or "").upper() not in TRUSTED:
            return None
        m = MARKER.match(str(c.get("body") or "")[:4000])
        if not m:
            return None
        try:
            return validate(json.loads(m.group(1)), self.clean)
        except ValueError:
            return None

    # ---- out ----
    def current(self) -> list[dict]:
        """Every record not yet expired, newest first (copies)."""
        with self._lock:
            if self._expire(self.clock()):
                self._save()
            return sorted((dict(r) for r in self.records.values()), key=lambda r: -r["at"])

    # ---- limits and keeping ----
    def _expire(self, t: float) -> bool:
        old = [k for k, r in self.records.items() if t - r["at"] > KEEP_HOURS * 3600]
        for k in old:
            del self.records[k]
        return bool(old)

    def _make_room(self, source: str, t: float, strict: bool = True) -> bool:
        """A new session from `source`: past MAX_SESSIONS, forget its oldest finished or quiet one; if all are working
        now, refuse (TooMany when `strict`, else False)."""
        mine = [(r["at"], k) for k, r in self.records.items() if r["source"] == source]
        if len(mine) < MAX_SESSIONS:
            return True
        idle = sorted((at, k) for at, k in mine if self.records[k]["state"] == "done" or t - at > self.active_seconds)
        if idle:
            del self.records[idle[0][1]]
            return True
        if strict:
            raise TooMany(f"at most {MAX_SESSIONS} live sessions per machine")
        return False

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.log(f"live: {self.path.name} unreadable ({type(e).__name__}); starting with no agents")
            return
        t = self.clock()
        for r in (data.get("records") if isinstance(data, dict) else None) or []:
            try:
                rec = validate(r, self.clean)
                at = float(r.get("at"))
            except (ValueError, TypeError, AttributeError):
                continue
            source = str(r.get("source") or "")
            if not re.match(r"^(machine|github):.{1,200}$", source) or t - at > KEEP_HOURS * 3600:
                continue
            self.records[f"{source}|{rec['agent']}|{rec['session']}"] = {
                **rec, "source": source, "machine": str(r.get("machine") or "")[:80], "repo": str(r.get("repo") or "")[:140],
                "url": _web(r.get("url")), "at": min(at, t)}

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"version": 1, "records": list(self.records.values())}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:          # keeping it in memory still works; say why once per failure
            self.log(f"live: could not save {self.path.name}: {e}")


def _web(v) -> str:
    s = str(v or "").strip()
    return s if s.startswith("https://") and " " not in s and len(s) < 500 else ""
