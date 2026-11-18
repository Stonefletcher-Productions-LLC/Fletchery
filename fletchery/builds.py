"""Scheduled builds: pick a version and a time, and Fletchery packages whatever is on the branch at that time.

Builds adds a row: version (0.3.9), branch (the base branch unless you name another), when (now or a time),
once or every day, with or without the Full tests first. Rows live in <state dir>/builds.json, so a restart keeps them.

What happens to a row:

    scheduled  waiting for its time. A daily row stays here: at its time it starts a one-off copy and moves on a day.
    queued     its time came: the branch was read once (`sha` is what gets built, whatever lands later) and a
               `package` job waits for a machine that can package (machines.py: labels os:windows and role:cook).
               Every build machine runs node_worker.py, this PC included, so there is one way to build.
    running    a machine is building it.
    done / failed / cancelled

Packages go to the machine's own `buildsDir` and are copied back to this PC (`workers.builds.outDir`, default
<state dir>\builds).

Profile (all optional), in `workers`:
    "builds": {"outDir": "E:\\\\lifeshare-scratch\\\\LifeShare-builds", "engine": "E:\\\\UE_5.8", "timeoutMinutes": 300,
               "nodeLabels": ["unreal:5.8.3"]}
`nodeLabels` are extra labels a machine needs to take a build (for example a matching engine version).
`"publish": {"to": "github", "keep": 3}` also uploads each finished build to a private pre-release on the project
repository and keeps the newest three, so it can be downloaded from any computer (publish.py).
"""
from __future__ import annotations

import re
import subprocess
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

VERSION = re.compile(r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$")
REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
TERMINAL = ("done", "failed", "cancelled")
KEEP_FINISHED = 30          # finished rows kept for the list; older ones are dropped


def utc(value: str = "") -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as e:
        raise ValueError("Choose a valid date and time.") from e
    if t.tzinfo is None:
        raise ValueError("The time must include a time zone.")
    return t.astimezone(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class Builds:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self._publishing: threading.Thread | None = None
        for row in self.rows:
            if row["state"] in ("waiting", "running", "queued") and row.get("route") == "pc":   # the retired PC route
                row.update(state="failed", finished=iso(utc()), reason="Builds now run on a build machine. Queue it again.")
            if row.get("publish") in ("zipping", "uploading", "failed"):  # cut off by a restart, or failed: try again
                row["publish"] = ""

    # ---- state ----
    @property
    def rows(self) -> list:
        return self.engine.state.builds

    @property
    def conf(self) -> dict:
        return getattr(self.engine.cfg, "builds", None) or {}

    def save(self) -> None:
        self.engine.state.save()

    def _log(self, text: str) -> None:
        try:
            self.engine.log("builds: " + text)
        except Exception:
            pass

    def update(self, row: dict, **values) -> None:
        row.update(values)
        self.save()

    def view(self) -> dict:
        with self.lock:
            rows = deepcopy(self.rows)
        coord = getattr(self.engine, "machines", None)
        nodes = []
        if coord is not None:
            try:
                nodes = coord.machines.eligible(self.node_labels(), self._node_ram(), "package")
            except Exception:
                nodes = []
        return {"builds": rows, "base": self.engine.cfg.base_branch, "nodes": nodes, "machines": coord is not None}

    def node_labels(self) -> list:
        import machines
        extra = [str(x) for x in (self.conf.get("nodeLabels") or []) if isinstance(x, str)]
        return list(machines.RECIPES["package"]["labels"]) + [x for x in extra if x not in machines.RECIPES["package"]["labels"]]

    def _node_ram(self) -> float:
        import machines
        return float(machines.RECIPES["package"]["min_ram_gb"])

    def out_dir(self) -> Path:
        return Path(self.conf.get("outDir") or Path(self.engine.state_dir) / "builds")

    # ---- what the dashboard does ----
    def add(self, data: dict) -> dict:
        version = str(data.get("version") or "").strip()
        if not VERSION.match(version):
            raise ValueError("Enter a version such as 0.3.9.")
        ref = str(data.get("ref") or "").strip() or self.engine.cfg.base_branch
        if not (SHA.match(ref.lower()) or (REF.match(ref) and ".." not in ref)):
            raise ValueError("Give a branch name or a full commit SHA.")
        at = utc(data.get("at") or "")
        repeat = str(data.get("repeat") or "")
        if repeat not in ("", "daily"):
            raise ValueError("A build runs once or every day.")
        if getattr(self.engine, "machines", None) is None:
            raise ValueError("No build machines are set up (workers.machines in the profile).")
        tests = "full" if data.get("tests") in ("full", True) else ""
        row = {"id": uuid.uuid4().hex[:12], "version": version, "ref": ref, "at": iso(at), "repeat": repeat,
               "tests": tests, "state": "scheduled", "created": iso(utc()), "sha": "", "route": "",
               "job": 0, "machine": "", "reason": "", "output": "", "started": "", "finished": "", "parent": ""}
        with self.lock:
            if sum(1 for r in self.rows if r["state"] not in TERMINAL) >= 50:
                raise ValueError("50 builds are already scheduled or waiting.")
            self.rows.append(row)
            self.save()
        self._log(f"{version} of {ref} scheduled for {row['at']}{' and every day after' if repeat else ''}"
                  f"{' with Full tests' if tests else ''}")
        self.engine.wake()
        return deepcopy(row)

    def cancel(self, build_id: str) -> dict:
        with self.lock:
            row = next((r for r in self.rows if r["id"] == build_id), None)
            if row is None:
                raise ValueError("Build not found.")
            if row["state"] in TERMINAL:
                raise ValueError(f"This build is already {row['state']}.")
            if row.get("job") and row["state"] in ("queued", "running"):
                coord = getattr(self.engine, "machines", None)
                if coord is not None:
                    state = coord.machines.cancel(int(row["job"]))
                    if state == "stopping":
                        self.update(row, reason="Stopping on the build machine.")
                        return {"message": "The machine stops this build at its next heartbeat."}
            self.update(row, state="cancelled", finished=iso(utc()),
                        reason="Repeat stopped." if row.get("repeat") else "Cancelled before it started.")
        self._log(f"{row['version']} cancelled")
        self.engine.wake()
        return {"message": "Repeat stopped." if row.get("repeat") else "Build cancelled."}

    def run_now(self, build_id: str) -> dict:
        """A scheduled build (or the next run of a daily one) starts now instead of at its time."""
        with self.lock:
            row = next((r for r in self.rows if r["id"] == build_id), None)
            if row is None:
                raise ValueError("Build not found.")
            if row["state"] != "scheduled":
                raise ValueError(f"This build is already {row['state']}.")
            self.update(row, at=iso(utc()))
        self._log(f"{row['version']}: start now")
        self.engine.wake()
        return {"message": "Starting now."}

    def retry(self, build_id: str) -> dict:
        """Build a failed or cancelled build again: same version, branch and options, starting now. The branch is read
        again, so a fix that landed since is in it."""
        with self.lock:
            row = next((r for r in self.rows if r["id"] == build_id), None)
        if row is None:
            raise ValueError("Build not found.")
        if row["state"] not in ("failed", "cancelled", "done"):
            raise ValueError(f"This build is still {row['state']}.")
        return self.add({"version": row["version"], "ref": row["ref"], "tests": row.get("tests") or ""})

    # ---- the scheduler: off the event loop (it may ask GitHub for a branch head) ----
    def tick(self, at: datetime | None = None) -> None:
        at = at or utc()
        with self.lock:
            rows = list(self.rows)
        for row in rows:
            try:
                if row["state"] == "scheduled" and utc(row["at"]) <= at:
                    self._due(row, at)
                elif row["state"] == "waiting":
                    self._route(row)
                elif row["state"] in ("queued", "running") and row.get("route") == "node":
                    self._follow_node(row)
            except Exception as e:  # one bad row never stops the others
                self.update(row, reason=f"{type(e).__name__}: {e}"[:300])
        self._trim()
        self._publish_next()

    def _due(self, row: dict, at: datetime) -> None:
        if row.get("repeat"):
            # A daily schedule starts a one-off copy and moves to the next day it has not reached yet.
            run = {**deepcopy(row), "id": uuid.uuid4().hex[:12], "repeat": "", "parent": row["id"], "created": iso(at),
                   "state": "scheduled"}
            nxt = utc(row["at"])
            while nxt <= at:
                nxt += timedelta(days=1)
            with self.lock:
                self.rows.append(run)
                self.update(row, at=iso(nxt), reason=f"Last started {iso(at)[:16].replace('T', ' ')}Z")
            row = run
        sha = self._resolve(row["ref"])
        if not sha:
            raise ValueError(f"GitHub has no commit for {row['ref']!r}")
        self.update(row, sha=sha, state="waiting", started="", reason=f"Building {row['ref']} at {sha[:10]}; waiting for a machine.")
        self._log(f"{row['version']}: {row['ref']} is at {sha[:10]}")
        self._route(row)

    def _resolve(self, ref: str) -> str:
        if SHA.match(ref.lower()):
            return ref.lower()
        coord = getattr(self.engine, "machines", None)
        if coord is not None:
            return coord.resolve(ref)
        data = self.engine.gh.api(f"repos/{self.engine.cfg.repo}/commits/{ref}", fresh=True)
        return str((data or {}).get("sha") or "")

    def _route(self, row: dict) -> None:
        """Queue the `package` job (machines.py). Any machine with the labels takes it, this PC's own worker included."""
        coord = getattr(self.engine, "machines", None)
        if coord is None:
            self.update(row, state="failed", finished=iso(utc()), reason="No build machines are set up.")
            return
        nodes = coord.machines.eligible(self.node_labels(), self._node_ram(), "package")
        job = coord.machines.enqueue("package", row["sha"], {"version": row["version"], "tests": row["tests"]},
                                     ref=row["ref"], requested_by=f"build {row['version']}", labels=self.node_labels())
        self.update(row, route="node", state="queued", job=job,
                    reason=f"Queued for {', '.join(nodes) if nodes else 'a build machine (none can take it right now)'} as job {job}.")
        self._log(f"{row['version']}: job {job} queued")

    def _follow_node(self, row: dict) -> None:
        coord = getattr(self.engine, "machines", None)
        job = coord.machines.job(int(row["job"])) if coord is not None else None
        if job is None:
            self.update(row, state="failed", finished=iso(utc()), reason="The node job is gone.")
            return
        state = job["state"]
        if state == "running" and row["state"] != "running":
            self.update(row, state="running", machine=job["machine"], started=job["started"] or iso(utc()),
                        reason=f"Building on {job['machine']}.")
        elif state == "passed":
            self.update(row, state="done", machine=job["machine"], finished=job["finished"] or iso(utc()),
                        output=_out_from(job["summary"]) or job["summary"], reason=job["summary"])
            self._log(f"{row['version']}: {job['summary']}")
        elif state in ("failed", "lost", "cancelled"):
            summary = job.get("summary") or state
            self.update(row, state="cancelled" if state == "cancelled" else "failed", machine=job["machine"],
                        finished=job["finished"] or iso(utc()), reason=summary)
            self._log(f"{row['version']}: {summary}")

    def _trim(self) -> None:
        with self.lock:
            done = [r for r in self.rows if r["state"] in TERMINAL]
            if len(done) > KEEP_FINISHED:
                drop = {id(r) for r in sorted(done, key=lambda r: r.get("finished") or r["created"])[:len(done) - KEEP_FINISHED]}
                self.rows[:] = [r for r in self.rows if id(r) not in drop]
                self.save()

    # ---- downloads (publish.py) ----
    def _publish_next(self) -> None:
        """Start publishing the newest finished build that has not been published, one at a time, in its own thread
        (an upload can take an hour; the scheduler keeps ticking)."""
        import publish
        p = publish.conf(self.conf)
        if p is None or (self._publishing is not None and self._publishing.is_alive()):
            return
        keep = max(1, int(p.get("keep") or 3))
        with self.lock:
            done = sorted((r for r in self.rows if r["state"] == "done"), key=lambda r: r.get("finished") or "", reverse=True)[:keep]
            row = next((r for r in done if not r.get("publish")), None)
        if row is None:
            return
        self.update(row, publish="zipping", publish_reason="")
        self._publishing = threading.Thread(target=self.publish, args=(row, p), name=f"publish-{row['version']}", daemon=True)
        self._publishing.start()

    def publish(self, row: dict, p: dict) -> None:
        import publish
        folder = Path(row.get("output") or "")
        coord = getattr(self.engine, "machines", None)
        if row.get("output") and not folder.is_dir() and coord is not None:
            # A node build whose summary named the node's folder: the copy it sent back has the same name here.
            try:
                folder = Path(coord.machines.upload_dir()) / PureWindowsPath(row["output"]).name
            except Exception:
                pass
        if not row.get("output") or not folder.is_dir():
            self.update(row, publish="skipped", publish_reason=f"The package folder is not on this PC ({row.get('output') or 'none'}).")
            return
        gh = self.engine.gh
        pub = publish.Publisher(gh, str(p.get("repo") or self.engine.cfg.repo), getattr(gh, "gh", "gh"), self._log)
        try:
            part = int(float(p.get("partGb") or 1.9) * 2**30)
            stem = f"{self.engine.cfg.name or 'Build'}-{row['version']}-{row['sha'][:10]}"
            dest = self.out_dir() / "_downloads"
            zips = publish.make_zips(folder, dest, stem, min(part, 2**31 - 2**20))
            self.update(row, publish="uploading",
                        publish_reason=f"Uploading {len(zips)} zip{'s' if len(zips) > 1 else ''} ({sum(z.stat().st_size for z in zips) / 2**30:.2f} GiB).")
            self._log(f"{row['version']}: uploading {', '.join(z.name for z in zips)}")
            url = pub.release({**row, "name": self.engine.cfg.name}, zips)
            gone = pub.prune(max(1, int(p.get("keep") or 3)))
            drive = ""
            drive_keep = max(1, int(p.get("driveKeep") or p.get("keep") or 3))
            if p.get("driveRemote"):
                try:
                    old, links = publish.to_remote(zips, str(p["driveRemote"]), self.engine.cfg.name or "Build",
                                                   drive_keep, str(p.get("rclone") or "rclone"))
                    row["drive"] = links[0] if len(links) == 1 else " ".join(links)
                    drive = f" Drive: {row['drive']}" + (f" (removed {', '.join(old)})." if old else ".")
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                    drive = f" Not uploaded to {p['driveRemote']}: {e}"
            elif p.get("driveDir"):
                try:
                    old = publish.to_drive(zips, Path(p["driveDir"]), self.engine.cfg.name or "Build", drive_keep)
                    drive = f" Copied to {p['driveDir']}" + (f" (removed {', '.join(old)})." if old else ".")
                except OSError as e:
                    drive = f" Not copied to {p['driveDir']}: {e}"
            for z in zips:
                z.unlink(missing_ok=True)
            self.update(row, publish="published", download=url,
                        publish_reason=f"Download: {url}" + (f" (removed {', '.join(gone)})" if gone else "") + drive)
            self._log(f"{row['version']}: download ready at {url}" + (f"; removed {', '.join(gone)}" if gone else ""))
        except Exception as e:
            self.update(row, publish="failed", publish_reason=f"{type(e).__name__}: {e}"[:400])
            self._log(f"{row['version']}: publish failed: {e}")



def _out_from(summary: str) -> str:
    """Where to find the build: the copy on this PC when the node sent one back, else the node's folder."""
    m = re.search(r"; copied to (.+?) on the PC", summary or "") or re.match(r"Packaged to (.+?) on ", summary or "")
    return m.group(1) if m else ""
