"""Keep Fletchery's child processes from opening console windows, and let it step aside while a game runs.

Why this exists (2026-11-16): Fletchery and its node worker run windowless (pythonw, logon task). On Windows 11 a
console program (git, powershell, cmd, python, UnrealEditor-Cmd) started from a windowless parent gets a brand new
console, and with Windows Terminal as the default terminal that is a visible window that takes focus. Every git call of
a PR check opened one, all day, on top of Alex's game. `install()` makes every subprocess.Popen in this process (asyncio
and the agent SDK included) default to CREATE_NO_WINDOW unless the caller asked for a console or a detached process.

`busy_with()` names the first running program from a "yield to" list (games), so the loop and the node worker start
nothing new on this PC while one runs. Standard library only.
"""
from __future__ import annotations

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
_CONSOLE_FLAGS = 0x00000008 | 0x00000010   # DETACHED_PROCESS | CREATE_NEW_CONSOLE: the caller chose on purpose
_installed = False


def flags_for(creationflags: int) -> int:
    """The creation flags a child really gets: CREATE_NO_WINDOW added unless the caller picked a console mode."""
    creationflags = int(creationflags or 0)
    if creationflags & _CONSOLE_FLAGS:
        return creationflags
    return creationflags | CREATE_NO_WINDOW


def install() -> bool:
    """Patch subprocess.Popen once for this process. No-op off Windows. Returns whether the patch is active."""
    global _installed
    if os.name != "nt":
        return False
    if _installed:
        return True
    original = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        kwargs["creationflags"] = flags_for(kwargs.get("creationflags", 0))
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__
    _installed = True
    return True


def running_names() -> set[str]:
    """Lower-case image names of the running processes (Windows: tasklist; elsewhere /proc). Empty if unknown."""
    names: set[str] = set()
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20,
                                 creationflags=CREATE_NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return names
        for line in out.splitlines():
            if line.startswith('"'):
                names.add(line[1:].split('"', 1)[0].lower())
        return names
    try:
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                        names.add(f.read().strip().lower())
                except OSError:
                    pass
    except OSError:
        pass
    return names


def busy_with(yield_to, names: set[str] | None = None) -> str:
    """The first program from `yield_to` (image names such as "Wow.exe") that is running now, else ""."""
    wanted = [str(n).strip() for n in (yield_to or []) if str(n).strip()]
    if not wanted:
        return ""
    names = running_names() if names is None else names
    for n in wanted:
        if n.lower() in names:
            return n
    return ""
