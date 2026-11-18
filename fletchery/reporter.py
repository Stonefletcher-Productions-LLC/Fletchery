"""Fletchery reporter: tells the Live tab what this machine is doing, every few seconds. Read-only on the machine.

What it reads (nothing here starts, stops or changes anything):
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

It posts that to Fletchery's machines worker API (/v1/report), signed with this machine's existing node key, so no new
secret and no new open port. The Live tab marks the machine silent after a minute without a report.

    python reporter.py run --config reporter.json     (pythonw on Windows: no window)
    python reporter.py once --config reporter.json    (print one report, send nothing)
    python reporter.py install --config reporter.json (Windows: a Task Scheduler entry for this user that starts it
                                                       every 5 minutes if it is not running; no admin needed)

reporter.json:
    {"coordinator": "http://100.83.43.111:8771", "machine": "SF-NODE-01", "keyFile": "C:\\StoneNode\\keys\\fletchery_node_key",
     "leaseFile": "E:\\GitHubRunner\\ResourceLeases\\heavy.json", "disks": ["C:\\", "E:\\"], "every": 10}
or {"nodeConfig": "<the node worker's node.json>"}, which supplies coordinator, machine, keyFile and heavyLeaseFile.

Standard library only."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2"  # 2: sends disk.json (the disk guard) and each drive's total
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


class Reporter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lease = Path(cfg["leaseFile"]) if cfg.get("leaseFile") else None
        self.cpu = Cpu()
        self.idle_since = 0.0

    def collect(self) -> dict:
        lease = None
        queue, acts = [], []
        if self.lease:
            rec = _read_json(self.lease)
            if isinstance(rec, dict) and (rec.get("purpose") or rec.get("runId")):
                purpose = str(rec.get("purpose") or f"GitHub run {rec.get('runId')}")
                lease = {"purpose": purpose[:300], "since": str(rec.get("acquiredUtc") or "")[:40],
                         "owner": str(rec.get("owner") or "")[:20], "alive": alive(rec.get("pid"))}
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
        return {"version": VERSION, "host": socket.gethostname(), "at": now_iso(), "lease": lease, "queue": queue[:10],
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


def run(cfg: dict) -> int:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", int(cfg.get("lockPort") or LOCK_PORT)))
    except OSError:
        return 0          # already running
    log = Path(cfg.get("logFile") or Path(cfg["keyFile"]).parent.parent / "fletchery-reporter.log")
    rep = Reporter(cfg)
    said = ""
    while True:
        started = time.monotonic()
        try:
            rep.send(rep.collect())
            msg = ""
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
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
