"""A Fletchery project profile: one JSON file per project. Fletchery only reads it (old keys from the retired coding
agents and merge rounds are ignored, so an existing profile keeps working).

    {
      "name": "LifeShare", "repo": "owner/LifeShare",
      "workers": {
        "baseBranch": "Test-Staging",
        "dashboardPort": 8770,                       # the desktop window: http://127.0.0.1:8770
        "stateDir": "E:\\lifeshare-scratch\\foreman",  # "" = %LOCALAPPDATA%\\Fletchery\\<name>
        "portal": {"port": 8772, "users": ["you@example.com"], "push": {...}, "live": {...}},   # portal.py
        "machines": {"listen": "100.64.0.10:8771", "assetRoots": [...]},                   # machines.py
        "builds": {"outDir": "...", "nodeLabels": [...], "publish": {...}},                 # builds.py
        "scratchRoots": ["E:\\lifeshare-scratch"], "scratchMode": "trim", ...               # scratch.py
      }
    }
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path

HERE = Path(__file__).parent
PRODUCT_ROOT = HERE.parent  # this repository / install folder


def default_state_dir(name: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (name or "project")).strip("-") or "project"
    return str(Path(base) / "Fletchery" / safe)


# profile key (in "workers") -> Config field
WORKER_KEYS = {
    "baseBranch": "base_branch", "stateDir": "state_dir", "dashboardPort": "dashboard_port",
    "heavyLeaseFile": "heavy_lease_file", "builds": "builds", "machines": "machines", "portal": "portal",
    "scratchRoots": "scratch_roots", "scratchMode": "scratch_mode", "scratchKeep": "scratch_keep",
    "scratchTrimHours": "scratch_trim_hours", "scratchRemoveDays": "scratch_remove_days",
    "scratchDisposable": "scratch_disposable", "scratchBackupDir": "scratch_backup_dir",
}
# profile key (top level) -> Config field
PROFILE_KEYS = {"name": "name", "repo": "repo"}


@dataclass
class Config:
    name: str = "project"
    repo: str = ""                           # owner/name: builds are made from this repository
    base_branch: str = "main"                # what a build packages when no branch is named
    state_dir: str = ""                      # "" = %LOCALAPPDATA%\Fletchery\<name>
    dashboard_port: int = 8770               # the desktop window's page, on 127.0.0.1 only
    heavy_lease_file: str = ""               # LaneGuard's heavy.json on this PC (shown on Live by the reporter)
    builds: dict = None                      # builds.py: {"outDir", "nodeLabels", "timeoutMinutes", "publish"}
    machines: dict = None                    # machines.py: {"listen": "<Tailscale IP>:8771", "assetRoots": [...]}
    portal: dict = None                      # portal.py: {"port": 8772, "users": [...], "push": {...}, "live": {...}}
    # Scratch retention (scratch.py; 2026-11-16: sessions' own worktrees filled both machines). Folders directly under
    # `scratch_roots`: build output is trimmed once idle `scratch_trim_hours`; in "remove" mode a clean, pushed,
    # unlocked worktree idle `scratch_remove_days` is listed, a day later quarantined, and a week after that removed
    # with `git worktree remove`. A folder that is not a git checkout is never removed. No roots = off.
    # Modes: off, report, trim, remove.
    scratch_roots: list = None
    scratch_mode: str = "trim"
    scratch_keep: list = None
    scratch_trim_hours: float = 24
    scratch_remove_days: float = 7
    scratch_disposable: list = None
    scratch_backup_dir: str = ""

    @staticmethod
    def load(path: Path | None = None) -> "Config":
        cfg = Config()
        cfg._path = Path(path) if path else None
        if cfg._path and cfg._path.exists():
            data = json.loads(cfg._path.read_text(encoding="utf-8-sig"))
            for k, v in data.items():
                if k in PROFILE_KEYS:
                    setattr(cfg, PROFILE_KEYS[k], v)
            for k, v in (data.get("workers") or {}).items():
                if k in WORKER_KEYS:
                    setattr(cfg, WORKER_KEYS[k], v)
        if not cfg.state_dir:
            cfg.state_dir = default_state_dir(cfg.name)
        return cfg

    def dump(self) -> str:
        return json.dumps({f.name: getattr(self, f.name) for f in fields(Config)}, indent=2)
