"""The page's server (portal.py) and the phone's notifications (push.py): what it shows, who may ask, and who may use
the controls (the desktop window always; the phone's listed accounts unless controls are off; nobody else)."""
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import portal  # noqa: E402
import push  # noqa: E402
from config import Config  # noqa: E402

HOSTNAME = "your-pc.your-tailnet.ts.net"
USER = "you@example.com"
STATUS = {
    "project": "Harbor", "repo": "example/harbor", "pid": 4242, "started": "2026-11-16T03:00:00+00:00", "version": "abc12345",
    "errors": {"machines": r"Build machines not started: OSError in E:\scratch\foreman"}, "restarting": "",
    "secret": "page-secret", "keys": [{"name": "ANTHROPIC_API_KEY", "tail": "abcd"}],
    "builds": {"base": "Test-Staging", "nodes": ["NODE-01"], "out_dir": r"E:\lifeshare-scratch\builds", "builds": [
        {"id": "b1", "version": "0.4.5", "state": "done", "ref": "Test-Staging", "sha": "326ed7bfd3aa", "finished": "2026-11-16T02:00:00+00:00",
         "route": "node", "machine": "NODE-01", "output": r"E:\builds\LifeShare-0.4.5", "publish": "published",
         "download": "https://github.com/example/harbor/releases/tag/build-0.4.5",
         "drive": "https://drive.google.com/file/d/one https://drive.google.com/file/d/two javascript:alert(1)",
         "reason": r"Packaged to E:\builds\LifeShare-0.4.5"},
        {"id": "b2", "version": "0.4.6", "state": "scheduled", "at": "2026-11-17T02:00:00+00:00", "ref": "main"}]},
    "machines": {"listen": "100.64.0.10:8771", "held": True, "machines": [
        {"name": "NODE-01", "online": True, "last_seen": "2026-11-16T04:00:00+00:00", "labels": ["os:windows", "role:cook"],
         "job": {"id": 7, "recipe": "package", "ref": "main", "state": "running", "steps": [{"name": "Build the editor", "state": "running"}]},
         "paused_work": []},
        {"name": "old", "revoked": True}],
        "jobs": [{"id": 7, "recipe": "package", "state": "running", "machine": "NODE-01", "params": {"version": "0.4.6", "cmd": "x"},
                  "requested_by": r"build 0.4.6 in C:\Users\you", "log_bytes": 10}]},
    "scratch": {"checked_at": "2026-11-16T03:30:00+00:00", "mode": "trim", "freed_gb": 3, "roots": [r"E:\lifeshare-scratch"]},
    "log": [{"at": "2026-11-16T04:00:00+00:00", "line": r"job 7 uses C:\Users\you\wt\slot-1"}],
}


class Summary(unittest.TestCase):
    def setUp(self):
        self.s = portal.summary(STATUS)

    def test_builds_machines_and_jobs(self):
        done = next(b for b in self.s["builds"] if b["version"] == "0.4.5")
        self.assertEqual([l["label"] for l in done["links"]], ["GitHub download", "Google Drive", "Google Drive part 2"])
        self.assertEqual(done["machine"], "NODE-01")
        self.assertEqual([m["name"] for m in self.s["machines"]], ["NODE-01"])
        self.assertTrue(self.s["machines"][0]["builds"])
        self.assertEqual(self.s["machines"][0]["job"]["steps"][0]["name"], "Build the editor")
        self.assertEqual((self.s["jobs"][0]["id"], self.s["jobs"][0]["version"]), (7, "0.4.6"))
        self.assertEqual((self.s["held"], self.s["base"], self.s["build_machines"]), (True, "Test-Staging", ["NODE-01"]))

    def test_the_app_itself(self):
        self.assertEqual((self.s["started"], self.s["version"]), ("2026-11-16T03:00:00+00:00", "abc12345"))
        self.assertEqual(len(self.s["errors"]), 1)
        self.assertEqual(self.s["scratch"]["freed_gb"], 3)
        self.assertNotIn("roots", self.s["scratch"])

    def test_nothing_private_leaves_the_pc(self):
        text = json.dumps(self.s)
        for secret in ("ANTHROPIC", "abcd", "page-secret", "lifeshare-scratch", "E:\\\\", "C:\\\\", "100.64.0.10",
                       "javascript:", "out_dir", "4242", '"cmd"'):
            self.assertNotIn(secret, text)
        self.assertIn("[a folder on the PC]", text)

    def test_empty_status(self):
        s = portal.summary({})
        self.assertEqual((s["builds"], s["machines"], s["jobs"], s["held"], s["machines_on"]), ([], [], [], False, False))


class Settings(unittest.TestCase):
    def test_off_and_defaults(self):
        self.assertIsNone(portal.settings(None))
        self.assertIsNone(portal.settings({"enabled": False, "port": 8772}))
        self.assertEqual(portal.settings({"hosts": ["Your-PC.Your-Tailnet.ts.net."], "users": ["You@Example.com"]}),
                         {"port": 8772, "hosts": [HOSTNAME], "users": [USER], "push": None, "live": {}, "controls": True})
        self.assertIsNone(portal.settings({"live": False})["live"])

    def test_bad_blocks(self):
        for bad in ("yes", {"port": 80}, {"port": "x"}, {"hosts": "a.ts.net"}, {"hosts": ["https://a.ts.net"]},
                    {"hosts": ["a.ts.net:8443"]}, {"push": "on"}, {"controls": "yes"}):
            with self.assertRaises(ValueError):
                portal.settings(bad)

    def test_profile_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.json"
            p.write_text(json.dumps({"name": "x", "workers": {"portal": {"port": 9001}}}), encoding="utf-8")
            self.assertEqual(Config.load(p).portal, {"port": 9001})


class Served(unittest.TestCase):
    extra: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        conf = portal.settings({"port": 8772, "hosts": [HOSTNAME], "users": [USER], "push": {"contact": "mailto:a@example.com"},
                                **self.extra})
        import socket
        ports = []
        for _ in range(2):
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                ports.append(s.getsockname()[1])
        conf["port"], self.desk = ports
        self.calls = []

        def action(name):
            def run(d):
                self.calls.append((name, dict(d)))
                if d.get("bad"):
                    raise ValueError("Build not found.")
                return {"message": "ok"}
            return run
        acts = {p: action(p) for p in ("/api/builds/add", "/api/builds/cancel", "/api/jobs/cancel", "/api/machines/hold", "/api/restart")}
        self.p = portal.Portal(lambda: STATUS, conf, self.tmp.name, desktop_port=self.desk, actions=acts,
                               job_log=lambda job: f"job {job} ran in E:\\lifeshare-scratch\\slot-1")
        self.p.push.start = lambda: None
        self.p.live.start = lambda: None      # never reads the real GitHub in tests (test_devwatch.py fakes it)
        self.p.start()
        self.port = conf["port"]

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def ask(self, path, headers=None, data=None, method=None, port=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port or self.port}{path}", headers=headers or {}, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def remote(self, **extra):
        return {"Host": HOSTNAME, "X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": USER, **extra}

    def test_this_pc_reads_everything(self):
        for path in portal.STATIC:
            code, body, headers = self.ask(path)
            self.assertEqual((code, bool(body)), (200, True), path)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        code, body, _ = self.ask("/api/summary")
        self.assertEqual((code, json.loads(body)["project"]), (200, "Harbor"))

    def test_text_is_sent_compressed_when_asked(self):
        import gzip
        for path in ("/", "/portal.js"):
            plain = self.ask(path)[1]
            code, body, headers = self.ask(path, self.remote(**{"Accept-Encoding": "gzip, br"}))
            self.assertEqual((code, headers.get("Content-Encoding")), (200, "gzip"), path)
            self.assertLess(len(body), len(plain))
            self.assertEqual(gzip.decompress(body), plain)
        self.assertIsNone(self.ask("/icon-192.png", {"Accept-Encoding": "gzip"})[2].get("Content-Encoding"))
        self.assertIsNone(self.ask("/")[2].get("Content-Encoding"))

    def test_page_has_no_inline_code(self):
        html = (portal.UI / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<style", html)
        self.assertNotIn(" style=", html)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(html, r"\son[a-z]+=")

    def test_tailnet_member_on_the_list(self):
        self.assertEqual(self.ask("/api/summary", self.remote())[0], 200)
        self.assertEqual(self.ask("/api/summary", self.remote(Host=HOSTNAME + ":8443"))[0], 200)
        self.assertEqual(self.ask("/api/summary", self.remote(**{"Tailscale-User-Login": "YOU@example.com"}))[0], 200)

    def test_everyone_else_is_refused(self):
        cases = {"another account": self.remote(**{"Tailscale-User-Login": "eve@example.com"}),
                 "Funnel or a tagged device (no login)": {"Host": HOSTNAME, "X-Forwarded-For": "8.8.8.8"},
                 "no proxy headers at all": {"Host": HOSTNAME},
                 "another name for this PC (DNS rebinding)": self.remote(Host="evil.example.com"),
                 "proxied but addressed as local": {"Host": f"127.0.0.1:{self.port}", "X-Forwarded-For": "8.8.8.8"}}
        for name, headers in cases.items():
            for path in ("/", "/api/summary", "/api/push"):
                self.assertEqual(self.ask(path, headers)[0], 403, f"{name}: {path}")
        code, body, _ = self.ask("/api/summary", cases["another account"])
        self.assertIn("eve@example.com", json.loads(body)["error"])
        self.assertNotIn("Harbor", body.decode())

    def phone_post(self, path, body=b"{}", **extra):
        return self.ask(path, self.remote(**{"Origin": f"https://{HOSTNAME}", "Content-Type": "application/json", **extra}), body, "POST")

    def desk_post(self, path, body=b"{}", **extra):
        h = {"Host": f"127.0.0.1:{self.desk}", "Origin": f"http://127.0.0.1:{self.desk}", "Content-Type": "application/json", **extra}
        return self.ask(path, h, body, "POST", port=self.desk)

    def test_the_window_has_every_control(self):
        for path in ("/api/builds/add", "/api/builds/cancel", "/api/jobs/cancel", "/api/machines/hold", "/api/restart"):
            self.assertEqual(self.desk_post(path)[0], 200, path)
        self.assertEqual(self.calls[-1], ("/api/restart", {"_from": "the window"}))
        code, body, _ = self.desk_post("/api/builds/cancel", b'{"bad": 1}')
        self.assertEqual((code, json.loads(body)["error"]), (400, "Build not found."))
        h = {"Host": f"127.0.0.1:{self.desk}"}
        s = json.loads(self.ask("/api/summary", h, port=self.desk)[1])
        self.assertEqual((s["desktop"], s["controls"]), (True, True))
        self.assertEqual(self.ask("/api/status", h, port=self.desk)[0], 200)
        log = self.ask("/api/jobs/log?id=7", h, port=self.desk)[1].decode()
        self.assertIn("lifeshare-scratch", log)   # the window is on this PC: paths as they are

    def test_the_window_port_answers_only_this_pc(self):
        for headers in ({"Host": f"127.0.0.1:{self.desk}", "X-Forwarded-For": "100.64.0.9"},
                        {"Host": "evil.example.com"}, {"Host": f"127.0.0.1:{self.desk}", "Tailscale-User-Login": USER}):
            self.assertEqual(self.ask("/api/summary", headers, port=self.desk)[0], 403, headers)
        self.assertEqual(self.desk_post("/api/restart", Origin="https://evil.example.com")[0], 403)
        self.assertEqual(self.desk_post("/api/restart", **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.calls, [])

    def test_the_phone_has_the_controls_but_not_a_new_build(self):
        s = json.loads(self.ask("/api/summary", self.remote())[1])
        self.assertEqual((s["desktop"], s["controls"]), (False, True))
        self.assertEqual(self.phone_post("/api/restart")[0], 200)
        self.assertEqual(self.calls, [("/api/restart", {"_from": f"the phone ({USER})"})])
        self.assertEqual(self.phone_post("/api/builds/add", b'{"version": "1.0.0"}')[0], 403)
        self.assertEqual(len(self.calls), 1)
        log = self.ask("/api/jobs/log?id=7", self.remote())[1].decode()
        self.assertIn("job 7 ran in [a folder on the PC]", log)

    def test_strangers_and_other_pages_cannot_use_the_controls(self):
        self.assertEqual(self.phone_post("/api/restart", **{"Tailscale-User-Login": "eve@example.com"})[0], 403)
        self.assertEqual(self.phone_post("/api/restart", Origin="https://evil.example.com")[0], 403)
        self.assertEqual(self.phone_post("/api/restart", **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.ask("/api/restart", {"Content-Type": "application/json"}, b"{}", "POST")[0], 403)   # no Origin
        self.assertEqual(self.ask("/api/jobs/log?id=7", self.remote(**{"Tailscale-User-Login": "eve@example.com"}))[0], 403)
        self.assertEqual(self.calls, [])

    def test_nothing_else_is_served(self):
        for path in ("/api/pause", "/api/task", "/api/settings/save", "/api/merge/now", "/api/summary"):
            self.assertEqual(self.phone_post(path)[0], 404, path)
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.ask("/api/summary", method=method)[0], 501)
        for path in ("/api/status", "/api/merge/log", "/../fletchery/config.py", "/portal.js/../../index.html"):
            self.assertEqual(self.ask(path)[0], 404, path)

    def test_posts_need_the_portals_own_page(self):
        body = json.dumps({"endpoint": "https://web.push.apple.com/x"}).encode()
        self.assertEqual(self.ask("/api/push/unsubscribe", {"Content-Type": "application/json"}, body, "POST")[0], 403)
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin="https://evil.example.com"), body, "POST")[0], 403)
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin=f"https://{HOSTNAME}", **{"Sec-Fetch-Site": "cross-site"}), body, "POST")[0], 403)
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin=f"https://{HOSTNAME}"), body, "POST")[0], 200)
        big = b"{" + b" " * portal.MAX_BODY + b"}"
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin=f"https://{HOSTNAME}"), big, "POST")[0], 413)


class ReadOnlyPhone(Served):
    extra = {"controls": False}

    def test_the_phone_is_read_only_when_controls_are_off(self):
        s = json.loads(self.ask("/api/summary", self.remote())[1])
        self.assertFalse(s["controls"])
        code, body, _ = self.phone_post("/api/restart")
        self.assertEqual(code, 403)
        self.assertIn("read-only", json.loads(body)["error"])
        self.assertEqual(self.ask("/api/jobs/log?id=7", self.remote())[0], 404)
        self.assertEqual(self.desk_post("/api/restart")[0], 200)   # the window still can

    # the inherited tests below assume phone controls; they run in Served
    test_the_phone_has_the_controls_but_not_a_new_build = None
    test_strangers_and_other_pages_cannot_use_the_controls = None


@unittest.skipUnless(push.crypto_ready(), "the cryptography package is not installed")
class Notifications(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.phone = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        point = self.phone.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.sub = {"endpoint": "https://web.push.apple.com/QABC", "keys": {"p256dh": push.b64(point), "auth": push.b64(self.auth)}}
        self.view = portal.summary(STATUS)
        self.sent, self.logs, self.answer = [], [], 201
        self.push = self.make()

    def make(self, conf=None):
        p = push.Push(Path(self.tmp.name) / "push.json", conf or {"contact": "mailto:a@example.com"}, lambda: self.view, self.logs.append)
        p.send = lambda endpoint, body, headers: (self.sent.append((endpoint, body, headers)), self.answer)[1]
        return p

    def tearDown(self):
        self.tmp.cleanup()

    def notes(self):
        return [json.loads(push.decrypt(body, self.phone, self.auth)) for _, body, _ in self.sent]

    def test_only_the_phone_can_read_it(self):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.asymmetric import ec
        body = push.encrypt(b"hello", push.unb64(self.sub["keys"]["p256dh"]), self.auth)
        self.assertEqual(push.decrypt(body, self.phone, self.auth), b"hello")
        self.assertNotIn(b"hello", body)
        with self.assertRaises(InvalidTag):
            push.decrypt(body, ec.generate_private_key(ec.SECP256R1()), self.auth)
        with self.assertRaises(InvalidTag):
            push.decrypt(body, self.phone, b"f" * 16)

    def test_signed_as_this_install(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        private, public = push.new_key()
        header = push.vapid_header("https://web.push.apple.com/QABC", private, public, "mailto:a@example.com", now=1000)
        token = header.split("t=")[1].split(",")[0]
        head, claims, sig = token.split(".")
        self.assertEqual(json.loads(push.unb64(claims)), {"aud": "https://web.push.apple.com", "exp": 1000 + 12 * 3600, "sub": "mailto:a@example.com"})
        raw = push.unb64(sig)
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64(public))
        key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        self.assertTrue(header.endswith(f"k={public}"))

    def test_only_real_push_services(self):
        for bad in ("http://web.push.apple.com/x", "https://127.0.0.1/x", "https://evil.example.com/x", "https://apple.com.evil.io/x",
                    "https://web.push.apple.com:8443/x", "https://u@web.push.apple.com/x", "file:///c:/x", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.push.subscribe({"subscription": {**self.sub, "endpoint": bad}})
        self.assertTrue(push.endpoint_ok("https://fcm.googleapis.com/fcm/send/abc"))

    def test_first_look_is_quiet_then_news_is_sent_once(self):
        self.push.subscribe({"subscription": self.sub})
        self.assertEqual(self.push.check(), [])
        self.assertEqual(self.push.check(), [])
        self.assertEqual(self.sent, [])
        status = json.loads(json.dumps(STATUS))
        status["builds"]["builds"][1].update(state="done", publish="published", download="https://github.com/example/harbor/releases/tag/b2")
        self.view = portal.summary(status)
        self.view["live_blockers"] = [{"kind": "check", "key": "check:pr:9", "title": "PR #9: compile failed", "detail": "", "since": ""}]
        self.assertEqual(sorted(self.push.check()), ["build:b2:links", "stuck:check:pr:9"])
        self.assertEqual(sorted(n["title"] for n in self.notes()), ["Build 0.4.6 is ready to download", "Check failed"])
        self.assertEqual(self.push.check(), [])
        self.assertEqual(self.make().check(), [], "a restart announces nothing again")
        self.assertEqual(self.sent[0][2]["Content-Encoding"], "aes128gcm")
        self.assertTrue(self.sent[0][2]["Authorization"].startswith("vapid t="))

    def test_a_build_waits_for_its_download(self):
        self.push.subscribe({"subscription": self.sub})
        self.push.check()
        status = json.loads(json.dumps(STATUS))
        status["builds"]["builds"][1].update(state="done", publish="uploading")
        self.view = portal.summary(status)
        self.assertEqual(self.push.check(), [])
        status["builds"]["builds"][1].update(state="failed", publish="", reason="Compile failed")
        self.view = portal.summary(status)
        self.assertEqual(self.push.check(), ["build:b2:failed"])
        self.assertEqual(self.notes()[0]["body"], "Compile failed")

    def test_chosen_events_only(self):
        p = self.make({"contact": "mailto:a@example.com", "events": ["build"]})
        p.subscribe({"subscription": self.sub})
        p.check()
        self.view = dict(self.view, live_blockers=[{"kind": "check", "key": "check:pr:9", "title": "PR #9", "since": ""}])
        self.assertEqual(p.check(), [])

    def test_a_gone_phone_is_forgotten(self):
        self.push.subscribe({"subscription": self.sub})
        self.assertEqual(self.push.info()["phones"], 1)
        self.answer = 410
        with self.assertRaises(ValueError):
            self.push.test({"subscription": self.sub})
        self.assertEqual(self.push.info()["phones"], 0)

    def test_contact_is_required(self):
        p = self.make({"contact": ""})
        self.assertFalse(p.info()["available"])
        with self.assertRaises(ValueError):
            p.subscribe({"subscription": self.sub})

    def test_test_and_unsubscribe(self):
        self.push.subscribe({"subscription": self.sub})
        self.assertEqual(self.push.test({"subscription": self.sub}), {"sent": 1})
        self.assertEqual(self.notes()[0]["body"], "Notifications work on this phone.")
        self.push.unsubscribe({"subscription": self.sub})
        self.assertEqual(self.push.info()["phones"], 0)
        saved = json.loads((Path(self.tmp.name) / "push.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["subscriptions"], [])


if __name__ == "__main__":
    unittest.main()
