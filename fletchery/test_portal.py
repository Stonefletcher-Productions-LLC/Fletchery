"""The phone portal (portal.py) and its notifications (push.py): what it shows, who may ask, and that it can change
nothing."""
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
    "project": "Harbor", "repo": "example/harbor", "repo_url": "https://github.com/example/harbor",
    "last_pass": "2026-11-16T04:00:00+00:00", "next_pass_at": "2026-11-16T04:10:00+00:00", "passing": False,
    "max_parallel": 4, "spent_today": 12.5, "daily_budget": 60, "auth": "subscription", "held": True,
    "signed_out": "Run claude, then /login.", "limit_until": "", "restarting": "", "sync_error": r"gh failed in E:\scratch\foreman",
    "page_id": "secret-page", "agents": {"keys": [{"name": "ANTHROPIC_API_KEY", "tail": "abcd"}], "list": [{"name": "opus"}]},
    "storage": {"report": {"root": r"E:\lifeshare-scratch"}}, "worker_ranks": {"scores": {"opus": 91}},
    "tasks": [
        {"number": 1, "title": "Blocked one", "status": "blocked", "why": "needs a decision", "url": "https://github.com/example/harbor/issues/1"},
        {"number": 2, "title": "Dead end", "status": "queued", "needs_person": True, "why": "Needs you: pick A or B", "url": "javascript:alert(1)"},
        {"number": 3, "title": "Writing", "status": "running", "agent": "opus", "branch": "task/3"},
        {"number": 4, "title": "Queued", "status": "queued", "why": "waits for #3", "depends": [3]},
        {"number": 5, "title": "Has PR", "status": "pr", "pr": 50, "pr_url": "https://github.com/example/harbor/pull/50"},
    ],
    "live": [{"task": 3, "title": "Writing", "agent": "opus", "stage": "Writing", "since": "2026-11-16T03:50:00+00:00",
              "last": "Edit src/app.py", "last_at": "2026-11-16T03:59:00+00:00", "emergency": False}],
    "merge": [{"pr": 50, "task": 5, "title": "Has PR", "pr_url": "https://github.com/example/harbor/pull/50", "ready": True, "flags": ["strict NavCheck"]}],
    "merge_view": {"round": 16, "state": "running", "active": True, "headline": "Game tests running", "prs": [50], "pc_text": "the PC is free",
                   "steps": [{"name": "Compile", "state": "done", "duration_seconds": 372, "files": [{"url": "/api/merge/log?round=16"}]},
                             {"name": "Game tests", "state": "running", "message": "Verify the rebuilt game."}],
                   "previous": {"round": "15", "result": "landed 4 PRs", "ended": "2026-11-16T01:00:00+00:00"}},
    "merge_auto": {"on": True, "held": False, "running": {"agent": "opus"}, "backlog": {"count": 3}, "why": ""},
    "builds": {"out_dir": r"E:\lifeshare-scratch\builds", "builds": [
        {"id": "b1", "version": "0.4.5", "state": "done", "ref": "Test-Staging", "sha": "326ed7bfd3aa", "finished": "2026-11-16T02:00:00+00:00",
         "route": "pc", "output": r"E:\builds\LifeShare-0.4.5", "publish": "published",
         "download": "https://github.com/example/harbor/releases/tag/build-0.4.5",
         "drive": "https://drive.google.com/file/d/one https://drive.google.com/file/d/two", "reason": r"Packaged to E:\builds\LifeShare-0.4.5"},
        {"id": "b2", "version": "0.4.6", "state": "scheduled", "at": "2026-11-17T02:00:00+00:00", "ref": "main"}]},
    "machines": {"listen": "100.64.0.10:8771", "machines": [
        {"name": "NODE-01", "online": True, "last_seen": "2026-11-16T04:00:00+00:00", "job": {"recipe": "package", "ref": "main"}, "paused_work": []},
        {"name": "old", "revoked": True}]},
    "log": [{"at": "2026-11-16T04:00:00+00:00", "line": r"round 16 uses C:\Users\you\wt\round-16"}],
}


class Summary(unittest.TestCase):
    def setUp(self):
        self.s = portal.summary(STATUS)

    def test_groups_tasks_like_the_app(self):
        self.assertEqual([t["number"] for t in self.s["needs"]], [1, 2])
        self.assertEqual([t["number"] for t in self.s["running"]], [3])
        self.assertEqual([t["number"] for t in self.s["queued"]], [4])
        self.assertEqual([t["number"] for t in self.s["in_pr"]], [5])
        self.assertEqual(self.s["workers"][0]["stage"], "Writing")

    def test_merge_and_builds(self):
        m = self.s["merge"]
        self.assertTrue(m["active"])
        self.assertEqual([x["name"] for x in m["steps"]], ["Compile", "Game tests"])
        self.assertEqual(m["previous"]["round"], "15")
        self.assertEqual(m["waiting"][0]["pr"], 50)
        done = next(b for b in self.s["builds"] if b["version"] == "0.4.5")
        self.assertEqual([l["label"] for l in done["links"]], ["GitHub download", "Google Drive", "Google Drive part 2"])
        self.assertEqual(done["machine"], "this PC")
        self.assertEqual([m["name"] for m in self.s["machines"]], ["NODE-01"])

    def test_pauses(self):
        self.assertEqual([p["kind"] for p in self.s["pauses"]], ["signed_out", "held", "sync"])

    def test_nothing_private_leaves_the_pc(self):
        text = json.dumps(self.s)
        for secret in ("ANTHROPIC", "abcd", "secret-page", "lifeshare-scratch", "E:\\\\", "C:\\\\", "100.64.0.10",
                       "javascript:", "/api/merge/log", "out_dir", "worker_ranks"):
            self.assertNotIn(secret, text)
        self.assertIn("[a folder on the PC]", text)

    def test_empty_status(self):
        s = portal.summary({})
        self.assertEqual((s["needs"], s["builds"], s["merge"]["active"]), ([], [], False))


class Settings(unittest.TestCase):
    def test_off_and_defaults(self):
        self.assertIsNone(portal.settings(None))
        self.assertIsNone(portal.settings({"enabled": False, "port": 8772}))
        self.assertEqual(portal.settings({"hosts": ["Your-PC.Your-Tailnet.ts.net."], "users": ["You@Example.com"]}),
                         {"port": 8772, "hosts": [HOSTNAME], "users": [USER], "push": None, "live": {}})
        self.assertIsNone(portal.settings({"live": False})["live"])

    def test_bad_blocks(self):
        for bad in ("yes", {"port": 80}, {"port": "x"}, {"hosts": "a.ts.net"}, {"hosts": ["https://a.ts.net"]},
                    {"hosts": ["a.ts.net:8443"]}, {"push": "on"}):
            with self.assertRaises(ValueError):
                portal.settings(bad)

    def test_profile_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.json"
            p.write_text(json.dumps({"name": "x", "workers": {"portal": {"port": 9001}}}), encoding="utf-8")
            self.assertEqual(Config.load(p).portal, {"port": 9001})


class Served(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        conf = portal.settings({"port": 8772, "hosts": [HOSTNAME], "users": [USER], "push": {"contact": "mailto:a@example.com"}})
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            conf["port"] = s.getsockname()[1]
        self.p = portal.Portal(lambda: STATUS, conf, self.tmp.name)
        self.p.push.start = lambda: None
        self.p.live.start = lambda: None      # never reads the real GitHub in tests (test_devwatch.py fakes it)
        self.p.start()
        self.port = conf["port"]

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def ask(self, path, headers=None, data=None, method=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {}, data=data, method=method)
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

    def test_it_can_change_nothing(self):
        for path in ("/api/pause", "/api/task", "/api/settings/save", "/api/keys/set", "/api/merge/now", "/api/restart", "/api/summary"):
            origin = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
            self.assertEqual(self.ask(path, origin, b"{}", "POST")[0], 404, path)
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.ask("/api/summary", method=method)[0], 501)
        for path in ("/api/status", "/api/jobs/log?id=1", "/api/merge/log", "/../fletchery/config.py", "/portal.js/../../index.html"):
            self.assertEqual(self.ask(path)[0], 404, path)

    def test_posts_need_the_portals_own_page(self):
        body = json.dumps({"endpoint": "https://web.push.apple.com/x"}).encode()
        self.assertEqual(self.ask("/api/push/unsubscribe", {"Content-Type": "application/json"}, body, "POST")[0], 403)
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin="https://evil.example.com"), body, "POST")[0], 403)
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin=f"https://{HOSTNAME}", **{"Sec-Fetch-Site": "cross-site"}), body, "POST")[0], 403)
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin=f"https://{HOSTNAME}"), body, "POST")[0], 200)
        big = b"{" + b" " * portal.MAX_BODY + b"}"
        self.assertEqual(self.ask("/api/push/unsubscribe", self.remote(Origin=f"https://{HOSTNAME}"), big, "POST")[0], 413)


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
        import base64
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
        status["tasks"].append({"number": 9, "title": "New question", "status": "blocked"})
        status["merge_view"]["previous"] = {"round": "16", "result": "landed 2 PRs"}
        status["builds"]["builds"][1].update(state="done", publish="published", download="https://github.com/example/harbor/releases/tag/b2")
        status["signed_out"] = ""
        self.view = portal.summary(status)
        self.assertEqual(sorted(self.push.check()), ["build:b2:links", "merge:16", "needs:9"])
        self.assertEqual(sorted(n["title"] for n in self.notes()), ["Build 0.4.6 is ready to download", "Merge round 16 finished", "Needs you"])
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
        status = json.loads(json.dumps(STATUS))
        status["tasks"].append({"number": 9, "title": "New question", "status": "blocked"})
        self.view = portal.summary(status)
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
