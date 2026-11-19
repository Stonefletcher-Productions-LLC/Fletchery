"""The Fletchery backend: one process on the PC that

  - serves the app (ui/portal/) to the desktop window on this PC (127.0.0.1:<dashboardPort>) and to the phone
    through Tailscale Serve (portal.py), with the controls: builds, machine jobs, machines, GitHub Actions runs, and
    the backend itself (actions() below);
  - watches all development on GitHub and on every machine for the Live tab (devwatch.py, reporter.py);
  - runs the build machines' job queue (machines.py) and scheduled builds with their downloads (builds.py, publish.py);
  - keeps scratch folders from filling the disk (scratch.py).

It writes nothing to GitHub except build releases and the Actions controls (cancel, re-run) someone presses. `python -m fletchery run --profile <file>` starts it; it exits with
75 when its own code changed on disk (an install), so its supervisor starts the new code at once.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
RESTART_EXIT = 75       # new code on disk, or Restart pressed: the supervisor starts it again at once
BUSY_EXIT = 77          # another backend already runs on this state folder: not an error, do not retry quickly
TICK_SECONDS = 15
SCRATCH_EVERY = 30 * 60
NO_MACHINE_MINUTES = 10  # a queued build no machine can take is flagged after this long
WORKER_DOWN_MINUTES = 10  # a registered build machine whose worker stopped calling in is flagged after this long


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(ts: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def code_fingerprint() -> str:
    """The backend's own Python, by content (tests excluded: changing a test needs no restart)."""
    digest = hashlib.sha256()
    for f in sorted(HERE.glob("*.py")):
        if f.name.startswith("test_"):
            continue
        try:
            digest.update(f.name.encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
        except OSError:
            continue
    return digest.hexdigest()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Busy(RuntimeError):
    pass


def take_lock(state_dir: Path) -> Path:
    """One backend per state folder. Created atomically (O_EXCL), so two starting at once cannot both win; a lock whose
    process is gone is taken over."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "fletchery.lock"
    for _ in range(3):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock.read_text().split()[0])
            except (OSError, ValueError, IndexError):
                pid = 0
            if pid and pid != os.getpid() and pid_alive(pid):
                raise Busy(f"Fletchery is already running (pid {pid}, {lock}). Not starting a second one.")
            try:
                lock.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()}\n")
        return lock
    raise Busy(f"Could not take {lock}.")


class State:
    """Scheduled builds (builds.py), in <state>/builds.json. The first start after the upgrade takes them from the old
    engine's state.json."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "builds.json"
        self.builds: list = []
        try:
            self.builds = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                old = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
                self.builds = list(old.get("builds") or [])
            except (OSError, ValueError, AttributeError):
                self.builds = []
        if not isinstance(self.builds, list):
            self.builds = []

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.builds, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class App:
    def __init__(self, cfg, log_print=print):
        from github import GitHub
        self.cfg = cfg
        self.state_dir = Path(cfg.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.started = now_iso()
        self.code = code_fingerprint()
        self.version = self.code[:8]   # shown in Activity: which code is running
        self._print = log_print
        self.lines: deque = deque(maxlen=400)
        self._log_lock = threading.Lock()
        self._load_activity()
        self.gh = GitHub(cfg.repo)
        self.state = State(self.state_dir)
        self.machines = None
        self.portal = None
        self.errors: dict[str, str] = {}
        self.restart_reason = ""
        self.scratch_last: dict = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        from builds import Builds
        self.builds = Builds(self)

    # ---- activity ----
    def _load_activity(self) -> None:
        try:
            with open(self.state_dir / "activity.jsonl", encoding="utf-8") as f:
                for line in f.readlines()[-400:]:
                    try:
                        self.lines.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass

    def log(self, line: str) -> None:
        row = {"at": now_iso(), "line": str(line)[:600]}
        with self._log_lock:
            self.lines.append(row)
            try:
                path = self.state_dir / "activity.jsonl"
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                if path.stat().st_size > 512 * 1024:   # bounded: keep the newest 400 lines
                    path.write_text("".join(json.dumps(r) + "\n" for r in self.lines), encoding="utf-8")
            except OSError:
                pass
        try:
            self._print(f"{row['at']} {row['line']}", flush=True)
        except Exception:
            pass

    def wake(self) -> None:
        self._wake.set()

    # ---- parts ----
    def start(self, dashboard_port: int | None = None) -> None:
        if self.cfg.machines:
            try:
                from machines import Coordinator
                self.machines = Coordinator(self.cfg, self.gh, self.log)
                self.machines.start()
            except Exception as e:   # a bad machines block must not stop the rest
                self.machines = None
                self.errors["machines"] = f"Build machines not started: {type(e).__name__}: {e}"
                self.log(self.errors["machines"])
        import layout
        import portal
        try:
            conf = portal.settings(self.cfg.portal) or {"port": 0, "hosts": [], "users": [], "push": None, "live": {}}
        except ValueError as e:
            self.errors["portal"] = f"workers.portal: {e}"
            self.log(self.errors["portal"])
            conf = {"port": 0, "hosts": [], "users": [], "push": None, "live": {}}
        port = self.cfg.dashboard_port if dashboard_port is None else dashboard_port
        self.portal = portal.Portal(self.status, conf, self.state_dir, self.log, self.cfg.repo, desktop_port=port,
                                    actions=self.actions(), job_log=self.job_log,
                                    layout=layout.Layout(layout.path_for(getattr(self.cfg, "_path", None), self.state_dir)))
        if self.portal.live is not None:
            self.portal.live.extra = self.machine_blockers
            self.portal.live.own_builds = self.machine_jobs
            api = getattr(self.machines, "api", None)
            if api is not None:
                api.reports = self.portal.live
        self.portal.start()
        for name, why in self.portal.errors.items():
            self.errors[name] = why
            self.log(why)
        threading.Thread(target=self._run, name="fletchery-tick", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.portal:
            self.portal.stop()
        if self.machines:
            self.machines.stop()

    def _run(self) -> None:
        last_scratch = 0.0
        while not self._stop.is_set():
            try:
                self.builds.tick()
                self.errors.pop("builds", None)
            except Exception as e:
                self.errors["builds"] = f"Builds: {type(e).__name__}: {e}"[:300]
            if self.cfg.scratch_roots and self.cfg.scratch_mode != "off" and time.monotonic() - last_scratch > SCRATCH_EVERY:
                last_scratch = time.monotonic()
                threading.Thread(target=self._scratch, name="fletchery-scratch", daemon=True).start()
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    def _scratch(self) -> None:
        import scratch
        c = self.cfg
        try:
            found = scratch.retain(list(c.scratch_roots), keep=list(c.scratch_keep or []), mode=c.scratch_mode,
                                   trim_hours=c.scratch_trim_hours, remove_days=c.scratch_remove_days,
                                   disposable=list(c.scratch_disposable or []), protected=[str(self.state_dir)],
                                   backup_dir=c.scratch_backup_dir or str(self.state_dir / "scratch-backups"),
                                   plan_file=str(self.state_dir / "scratch-plan.json"))
            self.scratch_last = {"checked_at": found["checked_at"], "mode": found["mode"], "error": found["error"],
                                 "waiting": found.get("waiting") or "",
                                 "freed_gb": round(found["freed_bytes"] / 1024 ** 3, 1), "trimmed": len(found["trimmed"]),
                                 "quarantined": len(found["quarantined"]), "removed": len(found["removed"])}
            if found["freed_bytes"] or found["quarantined"]:
                self.log(f"Scratch: trimmed {len(found['trimmed'])} build-output folders, quarantined "
                         f"{len(found['quarantined'])} idle worktrees, removed {len(found['removed'])} "
                         f"({found['freed_bytes'] / 1024 ** 3:.1f} GiB)")
            if found.get("waiting") and found["waiting"] != getattr(self, "_scratch_waiting", ""):
                self.log(f"Scratch: {found['waiting']}")
            self._scratch_waiting = found.get("waiting") or ""
            if found["error"]:
                self.log(f"Scratch: stopped: {found['error']}")
        except Exception as e:
            self.scratch_last = {"checked_at": now_iso(), "error": f"{type(e).__name__}: {e}"[:300]}

    # ---- what the screens read ----
    def machine_blockers(self) -> list:
        """For Live: what only this backend knows. A build machine whose worker stopped calling in (it cannot take
        builds), and a build or job that waits with no machine able to take it."""
        out = []
        if self.machines is None:
            return out
        view = self.machines.machines.view(jobs=60)
        waiting = [j for j in view["jobs"] if j.get("state") == "queued"]
        for m in view["machines"]:
            if m.get("revoked") or m.get("online"):
                continue
            seen = _age(m.get("last_seen") or "")
            if m.get("last_seen") and WORKER_DOWN_MINUTES * 60 < seen < 7 * 86400:
                out.append({"kind": "machine", "key": f"worker:{m['name']}", "since": m.get("last_seen") or "",
                            "title": f"{m['name']}: its build worker stopped calling in",
                            "detail": "Builds cannot run there until it is back (the 'Fletchery node worker' task)."})
        for j in waiting:
            if _age(j.get("created") or "") > NO_MACHINE_MINUTES * 60:
                out.append({"kind": "stuck", "key": f"job:{j.get('id')}", "since": j.get("created") or "",
                            "title": f"{j.get('requested_by') or 'Job ' + str(j.get('id'))} has waited "
                                     f"{int(_age(j.get('created') or '') // 60)} min for a build machine",
                            "detail": "No online machine with the labels it needs has taken it."})
        return out

    def machine_jobs(self) -> list:
        """For Live: the builds this backend runs on its machines, as machine activities ("Building now")."""
        if self.machines is None:
            return []
        out = []
        for m in self.machines.machines.view(jobs=1)["machines"]:
            j = m.get("job")
            if m.get("revoked") or not j or j.get("state") != "running" or not j.get("machine"):
                continue
            params = j.get("params") or {}
            what = f"Fletchery build {params.get('version')}" if j.get("recipe") == "package" else f"Fletchery job: {j.get('recipe')}"
            step = next((x.get("name") for x in reversed(j.get("steps") or []) if x.get("state") == "running"), "")
            out.append({"machine": j["machine"], "kind": "building", "since": j.get("started") or "",
                        "title": f"{what} (job #{j.get('id')})",
                        "detail": " · ".join(x for x in (f"step: {step}" if step else "", f"from {j.get('ref')}" if j.get("ref") else "") if x)})
        return out

    def status(self) -> dict:
        builds = self.builds.view()
        machines = self.machines.machines.view(jobs=40) if self.machines else None
        if self.machines:   # the worker API retries its bind after boot; show its problem only while it lasts
            if self.machines.error:
                self.errors["machines"] = self.machines.error
            else:
                self.errors.pop("machines", None)
        return {"project": self.cfg.name, "repo": self.cfg.repo, "pid": os.getpid(), "started": self.started,
                "version": self.version,
                "at": now_iso(), "errors": dict(self.errors), "builds": builds, "machines": machines,
                "scratch": self.scratch_last, "log": list(self.lines)[-120:], "restarting": self.restart_reason}

    # ---- the controls (portal.py decides who may use them: the window always, the phone unless controls are off) ----
    def actions(self) -> dict:
        def need_machines():
            if self.machines is None:
                raise ValueError("No build machines are set up (workers.machines).")
            return self.machines.machines

        def src(d):
            return str(d.get("_from") or "the window")

        def job_cancel(d):
            job = int(d.get("id") or 0)
            state = need_machines().cancel(job)
            self.log(f"machines: job {job} {'stopping' if state == 'stopping' else state} (from {src(d)})")
            return {"state": state}

        def machine_pause(d):
            name, on = str(d.get("name") or ""), bool(d.get("on"))
            out = need_machines().set_paused(name, str(d.get("kind") or "builds"), on)
            self.log(f"machines: {name} {'paused' if on else 'resumed'} (from {src(d)})")
            return {"paused": out}

        def job_retry(d):
            job = need_machines().retry(int(d.get("id") or 0), f"run again from {src(d)}")
            self.log(f"machines: job {int(d.get('id') or 0)} queued again as job {job} (from {src(d)})")
            return {"id": job}

        def hold(d):
            on = need_machines().set_held(bool(d.get("on")))
            self.log(f"machines: all paused, running jobs finish (from {src(d)})" if on else f"machines: resumed (from {src(d)})")
            return {"held": on}

        def build(fn, what):
            def go(d):
                out = fn(str(d.get("id") or ""))
                self.log(f"builds: {what} (from {src(d)})")
                return out
            return go

        def run_action(d, what):
            """GitHub Actions: cancel a run, or re-run it (all jobs, or only the failed ones). Only for a repository
            the Live tab watches, and a run id that is a number."""
            repo, run = str(d.get("repo") or ""), int(d.get("id") or 0)
            live = self.portal.live if self.portal else None
            known = {r["full_name"] for r in (live.repos if live else [])}
            if run <= 0 or repo not in known:
                raise ValueError("Unknown repository or run.")
            if what == "rerun-job":     # one job (a check run's id is its job's id): a check that gave no result
                self.gh.api(f"repos/{repo}/actions/jobs/{run}/rerun", "POST", {})
                self.log(f"Actions: re-run job {run} in {repo} (from {src(d)})")
            else:
                path = {"cancel": "cancel", "rerun": "rerun", "rerun-failed": "rerun-failed-jobs"}[what]
                self.gh.api(f"repos/{repo}/actions/runs/{run}/{path}", "POST", {})
                self.log(f"Actions: {what} run {run} in {repo} (from {src(d)})")
                if what == "cancel" and hasattr(live, "note_cancel"):
                    live.note_cancel(repo, run, f"from {src(d)}")
            if live is not None:
                live._wake.set()
            return {"ok": True}

        def live_refresh(d):
            if self.portal and self.portal.live is not None:
                self.portal.live._wake.set()
            self.wake()
            return {"ok": True}

        def alert_snooze(d):
            """Snooze one alert (key) or a whole group for 1, 4 or 24 hours (0 ends it): it stays on the page, the phone
            is not told."""
            live = self.portal.live if self.portal else None
            if live is None or not hasattr(live, "alerts"):
                raise ValueError("The Live feed is off.")
            try:
                hours = float(d.get("hours", 1))
            except (TypeError, ValueError):
                raise ValueError("Snooze for 1, 4 or 24 hours (0 ends it).")
            out = live.alerts.snooze(str(d.get("key") or "")[:200], str(d.get("group") or "")[:200], hours)
            what = f"group {d.get('group')}" if d.get("group") else f"alert {d.get('key')}"
            self.log(f"alerts: {what} " + (f"snoozed {hours:g} h" if hours else "unsnoozed") + f" (from {src(d)})")
            return out

        def restart(d):
            self.restart_reason = f"Restart pressed in {src(d)}."
            self.log(f"restart requested from {src(d)}")
            return {"ok": True}

        def add(d):
            return self.builds.add({k: v for k, v in d.items() if k != "_from"})

        return {"/api/builds/add": add, "/api/builds/cancel": build(self.builds.cancel, "stop or cancel"),
                "/api/builds/now": build(self.builds.run_now, "start now"),
                "/api/builds/retry": build(self.builds.retry, "build again"),
                "/api/jobs/cancel": job_cancel, "/api/jobs/retry": job_retry,
                "/api/machines/pause": machine_pause, "/api/machines/hold": hold,
                "/api/actions/cancel": lambda d: run_action(d, "cancel"),
                "/api/actions/rerun": lambda d: run_action(d, "rerun-failed" if d.get("failed") else "rerun"),
                "/api/actions/rerun-job": lambda d: run_action(d, "rerun-job"),
                "/api/live/refresh": live_refresh, "/api/alerts/snooze": alert_snooze, "/api/restart": restart}

    def job_log(self, job_id: int) -> str:
        return self.machines.machines.log_text(job_id, tail_bytes=256 * 1024) if self.machines else ""

    def job_steps(self, job_id: int) -> dict:
        return self.machines.machines.job_steps(job_id) if self.machines else {}

    # ---- run ----
    def serve(self, check_every: float = 5.0) -> int:
        """Until Ctrl+C, a restart request, or new code on disk. Returns the exit code."""
        start_code = self.code
        try:
            while not self._stop.wait(check_every):
                if self.restart_reason:
                    return RESTART_EXIT
                if code_fingerprint() != start_code:
                    self.restart_reason = "New Fletchery code was installed."
                    self.log("new code on disk: restarting")
                    return RESTART_EXIT
        except KeyboardInterrupt:
            return 0
        return 0
