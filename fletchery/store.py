"""What happened, kept: merges, runs, jobs, base-branch check results and machine disk readings, in one SQLite file in
the state folder (Python's own sqlite3, nothing to install). The Live feed (devwatch.py) is rebuilt from GitHub every
poll and forgets; this is what the Flow strip, base health, failure causes, queue estimates and alerts read when they
need more than "now".

Design (docs/DESIGN.md):
- Names and result values follow OpenTelemetry's CI/CD conventions where they exist: a run's `result` is one of
  success, failure, error, timeout, cancellation, skip; `error` means the system broke, `failure` that the code did.
- It holds names, numbers and times only: no logs, tokens, file contents or paths.
- Writes are upserts keyed by GitHub's own ids, so recording the same poll twice changes nothing.
- Rows older than `keep_days` (default 90) are deleted once a day.
- One connection, used under a lock: the backend has one writer (the feed) and a few readers (the page's requests).

Standard library only."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
KEEP_DAYS = 90

# GitHub conclusion -> OpenTelemetry cicd.pipeline.result
RESULT = {"success": "success", "failure": "failure", "timed_out": "timeout", "cancelled": "cancellation",
          "skipped": "skip", "neutral": "success", "action_required": "failure", "startup_failure": "error", "stale": "skip"}

TABLES = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS merges (
  repo TEXT NOT NULL, number INTEGER NOT NULL, base TEXT, title TEXT, author TEXT, agent TEXT,
  created REAL, merged REAL NOT NULL, PRIMARY KEY (repo, number));
CREATE INDEX IF NOT EXISTS merges_at ON merges (merged);
CREATE TABLE IF NOT EXISTS runs (
  repo TEXT NOT NULL, id INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, workflow TEXT, event TEXT, branch TEXT,
  sha TEXT, pr INTEGER, actor TEXT, result TEXT, created REAL, started REAL, finished REAL,
  PRIMARY KEY (repo, id, attempt));
CREATE INDEX IF NOT EXISTS runs_at ON runs (finished);
CREATE TABLE IF NOT EXISTS jobs (
  repo TEXT NOT NULL, id INTEGER NOT NULL, run INTEGER, name TEXT, workflow TEXT, runner TEXT, labels TEXT,
  result TEXT, created REAL, started REAL, finished REAL, PRIMARY KEY (repo, id));
CREATE INDEX IF NOT EXISTS jobs_at ON jobs (started);
CREATE TABLE IF NOT EXISTS base_checks (
  repo TEXT NOT NULL, branch TEXT NOT NULL, sha TEXT NOT NULL, name TEXT NOT NULL, result TEXT, at REAL,
  PRIMARY KEY (repo, branch, sha, name));
CREATE INDEX IF NOT EXISTS base_checks_at ON base_checks (repo, branch, at);
CREATE TABLE IF NOT EXISTS disks (machine TEXT NOT NULL, drive TEXT NOT NULL, at REAL NOT NULL, free_gb REAL,
  PRIMARY KEY (machine, drive, at));
CREATE TABLE IF NOT EXISTS events (kind TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, data TEXT,
  PRIMARY KEY (kind, key, at));
"""


def ts(iso) -> float | None:
    """GitHub's ISO time as seconds, or None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def iso(t: float | None) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else ""


def result_of(conclusion) -> str:
    return RESULT.get(str(conclusion or ""), "")


class Store:
    def __init__(self, path=None, keep_days: float = KEEP_DAYS, log=None):
        """`path` None keeps everything in memory (tests, or a backend with no state folder)."""
        self.log = log or (lambda line: None)
        self.keep = float(keep_days)
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=False, timeout=10)
        with self._lock:
            if self.path:
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(TABLES)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
            self.db.commit()
        self._pruned = 0.0

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _write(self, sql: str, rows) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    def query(self, sql: str, args=()) -> list:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    # ---- recording (from devwatch's poll) ----
    def merges(self, repo: str, prs: list, agent_of=None) -> None:
        """Merged pull requests (from the closed-PR list)."""
        self._write("INSERT OR REPLACE INTO merges VALUES (?,?,?,?,?,?,?,?)", (
            (repo, int(p["number"]), str((p.get("base") or {}).get("ref") or ""), str(p.get("title") or "")[:200],
             str((p.get("user") or {}).get("login") or "")[:60],
             (agent_of(str((p.get("head") or {}).get("ref") or ""), str((p.get("user") or {}).get("login") or "")) if agent_of else ""),
             ts(p.get("created_at")), ts(p["merged_at"]))
            for p in prs if isinstance(p, dict) and p.get("merged_at") and isinstance(p.get("number"), int)))

    def runs(self, repo: str, runs: list) -> None:
        """Finished Actions runs (a run still going is recorded when it ends)."""
        self._write("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            (repo, int(r["id"]), int(r.get("run_attempt") or 1), str(r.get("name") or "")[:120], str(r.get("event") or "")[:30],
             str(r.get("head_branch") or "")[:120], str(r.get("head_sha") or "")[:40],
             int(((r.get("pull_requests") or [{}])[0] or {}).get("number") or 0),
             str((r.get("triggering_actor") or r.get("actor") or {}).get("login") or "")[:60],
             result_of(r.get("conclusion")), ts(r.get("created_at")), ts(r.get("run_started_at")), ts(r.get("updated_at")))
            for r in runs if isinstance(r, dict) and isinstance(r.get("id"), int) and r.get("status") == "completed"))

    def jobs(self, repo: str, run: dict, jobs: list) -> None:
        """Jobs that started (queue wait = started - created) or finished."""
        created_run = ts((run or {}).get("created_at"))
        self._write("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            (repo, int(j["id"]), int((run or {}).get("id") or j.get("run_id") or 0), str(j.get("name") or "")[:120],
             str((run or {}).get("name") or j.get("workflow_name") or "")[:120], str(j.get("runner_name") or "")[:80],
             ",".join(str(x) for x in (j.get("labels") or []) if isinstance(x, str))[:300],
             result_of(j.get("conclusion")) if j.get("status") == "completed" else "",
             ts(j.get("created_at")) or created_run, ts(j.get("started_at")), ts(j.get("completed_at")))
            for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), int) and j.get("started_at")
            and j.get("status") in ("in_progress", "completed")))

    def base_checks(self, repo: str, branch: str, sha: str, checks: list) -> None:
        """The finished checks on a base branch's commit (base health reads the streak)."""
        self._write("INSERT OR REPLACE INTO base_checks VALUES (?,?,?,?,?,?)", (
            (repo, branch, sha, str(c.get("name") or "")[:120], result_of(c.get("conclusion")),
             ts(c.get("completed_at")) or time.time())
            for c in checks if isinstance(c, dict) and c.get("status") == "completed" and c.get("name")))

    def disk(self, machine: str, drive: str, free_gb: float, at: float | None = None) -> None:
        """One disk reading; at most one a minute is kept per drive."""
        t = at or time.time()
        last = self.query("SELECT MAX(at) FROM disks WHERE machine=? AND drive=?", (machine, drive))
        if last and last[0][0] and t - last[0][0] < 60:
            return
        self._write("INSERT OR REPLACE INTO disks VALUES (?,?,?,?)", [(machine, drive, t, float(free_gb))])

    def event(self, kind: str, key: str, data: str = "", at: float | None = None) -> None:
        """Anything else worth remembering (an alert fired or cleared, a problem opened or closed)."""
        self._write("INSERT OR REPLACE INTO events VALUES (?,?,?,?)", [(kind, key, at or time.time(), data[:2000])])

    def prune(self, now: float | None = None) -> None:
        """Delete rows older than `keep` days; once a day."""
        now = now or time.time()
        if now - self._pruned < 86400:
            return
        self._pruned = now
        cut = now - self.keep * 86400
        with self._lock:
            for table, col in (("merges", "merged"), ("runs", "finished"), ("jobs", "started"), ("base_checks", "at"),
                               ("disks", "at"), ("events", "at")):
                self.db.execute(f"DELETE FROM {table} WHERE {col} < ?", (cut,))
            self.db.commit()
