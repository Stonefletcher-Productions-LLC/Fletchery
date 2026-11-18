#!/usr/bin/env python3
"""Fletchery: one live view of all development (GitHub Actions, runners, PRs, checks, issues, every machine) plus the
build machines and scheduled builds. See README.md.

    python -m fletchery run --profile <file>      the backend (the desktop window and the supervisor start it)
    python -m fletchery config --profile <file>   print the settings it would use
    python -m fletchery live --profile <file>     one GitHub read, printed as the Live tab's JSON (checks a token)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nowindow  # noqa: E402

nowindow.install()  # before anything spawns: no child of Fletchery opens a console window (nowindow.py)

from app import App, BUSY_EXIT, Busy, take_lock  # noqa: E402
from config import Config  # noqa: E402


def lower_priority() -> None:
    """Fletchery runs below normal: builds, games and everything else win the CPU."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        except Exception:
            pass
    else:
        try:
            os.nice(10)
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fletchery", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "config", "live"])
    ap.add_argument("--profile", default=os.environ.get("FLETCHERY_PROFILE", ""))
    a = ap.parse_args(argv)
    cfg = Config.load(Path(a.profile) if a.profile else None)
    if a.command == "config":
        print(cfg.dump())
        return 0
    if a.command == "live":
        import json
        import devwatch
        w = devwatch.DevWatch(((cfg.portal or {}).get("live") or {}) if isinstance(cfg.portal, dict) else {})
        w.poll()
        print(json.dumps(w.snapshot(), indent=1))
        return 0
    lower_priority()
    try:
        lock = take_lock(Path(cfg.state_dir))
    except Busy as e:
        print(str(e), flush=True)
        return BUSY_EXIT
    app = App(cfg)
    code = 0
    try:
        app.start()
        app.log(f"Fletchery started for {cfg.name}: pid {os.getpid()}, window http://127.0.0.1:{cfg.dashboard_port}")
        code = app.serve()
    finally:
        app.stop()
        try:
            if lock.read_text().split()[0] == str(os.getpid()):
                lock.unlink()
        except (OSError, IndexError):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
