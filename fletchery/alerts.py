"""Alerts: which of the things the Live feed flags reach a person, and when (Grafana's alerting rules, small).

Every blocker the feed finds (devwatch.py: a red base, an own bug, a silent machine, a low disk, a frozen hold, a run
nobody picks up, nothing merged for hours...) is a *signal* with a stable key. An alert rule per kind decides:

- **for**: how long a signal must last before it fires (a pending period, so a blip never pages anyone);
- **keepFiring**: how long a fired alert stays up after its signal goes, so one that flaps does not fire again and
  again;
- **notify**: whether it reaches the phone at all (off: it still shows on the page).

Fired alerts are grouped (by project or machine) so the phone gets one notification for a group that starts together,
and one quiet "cleared" when it ends. Any alert, or a whole group, can be snoozed for 1, 4 or 24 hours from the page.

State (what is pending, firing, snoozed) is one small JSON file in the state folder; fired and cleared alerts are also
written to the history store (store.py events) for the record.

Profile, under `workers.portal.live.alerts` (minutes; all optional):

    "alerts": {"disk": {"for": 5}, "flow": {"for": 0, "notify": false}, "runner": {"off": true}}

Standard library only."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# kind -> (for, keepFiring) in seconds. Kinds that already wait in their own rule (a run waiting 20 min, a step over
# 150 min) fire at once.
DEFAULTS = {
    "feed": (180, 120),       # GitHub can't be read
    "machine": (180, 300),    # a machine stopped reporting
    "runner": (300, 300),     # a runner offline
    "disk": (300, 900),       # low disk, or forecast to fill
    "base": (0, 0),           # a base branch is red
    "check": (0, 0),          # an own bug (rules.problems)
    "stuck": (0, 120),        # a hold, step, queue or run past its limit
    "label": (0, 0),          # an issue or PR waiting on a person
    "flow": (0, 600),         # nothing merged for hours while work waits
}
SNOOZE_HOURS = (1, 4, 24)
RESOLVED_SHOWN = 2 * 3600     # a cleared alert stays on the page (and in the summary for the phone) this long
MAX_KEYS = 2000


def group_of(b: dict) -> str:
    """Which group a signal belongs to: its project, else its machine ("machine:SF-NODE-01"), else its kind."""
    if b.get("project"):
        return str(b["project"])
    if b.get("repo"):
        return str(b["repo"])
    key = str(b.get("key") or "")
    parts = key.split(":")
    if len(parts) >= 2 and parts[0] in ("machine", "disk", "disklow", "diskfull", "diskfc", "lease", "idle", "longhold", "queue", "runner"):
        return "machine:" + parts[1]
    return str(b.get("kind") or "other")


class Alerts:
    def __init__(self, path=None, conf: dict | None = None, store=None, log=None):
        self.path = Path(path) if path else None
        self.store = store
        self.log = log or (lambda line: None)
        self.rules: dict[str, dict] = {}
        conf = conf if isinstance(conf, dict) else {}
        for kind, (wait, keep) in DEFAULTS.items():
            c = conf.get(kind) if isinstance(conf.get(kind), dict) else {}
            try:
                self.rules[kind] = {"for": 60 * float(c["for"]) if "for" in c else wait,
                                    "keep": 60 * float(c["keepFiring"]) if "keepFiring" in c else keep,
                                    "notify": bool(c.get("notify", True)), "off": bool(c.get("off", False))}
            except (TypeError, ValueError):
                self.log(f"alerts: rule {kind!r} ignored: give minutes as numbers")
                self.rules[kind] = {"for": wait, "keep": keep, "notify": True, "off": False}
        self._lock = threading.Lock()
        # key -> {"first", "last", "fired", "title", "detail", "url", "kind", "group", "repo", "cleared"}
        self.state: dict[str, dict] = {}
        self.snoozed: dict[str, float] = {}      # key or "group:<group>" -> until
        self._saved = ""
        self._load()

    def rule(self, kind: str) -> dict:
        return self.rules.get(kind) or {"for": 0, "keep": 0, "notify": True, "off": False}

    # ---- state ----
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = {str(k): v for k, v in (data.get("state") or {}).items() if isinstance(v, dict)}
            self.snoozed = {str(k): float(v) for k, v in (data.get("snoozed") or {}).items()}
        except (OSError, ValueError, TypeError) as e:
            self.log(f"alerts: could not read {self.path.name}: {e}; starting fresh")

    def _save(self) -> None:
        if not self.path:
            return
        body = json.dumps({"state": self.state, "snoozed": self.snoozed}, sort_keys=True)
        if body == self._saved:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self.path)
            self._saved = body
        except OSError as e:
            self.log(f"alerts: could not save: {e}")

    # ---- snooze ----
    def snooze(self, key: str = "", group: str = "", hours: float = 1, now: float | None = None) -> dict:
        """Snooze one alert (`key`) or a whole group for `hours` (1, 4 or 24); 0 ends a snooze."""
        now = now or time.time()
        if not key and not group:
            raise ValueError("Say which alert or group to snooze.")
        if hours not in (0,) + SNOOZE_HOURS:
            raise ValueError("Snooze for 1, 4 or 24 hours (0 ends it).")
        target = f"group:{group}" if group else str(key)
        with self._lock:
            if hours:
                self.snoozed[target] = now + 3600 * float(hours)
            else:
                self.snoozed.pop(target, None)
            self._save()
        return {"ok": True, "until": self.snoozed.get(target, 0)}

    def _snoozed_until(self, key: str, group: str, now: float) -> float:
        return max(self.snoozed.get(key, 0), self.snoozed.get(f"group:{group}", 0)) if (
            self.snoozed.get(key, 0) > now or self.snoozed.get(f"group:{group}", 0) > now) else 0

    # ---- the rules ----
    def evaluate(self, signals: list, now: float | None = None) -> dict:
        """Take this moment's signals (devwatch blockers) and say what is pending, firing, snoozed and recently
        cleared. Idempotent for the same moment, so every page read may call it."""
        now = now or time.time()
        events = []
        with self._lock:
            seen = set()
            for b in signals:
                key = str(b.get("key") or b.get("title") or "")
                if not key:
                    continue
                r = self.rule(str(b.get("kind") or "stuck"))
                if r["off"]:
                    continue
                seen.add(key)
                s = self.state.get(key)
                if s is None or s.get("cleared"):
                    s = self.state[key] = {"first": now, "fired": 0}
                s.update({"last": now, "kind": str(b.get("kind") or "stuck"), "title": str(b.get("title") or "")[:200],
                          "detail": str(b.get("detail") or "")[:300], "url": str(b.get("url") or ""), "repo": str(b.get("repo") or ""),
                          "group": group_of(b), "since": str(b.get("since") or "")})
                if not s["fired"] and now - s["first"] >= r["for"]:
                    s["fired"] = now
                    events.append(("fired", key, s))
            for key, s in list(self.state.items()):
                if key in seen or s.get("cleared"):
                    continue
                if not s.get("fired"):
                    del self.state[key]            # a pending signal that went away never fired: forget it
                    continue
                if now - s.get("last", now) >= self.rule(s.get("kind", "stuck"))["keep"]:
                    s["cleared"] = now
                    events.append(("cleared", key, s))
            for key, s in list(self.state.items()):
                if s.get("cleared") and now - s["cleared"] > RESOLVED_SHOWN:
                    del self.state[key]
            self.snoozed = {k: v for k, v in self.snoozed.items() if v > now}
            if len(self.state) > MAX_KEYS:
                for key in sorted(self.state, key=lambda k: self.state[k].get("last", 0))[: len(self.state) - MAX_KEYS]:
                    del self.state[key]
            out = {"firing": [], "pending": [], "cleared": []}
            for key, s in self.state.items():
                row = {"key": key, "kind": s.get("kind"), "group": s.get("group"), "title": s.get("title"), "detail": s.get("detail"),
                       "url": s.get("url"), "repo": s.get("repo"), "since": s.get("since"), "first": s.get("first"),
                       "fired": s.get("fired") or 0, "notify": self.rule(s.get("kind", "stuck"))["notify"]}
                until = self._snoozed_until(key, s.get("group", ""), now)
                if until:
                    row["snoozed_until"] = until
                if s.get("cleared"):
                    out["cleared"].append({**row, "cleared": s["cleared"]})
                elif s.get("fired"):
                    out["firing"].append(row)
                else:
                    row["fires_in"] = round(max(0.0, self.rule(s.get("kind", "stuck"))["for"] - (now - s["first"])))
                    out["pending"].append(row)
            self._save()
        for what, key, s in events:
            if self.store is not None:
                try:
                    self.store.event(f"alert-{what}", key, json.dumps({"title": s.get("title"), "group": s.get("group")}), now)
                except Exception as e:      # the record is a nicety
                    self.log(f"alerts: could not record: {e}")
        for k in ("firing", "pending", "cleared"):
            out[k].sort(key=lambda r: -(r.get("fired") or r.get("first") or 0))
        return out
