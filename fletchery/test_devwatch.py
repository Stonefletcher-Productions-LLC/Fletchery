"""The Live tab's feed (devwatch.py) and the machines' reporter (reporter.py): what is shown, what counts as stuck, the
free 304s, and the /v1/report route. No real GitHub: a fake answers by path, and a local server stands in for ETags."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import devwatch  # noqa: E402
import reporter  # noqa: E402


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


ORG, REPO, OLD = "Acme", "Acme/Game", "Acme/Old"


class FakeGitHub:
    remaining, calls, free = 4000, 0, 0

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, path):
        self.asked.append(path)
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise devwatch.GitHubError(404, "Not Found")


def world(**over):
    sha_ok, sha_bad = "a" * 40, "b" * 40
    answers = {
        "user/repos": [
            {"full_name": REPO, "owner": {"login": ORG, "type": "Organization"}, "pushed_at": ago(30), "html_url": "https://github.com/Acme/Game"},
            {"full_name": OLD, "owner": {"login": ORG, "type": "Organization"}, "pushed_at": ago(60 * 24 * 90)},
            {"full_name": "Acme/Gone", "owner": {"login": ORG, "type": "Organization"}, "pushed_at": ago(5), "archived": True}],
        f"repos/{REPO}/actions/runs/7/jobs": {"jobs": [{"name": "Compile and test", "status": "in_progress", "runner_name": "ALEX-PC-unreal-pr",
                                                         "steps": [{"name": "Checkout", "status": "completed"},
                                                                   {"name": "Build editor", "status": "in_progress", "started_at": ago(75)},
                                                                   {"name": "Specs", "status": "queued"}]}]},
        f"repos/{REPO}/actions/runs": {"workflow_runs": [
            {"id": 7, "name": "PR check", "status": "in_progress", "event": "pull_request", "created_at": ago(80), "run_started_at": ago(80),
             "updated_at": ago(1), "html_url": "https://github.com/Acme/Game/actions/runs/7", "pull_requests": [{"number": 12}]},
            {"id": 8, "name": "PR check", "status": "queued", "created_at": ago(30), "updated_at": ago(30), "pull_requests": [{"number": 13}]},
            {"id": 6, "name": "PR check", "status": "completed", "conclusion": "success", "created_at": ago(200), "updated_at": ago(180)}]},
        f"repos/{REPO}/pulls": [
            {"number": 12, "title": "Bus lines", "html_url": "https://github.com/Acme/Game/pull/12", "head": {"sha": sha_ok, "ref": "bus"}, "base": {"ref": "main"},
             "user": {"login": "dev"}, "updated_at": ago(2), "labels": []},
            {"number": 13, "title": "Weather", "html_url": "https://github.com/Acme/Game/pull/13", "head": {"sha": sha_bad, "ref": "wx"}, "base": {"ref": "main"},
             "user": {"login": "dev"}, "updated_at": ago(5), "labels": [{"name": "status:needs-alex"}]}],
        f"repos/{REPO}/commits/{sha_ok}/check-runs": {"check_runs": [{"name": "Compile and test", "status": "in_progress", "started_at": ago(20)}]},
        f"repos/{REPO}/commits/{sha_bad}/check-runs": {"check_runs": [{"name": "Compile and test", "status": "completed", "conclusion": "failure",
                                                                       "completed_at": ago(4), "html_url": "https://github.com/Acme/Game/runs/1"}]},
        f"repos/{REPO}/issues": [{"number": 40, "title": "Bench clipping", "html_url": "https://github.com/Acme/Game/issues/40", "labels": [{"name": "foreman:blocked"}],
                                  "created_at": ago(3), "updated_at": ago(2)},
                                 {"number": 13, "title": "Weather", "pull_request": {}, "labels": []}],
        f"orgs/{ORG}/actions/runners": {"runners": [
            {"id": 1, "name": "ALEX-PC-unreal-pr", "status": "online", "busy": True, "labels": [{"name": "self-hosted", "type": "read-only"}, {"name": "unreal-pr", "type": "custom"}]},
            {"id": 2, "name": "NODE-unreal-light", "status": "offline", "busy": False, "labels": [{"name": "unreal-light", "type": "custom"}]}]},
    }
    answers.update(over)
    return answers


class Feed(unittest.TestCase):
    def watch(self, **over):
        w = devwatch.DevWatch({"names": {"ALEX-PC": "Gaming PC"}}, github=FakeGitHub(world(**over)))
        w.poll()
        return w

    def test_finds_repositories_by_itself(self):
        w = self.watch()
        self.assertEqual([r["full_name"] for r in w.repos], [REPO])     # old and archived ones are left out
        self.assertEqual(w.interval, w.fast)                             # something runs: ask often

    def test_snapshot(self):
        s = self.watch().snapshot()
        run = s["runs"][0]
        self.assertEqual((run["id"], run["runner"], run["step"], run["steps_done"], run["steps_total"]), (7, "ALEX-PC-unreal-pr", "Build editor", 1, 3))
        self.assertEqual([i["number"] for i in s["issues"]], [40])      # PRs are not issues
        self.assertEqual(s["prs"][0]["checks"]["state"], "failed")
        pc = next(m for m in s["machines"] if m["name"] == "Gaming PC")
        self.assertTrue(pc["busy"])
        self.assertIn("PR check · PR #12", pc["activities"][0]["title"])
        node = next(r for r in s["runners"] if r["name"] == "NODE-unreal-light")
        self.assertEqual((node["machine"], node["online"]), ("NODE", False))

    def test_stuck_and_blocked(self):
        titles = " | ".join(b["title"] for b in self.watch().snapshot()["blockers"])
        for want in ("waited 30 min for a runner", "\"Build editor\" has run 75 min", "PR #13 check failed",
                     "Runner NODE-unreal-light is offline", "PR #13 is marked blocked", "#40 is blocked"):
            self.assertIn(want, titles)

    def test_one_bad_repo_keeps_the_rest(self):
        w = self.watch()
        w.gh.answers[f"repos/{REPO}/pulls"] = devwatch.GitHubError(502, "Bad Gateway")
        with self.assertRaises(devwatch.GitHubError):   # every repo failed: the feed says so, the last answers stay
            w.poll()
        self.assertEqual(len(w.prs[REPO]), 2)
        self.assertIn("feed", [b["kind"] for b in w.snapshot()["blockers"]])

    def test_no_admin_scope_still_shows_runners_from_jobs(self):
        w = self.watch(**{f"orgs/{ORG}/actions/runners": devwatch.GitHubError(403, "Must have admin rights")})
        s = w.snapshot()
        self.assertIn("admin:org", s["fresh"]["runner_note"])
        self.assertEqual([r["name"] for r in s["runners"]], ["ALEX-PC-unreal-pr"])

    def test_finished_checks_are_not_asked_again(self):
        w = self.watch()
        w.gh.asked.clear()
        w.poll()
        self.assertFalse(any("b" * 40 in p for p in w.gh.asked))
        self.assertTrue(any("a" * 40 in p for p in w.gh.asked))

    def test_machine_reports(self):
        w = self.watch()
        w.report("GAMING-PC", {"host": "ALEX-PC", "busy": True, "idle_seconds": 900, "procs": {"claude": 2, "unreal": 1},
                               "lease": {"purpose": r"Hand merge batch 7: E:\scratch\Build.ps1", "since": ago(30), "alive": True},
                               "queue": [{"purpose": "local run PR check #14", "since": ago(100)}],
                               "stats": {"cpu": 2.0, "ram_used_gb": 20, "ram_total_gb": 32, "disks": [{"name": "E:", "free_gb": 140}]}})
        w.report("NODE-01", {"host": "SF-NODE-01", "lease": {"purpose": "Proof run #2263", "since": ago(10), "alive": False}})
        w.reports["NODE-01"]["at"] = ago(2)
        s = w.snapshot()
        pc = next(m for m in s["machines"] if m["name"] == "Gaming PC")
        kinds = [a["kind"] for a in pc["activities"]]
        self.assertIn("merging", kinds)
        self.assertIn("coding", kinds)
        self.assertNotIn("E:\\scratch", json.dumps(s))                  # folder paths stay on the machine
        self.assertIn("ALEX-PC-unreal-pr", pc["runners"])                 # runner and report land on one machine
        titles = " | ".join(b["title"] for b in s["blockers"])
        for want in ("CPU has been idle 15 min", "waited 100 min in the queue", "SF-NODE-01 stopped reporting",
                     "its process is gone"):
            self.assertIn(want, titles)

    def test_bounds(self):
        w = devwatch.DevWatch({}, github=FakeGitHub({}))
        with self.assertRaises(ValueError):
            w.report("X", {"blob": "x" * 70000})

    def test_kinds(self):
        for text, kind in (("PR check #2290 (GitHub run 1)", "check"), ("Hand merge batch 7", "merging"),
                           ("NavCheck proof for #12", "proof"), ("Build 0.4.5", "building"), ("something", "working")):
            self.assertEqual(devwatch.kind_of(text), kind, text)


class Etags(unittest.TestCase):
    def test_304_is_free_and_answers_from_memory(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.headers.get("If-None-Match"))
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.send_header("X-RateLimit-Remaining", "4999")
                    self.end_headers()
                    return
                body = b'{"ok": 1}'
                self.send_response(200)
                self.send_header("ETag", '"v1"')
                self.send_header("X-RateLimit-Remaining", "4998")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            gh = devwatch.GitHub(lambda: "t")
            url = f"http://127.0.0.1:{srv.server_address[1]}/x"
            self.assertEqual(_plain(gh, url), {"ok": 1})
            self.assertEqual(_plain(gh, url), {"ok": 1})
            self.assertEqual((gh.calls, gh.free, gh.remaining), (1, 1, 4999))
            self.assertEqual(hits, [None, '"v1"'])
        finally:
            srv.shutdown()


def _plain(gh, url):
    old = devwatch.API
    try:
        devwatch.API = url.rsplit("/", 1)[0]
        return gh.get("x")
    finally:
        devwatch.API = old


class Reporter(unittest.TestCase):
    def test_reads_the_lease_queue_and_activities(self):
        with tempfile.TemporaryDirectory() as d:
            lease = Path(d) / "heavy.json"
            lease.write_text(json.dumps({"owner": "local", "pid": os.getpid(), "purpose": "Hand merge batch 7", "acquiredUtc": ago(3)}), encoding="utf-8")
            (Path(d) / "heavy.queue").mkdir()
            (Path(d) / "heavy.queue" / "2-20261118T000000000000Z-abc.json").write_text(json.dumps({"purpose": "local run PR check #9", "queuedUtc": ago(1)}))
            old = Path(d) / "heavy.queue" / "2-20261118T000000000000Z-old.json"
            old.write_text(json.dumps({"purpose": "dead run"}))
            os.utime(old, (time.time() - 600, time.time() - 600))
            (Path(d) / "activity").mkdir()
            (Path(d) / "activity" / "proof.json").write_text(json.dumps({"title": "Proof run for #2263", "kind": "proof"}))
            r = reporter.Reporter({"leaseFile": str(lease), "disks": [d], "keyFile": "k", "coordinator": "x", "machine": "m"})
            rep = r.collect()
        self.assertEqual((rep["lease"]["purpose"], rep["lease"]["alive"]), ("Hand merge batch 7", True))
        self.assertEqual([q["purpose"] for q in rep["queue"]], ["local run PR check #9"])
        self.assertEqual(rep["activities"][0]["title"], "Proof run for #2263")
        self.assertTrue(rep["busy"])
        self.assertTrue(rep["stats"]["disks"])

    def test_dead_pid(self):
        self.assertFalse(reporter.alive(2 ** 22 + 12345))
        self.assertIsNone(reporter.alive("x"))

    def test_route(self):
        import machines
        with tempfile.TemporaryDirectory() as d:
            m = machines.Machines(d, "Acme/Game")
            key = m.add_machine("NODE-01")
            api = machines.WorkerApi(m, "127.0.0.1:0")
            self.assertEqual(api.handle("/v1/report", "NODE-01", key, "1.2.3.4", {})[0], 503)
            api.reports = devwatch.DevWatch({}, github=FakeGitHub({}))
            self.assertEqual(api.handle("/v1/report", "NODE-01", key, "1.2.3.4", {"host": "SF-NODE-01"}), (200, {"ok": True, "every": 10}))
            self.assertEqual(api.handle("/v1/report", "NODE-01", "wrong", "1.2.3.4", {})[0], 401)
            self.assertIn("NODE-01", api.reports.reports)


if __name__ == "__main__":
    unittest.main()
