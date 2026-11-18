"""Agent presence (presence.py), its endpoint (machines.py POST /v1/agent) and the helper agents run (agent_status.py):
what is accepted, the limits, expiry, keeping across a restart, the status-issue relay, and one heartbeat end to end
over HTTP. The Agents card built from it is tested in test_devwatch.py."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import agent_status  # noqa: E402
import devwatch  # noqa: E402
import machines  # noqa: E402
import presence  # noqa: E402

T0 = 1_800_000_000.0
GOOD = {"agent": "claude", "session": "thread-4f2a", "work": "#2350", "step": "Importing hair", "state": "working"}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def comment(fields, at=T0, assoc="MEMBER", cid=1, extra="**Claude** (thread-4f2a) on #2350: Importing hair"):
    body = f"<!-- fletchery-agent {json.dumps(fields)} -->\n{extra}"
    return {"id": cid, "body": body, "author_association": assoc, "updated_at": iso(at),
            "html_url": f"https://github.com/Acme/Game/issues/9#issuecomment-{cid}"}


class Validate(unittest.TestCase):
    def test_accepts_the_documented_shapes(self):
        self.assertEqual(presence.validate(GOOD)["agent"], "claude")
        for work in ("", "#1", "Acme/Game#123", "a-b/c.d_e#9"):
            presence.validate({**GOOD, "work": work})
        self.assertEqual(presence.validate({**GOOD, "agent": "Codex"})["agent"], "codex")
        self.assertEqual(presence.validate({**GOOD, "step": "two\nlines\t here"})["step"], "two lines here")
        self.assertEqual(presence.validate({"agent": "codex", "session": "s"})["state"], "working")

    def test_refuses_anything_else(self):
        bad = [{**GOOD, "agent": "copilot"}, {**GOOD, "agent": 1}, {**GOOD, "session": ""}, {**GOOD, "session": "a b"},
               {**GOOD, "session": "x" * 81}, {**GOOD, "session": "../../etc"}, {**GOOD, "work": "123"},
               {**GOOD, "work": "#12a"}, {**GOOD, "work": "https://evil.example/#1"}, {**GOOD, "work": "a/b/c#1"},
               {**GOOD, "step": "x" * 201}, {**GOOD, "step": ["x"]}, {**GOOD, "state": "sleeping"},
               {**GOOD, "branch": "a..b"}, {**GOOD, "branch": "a b"}, "not an object", None]
        for b in bad:
            with self.assertRaises(ValueError, msg=repr(b)[:80]):
                presence.validate(b)

    def test_folder_paths_are_scrubbed_from_the_step(self):
        step = presence.validate({**GOOD, "step": r"Building E:\lifeshare-scratch\wt-12\LifeShare.uproject"},
                                 clean=devwatch._scrub)["step"]
        self.assertNotIn("lifeshare-scratch", step)
        self.assertIn("[a folder]", step)


class Store(unittest.TestCase):
    def test_one_row_per_session_updated_in_place(self):
        clock = Clock()
        p = presence.Presence(clock=clock)
        p.beat("NODE-01", GOOD)
        clock.t += 60
        p.beat("NODE-01", {**GOOD, "step": "Testing"})
        p.beat("NODE-01", {**GOOD, "session": "other"})
        rows = p.current()
        self.assertEqual(len(rows), 2)
        mine = next(r for r in rows if r["session"] == "thread-4f2a")
        self.assertEqual((mine["step"], mine["at"], mine["machine"]), ("Testing", T0 + 60, "NODE-01"))

    def test_the_time_is_when_fletchery_received_it(self):
        p = presence.Presence(clock=Clock())
        p.beat("NODE-01", {**GOOD, "at": "2099-01-01T00:00:00Z"})    # a claimed time is ignored
        self.assertEqual(p.current()[0]["at"], T0)

    def test_rate_limit_per_machine(self):
        clock = Clock()
        p = presence.Presence(clock=clock)
        for i in range(presence.RATE_PER_MINUTE):
            p.beat("NODE-01", {**GOOD, "session": f"s{i % 5}"})
        with self.assertRaises(presence.TooMany):
            p.beat("NODE-01", GOOD)
        p.beat("PC", GOOD)                       # another machine has its own allowance
        clock.t += 61
        p.beat("NODE-01", GOOD)                  # and a minute later this one may again

    def test_session_cap_forgets_idle_ones_first_and_refuses_when_all_work(self):
        clock = Clock()
        p = presence.Presence(clock=clock)
        for i in range(presence.MAX_SESSIONS):
            if i % 50 == 0 and i:
                clock.t += 61
            p.beat("NODE-01", {**GOOD, "session": f"s{i}", "state": "done" if i == 7 else "working"})
            p._beats.clear()                     # this test is about sessions, not the rate
        p.beat("NODE-01", {**GOOD, "session": "new"})                # room made by forgetting the finished s7
        self.assertNotIn("s7", {r["session"] for r in p.current()})
        with self.assertRaises(presence.TooMany):                    # all 50 working now: refused
            p.beat("NODE-01", {**GOOD, "session": "one-more"})
        clock.t += 16 * 60                                           # quiet past the window: the oldest gives way
        p._beats.clear()
        p.beat("NODE-01", {**GOOD, "session": "one-more"})
        self.assertEqual(len(p.current()), presence.MAX_SESSIONS)

    def test_records_expire_after_a_day(self):
        clock = Clock()
        p = presence.Presence(clock=clock)
        p.beat("NODE-01", GOOD)
        clock.t += presence.KEEP_HOURS * 3600 - 1
        self.assertEqual(len(p.current()), 1)
        clock.t += 2
        self.assertEqual(p.current(), [])

    def test_kept_across_a_restart_and_written_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "agent-presence.json"
            clock = Clock()
            p = presence.Presence(path, clock=clock)
            p.beat("NODE-01", GOOD)
            self.assertEqual(sorted(os.listdir(d)), ["agent-presence.json"])     # no temp file left behind
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("key", saved.lower())
            again = presence.Presence(path, clock=clock)
            self.assertEqual([(r["session"], r["at"]) for r in again.current()], [("thread-4f2a", T0)])
            # A hand-edited or damaged file never brings in what the endpoint would refuse.
            data = json.loads(saved)
            data["records"].append({**GOOD, "session": "bad session", "source": "machine:X", "at": T0})
            data["records"].append({**GOOD, "session": "old", "source": "machine:X", "at": T0 - 2 * 86400})
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(len(presence.Presence(path, clock=clock).current()), 1)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(presence.Presence(path, clock=clock).current(), [])


class Relay(unittest.TestCase):
    def test_reads_members_markers_with_githubs_clock(self):
        p = presence.Presence(clock=Clock(T0 + 30))
        took = p.relay("Acme/Game", [
            comment(GOOD, at=T0 - 120),
            comment({**GOOD, "session": "stranger"}, assoc="NONE", cid=2),            # not a member: ignored
            comment({**GOOD, "session": "contrib"}, assoc="CONTRIBUTOR", cid=3),      # not trusted either
            {"id": 4, "body": "Thanks!", "author_association": "OWNER", "updated_at": iso(T0)},   # no marker
            comment({**GOOD, "session": "bad", "agent": "robot"}, cid=5),             # bad field: skipped
            comment({**GOOD, "session": "later"}, at=T0 + 3600, cid=6),              # ahead of our clock: clamped
        ])
        self.assertEqual(took, 2)
        rows = {r["session"]: r for r in p.current()}
        self.assertEqual(set(rows), {"thread-4f2a", "later"})
        self.assertEqual(rows["thread-4f2a"]["at"], T0 - 120)
        self.assertEqual(rows["later"]["at"], T0 + 30)
        self.assertEqual((rows["thread-4f2a"]["machine"], rows["thread-4f2a"]["repo"]), ("", "Acme/Game"))
        self.assertTrue(rows["thread-4f2a"]["url"].startswith("https://github.com/"))

    def test_an_older_answer_never_rolls_a_session_back(self):
        p = presence.Presence(clock=Clock(T0))
        p.relay("Acme/Game", [comment({**GOOD, "state": "done"}, at=T0 - 10)])
        p.relay("Acme/Game", [comment(GOOD, at=T0 - 60)])
        self.assertEqual(p.current()[0]["state"], "done")

    def test_marker_must_open_the_comment_and_bounds_hold(self):
        p = presence.Presence(clock=Clock())
        quoted = comment(GOOD)
        quoted["body"] = "> quoting someone\n" + quoted["body"]
        many = [comment({**GOOD, "session": f"s{i}"}, cid=i) for i in range(presence.MAX_COMMENTS + 50)]
        self.assertEqual(p.relay("Acme/Game", [quoted]), 0)
        self.assertEqual(p.relay("Acme/Game", "not a list"), 0)
        p.relay("Acme/Game", many)
        self.assertLessEqual(len(p.current()), presence.MAX_SESSIONS)


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.m = machines.Machines(self.dir.name, "Acme/Game")
        self.key = self.m.add_machine("NODE-01")
        self.api = machines.WorkerApi(self.m, "127.0.0.1:0")

    def tearDown(self):
        self.dir.cleanup()

    def test_signed_checked_and_limited(self):
        api, key = self.api, self.key
        self.assertEqual(api.handle("/v1/agent", "NODE-01", key, "1.2.3.4", GOOD)[0], 503)   # Live tab off
        api.reports = devwatch.DevWatch({}, github=None, token_fn=lambda: "")
        self.assertEqual(api.handle("/v1/agent", "NODE-01", "wrong", "1.2.3.4", GOOD)[0], 401)
        self.assertEqual(api.handle("/v1/agent", "NOBODY", key, "1.2.3.4", GOOD)[0], 401)
        self.assertEqual(api.handle("/v1/agent", "NODE-01", key, "1.2.3.4", GOOD), (200, {"ok": True, "state": "working"}))
        code, body = api.handle("/v1/agent", "NODE-01", key, "1.2.3.4", {**GOOD, "work": "rm -rf"})
        self.assertEqual((code, body["error"].split(":")[0]), (400, "work"))
        for _ in range(presence.RATE_PER_MINUTE):
            code, _ = api.handle("/v1/agent", "NODE-01", key, "1.2.3.4", GOOD)
        self.assertEqual(code, 429)
        self.m.revoke("NODE-01")
        self.assertEqual(api.handle("/v1/agent", "NODE-01", key, "1.2.3.4", GOOD)[0], 401)

    def test_bad_keys_lock_the_address_out(self):
        self.api.reports = devwatch.DevWatch({}, github=None, token_fn=lambda: "")
        for _ in range(10):
            self.api.handle("/v1/agent", "NODE-01", "guess", "5.6.7.8", GOOD)
        self.assertEqual(self.api.handle("/v1/agent", "NODE-01", self.key, "5.6.7.8", GOOD)[0], 429)
        self.assertEqual(self.api.handle("/v1/agent", "NODE-01", self.key, "1.2.3.4", GOOD)[0], 200)

    def test_helper_end_to_end_over_http(self):
        live = devwatch.DevWatch({"names": {"NODE-01": "Test node"}}, github=None, token_fn=lambda: "")
        self.api.reports = live
        self.api.start()
        try:
            key_file = Path(self.dir.name) / "node_key"
            key_file.write_text(self.key + "\n", encoding="utf-8")
            node = Path(self.dir.name) / "node.json"
            node.write_text(json.dumps({"coordinator": f"http://127.0.0.1:{self.api.server.server_address[1]}",
                                        "machine": "NODE-01", "keyFile": str(key_file)}), encoding="utf-8")
            rep = Path(self.dir.name) / "reporter.json"
            rep.write_text(json.dumps({"nodeConfig": str(node)}), encoding="utf-8")
            with mock.patch.object(agent_status, "git", lambda *a: ""):
                code = agent_status.main(["--agent", "codex", "--session", "s1", "--work", "Acme/Game#7",
                                          "--step", "Fixing the tests", "--config", str(rep)])
                self.assertEqual(code, 0)
                err = io.StringIO()
                with redirect_stderr(err):
                    code = agent_status.main(["--agent", "codex", "--session", "bad session", "--config", str(rep)])
                self.assertEqual(code, 2)
                self.assertIn("session", err.getvalue())
                self.assertNotIn(self.key, err.getvalue())
            row = live.presence.current()[0]
            self.assertEqual((row["agent"], row["work"], row["step"], row["machine"]), ("codex", "Acme/Game#7", "Fixing the tests", "NODE-01"))
        finally:
            self.api.stop()


class Helper(unittest.TestCase):
    def test_fills_in_the_repository_branch_and_session(self):
        answers = {("config", "--get", "remote.origin.url"): "git@github.com:Acme/Game.git",
                   ("rev-parse", "--abbrev-ref", "HEAD"): "claude/bus-lines",
                   ("rev-parse", "--show-toplevel"): "/work/wt-12"}
        with mock.patch.object(agent_status, "git", lambda *a: answers.get(a, "")):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(agent_status.main(["--agent", "claude", "--work", "#12", "--step", "a\nb", "--dry-run"]), 0)
            body = json.loads(buf.getvalue())
            again = agent_status.default_session()
        self.assertEqual((body["work"], body["branch"], body["step"]), ("Acme/Game#12", "claude/bus-lines", "a b"))
        self.assertTrue(body["session"].startswith("wt-"))
        self.assertEqual(body["session"], again)                 # the same worktree: the same row
        presence.validate(body)                                  # what it sends, the endpoint takes

    def test_https_remote_and_detached_head(self):
        answers = {("config", "--get", "remote.origin.url"): "https://github.com/Acme/Game", ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD"}
        with mock.patch.object(agent_status, "git", lambda *a: answers.get(a, "")):
            a = argparse_ns(work="#3")
            body = agent_status.heartbeat(a)
        self.assertEqual((body["work"], body["branch"]), ("Acme/Game#3", ""))

    def test_no_configuration_says_how_to_fix_it(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"FLETCHERY_AGENT_CONFIG": ""}), \
                mock.patch.object(agent_status, "REMEMBERED", Path(d) / "agent.json"), \
                mock.patch.object(agent_status, "git", lambda *a: ""):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(agent_status.main(["--agent", "claude", "--session", "s"]), 2)
            self.assertIn("setup", err.getvalue())
            cfg = Path(d) / "reporter.json"
            cfg.write_text(json.dumps({"coordinator": "http://127.0.0.1:9", "machine": "PC", "keyFile": str(Path(d) / "k")}))
            (Path(d) / "k").write_text("secret-key")
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(agent_status.main(["setup", "--config", str(cfg)]), 0)
            self.assertEqual(agent_status.find_config(), cfg.resolve())
            err = io.StringIO()
            with redirect_stderr(err):     # nothing listens there: exit 1, carry on, and the key is never printed
                self.assertEqual(agent_status.main(["--agent", "claude", "--session", "s"]), 1)
            self.assertNotIn("secret-key", err.getvalue())


def argparse_ns(**kw):
    import argparse
    base = {"agent": "claude", "session": "s", "work": "", "branch": None, "step": "", "state": "working"}
    return argparse.Namespace(**{**base, **kw})


if __name__ == "__main__":
    unittest.main()
