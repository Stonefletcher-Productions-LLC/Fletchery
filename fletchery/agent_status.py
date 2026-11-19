"""Tell Fletchery's Live tab what this coding agent is doing: one heartbeat, then exit. Standard library only.

For Claude Code and Codex running on a build machine (the PC or a node). It posts to the machines worker API
(`POST /v1/agent`) with the machine's existing key, the same way reporter.py does, so there is no new secret and no new
port. Cloud agents, which cannot reach the tailnet, edit a status issue comment instead (docs/AGENT-PRESENCE.md).

    python agent_status.py --agent claude --work "#123" --step "Writing the importer tests"
    python agent_status.py --agent codex --state waiting --step "Waiting on PR checks"
    python agent_status.py --agent claude --state done
    python agent_status.py setup --config <reporter.json or node.json>    (once per machine)

At the start of a task, at each new step (at most once a minute) and at the end (--state done).

- `--work`: the issue or PR, `#123` or `owner/repo#123`. `#123` gets the repository from this folder's `origin`.
- `--session`: defaults to a short hash of this git worktree's folder (one worktree per task), so every call from the
  same task updates the same row. Give one to override.
- `--branch`: defaults to the current git branch, so the Live tab ties the heartbeat to that branch's pushes.

Which machine and key: `--config`, else the file in FLETCHERY_AGENT_CONFIG, else the one `setup` remembered in
~/.fletchery/agent.json. Any of reporter.json (`coordinator`, `machine`, `keyFile`, or `nodeConfig`) or the node
worker's node.json works. The key is read from its file and never printed.

Exit code 0 when Fletchery took it, 1 when it could not be sent (the agent should carry on with its work), 2 when the
arguments or the configuration are wrong."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REMEMBERED = Path.home() / ".fletchery" / "agent.json"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0      # never flash a console window on the PC
GITHUB_REMOTE = re.compile(r"github\.com[:/]([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")


class Problem(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise Problem(f"{path} does not exist")
    except (OSError, ValueError) as e:
        raise Problem(f"{path} is not readable JSON ({type(e).__name__})")
    if not isinstance(data, dict):
        raise Problem(f"{path} is not a JSON object")
    return data


def find_config(given: str = "") -> Path:
    if given:
        return Path(given)
    if os.environ.get("FLETCHERY_AGENT_CONFIG"):
        return Path(os.environ["FLETCHERY_AGENT_CONFIG"])
    if REMEMBERED.exists():
        path = _read_json(REMEMBERED).get("config")
        if isinstance(path, str) and path:
            return Path(path)
    raise Problem("no configuration: pass --config, set FLETCHERY_AGENT_CONFIG, or run `agent_status.py setup --config ...` once")


def load(path: Path) -> dict:
    """coordinator, machine and keyFile, from reporter.json (or the node.json it points to) or node.json itself."""
    cfg = _read_json(path)
    if cfg.get("nodeConfig"):
        node = _read_json(Path(cfg["nodeConfig"]))
        for k in ("coordinator", "machine", "keyFile"):
            cfg.setdefault(k, node.get(k))
    missing = [k for k in ("coordinator", "machine", "keyFile") if not cfg.get(k)]
    if missing:
        raise Problem(f"{path} needs {', '.join(missing)} (or nodeConfig)")
    if not str(cfg["coordinator"]).startswith(("http://", "https://")):
        raise Problem(f"{path}: coordinator must be the worker API's http:// address")
    return cfg


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def default_session() -> str:
    where = git("rev-parse", "--show-toplevel") or os.getcwd()
    return "wt-" + hashlib.sha256(os.path.normcase(os.path.abspath(where)).encode("utf-8")).hexdigest()[:12]


def full_work(work: str) -> str:
    """`#123` becomes `owner/repo#123` from this folder's origin, when it is on GitHub."""
    if not work.startswith("#"):
        return work
    m = GITHUB_REMOTE.search(git("config", "--get", "remote.origin.url"))
    return f"{m.group(1)}/{m.group(2)}{work}" if m else work


def heartbeat(a) -> dict:
    branch = a.branch if a.branch is not None else git("rev-parse", "--abbrev-ref", "HEAD")
    return {"agent": a.agent, "session": a.session or default_session(), "work": full_work(a.work or ""),
            "branch": "" if branch == "HEAD" else branch, "step": " ".join((a.step or "").split())[:200], "state": a.state,
            "title": " ".join((getattr(a, "title", "") or "").split())[:120]}


def send(cfg: dict, body: dict, timeout: float = 5) -> dict:
    try:
        key = Path(cfg["keyFile"]).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise Problem(f"the machine key file cannot be read ({type(e).__name__})")
    req = urllib.request.Request(str(cfg["coordinator"]).rstrip("/") + "/v1/agent", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                                                         "X-Fletchery-Machine": str(cfg["machine"])})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or ""
        except ValueError:
            why = ""
        raise Problem(f"Fletchery refused it: HTTP {e.code} {why}".strip(), 2 if e.code == 400 else 1)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Problem(f"Fletchery could not be reached ({type(e).__name__}); carry on, the next heartbeat may get through", 1)


def setup(config: str) -> str:
    path = Path(config).resolve()
    load(path)                       # check it before remembering it
    REMEMBERED.parent.mkdir(parents=True, exist_ok=True)
    REMEMBERED.write_text(json.dumps({"config": str(path)}), encoding="utf-8")
    return f"agent_status: will use {path} (remembered in {REMEMBERED})"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["setup"]:
            sp = argparse.ArgumentParser(prog="agent_status.py setup")
            sp.add_argument("--config", required=True)
            print(setup(sp.parse_args(argv[1:]).config))
            return 0
        ap = argparse.ArgumentParser(description="Tell Fletchery's Live tab what this coding agent is doing.")
        ap.add_argument("--agent", required=True, choices=["claude", "codex"])
        ap.add_argument("--session", default="")
        ap.add_argument("--work", default="", help="#123 or owner/repo#123")
        ap.add_argument("--branch", default=None)
        ap.add_argument("--step", default="", help="what you are doing now, one line (200 characters at most)")
        ap.add_argument("--title", default="", help="the task's name, shown when no issue or PR is named (120 at most)")
        ap.add_argument("--state", default="working", choices=["working", "waiting", "done"])
        ap.add_argument("--config", default="")
        ap.add_argument("--dry-run", action="store_true", help="print the heartbeat, send nothing")
        a = ap.parse_args(argv)
        body = heartbeat(a)
        if a.dry_run:
            print(json.dumps(body))
            return 0
        send(load(find_config(a.config)), body)
        return 0
    except Problem as e:
        print(f"agent_status: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
