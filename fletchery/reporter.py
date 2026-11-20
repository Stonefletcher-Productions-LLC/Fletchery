"""Fletchery reporter: tells the Live tab what this machine is doing, every few seconds.

What it reads (reading starts, stops and changes nothing):
  - LaneGuard's lease (heavy.json): who holds the machine, named by the run itself ("PR check #2290", "Hand merge batch
    7"), since when, and whether that process is still alive;
  - LaneGuard's queue (heavy.queue beside the lease): who waits for the machine;
  - <lease folder>/activity/*.json: anything else a script wants shown, one small file per activity, kept fresh while it
    runs: {"title": "Proof run for #2263", "kind": "proof", "detail": "NavCheck", "since": "<UTC ISO>"}. A file not
    rewritten for 3 minutes is ignored (its script died);
  - which tools run (GitHub job, Unreal editor, compiler, shader workers, Claude, Blender), counted by process name;
  - CPU, memory and free disk;
  - <lease folder>/disk.json, written by LifeShare's disk guard (Tools/CI/DiskGuard.ps1) every 10 minutes: the build
    drive's free and total space, its state (ok, low, full), the thresholds, the runner services it stopped and the
    biggest folders. Sent as it is, bounded; the Live tab warns from it.

The one thing it does to the machine: restart it when the backend's answer says to (restart.py; the Restart button).

It posts that to Fletchery's machines worker API (/v1/report), signed with this machine's existing node key, so no new
secret and no new open port. The Live tab marks the machine silent after a minute without a report.

    python reporter.py run --config reporter.json     (pythonw on Windows: no window)
    python reporter.py once --config reporter.json    (print one report, send nothing)
    python reporter.py install --config reporter.json (Windows: a Task Scheduler entry for this user that starts it
                                                       every 5 minutes if it is not running; no admin needed)

reporter.json:
    {"coordinator": "http://100.64.0.10:8771", "machine": "SF-NODE-01", "keyFile": "C:\\StoneNode\\keys\\fletchery_node_key",
     "leaseFile": "E:\\GitHubRunner\\ResourceLeases\\heavy.json", "disks": ["C:\\", "E:\\"], "every": 10}
or {"nodeConfig": "<the node worker's node.json>"}, which supplies coordinator, machine, keyFile and heavyLeaseFile.
Set runnerRoots to the local GitHub runner installation paths if the PR runner is outside the default
UnrealPR folder beside ResourceLeases. Only matched job progress is sent.

Standard library only."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import restart as restartmod  # noqa: E402

VERSION = "6"  # reports its boot time and acts on a restart request in the answer (restart.py)
WINDOWS = os.name == "nt"
DEFAULT_LEASE = r"E:\GitHubRunner\ResourceLeases\heavy.json"
FRESH = 180                     # seconds a queue ticket or activity file stays valid without a rewrite
LOCK_PORT = 8779                # one reporter per machine: the second one cannot bind and exits
TOOLS = {                       # process names (lower case) -> what the Live tab calls them
    "runner.worker.exe": "runner", "runner.worker": "runner",
    "unrealeditor-cmd.exe": "unreal", "unrealeditor.exe": "unreal",
    "cl.exe": "compiler", "link.exe": "compiler", "clang-cl.exe": "compiler",
    "shadercompileworker.exe": "shaders",
    "claude.exe": "claude", "claude": "claude",
    "blender.exe": "blender", "blender": "blender",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def load_config(path: str) -> dict:
    cfg = _read_json(Path(path))
    if not isinstance(cfg, dict):
        sys.exit(f"reporter: {path} is not a JSON object")
    if cfg.get("nodeConfig"):
        node = _read_json(Path(cfg["nodeConfig"])) or {}
        for ours, theirs in (("coordinator", "coordinator"), ("machine", "machine"), ("keyFile", "keyFile"), ("leaseFile", "heavyLeaseFile")):
            if not cfg.get(ours) and node.get(theirs):
                cfg[ours] = node[theirs]
    for k in ("coordinator", "machine", "keyFile"):
        if not cfg.get(k):
            sys.exit(f"reporter: {path} needs \"{k}\" (or \"nodeConfig\")")
    cfg.setdefault("leaseFile", os.environ.get("LS_HEAVY_LEASE") or DEFAULT_LEASE)
    cfg.setdefault("disks", ["C:\\", "E:\\"] if WINDOWS else ["/"])
    cfg["every"] = max(5, int(cfg.get("every") or 10))
    return cfg


# ---- the machine ----
if WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _MEM(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class _PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE


def process_names() -> list[str]:
    """Lower-case image names of every running process, read without starting a program."""
    names: list[str] = []
    if WINDOWS:
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return names
        try:
            pe = _PE()
            pe.dwSize = ctypes.sizeof(_PE)
            ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                names.append(pe.szExeFile.lower())
                ok = _k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            _k32.CloseHandle(snap)
        return names
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if pid.isdigit():
            try:
                names.append(Path(f"/proc/{pid}/comm").read_text().strip().lower())
            except OSError:
                pass
    return names


def alive(pid) -> bool | None:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if WINDOWS:
        h = _k32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5      # access denied: it exists
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Cpu:
    """Whole-machine CPU use between two calls, in percent."""

    def __init__(self):
        self.prev = self._times()

    @staticmethod
    def _times():
        if WINDOWS:
            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            v = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # noqa: E731
            return v(idle), v(kernel) + v(user)          # kernel time includes idle
        try:
            parts = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            return parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts)
        except (OSError, ValueError, IndexError):
            return None

    def percent(self) -> float | None:
        cur = self._times()
        prev, self.prev = self.prev, cur
        if not cur or not prev or cur[1] <= prev[1]:
            return None
        return round(100.0 * (1 - (cur[0] - prev[0]) / (cur[1] - prev[1])), 1)


def memory() -> dict:
    if WINDOWS:
        m = _MEM()
        m.dwLength = ctypes.sizeof(_MEM)
        if _k32.GlobalMemoryStatusEx(ctypes.byref(m)):
            gb = 1024 ** 3
            return {"ram_total_gb": round(m.ullTotalPhys / gb, 1), "ram_used_gb": round((m.ullTotalPhys - m.ullAvailPhys) / gb, 1)}
        return {}
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        kb = lambda k: int(info[k].split()[0])  # noqa: E731
        return {"ram_total_gb": round(kb("MemTotal") / 1048576, 1), "ram_used_gb": round((kb("MemTotal") - kb("MemAvailable")) / 1048576, 1)}
    except (OSError, KeyError, ValueError):
        return {}


GUARD_FRESH = 3600   # a disk.json older than this is left out (its guard stopped); the drive figures above stay


def disk_guard(folder: Path) -> dict | None:
    """LifeShare's disk guard report (<lease folder>/disk.json), or None when there is none or it is stale."""
    f = folder / "disk.json"
    try:
        if time.time() - f.stat().st_mtime > GUARD_FRESH:
            return None
    except OSError:
        return None
    rec = _read_json(f)
    if not isinstance(rec, dict):
        return None
    out = {}
    for k in ("machine", "drive", "state", "updatedUtc", "lastCleanUtc"):
        if rec.get(k) is not None:
            out[k] = str(rec.get(k))[:60]
    for k in ("freeGB", "totalGB", "wantGB", "minGB", "warnGB", "lastFreedGB"):
        if isinstance(rec.get(k), (int, float)) and not isinstance(rec.get(k), bool):
            out[k] = rec[k]
    stopped = rec.get("runnerStopped")
    out["runnerStopped"] = [str(x)[:80] for x in (stopped if isinstance(stopped, list) else [stopped] if stopped else [])][:10]
    out["holders"] = [{"path": str(h.get("path") or "")[:260], "gb": h["gb"]} for h in (rec.get("holders") or [])[:10]
                      if isinstance(h, dict) and isinstance(h.get("gb"), (int, float))]
    return out


def disks(roots) -> list:
    out = []
    for r in roots or []:
        try:
            u = shutil.disk_usage(r)
            out.append({"name": str(r).rstrip("\\/") or "/", "free_gb": round(u.free / 1024 ** 3, 1),
                        "total_gb": round(u.total / 1024 ** 3, 1)})
        except OSError:
            pass
    return out


# Only send descriptions of known outputs. Raw logs, paths and filenames stay on the machine.
CHECK_OUTPUTS = {
    "Logs/Build_LifeShareEditor.log": "Editor build",
    "Logs/BuildContent.log": "Game data build",
    "Logs/ValidateData.log": "Data validation",
    "Logs/Specs.log": "Automation specs",
    "Logs/CheckHomes.log": "Home checks",
    "Logs/ImportEnvironment.log": "Environment import",
    "Logs/ImportCharacters.log": "Character import",
    "Logs/ImportAnimals.log": "Animal import",
    "Logs/ImportFurniture.log": "Furniture import",
    "Logs/ImportAudio.log": "Importing game audio",
    "Logs/ImportIcons.log": "Importing interface icons",
    "Logs/ImportActivityWardrobe.log": "Importing activity outfits",
    "Logs/ImportQuaternius.log": "Importing character activity animations",
    "Logs/ImportFolkClips.log": "Importing Folks' animations",
    "Logs/ImportVehicles.log": "Importing vehicles",
    "Logs/ImportHomeKit-stage.log": "Preparing home-building assets",
    "Logs/ImportHomeKit.log": "Importing home-building assets",
    "Logs/ImportSimReady.log": "Importing SimReady props",
    "Logs/BuildMaps.log": "Building game maps",
    "Logs/BuildTown_City.log": "Building the City map",
    "Logs/NavBake_Bake.txt": "Baking walkable areas in City",
    "Logs/NavBake_Game.txt": "Measuring walkable areas in the game",
    "Logs/NavBake_Diff.txt": "Comparing walkable areas in City",
    "LSMapShots/City.log": "Taking a City map photo",
    "Logs/ImportMapShots.log": "Importing map photos",
    "LSHomeCardShots/City.log": "Taking City home-selection photos",
    "Logs/ImportHomeShots.log": "Importing home-selection photos",
}
SCENARIO_OUTPUTS = {
    "LSNavCheck": ("navcheck_", "Checking walkable routes in City", ("host.log", "navcheck.txt")),
    "LSFlowTest": ("flow_", "Testing the menus, family creation and save/load", ("Ava.log", "Ben.log")),
    "LSFourPlayerSmoke": ("four_", "Testing a four-player game", ("Ava.log", "Ben.log", "Cleo.log", "Dana.log")),
    "LSM3bTest": ("m3b_", "Testing multiplayer consent and spending", ("Ava.log", "Ben.log")),
    "LSM4bTest": ("m4b_", "Testing multiplayer disconnect and rejoin", ("Ava.log", "Ben1.log", "Ben2.log")),
    "LSM5aTest": ("m5a_", "Testing visits between player families", ("Ava.log", "Ben.log")),
}


def _output_time(path: Path) -> float:
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            return info.st_mtime
    except (OSError, ValueError):
        pass
    return 0


def _check_observations(saved: Path, acquired: float, now: float):
    for relative, label in CHECK_OUTPUTS.items():
        yield _output_time(saved / relative), label

    # Unreal may buffer its open log on Windows. Each separately written photo is a measured milestone.
    photos = saved / "LSHomeCardShots"
    latest_photo = 0
    count = 0
    try:
        with os.scandir(photos) as entries:
            for entry in islice(entries, 256):
                if re.fullmatch(r"T_UI_Home_City_[A-Za-z0-9_-]+\.png", entry.name, re.I):
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_size and acquired <= info.st_mtime <= now:
                        latest_photo = max(latest_photo, info.st_mtime)
                        count += 1
            truncated = next(entries, None) is not None
        if count:
            log_time = _output_time(photos / "City.log")
            stamp = max(latest_photo, log_time if acquired <= log_time <= now else 0)
            amount = f"at least {count}" if truncated else str(count)
            noun = "photo" if count == 1 else "photos"
            yield stamp, f"City home-selection photos ({amount} {noun} saved)"
    except (OSError, ValueError):
        pass

    # Test scripts create a new tagged directory per run. Ignore earlier runs and archived retries.
    for folder, (prefix, label, filenames) in SCENARIO_OUTPUTS.items():
        area = saved / folder
        try:
            if area.stat().st_mtime < acquired:
                continue
            with os.scandir(area) as entries:
                for entry in islice(entries, 256):
                    if not entry.name.startswith(prefix) or not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.stat(follow_symlinks=False).st_mtime >= acquired:
                        for filename in filenames:
                            yield _output_time(Path(entry.path) / filename), label
        except (OSError, ValueError):
            continue


_GUID = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
_PAGE_NAME = re.compile(rf"({_GUID}_{_GUID})_(\d{{1,6}})\.log", re.I)
_PR_RUN = re.compile(r"PR check #(\d+) \(GitHub run (\d+)\)", re.I)
_BATCH = re.compile(rb"uploaded (\d{1,9}) of (\d{1,9})")
_UPLOAD_START = re.compile(rb"LFS: (\d{1,9}) file\(s\) to upload, in batches of \d+\.")


def _page_lines(path, tail=False):
    # The runner flushes chunks which can end halfway through a counter. Use complete lines only.
    with path.open("rb") as page:
        if tail:
            size = page.seek(0, os.SEEK_END)
            offset = max(size - 65536, 0)
            page.seek(offset)
            if offset:
                page.readline(65536)
        data = page.read(65536)
    for line in data.removeprefix(b"\xef\xbb\xbf").split(b"\n")[:-1]:
        timestamp, _, message = line.rstrip(b"\r").partition(b" ")
        if not timestamp.endswith(b"Z"):
            continue
        try:
            stamp = datetime.fromisoformat(timestamp[:-1].decode("ascii") + "+00:00").timestamp()
        except (ValueError, OSError, OverflowError):
            continue
        yield stamp, message.strip()


def _runner_pages(root, acquired):
    """Group the local runner's recent page files by job AND step, including numbered rotations."""
    streams = {}
    try:
        with os.scandir(root / "_diag" / "pages") as pages:
            for entry in pages:
                match = _PAGE_NAME.fullmatch(entry.name)
                if not match:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    stream = streams.setdefault(match[1], [])
                    stream.append((int(match[2]), info.st_mtime, Path(entry.path)))
    except (OSError, ValueError):
        return []
    recent = sorted(streams.items(), key=lambda pair: max(page[1] for page in pair[1]), reverse=True)
    return [(name, sorted(pages)) for name, pages in recent[:4] if max(p[1] for p in pages) >= acquired - 120]


def _upload_observation(events, now):
    # A job-level and step-level stream can contain the exact same batch a few microseconds apart.
    samples = []
    stamp, label = 0, ""
    for observed, message in sorted(events):
        start = _UPLOAD_START.fullmatch(message)
        batch = _BATCH.fullmatch(message)
        if batch or start:
            done, total = (int(batch[1]), int(batch[2])) if batch else (0, int(start[1]))
            if not total or done > total:
                continue
            if samples and (total != samples[-1][2] or done < samples[-1][1]):
                samples.clear()
            if samples and done == samples[-1][1]:
                continue
            stamp = observed
            samples.append((stamp, done, total))
            label = "Uploaded generated assets" if done == total else "Uploading generated assets"
        elif message.startswith(b"::error::LFS upload kept failing at file "):
            stamp, label = observed, "Generated asset upload failed"
            samples.clear()
        elif message.startswith(b"== Game scenarios and navigation mapped to the paths "):
            stamp, label = observed, "Starting game scenarios and navigation checks"
            samples.clear()
    if not label:
        return None
    result = {"label": label}
    if samples:
        stamp, done, total = samples[-1]
        eta = 0 if done == total else None
        if done < total and now - stamp <= FRESH:
            recent = [p for p in samples if stamp - p[0] <= 300]
            span = stamp - recent[0][0]
            if len(recent) >= 3 and span >= 20:
                eta = round((total - done) * span / (done - recent[0][1]))
        result["upload"] = {"done": done, "total": total, "eta_seconds": eta}
    return stamp, result


def _runner_events(lease, acquired, now, roots, bound):
    match = _PR_RUN.search(str(lease.get("purpose", "")))
    if not match:
        return set()
    marker = f"Holding the PC for PR check #{match[1]} (GitHub run {match[2]}):".encode()
    events = set()
    for root in roots:
        for stream, pages in _runner_pages(root, acquired):
            key = (root, stream)
            try:
                if key not in bound:
                    # Search bounded windows for the exact lease acquisition, then remember the stream across rotations.
                    candidates = dict.fromkeys([p[2] for p in pages[:2] + pages[-2:]])
                    found = any(acquired - 1 <= stamp <= min(acquired + 120, now) and line.startswith(marker)
                                for path in candidates for tail in (False, True) for stamp, line in _page_lines(path, tail))
                    if not found:
                        continue
                    bound.add(key)
                for _, _, path in pages[-2:]:
                    events.update((stamp, line[:160]) for stamp, line in _page_lines(path, tail=True)
                                  if acquired <= stamp <= now and line.startswith((
                                      b"LFS: ", b"uploaded ", b"::error::LFS upload kept failing at file ",
                                      b"== Game scenarios and navigation mapped to the paths ")))
            except OSError:
                continue  # the runner can remove a page after sending it to GitHub
    return events


class RunnerLogProgress:
    """Keep a lease's observations when the runner removes an uploaded page. Recalculate ETA on every read."""
    def __init__(self, roots):
        if isinstance(roots, str):
            roots = [roots]
        self.roots = []
        for root in roots[:4] if isinstance(roots, list) else []:
            if isinstance(root, str) and not root.startswith(("\\\\", "//")) and Path(root).is_absolute():
                self.roots.append(Path(root))
        self.identity = None
        self.bound = set()
        self.events = set()

    def latest(self, lease, acquired, now):
        identity = (lease.get("purpose"), acquired)
        if identity != self.identity:
            self.bound.clear()
            self.events.clear()
            self.identity = identity
        self.events.update(_runner_events(lease, acquired, now, self.roots, self.bound))
        if len(self.events) > 512:
            self.events = set(sorted(self.events)[-512:])
        return _upload_observation(self.events, now)


def latest_check_output(lease: dict, now: float, runner_logs=None) -> dict | None:
    """Older check scripts have no phase marker. Report the last known tool to write in this lease, by timestamp."""
    try:
        acquired = datetime.fromisoformat(lease["acquiredUtc"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, ValueError, OSError):
        return None
    newest, label = 0, ""
    roots = lease.get("workspaces")
    if isinstance(roots, str):
        roots = [roots]
    for root in roots[:4] if isinstance(roots, list) else []:
        if not isinstance(root, str) or root.startswith(("\\\\", "//")) or not Path(root).is_absolute():
            continue
        for stamp, task in _check_observations(Path(root) / "Saved", acquired, now):
            if acquired <= stamp <= now and stamp >= newest and stamp > 0:
                newest, label = stamp, task
    observation = {"label": label}
    runner = runner_logs.latest(lease, acquired, now) if runner_logs else None
    if runner and runner[0] >= newest:
        newest, observation = runner
    if newest:
        return {**observation, "at": datetime.fromtimestamp(newest, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return None


class Reporter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lease = Path(cfg["leaseFile"]) if cfg.get("leaseFile") else None
        roots = cfg.get("runnerRoots")
        if roots is None:
            roots = [str(self.lease.parent.parent / "UnrealPR")] if self.lease else []
        self.runner_logs = RunnerLogProgress(roots)
        self.cpu = Cpu()
        self.idle_since = 0.0
        self.restart_ack = ""
        self.ready = (0.0, {})     # (read at, restart.readiness()): asked every 10 minutes, it starts a program

    def collect(self) -> dict:
        lease = None
        queue, acts = [], []
        if self.lease:
            rec = _read_json(self.lease)
            if isinstance(rec, dict) and (rec.get("purpose") or rec.get("runId")):
                purpose = str(rec.get("purpose") or f"GitHub run {rec.get('runId')}")
                lease = {"purpose": purpose[:300], "since": str(rec.get("acquiredUtc") or "")[:40],
                         "owner": str(rec.get("owner") or "")[:20], "alive": alive(rec.get("pid"))}
                if lease["alive"] and _PR_RUN.search(purpose):
                    latest = latest_check_output(rec, time.time(), self.runner_logs)
                    if latest:
                        lease["last_output"] = latest
            folder = self.lease.parent
            qdir = Path(os.environ.get("LS_LANE_QUEUE") or folder / "heavy.queue")
            now = time.time()
            for f in sorted(qdir.glob("*.json")) if qdir.is_dir() else []:
                try:
                    if now - f.stat().st_mtime > FRESH:
                        continue
                except OSError:
                    continue
                rec = _read_json(f) or {}
                queue.append({"purpose": str(rec.get("purpose") or "another run")[:300],
                              "since": str(rec.get("queuedUtc") or "")[:40]})
            marker = _read_json(folder / "heavy.round-waiting.json")
            try:
                if marker and now - (folder / "heavy.round-waiting.json").stat().st_mtime < FRESH:
                    queue.insert(0, {"purpose": "merge round" + (f" ({marker.get('why')})" if marker.get("why") else ""), "since": ""})
            except OSError:
                pass
            adir = folder / "activity"
            for f in sorted(adir.glob("*.json")) if adir.is_dir() else []:
                try:
                    if now - f.stat().st_mtime > FRESH:
                        continue
                except OSError:
                    continue
                rec = _read_json(f)
                if isinstance(rec, dict) and rec.get("title"):
                    acts.append({k: str(rec.get(k) or "")[:300] for k in ("title", "kind", "detail", "since")})
        guard = disk_guard(self.lease.parent) if self.lease else None
        procs: dict[str, int] = {}
        for n in process_names():
            if n in TOOLS:
                procs[TOOLS[n]] = procs.get(TOOLS[n], 0) + 1
        cpu = self.cpu.percent()
        stats = {"cpu": cpu, **memory(), "disks": disks(self.cfg.get("disks"))}
        if lease and cpu is not None and cpu < 5:
            self.idle_since = self.idle_since or time.time()
        else:
            self.idle_since = 0.0
        if time.time() - self.ready[0] > 600:
            self.ready = (time.time(), restartmod.readiness())
        return {"version": VERSION, "host": socket.gethostname(), "at": now_iso(), "lease": lease, "queue": queue[:10],
                "boot": restartmod.boot_time(), "restart_ack": self.restart_ack, "after_restart": self.ready[1],
                "activities": acts[:10], "procs": procs, "stats": stats, "disk_guard": guard,
                "idle_seconds": round(time.time() - self.idle_since) if self.idle_since else 0,
                "busy": bool(lease or procs.get("runner") or acts)}

    def send(self, report: dict) -> dict:
        key = Path(self.cfg["keyFile"]).read_text(encoding="utf-8").strip()
        req = urllib.request.Request(str(self.cfg["coordinator"]).rstrip("/") + "/v1/report",
                                     data=json.dumps(report).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                                              "X-Fletchery-Machine": str(self.cfg["machine"])})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")


def code_stamp() -> tuple:
    """Changes when the reporter's code on disk does (an update was installed)."""
    out = []
    for name in ("reporter.py", "restart.py"):
        try:
            st = (Path(__file__).resolve().parent / name).stat()
            out.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((name, 0, 0))
    return tuple(out)


def run(cfg: dict, argv=None) -> int:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", int(cfg.get("lockPort") or LOCK_PORT)))
    except OSError:
        return 0          # already running
    log = Path(cfg.get("logFile") or Path(cfg["keyFile"]).parent.parent / "fletchery-reporter.log")
    rep = Reporter(cfg)
    said = ""

    def note(text: str) -> None:
        try:
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{now_iso()} {text}\n")
        except OSError:
            pass

    actor = restartmod.Actor(log=note)
    stamp = code_stamp()
    while True:
        started = time.monotonic()
        try:
            answer = rep.send(rep.collect())
            msg = ""
            if isinstance(answer, dict) and answer.get("restart"):
                rep.restart_ack = actor.handle(answer["restart"]) or rep.restart_ack
        except urllib.error.HTTPError as e:
            msg = f"Fletchery refused the report: HTTP {e.code}"
        except Exception as e:  # keep reporting through any hiccup
            msg = f"{type(e).__name__}: {e}"[:300]
        if msg != said:          # log changes only, so the file stays small
            try:
                with log.open("a", encoding="utf-8") as f:
                    f.write(f"{now_iso()} {msg or 'reporting'}\n")
            except OSError:
                pass
            said = msg
        if argv is not None and code_stamp() != stamp:
            # New code was installed: hand over to it, so an update never needs anyone to stop this process by hand.
            time.sleep(5)          # the installer may still be copying
            note("new code installed; starting it")
            lock.close()
            try:
                import subprocess
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *argv], close_fds=True,
                                 creationflags=0x08000008 if WINDOWS else 0)   # no window, detached
            except OSError as e:
                note(f"could not start the new code ({e}); the scheduled task will")
            return 0
        time.sleep(max(1.0, cfg["every"] - (time.monotonic() - started)))


def install(cfg_path: str) -> int:
    if not WINDOWS:
        sys.exit("install is for Windows; elsewhere start `reporter.py run` from your init system")
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    me = Path(__file__).resolve()
    tr = f'"{pyw}" "{me}" run --config "{Path(cfg_path).resolve()}"'
    import subprocess
    out = subprocess.run(["schtasks", "/Create", "/F", "/TN", "Fletchery Reporter", "/SC", "MINUTE", "/MO", "5", "/TR", tr],
                         capture_output=True, text=True, creationflags=0x08000000)
    print((out.stdout + out.stderr).strip())
    if out.returncode == 0:
        subprocess.run(["schtasks", "/Run", "/TN", "Fletchery Reporter"], capture_output=True, creationflags=0x08000000)
    return out.returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report this machine's work to Fletchery's Live tab.")
    ap.add_argument("command", choices=["run", "once", "install"])
    ap.add_argument("--config", required=True)
    a = ap.parse_args(argv)
    if a.command == "install":
        return install(a.config)
    cfg = load_config(a.config)
    if a.command == "once":
        print(json.dumps(Reporter(cfg).collect(), indent=2))
        return 0
    return run(cfg, list(argv) if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
