"""GitHub through `gh api`: the few writes and branch reads that builds and downloads need (builds.py, publish.py).
The Live tab reads GitHub on its own, with ETags (devwatch.py)."""
from __future__ import annotations

import json
import subprocess
import time


def parse_pages(text: str):
    """`gh api --paginate` prints one JSON document per page back to back (`[...][...]`). Decode them one at a time:
    splitting on "][" breaks on any body that contains it, like a markdown link `[a][b]`."""
    dec = json.JSONDecoder()
    docs, i, text = [], 0, text.strip()
    while i < len(text):
        doc, i = dec.raw_decode(text, i)
        docs.append(doc)
        while i < len(text) and text[i].isspace():
            i += 1
    if not docs:
        return None
    if all(isinstance(d, list) for d in docs):
        return [x for d in docs for x in d]
    return docs[0] if len(docs) == 1 else docs


# Failures before the request reached GitHub: safe to repeat even for a POST.
_NEVER_CONNECTED = ("connectex", "connection refused", "no such host", "temporary failure in name resolution",
                    "tls handshake timeout", "dial tcp")


class GitHub:
    def __init__(self, repo: str, gh: str = "gh"):
        self.repo = repo
        self.gh = gh

    # ---- raw ----
    def _request(self, path: str, method: str = "GET", body: dict | None = None, paginate: bool = False) -> str:
        cmd = [self.gh, "api", "-X", method, path]
        if paginate:
            cmd.append("--paginate")
        if body is not None:
            cmd += ["--input", "-"]
        # gh prints UTF-8; Windows' default (cp1252) can't decode some bytes and the whole pass failed (2026-11-03)
        # Reads retry once. A write retries only when the connection never opened (nothing reached GitHub), so a retry
        # can't post a comment twice; a dropped PATCH crashed task #1013's planner and two worker passes (2026-11-08/09).
        attempts = 2 if method == "GET" else 3
        for attempt in range(attempts):
            if attempt and method != "GET":
                time.sleep(2 * attempt)
            try:
                p = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=15 if method == "GET" else 90)
            except subprocess.TimeoutExpired as e:
                if method == "GET" and attempt + 1 < attempts:  # a write that timed out may have landed
                    continue
                raise RuntimeError(f"GitHub {method} {path} timed out") from e
            if p.returncode == 0:
                break
            detail = p.stderr.strip() or p.stdout.strip()
            low = detail.lower()
            if method == "GET":
                transient = any(x in low for x in ("timeout", "timed out", "connectex", "connection reset", "connection refused", "no such host", "temporary failure", "tls handshake"))
            else:
                transient = any(x in low for x in _NEVER_CONNECTED)
            if not transient or attempt + 1 == attempts:
                raise RuntimeError(f"gh api {method} {path}: {detail}")
        return p.stdout

    def api(self, path: str, method: str = "GET", body: dict | None = None, paginate: bool = False, fresh: bool = False):
        """`fresh` is accepted for callers written against the old cached client; every call reads GitHub."""
        output = self._request(path, method, body, paginate)
        return parse_pages(output) if paginate else (json.loads(output) if output.strip() else None)
