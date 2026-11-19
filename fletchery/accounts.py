"""GitHub sign-ins available to this backend. Persist only the selected login."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
TOKEN_VARS = ("FLETCHERY_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def _gh(args, env):
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                                timeout=12, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("GitHub sign-in is unavailable.") from None
    if result.returncode:
        raise ValueError("GitHub could not verify this sign-in.")
    return result.stdout.strip()


def stored_env():
    return {k: v for k, v in os.environ.items() if k not in TOKEN_VARS}


class Accounts:
    def __init__(self, directory):
        self.path = Path(directory) / "github-account.json"
        self.lock = threading.Lock()
        self.selected = self.original = self.login = ""
        self._secret = ""
        self._until = 0
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            record = None
        if record is not None:
            self.selected, self.original = record["login"], record["original"]
            if not LOGIN.fullmatch(self.selected) or (self.original and not LOGIN.fullmatch(self.original)):
                raise ValueError("Invalid saved GitHub login.")
            self.login = self.selected

    def token(self):
        from devwatch import gh_token
        if not self.selected:
            return gh_token()
        with self.lock:
            if time.monotonic() >= self._until:
                self._secret = _gh(["auth", "token", "--hostname", "github.com", "--user", self.selected], stored_env())
                if not self._secret:
                    raise ValueError("The selected GitHub sign-in is unavailable.")
                self._until = time.monotonic() + 60
            return self._secret

    def cli_env(self):
        if self.selected or os.environ.get("FLETCHERY_GITHUB_TOKEN"):
            return {**stored_env(), "GH_TOKEN": self.token()}
        return None

    def identify(self):
        if self.selected:
            return
        try:
            login = _gh(["api", "--hostname", "github.com", "user", "--jq", ".login"], self.cli_env())
            self.login = login if LOGIN.fullmatch(login) else ""
        except ValueError:
            self.login = ""

    def choices(self):
        data = json.loads(_gh(["auth", "status", "--hostname", "github.com", "--json", "hosts"], stored_env()))
        return [row["login"] for row in data.get("hosts", {}).get("github.com", [])
                if row.get("state") == "success" and LOGIN.fullmatch(row.get("login") or "")]

    def select(self, login):
        if not isinstance(login, str) or not LOGIN.fullmatch(login) or login not in self.choices():
            raise ValueError("Choose a signed-in GitHub account.")
        token = _gh(["auth", "token", "--hostname", "github.com", "--user", login], stored_env())
        if not token or _gh(["api", "--hostname", "github.com", "user", "--jq", ".login"],
                            {**stored_env(), "GH_TOKEN": token}).lower() != login.lower():
            raise ValueError("GitHub could not verify the selected account.")
        with self.lock:
            original = self.original if self.selected else self.login
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"login": login, "original": original}), encoding="utf-8")
            tmp.replace(self.path)

    def live_directory(self):
        if not self.selected or self.selected.lower() == self.original.lower():
            return self.path.parent
        return self.path.parent / "accounts" / ("user-" + self.selected.lower())
