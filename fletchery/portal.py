"""The app's web server: the same page (ui/portal/) for the desktop window and the phone.

  - The desktop window (Fletchery.exe) opens http://127.0.0.1:<dashboardPort>. Only this PC can reach it. It has every
    control: queue, start, stop and re-run builds and machine jobs, pause one machine or all of them, cancel or re-run
    GitHub Actions runs, check GitHub now, restart the backend.
  - The phone opens the portal port through Tailscale Serve. It shows the same tabs and has the same controls, starting
    builds included, for the accounts in `users` only (`"controls": false` makes it read-only). No file path from this
    PC is in what it serves.

How the phone reaches it. The portal listens on this machine only (127.0.0.1). Tailscale Serve puts it on the private
network with a real HTTPS certificate:

    tailscale serve --bg --https=443 http://127.0.0.1:8772

Nothing is opened to the internet and no router port is forwarded. Serve adds `Tailscale-User-Login` to every request
(and removes it from what a client sends), so the portal knows who is asking:

  - the Host must be this machine or one of `hosts` (DNS rebinding);
  - a request that came through a proxy must carry a Tailscale login listed in `users`. Tailscale Funnel (public) traffic
    and tagged devices carry none, so they are refused;
  - a request with no proxy headers is this PC itself, and is allowed;
  - every POST must come from the app's own page (Origin and Sec-Fetch-Site).

Profile, under `workers`:

    "portal": {"port": 8772, "hosts": ["your-pc.your-tailnet.ts.net"], "users": ["you@example.com"],
               "controls": true, "push": {"contact": "mailto:you@example.com"}, "live": {...}}

`live` is the Live tab (devwatch.py), on unless `"live": false`. `push` is optional (push.py): notifications on the
phone for finished builds and anything Live lists as stuck. See docs/PORTAL.md."""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import api  # beside this file, like every Fletchery module

HERE = Path(__file__).parent
UI = HERE.parent / "ui" / "portal"
MAX_BODY = 8 * 1024
DEFAULT_PORT = 8772
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/portal.css": ("portal.css", "text/css; charset=utf-8"),
          "/portal.js": ("portal.js", "text/javascript; charset=utf-8"),
          "/live.js": ("live.js", "text/javascript; charset=utf-8"),
          "/kit.js": ("kit.js", "text/javascript; charset=utf-8"),
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
    controls = conf.get("controls", True)
    if not isinstance(controls, bool):
        raise ValueError("workers.portal.controls is true or false")
    return {"port": port, "hosts": hosts, "users": users, "push": ({} if push is True else push) or None,
            "live": None if live is False else live, "controls": controls}


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


def _job(j: dict) -> dict:
    params = j.get("params") or {}
    return {"id": j.get("id"), "recipe": _text(j.get("recipe"), 30), "state": _text(j.get("state"), 20),
            "machine": _text(j.get("machine"), 60), "ref": _text(j.get("ref"), 80), "sha": _text(j.get("sha"), 10),
            "version": _text(params.get("version"), 20), "requested_by": _text(_scrub(j.get("requested_by")), 120),
            "created": _text(j.get("created"), 40), "started": _text(j.get("started"), 40),
            "finished": _text(j.get("finished"), 40), "summary": _text(_scrub(j.get("summary")), 300),
            "cancel": bool(j.get("cancel")), "log_bytes": int(j.get("log_bytes") or 0),
            "steps": [{"name": _text(x.get("name"), 80), "state": _text(x.get("state"), 20),
                       "start": _text(x.get("start"), 40), "end": _text(x.get("end"), 40)}
                      for x in (j.get("steps") or [])[:40] if isinstance(x, dict)]}


def summary(status: dict) -> dict:
    """The backend's status, cut down to what the page shows. Built field by field so that nothing new in the status
    (keys, settings, paths) reaches the phone unless it is added here on purpose."""
    s = status or {}
    b = s.get("builds") or {}
    builds = [{"id": _text(x.get("id"), 40), "version": _text(x.get("version"), 40), "state": _text(x.get("state"), 20),
               "ref": _text(x.get("ref"), 80), "sha": _text(x.get("sha"), 10), "at": _text(x.get("at"), 40),
               "repeat": _text(x.get("repeat"), 10), "tests": _text(x.get("tests"), 10),
               "started": _text(x.get("started"), 40), "finished": _text(x.get("finished"), 40),
               "machine": _text(x.get("machine"), 60), "job": int(x.get("job") or 0),
               "reason": _text(_scrub(x.get("reason"))), "publish": _text(x.get("publish"), 20),
               "publish_reason": _text(_scrub(x.get("publish_reason")), 200), "links": _links(x)}
              for x in (b.get("builds") or [])[-40:]]
    builds.sort(key=lambda r: r["finished"] or r["started"] or r["at"], reverse=True)
    m = s.get("machines") or {}
    machines = []
    for x in m.get("machines") or []:
        if x.get("revoked"):
            continue
        machines.append({"name": _text(x.get("name"), 60), "online": bool(x.get("online")),
                         "last_seen": _text(x.get("last_seen"), 40), "labels": [_text(v, 40) for v in (x.get("labels") or [])[:12]],
                         "builds": "builds" not in (x.get("paused_work") or []) and "role:cook" in (x.get("labels") or []),
                         "paused": [_text(k, 20) for k in (x.get("paused_work") or [])[:8]],
                         "job": _job(x["job"]) if x.get("job") else None})
    jobs = [_job(j) for j in (m.get("jobs") or [])[:40]]
    return {"github_login": _text(s.get("github_login"), 39), "project": _text(s.get("project"), 80), "repo": _text(s.get("repo"), 140),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "started": _text(s.get("started"), 40),
            "version": _text(s.get("version"), 20),
            "base": _text(b.get("base"), 80), "build_machines": [_text(n, 60) for n in (b.get("nodes") or [])[:20]],
            "machines_on": bool(s.get("machines")), "held": bool(m.get("held")),
            "errors": [_text(_scrub(v), 300) for v in (s.get("errors") or {}).values()][:10],
            "restarting": _text(s.get("restarting"), 200),
            "builds": builds, "machines": machines, "jobs": jobs,
            "scratch": {k: (_text(_scrub(v), 200) if isinstance(v, str) else v) for k, v in (s.get("scratch") or {}).items()
                        if k in ("checked_at", "mode", "error", "waiting", "freed_gb", "trimmed", "quarantined", "removed")},
            "log": [{"at": _text(r.get("at"), 40), "line": _text(_scrub(r.get("line")), 300)}
                    for r in (s.get("log") or [])[-120:] if isinstance(r, dict)]}


PATHS = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|]*")


def _scrub(v) -> str:
    """Folder paths on this PC stay on this PC."""
    return PATHS.sub("[a folder on the PC]", str(v or ""))


class Portal:
    """`status_fn` is the backend's status (app.py App.status). `desktop_port` (0 = none) serves the same page to the
    desktop window on 127.0.0.1, where `actions` ({path: fn(data) -> dict}) may be posted; `conf["port"]` (0 = none)
    is the phone's portal, which may post them too unless `conf["controls"]` is false. Each
    action gets `_from` in its data: "the window" or "the phone (<login>)", for the activity log."""

    def __init__(self, status_fn, conf: dict, state_dir=None, log=None, repo: str = "", host: str = "127.0.0.1",
                 desktop_port: int = 0, actions: dict | None = None, job_log=None, layout=None, accounts=None):
        self.status_fn = status_fn
        self.conf = conf
        self.port = int(conf.get("port") or 0)
        self.controls = bool(conf.get("controls", True))
        self.desktop_port = int(desktop_port or 0)
        self.actions = dict(actions or {})
        self.job_log = job_log
        self.accounts = accounts
        self.layout = layout       # layout.Layout: where the cards sit (GET/POST /api/layout)
        self.host = host
        self.log = log or (lambda line: None)
        self.servers: list[ThreadingHTTPServer] = []
        self.errors: dict[str, str] = {}
        self._cache: tuple[float, bytes] = (0.0, b"")
        self._lock = threading.Lock()
        self._live_cache: tuple[float, bytes] = (0.0, b"")
        self.live = None
        if conf.get("live") is not None:
            import devwatch
            client = devwatch.GitHub(accounts.token, self.log, cli_env=accounts.cli_env) if accounts else None
            self.live = devwatch.DevWatch(conf["live"], self.log, github=client,
                                          state_dir=accounts.live_directory() if accounts else state_dir,
                                          count_account=accounts.login if accounts else "")
        self.push = None
        if conf.get("push") is not None and state_dir is not None:
            import push
            self.push = push.Push(Path(state_dir) / "portal-push.json", conf["push"], self.summary, self.log, repo)

    def summary(self) -> dict:
        out = summary(self.status_fn())
        if self.live is not None:   # for notifications (push.py): the alerts that fired (alerts.py), and those that cleared
            fields = ("kind", "key", "group", "title", "detail", "since", "repo", "fired")
            try:
                a = self.live.snapshot().get("alerts") or {}
                out["live_alerts"] = [{k: x.get(k) for k in fields} for x in a.get("firing") or []
                                      if x.get("notify") and not x.get("snoozed_until")]
                out["live_cleared"] = [{**{k: x.get(k) for k in fields}, "cleared": x.get("cleared")} for x in a.get("cleared") or []
                                       if x.get("notify") and not x.get("snoozed_until")]
            except Exception:
                out["live_alerts"], out["live_cleared"] = [], []
        return out

    def summary_bytes(self) -> bytes:
        """One read serves every screen that asks within two seconds."""
        with self._lock:
            at, body = self._cache
            if time.monotonic() - at > 2 or not body:
                body = json.dumps(self.summary()).encode("utf-8")
                self._cache = (time.monotonic(), body)
            return body

    def live_bytes(self) -> bytes:
        with self._lock:
            at, body = self._live_cache
            if time.monotonic() - at > 2 or not body:
                snapshot = self.live.snapshot()
                if self.accounts:
                    snapshot["github_login"] = self.accounts.login
                body = json.dumps(snapshot).encode("utf-8")
                self._live_cache = (time.monotonic(), body)
            return body

    # ---- who may ask ----
    def _local(self, host: str, port: int | None = None) -> bool:
        port = self.port if port is None else port
        return bool(port) and host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def is_desktop(self, headers, port: int) -> bool:
        """The desktop window: this PC, on the desktop port, with no proxy in between."""
        raw = (headers.get("Host") or "").strip().lower()
        proxied = any(headers.get(h) for h in ("Tailscale-User-Login", "X-Forwarded-For", "X-Forwarded-Host", "Forwarded"))
        return bool(self.desktop_port) and port == self.desktop_port and self._local(raw, port) and not proxied

    def allowed(self, headers, port: int | None = None) -> tuple[bool, str]:
        """(ok, why not). `headers` is any mapping with .get(); `port` is the server the request came to."""
        port = self.port if port is None else port
        if port == self.desktop_port and self.desktop_port:
            return (True, "") if self.is_desktop(headers, port) else (False, "The desktop page answers this PC only.")
        raw = (headers.get("Host") or "").strip().lower()
        local = self._local(raw, port)
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

    def can_act(self, headers, port: int, path: str) -> tuple[bool, str]:
        """May this request run a control? Called after allowed() and same_origin()."""
        if self.is_desktop(headers, port):
            return True, "the window"
        if port != self.port or not self.controls:
            return False, ""
        login = (headers.get("Tailscale-User-Login") or "").strip().lower()
        return True, f"the phone ({login})" if login else "the portal on this PC"

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
    def _handler(self, port: int):
        portal = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Fletchery"
            sys_version = ""
            timeout = 30   # a client that opens a connection and sends nothing does not hold a thread forever

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    self.close_connection = True

            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: str = "no-store") -> None:
                # Phones often reach the PC through a slow Tailscale relay, so text is sent compressed when asked.
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
                ok, why = portal.allowed(self.headers, port)
                if not ok:
                    self._json(403, {"error": why})
                return ok

            def do_GET(self):
                parts = urlsplit(self.path)
                path = parts.path
                if not self._gate():
                    return
                desktop = portal.is_desktop(self.headers, port)
                if path in STATIC:
                    name, ctype = STATIC[path]
                    try:
                        return self._send(200, (UI / name).read_bytes(), ctype, "no-cache")
                    except OSError:
                        return self._json(404, {"error": "not found"})
                if path == "/api/status" and desktop:   # the window's "is the backend up?"; nothing more
                    return self._json(200, {"ok": True, "desktop": True, "port": portal.port})
                if path == "/api/summary":
                    try:
                        body = json.loads(portal.summary_bytes())
                        body["desktop"] = desktop
                        body["controls"] = desktop or (port == portal.port and portal.controls)
                        return self._json(200, body)
                    except Exception as e:
                        return self._json(503, {"error": f"Fletchery could not report its status ({type(e).__name__})."})
                if path == "/api/live":
                    if portal.live is None:
                        return self._json(404, {"error": "The Live tab is off (workers.portal.live is false)."})
                    try:
                        return self._send(200, portal.live_bytes())
                    except Exception as e:
                        return self._json(503, {"error": f"The Live feed could not be read ({type(e).__name__})."})
                if path in ("/api/v2/state", "/api/v2/history", "/metrics"):      # the stable read API (api.py, docs/API.md)
                    if portal.live is None:
                        return self._json(404, {"error": "The Live tab is off (workers.portal.live is false)."})
                    try:
                        if path == "/api/v2/state":
                            return self._send(200, portal.live_bytes())
                        if path == "/metrics":
                            return self._send(200, api.metrics(json.loads(portal.live_bytes())).encode("utf-8"),
                                              "text/plain; version=0.0.4; charset=utf-8")
                        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                        return self._json(200, api.history(portal.live.store, q.get("table", ""), q.get("since", 0),
                                                           q.get("repo", ""), q.get("limit", 1000)))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    except Exception as e:
                        return self._json(503, {"error": f"Fletchery could not answer ({type(e).__name__})."})
                if path == "/api/jobs/log" and (desktop or portal.controls) and portal.job_log is not None:
                    try:
                        job = int((parse_qs(parts.query).get("id") or ["0"])[0])
                    except ValueError:
                        return self._json(400, {"error": "id is a number"})
                    text = portal.job_log(job) or ""
                    if not desktop:
                        text = _scrub(text)   # folder paths stay on this PC
                    return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
                if path == "/api/github/accounts":
                    may, _ = portal.can_act(self.headers, port, path)
                    if not may or portal.accounts is None:
                        return self._json(403, {"error": "This screen cannot switch GitHub accounts."})
                    try:
                        return self._json(200, {"logins": portal.accounts.choices()})
                    except (ValueError, OSError):
                        return self._json(503, {"error": "GitHub sign-ins are unavailable."})
                if path == "/api/layout":
                    may, _ = portal.can_act(self.headers, port, path)
                    saved = portal.layout.read() if portal.layout is not None else None
                    return self._json(200, {"layout": saved, "can_save": bool(may and portal.layout is not None)})
                if path == "/api/push":
                    return self._json(200, portal.push.info() if portal.push else
                                      {"available": False, "why": "Notifications are off (workers.portal.push)."})
                return self._json(404, {"error": "not found"})

            do_HEAD = do_GET

            def do_POST(self):
                if not self._gate():
                    return
                if not portal.same_origin(self.headers):
                    return self._json(403, {"error": "This request did not come from the app's own page."})
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
                pushes = {"/api/push/subscribe": "subscribe", "/api/push/unsubscribe": "unsubscribe", "/api/push/test": "test"}
                if self.path in pushes:
                    if not portal.push:
                        return self._json(400, {"error": "Notifications are off (workers.portal.push)."})
                    try:
                        return self._json(200, getattr(portal.push, pushes[self.path])(data))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    except Exception as e:
                        return self._json(502, {"error": f"{type(e).__name__}: {e}"[:300]})
                if self.path in ("/api/layout", "/api/layout/reset"):
                    ok, _ = portal.can_act(self.headers, port, self.path)
                    if not ok or portal.layout is None:
                        return self._json(403, {"error": "This screen keeps its layout to itself (the phone is read-only)."})
                    try:
                        if self.path == "/api/layout/reset":
                            portal.layout.reset()
                            return self._json(200, {"layout": None})
                        return self._json(200, {"layout": portal.layout.save(data.get("layout"))})
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    except OSError as e:
                        return self._json(502, {"error": f"The layout file could not be saved ({type(e).__name__})."})
                if self.path in portal.actions:
                    ok, who = portal.can_act(self.headers, port, self.path)
                    if not ok:
                        return self._json(403, {"error": "Do this in the Fletchery window on the PC." if portal.controls
                                                else "The phone is read-only (workers.portal.controls is false)."})
                    data["_from"] = who
                    try:
                        out = portal.actions[self.path](data)
                        with portal._lock:
                            portal._cache = (0.0, b"")   # the next read shows the change
                        return self._json(200, out if isinstance(out, dict) else {"ok": True})
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    except Exception as e:
                        return self._json(502, {"error": f"{type(e).__name__}: {e}"[:300]})
                return self._json(404, {"error": "not found"})

        return Handler

    def start(self) -> None:
        for name, port in (("desktop", self.desktop_port), ("portal", self.port)):
            if not port:
                continue
            try:
                server = ThreadingHTTPServer((self.host, port), self._handler(port))
            except OSError as e:
                self.errors[name] = f"The {name} page could not use port {port}: {e}"
                continue
            server.daemon_threads = True
            self.servers.append(server)
            threading.Thread(target=server.serve_forever, name=f"fletchery-{name}", daemon=True).start()
        if self.live is not None:
            self.live.start()
        if self.push:
            self.push.start()

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
        if self.push:
            self.push.stop()
        for server in self.servers:
            server.shutdown()
            server.server_close()
