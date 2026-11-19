"""The stable read API (docs/API.md), for scripts, other dashboards and agents:

- `GET /api/v2/state`: the Live snapshot (devwatch.py) with `"schema": 2`. Fields are only ever added.
- `GET /api/v2/history?table=merges&since=<unix seconds>&repo=Owner/Repo&limit=1000`: rows from the history store
  (store.py), oldest first, so a reader can page with `since` = the last row's time.
- `GET /metrics`: the same numbers in Prometheus text format, for Grafana or any scraper.

All three answer only who the page answers (portal.py `allowed`). They hold names, numbers and times: no logs, tokens,
paths or file contents. Standard library only."""
from __future__ import annotations

import re

SCHEMA = 2
# table -> (time column, columns returned)
HISTORY = {
    "merges": ("merged", ("repo", "number", "base", "title", "author", "agent", "created", "merged", "sha")),
    "runs": ("finished", ("repo", "id", "attempt", "workflow", "event", "branch", "sha", "pr", "actor", "result", "created", "started", "finished")),
    "jobs": ("started", ("repo", "id", "run", "name", "workflow", "runner", "labels", "result", "created", "started", "finished")),
    "base_checks": ("at", ("repo", "branch", "sha", "name", "result", "at")),
    "disks": ("at", ("machine", "drive", "at", "free_gb")),
    "events": ("at", ("kind", "key", "at", "data")),
}
MAX_ROWS = 5000
REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


def history(store, table: str, since: float = 0.0, repo: str = "", limit: int = 1000) -> dict:
    """Rows of one history table at or after `since`, oldest first. Raises ValueError for a bad question."""
    if table not in HISTORY:
        raise ValueError("table: one of " + ", ".join(HISTORY))
    col, cols = HISTORY[table]
    try:
        since, limit = float(since or 0), int(limit or 1000)
    except (TypeError, ValueError):
        raise ValueError("since is unix seconds and limit a number")
    limit = max(1, min(MAX_ROWS, limit))
    where, args = f"{col} >= ?", [since]
    if repo:
        if "repo" not in cols or not REPO.match(repo):
            raise ValueError("repo: Owner/Repo, for a table that has one")
        where += " AND repo = ?"
        args.append(repo)
    rows = store.query(f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY {col} ASC LIMIT ?", tuple(args) + (limit,))
    out = [dict(zip(cols, r)) for r in rows]
    return {"schema": SCHEMA, "table": table, "rows": out,
            "next": out[-1][col] if len(out) == limit else None}


def _label(v) -> str:
    return str(v if v is not None else "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:120]


class _Out:
    def __init__(self):
        self.lines, self.seen = [], set()

    def add(self, name: str, help_: str, value, labels: dict | None = None, kind: str = "gauge") -> None:
        if value is None:
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        if name not in self.seen:
            self.seen.add(name)
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        lab = ",".join(f'{k}="{_label(v)}"' for k, v in (labels or {}).items())
        self.lines.append(f"{name}{{{lab}}} {value:g}" if lab else f"{name} {value:g}")


def metrics(snap: dict) -> str:
    """The Live snapshot as Prometheus text (exposition format 0.0.4)."""
    o = _Out()
    s = snap or {}
    fresh, counts = s.get("fresh") or {}, s.get("counts") or {}
    o.add("fletchery_up", "1 while the backend answers", 1)
    o.add("fletchery_github_ok", "1 when the last GitHub read worked", 0 if fresh.get("error") else 1)
    o.add("fletchery_github_rate_remaining", "GitHub API calls left this hour", fresh.get("remaining"))
    for k in ("machines", "busy", "runs", "queued", "prs", "issues", "blockers", "agents", "changes"):
        o.add(f"fletchery_{k}", f"Live count: {k}", counts.get(k))
    a = s.get("alerts") or {}
    by: dict = {}
    for state in ("firing", "pending"):
        for x in a.get(state) or []:
            st = "snoozed" if x.get("snoozed_until") else state
            by[(st, x.get("kind"))] = by.get((st, x.get("kind")), 0) + 1
    for (st, kind), n in sorted(by.items(), key=str):
        o.add("fletchery_alerts", "Alerts by state (firing, pending, snoozed) and kind", n, {"state": st, "kind": kind})
    for m in s.get("machines") or []:
        o.add("fletchery_machine_online", "1 while the machine reports", 1 if m.get("online") else 0, {"machine": m.get("name")})
        o.add("fletchery_machine_busy", "1 while the machine works", 1 if m.get("busy") else 0, {"machine": m.get("name")})
        st = m.get("stats") or {}
        o.add("fletchery_machine_cpu_percent", "CPU use", st.get("cpu"), {"machine": m.get("name")})
        o.add("fletchery_machine_ram_used_gb", "RAM in use", st.get("ram_used_gb"), {"machine": m.get("name")})
        for d in m.get("disks") or []:
            lab = {"machine": m.get("name"), "drive": d.get("name")}
            o.add("fletchery_disk_free_gb", "Free disk", d.get("free_gb"), lab)
            o.add("fletchery_disk_full_in_seconds", "When the drive is full at its recent rate", d.get("full_in"), lab)
    for r in s.get("runners") or []:
        lab = {"runner": r.get("name"), "machine": r.get("machine")}
        o.add("fletchery_runner_online", "1 while the runner is online", 1 if r.get("online") else 0, lab)
        o.add("fletchery_runner_busy", "1 while the runner runs a job", 1 if r.get("busy") else 0, lab)
    for p in (s.get("queue") or {}).get("pools") or []:
        lab = {"pool": p.get("name")}
        o.add("fletchery_pool_workers", "Runners in the pool", p.get("workers"), lab)
        o.add("fletchery_pool_busy", "Busy runners in the pool", p.get("busy"), lab)
        o.add("fletchery_pool_waiting", "Jobs waiting for the pool", p.get("waiting_n", len(p.get("waiting") or [])), lab)
        o.add("fletchery_pool_wait_p50_seconds", "Median wait for a runner today", p.get("wait_p50"), lab)
        o.add("fletchery_pool_wait_p90_seconds", "p90 wait for a runner today", p.get("wait_p90"), lab)
        o.add("fletchery_pool_utilisation", "Share of the last day the pool's runners were busy", p.get("util"), lab)
    for h in s.get("bases") or []:
        lab = {"repo": h.get("repo"), "branch": h.get("branch")}
        o.add("fletchery_base_red", "1 while the base branch is red", 1 if h.get("state") == "red" else 0, lab)
        o.add("fletchery_base_red_seconds", "How long the base branch has been red", h.get("for") if h.get("state") == "red" else 0, lab)
    flow = s.get("flow") or {}
    for repo, f in sorted((flow.get("repos") or {}).items()):
        lab = {"repo": repo}
        o.add("fletchery_merges_last_hour", "Merges in the last hour", f.get("merges_hour"), lab)
        o.add("fletchery_merges_last_day", "Merges in the last day", f.get("merges_day"), lab)
        o.add("fletchery_since_merge_seconds", "Time since the last merge", f.get("since_merge"), lab)
        o.add("fletchery_lead_time_p50_seconds", "Median PR opened to merged, last week", f.get("lead_p50"), lab)
        o.add("fletchery_waiting_changes", "Open PRs on their way to merging", f.get("waiting"), lab)
    causes = s.get("causes") or {}
    for k in ("own", "base", "machine", "flaky", "cancelled"):
        o.add("fletchery_red_prs", "Red PR checks by cause", causes.get(k), {"cause": k})
    return "\n".join(o.lines) + "\n"
