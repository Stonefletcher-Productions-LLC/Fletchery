"""The phone portal: a second, read-only web server for following Fletchery away from the PC.

It is a small installable web app (ui/portal/): workers, tasks, what needs you, merge rounds, builds with their download
links, machines and the activity log. It can change nothing: there is no pause, no settings, no keys, no token for the
dashboard's own POSTs, and no file path from this PC in what it serves.

How it is reached. The portal listens on this machine only (127.0.0.1). Tailscale Serve puts it on the private network
with a real HTTPS certificate:

    tailscale serve --bg --https=443 http://127.0.0.1:8772

Nothing is opened to the internet and no router port is forwarded. Serve adds `Tailscale-User-Login` to every request
(and removes it from what a client sends), so the portal knows who is asking:

  - the Host must be this machine or one of `hosts` (DNS rebinding);
  - a request that came through a proxy must carry a Tailscale login listed in `users`. Tailscale Funnel (public) traffic
    and tagged devices carry none, so they are refused;
  - a request with no proxy headers is this PC itself, and is allowed;
  - the few POSTs (push subscriptions) must come from the portal's own page (Origin).

Profile, under `workers`:

    "portal": {"port": 8772, "hosts": ["your-pc.your-tailnet.ts.net"], "users": ["you@example.com"],
               "push": {"contact": "mailto:you@example.com"}}

`live` is optional (devwatch.py): the Live tab, on unless `"live": false`; its own settings go in that object.
`push` is optional (push.py): notifications on the phone for what needs you, finished rounds and finished builds.
See docs/PORTAL.md."""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).parent
UI = HERE.parent / "ui" / "portal"
MAX_BODY = 8 * 1024
DEFAULT_PORT = 8772
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/portal.css": ("portal.css", "text/css; charset=utf-8"),
          "/portal.js": ("portal.js", "text/javascript; charset=utf-8"),
          "/live.js": ("live.js", "text/javascript; charset=utf-8"),
          "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon-180.png": ("icon-180.png", "image/png"),
          "/icon-192.png": ("icon-192.png", "image/png"),
          "/icon-512.png": ("icon-512.png", "image/png")}
HOST = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def settings(conf) -> dict | None:
    """The checked `workers.portal` block, or None when the portal is off. Raises ValueError on a bad one."""
    if not conf:
        return None
    if not isinstance(conf, dict):
        raise ValueError("workers.portal must be an object, e.g. {\"port\": 8772, \"hosts\": [...], \"users\": [...]}")
    if conf.get("enabled") is False:
        return None
    try:
        port = int(conf.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        raise ValueError("workers.portal.port must be a number")
    if not 1024 <= port <= 65535:
        raise ValueError("workers.portal.port must be between 1024 and 65535")
    hosts, users = conf.get("hosts") or [], conf.get("users") or []
    if not isinstance(hosts, list) or not isinstance(users, list):
        raise ValueError("workers.portal.hosts and .users are lists")
    hosts = [str(h).strip().lower().rstrip(".") for h in hosts if str(h).strip()]
    for h in hosts:
        if not HOST.match(h):
            raise ValueError(f"workers.portal.hosts: {h!r} is not a host name (no https://, no port, no path)")
    users = [str(u).strip().lower() for u in users if str(u).strip()]
    push = conf.get("push")
    if push is not None and not isinstance(push, (dict, bool)):
        raise ValueError("workers.portal.push must be an object or true/false")
    live = conf.get("live", {})
    if live is not False and not isinstance(live, dict):
        raise ValueError("workers.portal.live must be an object or false (see devwatch.py)")
    return {"port": port, "hosts": hosts, "users": users, "push": ({} if push is True else push) or None,
            "live": None if live is False else live}


# ---- what the phone may see ----
def _text(v, limit: int = 400) -> str:
    return str(v or "")[:limit]


def _web(v) -> str:
    """Only real web links leave this PC: never a file path or another scheme."""
    s = str(v or "").strip()
    return s if s.startswith("https://") and " " not in s else ""


def _links(row: dict) -> list:
    out = []
    if _web(row.get("download")):
        out.append({"label": "GitHub download", "url": _web(row["download"])})
    for i, u in enumerate(x for x in str(row.get("drive") or "").split() if _web(x)):
        out.append({"label": "Google Drive" + (f" part {i + 1}" if i else ""), "url": u})
    return out


def summary(status: dict) -> dict:
    """The dashboard's status, cut down to what the read-only portal shows. Built field by field so that nothing new in
    the status (keys, settings, paths) reaches the phone unless it is added here on purpose."""
    s = status or {}
    live_tasks = {r.get("task") for r in s.get("live") or []}
    needs, queued, running, in_pr = [], [], [], []
    for t in s.get("tasks") or []:
        row = {"number": t.get("number"), "title": _text(t.get("title"), 200), "why": _text(t.get("why"), 300),
               "url": _web(t.get("url")), "pr": t.get("pr") or 0, "pr_url": _web(t.get("pr_url")),
               "agent": _text(t.get("agent"), 40), "depends": list(t.get("depends") or [])[:20]}
        st = t.get("status")
        if st == "blocked" or t.get("needs_person"):
            needs.append(row)
        elif t.get("number") in live_tasks and st != "pr" or st == "running":
            running.append(row)
        elif st == "pr":
            in_pr.append(row)
        elif st == "queued":
            queued.append(row)
    workers = [{"task": r.get("task"), "title": _text(r.get("title"), 200), "agent": _text(r.get("agent"), 40),
                "stage": _text(r.get("stage"), 80), "since": _text(r.get("since"), 40), "last": _text(r.get("last"), 200),
                "last_at": _text(r.get("last_at"), 40), "emergency": bool(r.get("emergency"))}
               for r in s.get("live") or []]
    mv, auto = s.get("merge_view") or {}, s.get("merge_auto") or {}
    prev = mv.get("previous") or {}
    merge = {"on": bool(auto.get("on")), "held": bool(auto.get("held")), "active": bool(mv.get("active")),
             "state": _text(mv.get("state"), 40), "round": _text(mv.get("round"), 40), "headline": _text(mv.get("headline")),
             "why": _text(auto.get("why")), "started": _text(mv.get("started"), 40), "updated": _text(mv.get("updated"), 40),
             "stale": bool(mv.get("stale")), "prs": [n for n in mv.get("prs") or [] if isinstance(n, int)][:50],
             "agent": _text((auto.get("running") or {}).get("agent"), 40),
             "steps": [{"name": _text(x.get("name"), 80), "state": _text(x.get("state"), 20),
                        "message": _text(x.get("message"), 200), "seconds": x.get("duration_seconds")}
                       for x in (mv.get("steps") or [])[:64] if isinstance(x, dict)],
             "previous": {"round": _text(prev.get("round"), 40), "result": _text(prev.get("result"), 120),
                          "ended": _text(prev.get("ended"), 40)} if prev else None,
             "backlog": int((auto.get("backlog") or {}).get("count") or 0),
             "waiting": [{"pr": m.get("pr"), "title": _text(m.get("title"), 200), "url": _web(m.get("pr_url")),
                          "ready": bool(m.get("ready")), "flags": [_text(f, 120) for f in (m.get("flags") or [])[:6]]}
                         for m in (s.get("merge") or [])[:100]]}
    builds = [{"id": _text(b.get("id"), 40), "version": _text(b.get("version"), 40), "state": _text(b.get("state"), 20),
               "ref": _text(b.get("ref"), 80), "sha": _text(b.get("sha"), 10), "at": _text(b.get("at"), 40),
               "started": _text(b.get("started"), 40), "finished": _text(b.get("finished"), 40),
               "machine": _text(b.get("machine") or ("this PC" if b.get("route") == "pc" else ""), 60),
               "reason": _text(_scrub(b.get("reason"))), "publish": _text(b.get("publish"), 20),
               "tests": _text(b.get("tests"), 10), "repeat": _text(b.get("repeat"), 10), "links": _links(b)}
              for b in ((s.get("builds") or {}).get("builds") or [])[-30:]]
    builds.sort(key=lambda b: b["finished"] or b["started"] or b["at"], reverse=True)
    machines = []
    for m in ((s.get("machines") or {}).get("machines") or []):
        if m.get("revoked"):
            continue
        job = m.get("job") or {}
        machines.append({"name": _text(m.get("name"), 60), "online": bool(m.get("online")),
                         "last_seen": _text(m.get("last_seen"), 40),
                         "job": _text(job.get("recipe"), 40), "job_ref": _text(job.get("ref"), 80),
                         "paused": [_text(k, 20) for k in (m.get("paused_work") or [])[:8]]})
    pc = s.get("pc") or {}
    pauses = []
    if s.get("signed_out"):
        pauses.append({"kind": "signed_out", "text": "A coding agent needs signing in again on the PC. " + _text(s["signed_out"], 200)})
    if s.get("held"):
        pauses.append({"kind": "held", "text": "Paused from the app: running work finishes, nothing new starts."})
    if s.get("limit_until"):
        pauses.append({"kind": "limit", "text": f"Usage limit: paused until {_text(s['limit_until'], 40)}. " + _text(s.get("limit_reason"), 200)})
    if s.get("restarting"):
        pauses.append({"kind": "restarting", "text": _text(s["restarting"], 200)})
    if s.get("sync_error"):
        pauses.append({"kind": "sync", "text": "GitHub could not be read: " + _text(_scrub(s["sync_error"]), 200)})
    return {"project": _text(s.get("project"), 80), "repo": _text(s.get("repo"), 140), "repo_url": _web(s.get("repo_url")),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "last_pass": _text(s.get("last_pass"), 40), "next_pass_at": _text(s.get("next_pass_at"), 40),
            "passing": bool(s.get("passing")), "pauses": pauses,
            "max_parallel": s.get("max_parallel") or 0, "spent_today": s.get("spent_today") or 0,
            "daily_budget": s.get("daily_budget") or 0, "auth": _text(s.get("auth"), 20),
            "pc": _text(_scrub(pc.get("text") or mv.get("pc_text")), 200),
            "needs": needs, "workers": workers, "running": running, "queued": queued, "in_pr": in_pr,
            "merge": merge, "builds": builds, "machines": machines,
            "log": [{"at": _text(r.get("at"), 40), "line": _text(_scrub(r.get("line")), 300)}
                    for r in (s.get("log") or [])[-40:] if isinstance(r, dict)]}


PATHS = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|]*")


def _scrub(v) -> str:
    """Folder paths on this PC stay on this PC."""
    return PATHS.sub("[a folder on the PC]", str(v or ""))


class Portal:
    def __init__(self, status_fn, conf: dict, state_dir=None, log=None, repo: str = "", host: str = "127.0.0.1"):
        self.status_fn = status_fn
        self.conf = conf
        self.port = conf["port"]
        self.host = host
        self.log = log or (lambda line: None)
        self.server: ThreadingHTTPServer | None = None
        self._cache: tuple[float, bytes] = (0.0, b"")
        self._lock = threading.Lock()
        self._live_cache: tuple[float, bytes] = (0.0, b"")
        self.live = None
        if conf.get("live") is not None:
            import devwatch
            self.live = devwatch.DevWatch(conf["live"], self.log)
        self.push = None
        if conf.get("push") is not None and state_dir is not None:
            import push
            self.push = push.Push(Path(state_dir) / "portal-push.json", conf["push"], self.summary, self.log, repo)

    def summary(self) -> dict:
        return summary(self.status_fn())

    def summary_bytes(self) -> bytes:
        """One read of the engine serves every phone that asks within three seconds."""
        with self._lock:
            at, body = self._cache
            if time.monotonic() - at > 3 or not body:
                body = json.dumps(self.summary()).encode("utf-8")
                self._cache = (time.monotonic(), body)
            return body

    def live_bytes(self) -> bytes:
        with self._lock:
            at, body = self._live_cache
            if time.monotonic() - at > 2 or not body:
                body = json.dumps(self.live.snapshot()).encode("utf-8")
                self._live_cache = (time.monotonic(), body)
            return body

    # ---- who may ask ----
    def _local(self, host: str) -> bool:
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}")

    def allowed(self, headers) -> tuple[bool, str]:
        """(ok, why not). `headers` is any mapping with .get()."""
        raw = (headers.get("Host") or "").strip().lower()
        local = self._local(raw)
        name = raw.rsplit(":", 1)[0] if re.search(r":\d+$", raw) else raw
        if not local and name.rstrip(".") not in self.conf["hosts"]:
            return False, "This address is not one of the portal's hosts (workers.portal.hosts)."
        login = (headers.get("Tailscale-User-Login") or "").strip().lower()
        proxied = bool(login or headers.get("X-Forwarded-For") or headers.get("X-Forwarded-Host")
                       or headers.get("Forwarded") or not local)
        if not proxied:
            return True, ""
        if not login:
            return False, ("No Tailscale sign-in came with this request. Open the portal from a phone or computer that is "
                           "signed in to the tailnet (not a tagged device, and not through Funnel).")
        if login not in self.conf["users"]:
            return False, (f"You are signed in to Tailscale as {login}, and that account is not on the portal's list. "
                           "Add it to workers.portal.users in the Fletchery profile, then restart Fletchery.")
        return True, ""

    def same_origin(self, headers) -> bool:
        origin = (headers.get("Origin") or "").strip().lower()
        host = (headers.get("Host") or "").strip().lower()
        if not origin or not host:
            return False
        parts = urlsplit(origin)
        if parts.scheme not in ("https", "http") or (headers.get("Sec-Fetch-Site") or "same-origin") != "same-origin":
            return False
        # The page's own address: what this request was sent to, or one of the portal's names (a proxy may rewrite Host).
        return parts.netloc == host or (parts.hostname or "").rstrip(".") in self.conf["hosts"]

    # ---- server ----
    def start(self) -> None:
        portal = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Fletchery"
            sys_version = ""

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True

            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: str = "no-store") -> None:
                # Phones often reach the PC through a slow Tailscale relay, so text is sent compressed when the phone takes it.
                squeeze = (len(body) > 1024 and not ctype.startswith("image/")
                           and "gzip" in (self.headers.get("Accept-Encoding") or "").lower())
                if squeeze:
                    body = gzip.compress(body, 5)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                if squeeze:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode("utf-8"))

            def _gate(self) -> bool:
                ok, why = portal.allowed(self.headers)
                if not ok:
                    self._json(403, {"error": why})
                return ok

            def do_GET(self):
                path = self.path.split("?", 1)[0].split("#", 1)[0]
                if not self._gate():
                    return
                if path in STATIC:
                    name, ctype = STATIC[path]
                    try:
                        return self._send(200, (UI / name).read_bytes(), ctype, "no-cache")
                    except OSError:
                        return self._json(404, {"error": "not found"})
                if path == "/api/summary":
                    try:
                        return self._send(200, portal.summary_bytes())
                    except Exception as e:
                        return self._json(503, {"error": f"Fletchery could not report its status ({type(e).__name__})."})
                if path == "/api/live":
                    if portal.live is None:
                        return portal_off(self)
                    try:
                        return self._send(200, portal.live_bytes())
                    except Exception as e:
                        return self._json(503, {"error": f"The Live feed could not be read ({type(e).__name__})."})
                if path == "/api/push":
                    return self._json(200, portal.push.info() if portal.push else
                                      {"available": False, "why": "Notifications are off for this portal (workers.portal.push)."})
                return self._json(404, {"error": "not found"})

            do_HEAD = do_GET

            def do_POST(self):
                if not self._gate():
                    return
                if not portal.same_origin(self.headers):
                    return self._json(403, {"error": "This request did not come from the portal's own page."})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    return self._json(413, {"error": "request too large"})
                try:
                    data = json.loads(self.rfile.read(length) or b"{}") if length else {}
                    if not isinstance(data, dict):
                        raise ValueError
                except ValueError:
                    return self._json(400, {"error": "bad JSON"})
                routes = {"/api/push/subscribe": "subscribe", "/api/push/unsubscribe": "unsubscribe", "/api/push/test": "test"}
                if self.path not in routes:
                    return self._json(404, {"error": "not found"})
                if not portal.push:
                    return self._json(400, {"error": "Notifications are off for this portal (workers.portal.push)."})
                try:
                    return self._json(200, getattr(portal.push, routes[self.path])(data))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                except Exception as e:
                    return self._json(502, {"error": f"{type(e).__name__}: {e}"[:300]})

        def portal_off(h):
            return h._json(404, {"error": "The Live tab is off for this portal (workers.portal.live is false)."})

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="fletchery-portal", daemon=True).start()
        if self.live is not None:
            self.live.start()
        if self.push:
            self.push.start()

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
        if self.push:
            self.push.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
