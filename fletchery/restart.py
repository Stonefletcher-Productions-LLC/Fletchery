"""Restart a machine from Fletchery (the Restart button on a machine's card, or POST /api/machines/restart).

Why: 2026-11-20, a cancelled PR check left an Unreal editor on the PC that no kill could end. It locked the build folder,
every later build failed at checkout, and nothing could clear it but a restart that only a person at the PC could do.

How a restart travels, with no new port and no new secret:

  1. The backend records the request (Restarts, <state>/restarts.json).
  2. The machine's reporter (reporter.py) reports every ~10 s; the answer to its next report carries the request.
     The reporter runs `shutdown /r /f` itself (any signed-in user may restart Windows; no admin needed).
  3. The restart guard (this file, `guard`) asks the backend the same question every 15 s (POST /v1/restart). It is a
     startup task that runs as SYSTEM, so it also works when nobody is signed in and the reporter is not running, and it
     outlives the sign-out. If Windows has not gone down HARD_AFTER seconds after the polite restart, it restarts the
     machine the hard way (NtShutdownSystem: no waiting for programs or services), which is what a process stuck in
     a driver needs.
  4. A request is for one boot only: it names the boot it was made in, and a machine that booted after the request
     never gets it. So a restart can never loop. A request nobody collected in PENDING seconds is dropped.
  5. The first report after the boot marks the request "back". A machine that is not back after OVERDUE seconds is
     flagged on the Live tab.

    python restart.py guard --config reporter.json           (the loop; pythonw on Windows: no window)
    python restart.py install-guard --config reporter.json   (Windows, from an elevated prompt, once per machine)
    python restart.py status                                 (what this machine does after a restart, as JSON)

Standard library only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WINDOWS = os.name == "nt"
PENDING = 600        # seconds a request waits for the machine to collect it
OVERDUE = 900        # seconds after which a machine that was told to restart and is not back is flagged
HARD_AFTER = 600     # seconds the guard gives the polite restart before the hard one
WAIT_MAX = 12 * 3600      # a restart that waits for an idle machine is given up after this long
DELAY = 20           # seconds Windows shows its "restarting" notice before it goes down
SLACK = 5            # seconds of clock noise allowed when comparing a boot time with a request time
GUARD_TASK = "Fletchery restart guard"
GUARD_EVERY = 15
NO_WINDOW = 0x08000000


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ---- the backend's side ----
class Restarts:
    """Who was told to restart, and whether it came back. One record per machine (its last request)."""

    def __init__(self, path: str | Path | None = None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.Lock()
        self.records: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.records = {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self.records = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.records, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass   # the record still lives in memory; the restart itself does not depend on the file

    def request(self, machine: str, by: str = "", reason: str = "", wait: bool = False) -> dict:
        """`wait`: hold the request until the machine reports that it is idle (poll with idle=True), so the build it
        is running finishes first. A waiting request is given up after WAIT_MAX."""
        at = self.clock()
        rec = {"id": f"{int(at)}-{os.urandom(3).hex()}", "requested": at, "by": str(by)[:80], "reason": str(reason)[:200],
               "state": "waiting" if wait else "requested", "sent": 0.0, "back": 0.0, "via": "", "asked": at}
        with self._lock:
            self.records[str(machine)] = rec
            self._save()
        return dict(rec)

    def cancel(self, machine: str) -> bool:
        """Only a request the machine has not collected yet can be withdrawn."""
        with self._lock:
            rec = self.records.get(str(machine))
            if not rec or rec["state"] not in ("requested", "waiting"):
                return False
            rec["state"] = "cancelled"
            self._save()
            return True

    def poll(self, machine: str, boot, ack: str = "", via: str = "reporter", idle: bool | None = None) -> dict | None:
        """One check-in from the machine: `boot` is when it started (ISO), `ack` the request it has just acted on.
        Returns the request to act on, or None. A check-in with no boot time (an old reporter) never gets one.
        `idle` is True only when the caller knows the machine is running nothing: that releases a waiting request."""
        boot_ts = _ts(boot)
        at = self.clock()
        with self._lock:
            rec = self.records.get(str(machine))
            if not rec or rec["state"] not in ("requested", "sent", "waiting"):
                return None
            changed = False
            out = None
            if rec["state"] == "waiting":
                if boot_ts and boot_ts > rec["requested"] - SLACK:
                    rec.update(state="back", back=at)     # it restarted some other way while it waited
                elif at - rec["requested"] > WAIT_MAX:
                    rec["state"] = "expired"
                elif idle is True and boot_ts:
                    rec.update(state="requested", requested=at)   # the clock for "pending" and "overdue" starts now
                else:
                    return None
                changed = True
                if rec["state"] != "requested":
                    self._save()
                    return None
            if boot_ts and boot_ts > rec["requested"] - SLACK:
                rec.update(state="back", back=at)     # it started after the request: the restart happened
                changed = True
            elif rec["state"] == "requested" and at - rec["requested"] > PENDING:
                rec["state"] = "expired"
                changed = True
            elif boot_ts:
                if ack and ack == rec["id"] and rec["state"] == "requested":
                    rec.update(state="sent", sent=at, via=str(via)[:20])
                    changed = True
                # Sent again until the machine goes down: the guard needs it for its hard restart, and a reporter that
                # already acted ignores a request it has seen.
                out = {"id": rec["id"], "delay": DELAY, "requested": rec["requested"], "by": rec["by"], "reason": rec["reason"]}
            if changed:
                self._save()
            return out

    def view(self, machine: str) -> dict | None:
        with self._lock:
            rec = self.records.get(str(machine))
            if not rec:
                return None
            at = self.clock()
            out = {"id": rec["id"], "state": rec["state"], "by": rec["by"], "reason": rec["reason"],
                   "asked": _iso(rec.get("asked") or rec["requested"]),
                   "requested": _iso(rec["requested"]), "sent": _iso(rec["sent"]), "back": _iso(rec["back"]),
                   "age": round(at - rec["requested"]), "via": rec.get("via") or ""}
            if rec["state"] == "back":
                out["took"] = round(rec["back"] - rec["requested"])
            out["overdue"] = rec["state"] in ("requested", "sent") and at - rec["requested"] > OVERDUE
            return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""


# ---- the machine's side ----
def boot_time() -> str:
    """When this machine started (UTC ISO), steady to the second between calls."""
    try:
        if WINDOWS:
            import ctypes
            k32 = ctypes.WinDLL("kernel32")
            k32.GetTickCount64.restype = ctypes.c_ulonglong
            up = k32.GetTickCount64() / 1000.0
            # Rounded, so two readings a moment apart name the same boot.
            return _iso(float(round((time.time() - up) / 4) * 4))
        with open("/proc/stat", encoding="ascii") as f:
            for line in f:
                if line.startswith("btime "):
                    return _iso(float(line.split()[1]))
    except Exception:
        pass
    return ""


def readiness() -> dict:
    """What happens after a restart, so the page can say it before anyone presses the button.
    signs_in: Windows signs the user in by itself (automatic sign-in), so the reporter, the backend and Claude start
    again with nobody at the keyboard. guard: the restart guard task is installed."""
    out = {"signs_in": None, "guard": None}
    if not WINDOWS:
        return out
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon") as k:
            def val(name):
                try:
                    return str(winreg.QueryValueEx(k, name)[0])
                except OSError:
                    return ""
            out["signs_in"] = val("AutoAdminLogon") == "1" and bool(val("DefaultUserName"))
    except OSError:
        pass
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", GUARD_TASK], capture_output=True, creationflags=NO_WINDOW, timeout=15)
        # The task belongs to SYSTEM: a user who is not an administrator is refused when asking about it ("Access is
        # denied"), which still says it is there. A missing task answers "cannot find the file" (Gaming PC, 2026-11-20:
        # the guard was installed and the card kept saying it was not).
        said = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").lower()
        out["guard"] = r.returncode == 0 or "access is denied" in said
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def restart_now(delay: int = DELAY, note: str = "", run=None) -> tuple[bool, str]:
    """Ask the operating system to restart, closing programs that do not close themselves. Returns (accepted, text)."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=60,
                                              **({"creationflags": NO_WINDOW} if WINDOWS else {})))
    note = ("Fletchery restart" + (": " + note if note else ""))[:120]
    if WINDOWS:
        argv = ["shutdown", "/r", "/f", "/t", str(max(0, int(delay))), "/c", note, "/d", "p:4:1"]
    else:
        argv = ["shutdown", "-r", "+1" if delay >= 60 else "now", note]
    try:
        r = run(argv)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    text = ((getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")).strip()[:200]
    # 1190: a restart is already scheduled (the reporter and the guard both ask; the second one is told so).
    return r.returncode in (0, 1190), text or f"exit {r.returncode}"


def hard_restart() -> str:
    """Restart without waiting for anything (Windows: NtShutdownSystem). The last resort when the polite restart hangs:
    open files are not saved by their programs, though the disk cache is still flushed."""
    if not WINDOWS:
        try:
            with open("/proc/sys/kernel/sysrq", "w") as f:
                f.write("1")
            with open("/proc/sysrq-trigger", "w") as f:
                f.write("b")
        except OSError as e:
            return f"{type(e).__name__}: {e}"
        return "asked"
    import ctypes
    ntdll = ctypes.WinDLL("ntdll")
    was = ctypes.c_bool()
    status = ntdll.RtlAdjustPrivilege(19, True, False, ctypes.byref(was))   # SeShutdownPrivilege
    if status:
        return f"no shutdown privilege (0x{status & 0xFFFFFFFF:08X})"
    status = ntdll.NtShutdownSystem(1)                                       # ShutdownReboot
    return f"NtShutdownSystem 0x{status & 0xFFFFFFFF:08X}"


class Actor:
    """Acts once on each request it is handed (the reporter and the guard each keep one)."""

    def __init__(self, restart=restart_now, log=lambda text: None, clock=time.time):
        self.restart = restart
        self.clock = clock
        self.log = log
        self.done: dict[str, float] = {}

    def handle(self, cmd) -> str:
        """Returns the id to acknowledge, or ""."""
        if not isinstance(cmd, dict) or not cmd.get("id"):
            return ""
        rid = str(cmd["id"])[:60]
        if rid in self.done:
            return rid
        boot = _ts(boot_time())
        if boot and boot > float(cmd.get("requested") or 0) - SLACK:
            return ""                      # made before this boot: already done
        try:
            delay = int(cmd.get("delay", DELAY))
        except (TypeError, ValueError):
            delay = DELAY
        ok, text = self.restart(delay, str(cmd.get("by") or ""))
        self.log(f"restart {rid} asked for by {cmd.get('by') or 'Fletchery'}: {'accepted' if ok else 'REFUSED'} ({text})")
        if not ok:
            return ""
        self.done[rid] = self.clock()
        return rid


def _call(cfg: dict, body: dict) -> dict:
    key = Path(cfg["keyFile"]).read_text(encoding="utf-8").strip()
    req = urllib.request.Request(str(cfg["coordinator"]).rstrip("/") + "/v1/restart", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                                                         "X-Fletchery-Machine": str(cfg["machine"])})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def guard(cfg: dict, once: bool = False, sleep=time.sleep, call=_call, hard=hard_restart, clock=time.time, actor=None) -> int:
    """The loop: ask the backend whether to restart; restart; and if the machine is still up HARD_AFTER seconds later,
    restart it the hard way."""
    log_path = Path(cfg.get("guardLog") or Path(cfg["keyFile"]).parent.parent / "fletchery-restart-guard.log")

    def log(text: str) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{now_iso()} {text}\n")
        except OSError:
            pass

    actor = actor or Actor()
    actor.log = log
    ack, said = "", ""
    while True:
        try:
            out = call(cfg, {"boot": boot_time(), "ack": ack, "via": "guard"})
            cmd = out.get("restart") if isinstance(out, dict) else None
            ack = actor.handle(cmd) or ack
            msg = ""
        except Exception as e:      # the backend restarts too; keep asking
            msg = f"{type(e).__name__}: {e}"[:200]
        if msg != said:
            log(msg or "asking")
            said = msg
        for rid, at in list(actor.done.items()):
            if clock() - at > HARD_AFTER:
                log(f"restart {rid}: Windows is still up after {HARD_AFTER} s; restarting the hard way")
                log(f"restart {rid}: {hard()}")
                actor.done[rid] = clock()      # if even that returned, try again after another wait
        if once:
            return 0
        sleep(GUARD_EVERY)


def install_guard(cfg_path: str) -> int:
    if not WINDOWS:
        sys.exit("install-guard is for Windows; elsewhere run `restart.py guard` as root from your init system")
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    tr = f'"{pyw}" "{Path(__file__).resolve()}" guard --config "{Path(cfg_path).resolve()}"'
    out = subprocess.run(["schtasks", "/Create", "/F", "/TN", GUARD_TASK, "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", tr],
                         capture_output=True, text=True, creationflags=NO_WINDOW)
    print((out.stdout + out.stderr).strip())
    if out.returncode:
        print("Run this from an elevated (Administrator) prompt: the guard is a SYSTEM startup task.")
        return out.returncode
    subprocess.run(["schtasks", "/Run", "/TN", GUARD_TASK], capture_output=True, creationflags=NO_WINDOW)
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fletchery's restart guard for this machine.")
    ap.add_argument("command", choices=["guard", "install-guard", "status"])
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    if a.command == "status":
        print(json.dumps({"boot": boot_time(), **readiness()}, indent=2))
        return 0
    if not a.config:
        ap.error("--config is required")
    if a.command == "install-guard":
        return install_guard(a.config)
    sys.path.insert(0, str(Path(__file__).parent))
    import reporter
    return guard(reporter.load_config(a.config))


if __name__ == "__main__":
    sys.exit(main())
