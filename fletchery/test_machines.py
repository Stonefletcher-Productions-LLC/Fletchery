"""Machines: the node registry, the job queue, the worker API and the node worker, end to end on this machine."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))
import machines  # noqa: E402
import node_worker  # noqa: E402
from machines import Machines, WorkerApi, check_listen  # noqa: E402

SHA = "a" * 40
NODE = ["os:windows", "ram:24", "role:tests", "role:cook", "unreal:5.8.4"]
BUILD = {"version": "0.4.5"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.m = Machines(self.tmp.name, "owner/repo")

    def tearDown(self):
        self.tmp.cleanup()

    def node(self, name="NODE-01", labels=NODE, ram=24):
        key = self.m.add_machine(name)
        self.m.seen(name, "100.64.0.20", labels, ram, "1", {})
        return key


class RegistryTests(Base):
    def test_key_is_shown_once_and_only_its_hash_is_kept(self):
        key = self.node()
        self.assertTrue(self.m.authenticate("NODE-01", key))
        self.assertFalse(self.m.authenticate("NODE-01", key + "x"))
        self.assertFalse(self.m.authenticate("other", key))
        self.assertNotIn(key.encode(), Path(self.m.path).read_bytes())

    def test_revoke_stops_the_key_and_requeues_its_job(self):
        key = self.node()
        job = self.m.enqueue("package", SHA, BUILD)
        self.assertEqual(self.m.claim("NODE-01")["id"], job)
        self.assertTrue(self.m.revoke("NODE-01"))
        self.assertFalse(self.m.authenticate("NODE-01", key))
        self.assertEqual(self.m.view()["jobs"][0]["state"], "queued")
        new = self.m.add_machine("NODE-01")  # re-adding gives a new key; the old one stays dead
        self.assertTrue(self.m.authenticate("NODE-01", new))
        self.assertFalse(self.m.authenticate("NODE-01", key))

    def test_a_build_goes_first_on_a_node_held_by_a_local_run(self):
        # 2026-11-20, job 756: GitHub PR checks held SF-NODE-01's lease back to back and only round jobs (retired)
        # could be claimed meanwhile, so the scheduled build never started.
        self.node()
        other = self.m.enqueue("checkout-only", SHA, {}, requested_by="PR check")
        build = self.m.enqueue("package", SHA, BUILD, requested_by="build 0.4.5")
        self.assertTrue(self.m.round_waiting("NODE-01"))   # the node keeps LaneGuard's marker: local runs wait
        # Not handed out while the local run holds the node (it would run beside it); claimed once the lease is free.
        self.assertIsNone(self.m.claim("NODE-01", rounds_only=True))
        got = self.m.claim("NODE-01")
        self.assertEqual((got["id"], got["round"]), (build, True))   # it holds the lease for its whole length
        self.assertFalse(self.m.round_waiting("NODE-01"))   # a PR check's job never holds local runs off
        self.assertEqual(self.m.job(other)["state"], "queued")

    def test_paused_builds_are_not_waited_for(self):
        self.node()
        self.m.enqueue("package", SHA, BUILD, requested_by="build 0.4.5")
        self.m.set_paused("NODE-01", "builds", True)
        self.assertFalse(self.m.round_waiting("NODE-01"))
        self.assertIsNone(self.m.claim("NODE-01", rounds_only=True))

    def test_bad_names_are_refused(self):
        for name in ("", "../x", "a b", "x" * 80):
            with self.assertRaises(ValueError):
                self.m.add_machine(name)

    def test_listen_must_be_one_private_address(self):
        self.assertEqual(check_listen("100.64.0.10:8771"), ("100.64.0.10", 8771))
        self.assertEqual(check_listen("127.0.0.1:8771"), ("127.0.0.1", 8771))
        for bad in ("0.0.0.0:8771", "[::]:8771", ":8771", "8.8.8.8:8771", "example.com:8771", "100.64.0.10"):
            with self.assertRaises(ValueError, msg=bad):
                check_listen(bad)


class QueueTests(Base):
    def test_jobs_go_to_a_node_with_the_labels_and_ram(self):
        self.node("small", ["os:windows", "role:cook"], 8)
        self.node("tests", ["os:windows", "role:tests"], 64)
        self.node("NODE-01")
        job = self.m.enqueue("package", SHA, {"version": "0.4.5", "tests": "full"})
        self.assertIsNone(self.m.claim("small"))   # too little RAM
        self.assertIsNone(self.m.claim("tests"))   # no role:cook
        got = self.m.claim("NODE-01")
        self.assertEqual((got["id"], got["params"]), (job, {"version": "0.4.5", "tests": "full"}))

    def test_extra_labels_narrow_the_choice(self):
        self.node()
        self.m.enqueue("package", SHA, BUILD, labels=["unreal:5.8.3"])
        self.assertIsNone(self.m.claim("NODE-01"))
        self.m.enqueue("package", SHA, BUILD, labels=["unreal:5.8.4"])
        self.assertEqual(self.m.claim("NODE-01")["id"], 2)

    def test_a_node_runs_one_job_at_a_time_oldest_first(self):
        self.node()
        a, b = self.m.enqueue("package", SHA, BUILD), self.m.enqueue("checkout-only", SHA)
        self.assertEqual(self.m.claim("NODE-01")["id"], a)
        self.assertIsNone(self.m.claim("NODE-01"))
        self.m.finish("NODE-01", a, 0, "ok")
        self.assertEqual(self.m.claim("NODE-01")["id"], b)

    def test_parameters_can_choose_a_version_never_a_command(self):
        for version in ("0.4; Remove-Item", "a b", "$(x)", "1.2", "../x"):
            with self.assertRaises(ValueError, msg=version):
                self.m.enqueue("package", SHA, {"version": version})
        for retired in ("powershell", "node-checks", "pr-check", "nightly-full"):
            with self.assertRaises(ValueError, msg=retired):
                self.m.enqueue(retired, SHA)
        with self.assertRaises(ValueError):
            self.m.enqueue("package", "main", BUILD)
        job = self.m.enqueue("checkout-only", SHA, {"only": "X", "cmd": "calc"})
        self.assertEqual(self.m.view()["jobs"][0]["params"], {})
        self.assertEqual(job, 1)

    def test_only_the_node_running_a_job_can_report_on_it(self):
        self.node(); self.node("other")
        job = self.m.enqueue("package", SHA, BUILD)
        self.m.claim("NODE-01")
        for call in (lambda: self.m.heartbeat("other", job), lambda: self.m.append_log("other", job, "x"),
                     lambda: self.m.finish("other", job, 0)):
            with self.assertRaises(PermissionError):
                call()

    def test_cancel_reaches_the_node_at_its_heartbeat(self):
        self.node()
        queued = self.m.enqueue("package", SHA, BUILD)
        running = self.m.enqueue("package", SHA, BUILD)
        self.assertEqual(self.m.cancel(queued), "cancelled")
        self.assertEqual(self.m.claim("NODE-01")["id"], running)
        self.assertFalse(self.m.heartbeat("NODE-01", running)["cancel"])
        self.assertEqual(self.m.cancel(running), "stopping")
        self.assertTrue(self.m.heartbeat("NODE-01", running)["cancel"])
        self.m.finish("NODE-01", running, 130, "Cancelled from Fletchery")
        self.assertEqual(self.m.view()["jobs"][0]["state"], "cancelled")

    def test_a_silent_node_loses_its_job_once_then_it_fails(self):
        self.node()
        job = self.m.enqueue("package", SHA, BUILD)
        self.m.claim("NODE-01")
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.assertEqual(self.m.reap(later), [job])
        self.assertEqual(self.m.view()["jobs"][0]["state"], "queued")
        self.m.claim("NODE-01")
        self.m.reap(later)
        row = self.m.view()["jobs"][0]
        self.assertEqual((row["state"], row["attempts"]), ("lost", 2))

    def test_a_restarted_coordinator_gives_nodes_time_to_call_in(self):
        # Audit 2026-11-18: after the PC's backend was down a few minutes, every running job was queued again and ran
        # twice. The coordinator's own start counts as a heartbeat.
        self.node()
        job = self.m.enqueue("package", SHA, BUILD)
        self.m.claim("NODE-01")
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        started = (later - timedelta(seconds=30)).isoformat()
        self.assertEqual(self.m.reap(later, since=started), [])
        self.assertEqual(self.m.job(job)["state"], "running")

    def test_a_job_past_its_limit_is_stopped_then_given_up(self):
        self.node()
        job = self.m.enqueue("checkout-only", SHA)   # 30-minute limit
        self.m.claim("NODE-01")
        t = datetime.now(timezone.utc)
        self.m.heartbeat("NODE-01", job)
        self.assertEqual(self.m.reap(t + timedelta(minutes=41), since=(t + timedelta(minutes=41)).isoformat()), [])
        self.assertTrue(self.m.heartbeat("NODE-01", job)["cancel"], "asked to stop after its limit plus grace")
        self.assertEqual(self.m.reap(t + timedelta(minutes=61), since=(t + timedelta(minutes=61)).isoformat()), [job])
        row = self.m.job(job)
        self.assertEqual(row["state"], "lost")
        self.assertIn("twice its 30-minute limit", row["summary"])

    def test_logs_append_and_are_capped(self):
        self.node()
        job = self.m.enqueue("package", SHA, BUILD)
        self.m.claim("NODE-01")
        self.m.append_log("NODE-01", job, "line 1\n")
        self.m.append_log("NODE-01", job, "line 2\n")
        self.assertEqual(self.m.log_text(job), "line 1\nline 2\n")
        old = machines.LOG_MAX_BYTES
        machines.LOG_MAX_BYTES = 20
        try:
            self.m.append_log("NODE-01", job, "x" * 100)
            self.m.append_log("NODE-01", job, "more")
        finally:
            machines.LOG_MAX_BYTES = old
        self.assertIn("[log cut at 50 MB]", self.m.log_text(job))
        self.assertNotIn("more", self.m.log_text(job))


class FakeGh:
    def __init__(self):
        self.posts = []

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        if method == "POST":
            self.posts.append((path, body))
            return {}
        return {"sha": "b" * 40}


class CoordinatorTests(unittest.TestCase):
    def test_builds_copied_back_land_in_the_builds_out_dir(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            cfg = type("C", (), {"state_dir": d, "repo": "o/r", "machines": {}, "builds": {"outDir": str(Path(d) / "out")}})()
            c = machines.Coordinator(cfg, FakeGh(), log=lambda *_: None)
            self.assertEqual(Path(c.machines.upload_dir()), Path(d) / "out")

    def test_resolve_refuses_odd_refs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            cfg = type("C", (), {"state_dir": d, "repo": "o/r", "machines": {}})()
            c = machines.Coordinator(cfg, FakeGh(), log=lambda *_: None)
            self.assertEqual(c.resolve("A" * 40), "a" * 40)
            self.assertEqual(c.resolve("main"), "b" * 40)
            for bad in ("", "../x", "a b", "x?y"):
                with self.assertRaises(ValueError):
                    c.resolve(bad)


    def test_worker_api_bind_is_retried_until_the_address_exists(self):
        # After a reboot the Tailscale address is not up yet: WinError 10049. The API must come up once it is.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            cfg = type("C", (), {"state_dir": d, "repo": "o/r", "machines": {"listen": "127.0.0.1:0"}})()
            c = machines.Coordinator(cfg, FakeGh(), log=lambda *_: None)
            calls = []

            def flaky():
                calls.append(1)
                if len(calls) == 1:
                    raise OSError(10049, "The requested address is not valid in its context")
            c.api.start = flaky
            self.assertFalse(c._start_api())
            self.assertIn("Worker API not started", c.error)
            self.assertIn("retrying", c.error)
            self.assertIsNotNone(c.api)
            self.assertTrue(c._start_api())
            self.assertEqual(c.error, "")


class ApiTests(Base):
    def test_unknown_keys_are_refused_and_throttled(self):
        api = WorkerApi(self.m, "127.0.0.1:0")
        key = self.node()
        self.assertEqual(api.handle("/v1/claim", "NODE-01", "nope", "1.2.3.4", {})[0], 401)
        for _ in range(10):
            api.handle("/v1/claim", "NODE-01", "nope", "1.2.3.4", {})
        self.assertEqual(api.handle("/v1/claim", "NODE-01", key, "1.2.3.4", {})[0], 429)
        self.assertEqual(api.handle("/v1/claim", "NODE-01", key, "5.6.7.8", {})[0], 200)

    def test_a_paused_node_checks_in_but_takes_nothing(self):
        api = WorkerApi(self.m, "127.0.0.1:0")
        key = self.node()
        self.m.enqueue("package", SHA, BUILD)
        code, body = api.handle("/v1/claim", "NODE-01", key, "ip", {"paused": True, "stats": {"paused": True}})
        self.assertEqual((code, body["job"]), (200, None))
        self.assertTrue(self.m.view()["machines"][0]["stats"]["paused"])


class WorkerRequestTests(Base):
    def setUp(self):
        super().setUp()
        self.key = self.node()
        self.api = WorkerApi(self.m, "127.0.0.1:0")
        self.api.reports = Mock()
        self.api.reports.report.return_value = {"ok": True}
        self.api.start()
        self.addCleanup(self.api.stop)
        self.handler = object.__new__(self.api.server.RequestHandlerClass)
        self.handler.headers = Message()
        self.handler.headers["X-Fletchery-Machine"] = "NODE-01"
        self.handler.headers["Authorization"] = "Bearer " + self.key
        self.handler.client_address = ("127.0.0.1", 31000)
        self.handler.path = "/v1/report"
        self.handler._json = Mock()

    def test_report_at_the_portal_limit_is_accepted(self):
        import portal
        self.assertEqual(self.handler.timeout, 30)
        self.assertEqual(machines.MAX_BODY, portal.MAX_BODY)
        body = b"{}" + b" " * (portal.MAX_BODY - 2)
        self.handler.headers["Content-Length"] = str(len(body))
        self.handler.rfile = io.BytesIO(body)
        self.handler.do_POST()
        self.handler._json.assert_called_once_with(200, {"ok": True})
        self.api.reports.report.assert_called_once_with("NODE-01", {}, "127.0.0.1")

    def test_existing_bulk_job_routes_keep_their_separate_limits(self):
        self.api._dispatch = Mock(return_value=(200, {"ok": True}))
        body = b"{}" + b" " * machines.MAX_BODY
        for path, limit in (("/v1/log", 1024 * 1024), ("/v1/result", 1024 * 1024), ("/v1/upload", 8 * 1024 * 1024)):
            with self.subTest(path=path):
                self.handler.path = path
                del self.handler.headers["Content-Length"]
                self.handler.headers["Content-Length"] = str(len(body))
                self.handler.rfile = io.BytesIO(body)
                self.handler.do_POST()
                self.assertEqual(self.handler._json.call_args.args[0], 200)
                self.handler.headers.replace_header("Content-Length", str(limit + 1))
                self.handler.rfile = Mock()
                self.handler.do_POST()
                self.assertEqual(self.handler._json.call_args.args[0], 413)
                self.handler.rfile.read.assert_not_called()

    def test_invalid_sizes_and_framing_are_rejected_before_a_read(self):
        for lengths, encoding, status in [(["-1"], None, 400), (["invalid"], None, 400),
                                          (["2", "2"], None, 400), (["2"], "chunked", 400),
                                          ([str(machines.MAX_BODY + 1)], None, 413)]:
            with self.subTest(lengths=lengths, encoding=encoding):
                del self.handler.headers["Content-Length"], self.handler.headers["Transfer-Encoding"]
                for value in lengths:
                    self.handler.headers["Content-Length"] = value
                if encoding:
                    self.handler.headers["Transfer-Encoding"] = encoding
                self.handler.rfile = Mock()
                self.handler.do_POST()
                self.assertEqual(self.handler._json.call_args.args[0], status)
                self.handler.rfile.read.assert_not_called()
        self.api.reports.report.assert_not_called()

    def test_only_complete_authenticated_reports_reach_the_feed(self):
        self.handler.headers["Content-Length"] = "2"
        self.handler.rfile = Mock()
        self.handler.rfile.read.side_effect = TimeoutError()
        self.handler.do_POST()
        self.assertEqual(self.handler._json.call_args.args[0], 408)
        self.handler.rfile = io.BytesIO(b"{")
        self.handler.do_POST()
        self.assertEqual(self.handler._json.call_args.args[0], 400)
        del self.handler.headers["Authorization"]
        self.handler.rfile = Mock()
        self.handler.do_POST()
        self.assertEqual(self.handler._json.call_args.args[0], 401)
        self.handler.rfile.read.assert_not_called()
        self.api.reports.report.assert_not_called()


class RoundMarkerTests(unittest.TestCase):
    def test_marker_is_written_refreshed_and_removed(self):
        with tempfile.TemporaryDirectory() as d:
            lease = Path(d) / "heavy.json"
            cfg = {"machine": "NODE-01", "heavyLeaseFile": str(lease)}
            marker = node_worker.round_marker_path(cfg)
            self.assertEqual(marker, Path(d) / "heavy.round-waiting.json")
            node_worker.mark_round(cfg, True, "a merge round job is queued for this machine")
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["machine"], "NODE-01")
            self.assertFalse(lease.exists(), "the marker never touches the lease itself")
            node_worker.mark_round(cfg, False)
            self.assertFalse(marker.exists())
            node_worker.mark_round(cfg, False)   # already gone: no error
            off = {"roundMarkerFile": "", "heavyLeaseFile": str(lease)}
            self.assertIsNone(node_worker.round_marker_path(off))
            node_worker.mark_round(off, True)
            self.assertFalse(marker.exists(), "an empty roundMarkerFile switches the marker off")


class RoundLeaseTests(unittest.TestCase):
    """2026-11-16, rounds 213-216: NODE-01 has no heavyLeaseFile, so each step of a round job took the lease on its
    own and local RunSpecs runs got in between (ImportAudio, BuildContent: NO RESULT). A round job now holds it whole."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = Path(self.tmp.name) / "heavy.json"
        self.cfg = {"machine": "NODE-01", "workRoot": self.tmp.name, "roundLeaseFile": str(self.lease),
                    "roundMarkerFile": ""}

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_round_job_holds_the_lease_and_its_steps_run_under_it(self):
        job = node_worker.Job(None, self.cfg, {"id": 7, "recipe": "node-checks", "sha": SHA, "round": True})
        token = job.hold_for_round()
        self.assertTrue(token)
        self.assertEqual(json.loads(self.lease.read_text(encoding="utf-8"))["token"], token)
        env = node_worker._git_env(self.cfg)
        self.assertEqual((env["LS_LANE_HOLD"], env["LS_HEAVY_LEASE"]), (token, str(self.lease)))
        node_worker.drop_lease(self.cfg, token)
        self.assertFalse(self.lease.exists())
        self.assertNotIn("LS_LANE_HOLD", node_worker._git_env(self.cfg))

    def test_other_jobs_and_a_switched_off_lease_take_nothing(self):
        pr = node_worker.Job(None, self.cfg, {"id": 8, "recipe": "node-checks", "sha": SHA})
        self.assertEqual(pr.hold_for_round(), "")
        off = dict(self.cfg, roundLeaseFile="")
        self.assertEqual(node_worker.Job(None, off, {"id": 9, "recipe": "node-checks", "sha": SHA, "round": True})
                         .hold_for_round(), "")
        self.assertFalse(self.lease.exists())

    def test_a_round_job_waits_for_a_local_holder_and_names_it(self):
        self.lease.write_text(json.dumps({"owner": "local", "token": "x", "purpose": "Tools\\RunSpecs.ps1"}), encoding="utf-8")
        said = []
        got = node_worker.take_lease(self.cfg, {"id": 10}, Path(self.tmp.name), wait=0, path=str(self.lease),
                                     waiting=said.append, sleep=lambda _: None)
        self.assertEqual(got, "")
        stops = iter([False, True])
        node_worker.take_lease(self.cfg, {"id": 10}, Path(self.tmp.name), wait=60, path=str(self.lease),
                               waiting=said.append, stop=lambda: next(stops), sleep=lambda _: None)
        self.assertIn("Tools\\RunSpecs.ps1", said)
        self.assertEqual(json.loads(self.lease.read_text(encoding="utf-8"))["token"], "x", "the local hold is untouched")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class EndToEndTests(Base):
    """A real worker against a real API, fetching a real commit from a local repository."""

    def test_the_worker_takes_a_job_fetches_the_commit_and_reports(self):
        root = Path(self.tmp.name)
        src = root / "src"
        src.mkdir()
        git(src, "init", "-q")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "the commit")
        sha = git(src, "rev-parse", "HEAD")
        key = self.m.add_machine("node")
        (root / "key").write_text(key)
        api = WorkerApi(self.m, "127.0.0.1:0", log=lambda *_: None)
        api.start()
        try:
            port = api.server.server_address[1]
            cfg = {"coordinator": f"http://127.0.0.1:{port}", "machine": "node", "keyFile": str(root / "key"),
                   "labels": ["os:linux"], "workRoot": str(root / "work"), "repoUrl": str(src),
                   "recipes": ["checkout-only"]}
            (root / "w.json").write_text(json.dumps(cfg))
            job = self.m.enqueue("checkout-only", sha, ref="main")
            self.assertEqual(node_worker.main(["once", "--config", str(root / "w.json")]), 0)
        finally:
            api.stop()
        row = self.m.view()["jobs"][0]
        self.assertEqual((row["id"], row["state"], row["machine"]), (job, "passed", "node"))
        log = self.m.log_text(job)
        self.assertIn(f"{sha} the commit", log)
        self.assertIn("##[step] Show the commit: exit 0", log)
        node = self.m.view()["machines"][0]
        self.assertTrue(node["online"])
        self.assertEqual(node["labels"], ["cap:asset-sync", "os:linux"])

    def test_the_worker_refuses_a_recipe_it_does_not_run(self):
        root = Path(self.tmp.name)
        (root / "key").write_text("k")
        cfg = {"coordinator": "http://127.0.0.1:1", "machine": "n", "keyFile": str(root / "key"),
               "workRoot": str(root / "w"), "repoUrl": "x", "recipes": ["checkout-only"]}
        job = node_worker.Job(node_worker.Coordinator(cfg), cfg, {"id": 1, "recipe": "nightly-full", "sha": SHA})
        code, summary, _ = job.run()
        self.assertEqual(code, 2)
        self.assertIn("does not run", summary)


class RecipeTests(unittest.TestCase):
    def test_node_checks_mirror_the_workflow(self):
        ws = Path("E:/w/LifeShare")
        steps = node_worker.recipe_steps("node-checks", {"only": "RunFlowTest,RunM5aTest", "strict_slots": True}, ws,
                                         {"engine": r"E:\UE_5.8"}, Path("T:/r.json"))
        names = [s[0] for s in steps]
        self.assertEqual(names[2], "Build, BuildContent and tests")
        tests = steps[2][1]
        self.assertEqual(tests[-7:], ["-Engine", r"E:\UE_5.8", "-ResultJson", str(Path("T:/r.json")), "-Only",
                                      "RunFlowTest,RunM5aTest", "-StrictSlots"][-7:])
        self.assertEqual([s[2] for s in steps], [False, False, False, True, True, True])
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("node-checks", {"only": "a;b"}, ws, {}, Path("r"))
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("anything", {}, ws, {}, Path("r"))

    def test_stop_processes_quotes_every_path(self):
        argv = node_worker._stop_processes(Path("E:/it's/LifeShare"))
        self.assertIn("it''s", argv[-1])
        self.assertIn("-ProbePath @(", argv[-1])

    def test_summary_from_the_result_json(self):
        r = {"machine": "NODE-01", "results": [{"name": "A", "pass": True}, {"name": "B", "pass": False}]}
        self.assertEqual(node_worker._summary(r), "1 of 2 failed on NODE-01: B")
        self.assertEqual(node_worker._summary({"results": [{"name": "A", "pass": True}]}), "1 checks passed")


if __name__ == "__main__":
    unittest.main()
