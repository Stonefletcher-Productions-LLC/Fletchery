"""Where each change is in the whole process, and how long each step usually takes. For the Live tab's Changes card and
the plain-words line on each machine (devwatch.py). Standard library only.

Every pull request walks the same six steps:

    Written -> Quick checks -> Build & test -> Into <base branch> -> Staging build -> In a release

- **Quick checks** and **Build & test** are the PR's check runs, split by name: `pipeline.quick` lists the quick ones
  (regular expressions; by default anything named like cheap, quick, lint or format); every other check is Build & test.
- **Into <base>** is the merge (by itself when auto-merge is on).
- **Staging build** is the first push run on the base branch after the merge (LifeShare: Test-Staging, "Build and test").
- **In a release** is a GitHub release published after that.

How long each step takes is learned from what already happened (`History`): every successful quick-check set, build,
job step and staging run adds its duration, kept per repository, kind of change (`pipeline.kinds`, matched on the PR's
title, labels and branch: a town change takes longer) and machine, the last 20 of each. An estimate is the median of
the most specific set with at least 3 samples. Without one, the bars say they are still learning instead of guessing.

Profile, under `workers.portal.live` (all optional):

    "pipeline": {"quick": ["^cheap", "lint"], "kinds": {"Town change": ["town", "road", "terrain"]}}

Saved to `<state dir>/step-times.json` (replaced atomically). It holds durations and names only."""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path

import rules

STAGES = ("written", "quick", "build", "merge", "staging", "release")
TIMED = ("quick", "build", "staging")      # the steps the overall bar and "time left" are made of
KEEP = 20                                  # samples kept per key
MIN_SAMPLES = 3                            # an estimate needs this many, at the most specific level that has them
MAX_KEYS = 3000
LATE = 1.5                                 # a step past 1.5x its usual time is "taking longer than usual"
MERGED_HOURS = 24                          # merged changes followed through staging for this long
DONE_SHOWN = 2 * 3600                      # a change that reached its last step stays on the card this long
QUICK_DEFAULT = (r"cheap", r"quick", r"lint", r"format", r"\bstyle\b")
NOISE = re.compile(r"^(set up job|complete job|post |run actions/|checkout\b|clean ?up\b)", re.I)
FAILED = ("failure", "timed_out", "action_required", "startup_failure")
OK = ("success", "neutral", "skipped")
WHY = {
    "written": "The change is being written. Its checks start when it is pushed as a pull request.",
    "quick": "Fast checks of text, formatting and files, so a full build is not spent on a simple mistake.",
    "build": "Every change is built and tested on a real machine before it can merge.",
    "merge": "Its checks passed. Merging puts it into {base}, together with everything else that passed.",
    "staging": "After changes merge, the whole game is built again to make sure they work together. A green run here "
               "is what the next playable build comes from.",
    "release": "It is in {base} and its staging build passed. It reaches players with the next release.",
}


def ts(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _text(v, limit: int = 200) -> str:
    return str(v if v is not None else "")[:limit]


class Rules:
    """Which checks are quick, and what kind of change a PR is."""

    def __init__(self, conf=None):
        conf = conf if isinstance(conf, dict) else {}
        pats = [str(p) for p in conf.get("quick") or QUICK_DEFAULT][:20]
        self.quick = []
        for p in pats:
            try:
                self.quick.append(re.compile(p, re.I))
            except re.error:
                continue
        self.kinds = []
        for label, words in list((conf.get("kinds") or {}).items())[:20] if isinstance(conf.get("kinds"), dict) else []:
            ws = [str(w).lower() for w in (words if isinstance(words, list) else []) if str(w).strip()][:30]
            if ws:
                self.kinds.append((_text(label, 40), ws))

    def is_quick(self, name: str) -> bool:
        return any(p.search(str(name or "")) for p in self.quick)

    def kind(self, title: str, labels=(), branch: str = "") -> str:
        hay = " ".join([str(title or ""), " ".join(str(x) for x in labels or ()), str(branch or "")]).lower()
        for label, words in self.kinds:
            if any(w in hay for w in words):
                return label
        return "Change"


def group(checks: list) -> dict:
    """The state of a set of check runs: none, queued, running, failed, rerun or done; when it started and ended."""
    if not checks:
        return {"status": "none", "started": "", "completed": ""}
    going = [c for c in checks if c.get("status") != "completed"]
    failed = [c for c in checks if c.get("status") == "completed" and c.get("conclusion") in FAILED]
    cancelled = [c for c in checks if c.get("status") == "completed" and c.get("conclusion") == "cancelled"]
    started = min((c.get("started_at") or "" for c in checks if c.get("started_at")), default="")
    status = ("failed" if failed else "running" if any(c.get("status") == "in_progress" for c in going) else
              "queued" if going else "rerun" if cancelled else "done")
    completed = max((c.get("completed_at") or "" for c in checks), default="") if status == "done" else ""
    # When a failed or cancelled set stopped: its clock stops there, so the bar doesn't keep counting while it waits.
    stopped = max((c.get("completed_at") or "" for c in (failed if status == "failed" else cancelled)), default="") \
        if status in ("failed", "rerun") else ""
    return {"status": status, "started": started, "completed": completed, "stopped": stopped,
            "failed": [_text(c.get("name"), 80) for c in failed][:3]}


def steps_of(job: dict | None) -> list:
    """A job's meaningful steps (no set-up, checkout or clean-up noise): name, state, start and end."""
    out = []
    for s in (job or {}).get("steps") or []:
        name = _text(s.get("name"), 80)
        if not name or NOISE.match(name):
            continue
        state = ("failed" if s.get("conclusion") in FAILED else "done" if s.get("status") == "completed" else
                 "now" if s.get("status") == "in_progress" else "todo")
        out.append({"name": name, "state": state, "started": _text(s.get("started_at"), 30),
                    "completed": _text(s.get("completed_at"), 30)})
    return out[:16]


# ---- what each step usually takes ----
class History:
    def __init__(self, path=None, log=None):
        self.path = Path(path) if path else None
        self.log = log or (lambda line: None)
        self._lock = threading.Lock()
        self.samples: dict[str, list] = {}       # "a|b|c" -> [[seconds, sample id], ...] newest last
        self._dirty = False
        self._load()

    @staticmethod
    def key(*parts) -> str:
        return "|".join(str(p).replace("|", "/")[:120] for p in parts)

    def add(self, parts: tuple, seconds: float, sample: str) -> None:
        """One duration for `parts` and each shorter prefix of it (so a rare kind still has the general average)."""
        if not (0 < seconds < 48 * 3600):
            return
        with self._lock:
            for n in range(len(parts), 1, -1):
                k = self.key(*parts[:n])
                got = self.samples.setdefault(k, [])
                if any(s[1] == sample for s in got):
                    continue
                got.append([round(seconds, 1), str(sample)[:120]])
                del got[:-KEEP]
                self._dirty = True
            if len(self.samples) > MAX_KEYS:      # forget the keys with the fewest samples
                for k in sorted(self.samples, key=lambda k: len(self.samples[k]))[: len(self.samples) - MAX_KEYS]:
                    del self.samples[k]

    def estimate(self, parts: tuple, own_from: int = 0) -> tuple[float | None, int]:
        return self.estimate_at(parts, own_from)[:2]

    def estimate_at(self, parts: tuple, own_from: int = 0) -> tuple[float | None, int, int]:
        """(median seconds, samples, level) at the most specific level with MIN_SAMPLES, else the best level with any.
        From `own_from` parts on (the kind of change), a level's own samples, however few, beat a more general level:
        a town change's one 30-minute build says more about the next town change than fifteen 2-minute tooling runs
        (2026-11-19: "39 min of ~2 min, usually 2 min for a town change")."""
        with self._lock:
            fallback = (None, 0, 0)
            for n in range(len(parts), 1, -1):
                got = self.samples.get(self.key(*parts[:n])) or []
                if len(got) >= MIN_SAMPLES or (got and own_from and n == own_from):
                    return statistics.median(s[0] for s in got), len(got), n
                if got and fallback[0] is None:
                    fallback = (statistics.median(s[0] for s in got), len(got), n)
            return fallback

    def remaining_exact(self, parts: tuple, elapsed: float) -> tuple[int | None, int]:
        """Time left in a reservation of this kind on this machine, among holds lasting at least this long."""
        with self._lock:
            longer = [s[0] for s in self.samples.get(self.key(*parts), ()) if s[0] > elapsed]
        return (round(statistics.median(longer) - elapsed), len(longer)) if longer else (None, 0)

    def save(self) -> None:
        with self._lock:
            if not self._dirty or not self.path:
                return
            self._dirty = False
            data = json.dumps({"version": 3, "samples": self.samples})
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            self.log(f"live: could not save {self.path.name}: {e}")

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.log(f"live: {self.path.name} unreadable ({type(e).__name__}); step times start again")
            return
        # Version 1 timed commit statuses and skipped checks too (seconds long), so its quick and build times are
        # dropped and learned again from real runs. Version 2 learned builds only from green runs, which on a project
        # that skips the build for tooling changes are mostly those 2-minute skips: its build times go too.
        version = data.get("version", 1) if isinstance(data, dict) else 1
        stale = ("quick|", "build|") if version < 2 else ("build|",) if version < 3 else ()
        for k, got in ((data.get("samples") or {}) if isinstance(data, dict) else {}).items():
            if stale and isinstance(k, str) and (k.startswith(stale) or (k.startswith("own|") and k.endswith("|build")
                                                                       and "build|" in stale)):
                continue
            if isinstance(k, str) and isinstance(got, list):
                clean = [[float(s[0]), str(s[1])[:120]] for s in got[-KEEP:]
                         if isinstance(s, list) and len(s) == 2 and isinstance(s[0], (int, float)) and 0 < s[0] < 48 * 3600]
                if clean:
                    self.samples[k[:400]] = clean
            if len(self.samples) >= MAX_KEYS:
                break


# ---- learning from what finished ----
def _timed(checks: list) -> bool:
    """Only check runs that really ran count: a commit status (posted once, at the end) or a skipped or neutral check
    lasts seconds and dragged a town change's quick checks to "usually 1 min" (2026-11-19, #2346 took 12)."""
    return bool(checks) and all(c.get("source") != "status" and c.get("conclusion") == "success" for c in checks)


def _build_span(checks: list, jobs: dict) -> float | None:
    """How long a finished build took, green or red. A build that failed in its own work (a failed test after 45
    minutes) took that long too, and on a busy project most long builds end red: learning only from green ones
    learned mostly the quick skips (2026-11-19: every green LifeShare "Compile and test" of the day took 2 minutes,
    the red town builds 13 to 45). Not counted: a cancelled check, a commit status, a failure in set-up (checkout,
    the machine) or one whose job is not known."""
    if not checks or any(c.get("source") == "status" or c.get("status") != "completed" for c in checks):
        return None
    for c in checks:
        if c.get("conclusion") == "success":
            continue
        if c.get("conclusion") != "failure":
            return None
        if not any(p["state"] == "failed" and not rules.SETUP_STEP.search(p["name"]) for p in steps_of(jobs.get(c.get("id")))):
            return None
    start = min((ts(c.get("started_at")) for c in checks if c.get("started_at")), default=0.0)
    end = max((ts(c.get("completed_at")) for c in checks if c.get("completed_at")), default=0.0)
    return end - start if start and end > start else None


def learn_checks(h: History, repo: str, kind: str, sha: str, quick: list, build: list, jobs: dict, machine_of,
                 number=None) -> None:
    for name, checks in (("quick", quick), ("build", build)):
        # A skipped or neutral check (a job a workflow condition left out) did not run: it is not part of the time,
        # and it must not stop the rest from being timed (2026-11-19: no build time was ever learned again, because
        # LifeShare's PR check always has skipped jobs).
        checks = [c for c in checks if c.get("conclusion") not in ("skipped", "neutral")]
        if name == "build":
            took = _build_span(checks, jobs)
        else:
            g = group(checks)
            took = ts(g["completed"]) - ts(g["started"]) if g["status"] == "done" and g["started"] and g["completed"] \
                and _timed(checks) else None
        if took:
            if number is not None:      # this PR's own last runs: the same files, so the best guess for its next one
                h.add(("own", repo, str(number), name), took, f"{sha}:{name}")
            machine = ""
            for c in checks:
                job = jobs.get(c.get("id"))
                if job and job.get("runner_name"):
                    machine = machine_of(job["runner_name"])
            h.add((name, repo, kind, machine), took, f"{sha}:{name}")
    for c in build:
        learn_job(h, repo, kind, jobs.get(c.get("id")))


def learn_job(h: History, repo: str, kind: str, job: dict | None) -> None:
    if not job or job.get("status") != "completed" or job.get("conclusion") not in OK:
        return
    for s in steps_of(job):
        if s["state"] == "done" and s["started"] and s["completed"]:
            h.add(("step", repo, _text(job.get("name"), 80), s["name"], kind), ts(s["completed"]) - ts(s["started"]),
                  f"{job.get('id')}:{s['name']}")


def learn_staging(h: History, repo: str, run: dict, jobs: list) -> None:
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return
    start, end = ts(run.get("run_started_at") or run.get("created_at")), ts(run.get("updated_at"))
    if start and end:
        h.add(("staging", repo, "all"), end - start, f"run:{run.get('id')}")
    for job in jobs or []:
        learn_job(h, repo, "staging", job)


# ---- one change, where it is ----
def _stages(base: str, states: dict) -> list:
    labels = {"written": "Written", "quick": "Quick checks", "build": "Build & test", "merge": f"Into {base or 'main'}",
              "staging": "Staging build", "release": "In a release"}
    return [{"id": s, "label": labels[s], "state": states.get(s, "todo")} for s in STAGES]


def own_estimate(h: History, repo: str, number, stage: str) -> tuple[float | None, int]:
    """The median of this PR's own earlier runs of a step, if it has any."""
    if number is None or stage not in ("quick", "build"):
        return None, 0
    with h._lock:
        got = h.samples.get(h.key("own", repo, str(number), stage)) or []
        return (statistics.median(s[0] for s in got), len(got)) if got else (None, 0)


def _step(h: History, repo: str, kind: str, stage: str, started: str, at: float, machine: str = "", job: dict | None = None,
          number=None) -> dict:
    """The current step's own bar: how long it has run against how long it usually takes, and its parts."""
    avg, n = own_estimate(h, repo, number, stage)
    own = avg is not None
    scope = "own" if own else ""
    if not own:
        avg, n, level = h.estimate_at((stage, repo, "all" if stage == "staging" else kind, machine), own_from=3)
        scope = "kind" if level >= 3 else "all"
    elapsed = max(0.0, at - ts(started)) if started else 0.0
    out = {"elapsed": round(elapsed), "avg": round(avg) if avg else None, "samples": n, "machine": machine, "parts": [],
           "own": own, "scope": scope}
    if avg:
        out["pct"] = min(99, int(100 * elapsed / avg))
        out["left"] = round(max(0.0, avg - elapsed))
        out["late"] = elapsed > LATE * avg
    parts = steps_of(job)
    for p in parts:
        pa, _ = h.estimate(("step", repo, _text((job or {}).get("name"), 80), p["name"], kind))
        p["avg"] = round(pa) if pa else None
    if parts:
        now = next((i for i, p in enumerate(parts) if p["state"] in ("now", "failed")), None)
        if now is None:
            now = next((i for i, p in enumerate(parts) if p["state"] == "todo"), len(parts) - 1)
        out["parts"] = [{k: p[k] for k in ("name", "state", "avg")} for p in parts]
        out["part"] = now
        cur = parts[now]
        if cur["state"] == "now" and cur["started"]:
            out["part_elapsed"] = round(max(0.0, at - ts(cur["started"])))
    return out


def _overall(h: History, repo: str, kind: str, states: dict, cur: str, step: dict | None) -> dict:
    """The whole road from the first check to a green staging build, weighted by how long each step usually takes."""
    avgs = {}
    for s in TIMED:
        avgs[s], _ = h.estimate((s, repo, "all" if s == "staging" else kind), own_from=3)
    if any(v is None for v in avgs.values()):
        return {"pct": None, "left": None, "learning": True}
    total = sum(avgs.values())
    done = sum(avgs[s] for s in TIMED if states.get(s) in ("done", "skipped"))
    left = sum(avgs[s] for s in TIMED if states.get(s) in ("todo", None) and s != cur)
    if cur in TIMED and step:
        inside = min(step.get("elapsed") or 0, avgs[cur])
        done += inside
        left += max(0.0, avgs[cur] - (step.get("elapsed") or 0))
    if cur in ("release",):
        done, left = total, 0.0
    return {"pct": max(1, min(99, int(100 * done / total))) if total else None, "left": round(left), "learning": False}


def open_change(h: History, rules: Rules, repo: str, pr: dict, checks: list, jobs: dict, machine_of, at: float) -> dict:
    """An open PR: which of its six steps it is on, from its checks."""
    base = _text((pr.get("base") or {}).get("ref"), 80)
    kind = rules.kind(pr.get("title"), [x.get("name") for x in pr.get("labels") or [] if isinstance(x, dict)],
                      (pr.get("head") or {}).get("ref"))
    quick = [c for c in checks if rules.is_quick(c.get("name"))]
    build = [c for c in checks if not rules.is_quick(c.get("name"))]
    q, b = group(quick), group(build)
    states = {"written": "done"}
    status, cur, started, note, job = "", "", "", "", None
    if pr.get("draft"):
        cur, status, note = "written", "waiting", "A draft: checks run when it is marked ready."
        states["written"] = "now"
    elif q["status"] in ("none",) and b["status"] == "none":
        cur, status, note = "quick", "queued", "Waiting for its checks to start."
    elif q["status"] not in ("done", "none"):
        cur, status, started = "quick", q["status"], q["started"]
    else:
        states["quick"] = "done" if q["status"] == "done" else "skipped"
        if b["status"] == "none":
            cur, status, note = "build", "queued", "Waiting for the build to start."
        elif b["status"] != "done":
            cur, status, started = "build", b["status"], b["started"]
            running = [c for c in build if c.get("status") != "completed"] or build
            job = next((jobs.get(c.get("id")) for c in running if jobs.get(c.get("id"))), None)
        else:
            states["build"] = "done"
            cur = "merge"
            status = "waiting"
            note = ("Merges by itself in a moment." if pr.get("auto_merge") else
                    "Checks pass. Waiting for someone to merge it.")
    if cur and cur not in states:
        states[cur] = "failed" if status in ("failed",) else "now"
    if status == "failed":
        failed = (q if cur == "quick" else b).get("failed") or []
        note = "Failed: " + ", ".join(failed) + ". It starts again after a fix is pushed." if failed else note
    elif status == "rerun":
        note = "A check was cancelled or gave no result. Re-run it from the PR."
    machine = machine_of((job or {}).get("runner_name") or "") if job and job.get("runner_name") else ""
    stopped = (q if cur == "quick" else b).get("stopped") if status in ("failed", "rerun") and cur in ("quick", "build") else ""
    step = _step(h, repo, kind, cur, started, ts(stopped) if stopped else at, machine, job, pr.get("number")) \
        if cur in TIMED and started else None
    if step and stopped:
        step["late"], step["left"], step["stopped"] = False, None, _text(stopped, 30)
    if status == "queued" and cur in TIMED:
        avg, _ = own_estimate(h, repo, pr.get("number"), cur)
        if avg is None:
            avg, _ = h.estimate((cur, repo, kind), own_from=3)
        step = {"elapsed": 0, "avg": round(avg) if avg else None, "pct": 0 if avg else None, "left": round(avg) if avg else None,
                "late": False, "machine": "", "parts": []}
    return {"key": f"{repo}#{pr.get('number')}", "repo": repo, "number": pr.get("number"), "title": _text(pr.get("title")),
            "url": _text(pr.get("html_url"), 300), "kind": kind, "base": base, "merged": False,
            "stage": cur, "status": status, "stages": _stages(base, states), "step": step,
            "overall": _overall(h, repo, kind, states, cur, step), "note": note, "auto_merge": bool(pr.get("auto_merge")),
            "why": WHY.get(cur, "").format(base=base or "the main branch"), "updated": _text(pr.get("updated_at"), 30),
            **({"since": _text(stopped, 30)} if stopped else {})}


def merged_change(h: History, rules: Rules, repo: str, pr: dict, runs: list, jobs_by_run: dict, releases: list,
                  machine_of, at: float) -> dict | None:
    """A PR merged in the last day: its staging build and release, until it reached the end (then shown a while)."""
    merged = ts(pr.get("merged_at"))
    if not merged or at - merged > MERGED_HOURS * 3600:
        return None
    base = _text((pr.get("base") or {}).get("ref"), 80)
    kind = rules.kind(pr.get("title"), [x.get("name") for x in pr.get("labels") or [] if isinstance(x, dict)],
                      (pr.get("head") or {}).get("ref"))
    states = {s: "done" for s in ("written", "quick", "build", "merge")}
    pushes = sorted((r for r in runs if r.get("event") == "push" and r.get("head_branch") == base
                     and ts(r.get("created_at")) >= merged - 5 and r.get("conclusion") != "cancelled"),
                    key=lambda r: ts(r.get("created_at")))
    run = pushes[0] if pushes else None
    note, step, status, cur, since = "", None, "", "", ""
    if run is None:
        if at - merged > 30 * 60:
            return None        # this branch has no staging build (or it left the recent runs): nothing to follow
        cur, status, note = "staging", "queued", "Waiting for the staging build to start."
    elif run.get("status") != "completed":
        cur = "staging"
        status = "running" if run.get("status") == "in_progress" else "queued"
        jobs = jobs_by_run.get(run.get("id")) or []
        job = next((j for j in jobs if j.get("status") == "in_progress"), jobs[0] if jobs else None)
        machine = machine_of(job["runner_name"]) if job and job.get("runner_name") else ""
        step = _step(h, repo, "staging", "staging", run.get("run_started_at") or run.get("created_at"), at, machine, job) \
            if status == "running" else None
        note = f"Building {base} with this change and everything merged before it."
    elif run.get("conclusion") != "success":
        cur, status, note = "staging", "failed", f"The staging build failed ({_text(run.get('conclusion'), 20)})."
        since = run.get("updated_at") or ""
    else:
        states["staging"] = "done"
        done_at = ts(run.get("updated_at"))
        rel = next((r for r in releases or [] if ts(r.get("published_at") or r.get("created_at")) >= done_at), None)
        if rel:
            states["release"] = "done"
            cur, status, note = "release", "done", f"In {_text(rel.get('name') or rel.get('tag_name'), 60)}."
            done_at = ts(rel.get("published_at") or rel.get("created_at"))
        else:
            cur, status, note = "release", "waiting", "Waiting for the next release."
        if at - done_at > DONE_SHOWN:
            return None
    if cur not in states:
        states[cur] = "failed" if status == "failed" else "now"
    out = {"key": f"{repo}#{pr.get('number')}", "repo": repo, "number": pr.get("number"), "title": _text(pr.get("title")),
           "url": _text(pr.get("html_url"), 300), "kind": kind, "base": base, "merged": True, "stage": cur, "status": status,
           "stages": _stages(base, states), "step": step, "overall": _overall(h, repo, kind, states, cur, step), "note": note,
           "why": WHY.get(cur, "").format(base=base or "the main branch"), "updated": _text(pr.get("merged_at"), 30),
           "run": run.get("id") if run else None}
    if since:
        out["since"] = since
    return out
