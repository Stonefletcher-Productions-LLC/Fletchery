"""The history store (store.py) and the Flow strip (flow.py): what is kept, that recording twice changes nothing, and the
numbers and verdict the strip shows."""
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import flow  # noqa: E402
import store  # noqa: E402

NOW = 1_800_000_000.0


def at(minutes_ago: float) -> str:
    return datetime.fromtimestamp(NOW - minutes_ago * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(n, merged_min, opened_min, base="main", branch="feature", author="ana"):
    return {"number": n, "title": f"Change {n}", "merged_at": at(merged_min) if merged_min is not None else None,
            "created_at": at(opened_min), "base": {"ref": base}, "head": {"ref": branch}, "user": {"login": author}}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = store.Store(None)
        self.addCleanup(self.s.close)

    def test_merges_are_kept_once_and_closed_unmerged_are_not(self):
        prs = [pr(1, 30, 120), pr(2, None, 60), pr(3, 90, 300, branch="claude/x")]
        self.s.merges("Acme/Game", prs, lambda b, a: "Claude" if b.startswith("claude/") else "")
        self.s.merges("Acme/Game", prs, None)
        rows = self.s.query("SELECT number, agent FROM merges ORDER BY number")
        self.assertEqual([r[0] for r in rows], [1, 3])

    def test_runs_only_when_finished_with_otel_results(self):
        self.s.runs("Acme/Game", [
            {"id": 1, "status": "completed", "conclusion": "timed_out", "created_at": at(10), "updated_at": at(1), "event": "push"},
            {"id": 2, "status": "in_progress", "created_at": at(5)},
            {"id": 3, "status": "completed", "conclusion": "startup_failure", "created_at": at(5), "updated_at": at(4)}])
        self.assertEqual(self.s.query("SELECT id, result FROM runs ORDER BY id"), [(1, "timeout"), (3, "error")])

    def test_jobs_keep_queue_wait(self):
        run = {"id": 9, "name": "PR check", "created_at": at(60)}
        self.s.jobs("Acme/Game", run, [
            {"id": 91, "status": "in_progress", "started_at": at(20), "runner_name": "PC-unreal-pr", "labels": ["unreal-pr"]},
            {"id": 92, "status": "queued"}])
        rows = self.s.query("SELECT id, started - created, labels FROM jobs")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][1], 40 * 60, delta=1)
        self.assertEqual(rows[0][2], "unreal-pr")

    def test_prune_drops_old_rows(self):
        self.s.merges("A/B", [pr(1, 60 * 24 * 100, 60 * 24 * 101), pr(2, 10, 20)])
        self.s.prune(now=NOW)
        self.assertEqual([r[0] for r in self.s.query("SELECT number FROM merges")], [2])

    def test_disk_readings_at_most_once_a_minute(self):
        self.s.disk("NODE", "E:", 40, at=NOW)
        self.s.disk("NODE", "E:", 39, at=NOW + 30)
        self.s.disk("NODE", "E:", 38, at=NOW + 90)
        self.assertEqual(len(self.s.query("SELECT * FROM disks")), 2)

    def test_file_store_survives_reopen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a = store.Store(Path(d) / "x" / "fletchery.db")
            a.merges("A/B", [pr(5, 1, 2)])
            a.close()
            b = store.Store(Path(d) / "x" / "fletchery.db")
            self.assertEqual(b.query("SELECT number FROM merges"), [(5,)])
            b.close()


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.s = store.Store(None)
        self.addCleanup(self.s.close)
        self.f = flow.Flow(self.s)

    def test_counts_last_merge_and_lead_time(self):
        self.s.merges("Acme/Game", [pr(1, 20, 80), pr(2, 50, 170), pr(3, 60 * 5, 60 * 6, base="Test-Staging")])
        self.s.merges("Acme/Tool", [pr(7, 60 * 30, 60 * 31)])
        out = self.f.compute(["Acme/Game", "Acme/Tool"], {"Acme/Game": 2}, NOW)
        g = out["repos"]["Acme/Game"]
        self.assertEqual(g["merges_hour"], 2)
        self.assertEqual(g["merges_day"], 3)
        self.assertEqual(sum(g["hours"]), 3)
        self.assertEqual(g["hours"][23], 2)
        self.assertEqual({(x["base"], x["number"]) for x in g["last"]}, {("main", 1), ("Test-Staging", 3)})
        self.assertEqual(g["since_merge"], 20 * 60)
        self.assertEqual(g["lead_p50"], 60 * 60)
        self.assertEqual(g["verdict"]["state"], "moving")
        self.assertEqual(out["all"]["merges_week"], 4)
        self.assertEqual(out["repos"]["Acme/Tool"]["verdict"]["state"], "idle")

    def test_slow_and_stopped_only_with_waiting_work(self):
        self.s.merges("Acme/Game", [pr(1, 120, 200)])
        self.assertEqual(self.f.compute(["Acme/Game"], {"Acme/Game": 3}, NOW)["all"]["verdict"]["state"], "slow")
        self.assertEqual(self.f.compute(["Acme/Game"], {}, NOW)["all"]["verdict"]["state"], "idle")
        late = self.f.compute(["Acme/Game"], {"Acme/Game": 3}, NOW + 3 * 3600)["all"]["verdict"]
        self.assertEqual(late["state"], "stopped")
        self.assertIn("3 changes wait", late["why"])

    def test_queue_wait_percentiles_and_red_share(self):
        run = {"id": 1, "name": "PR check", "created_at": at(100)}
        self.s.jobs("Acme/Game", run, [{"id": i, "status": "completed", "conclusion": "success",
                                        "created_at": at(100), "started_at": at(100 - w), "completed_at": at(1)}
                                       for i, w in enumerate([1, 2, 3, 4, 50])])
        self.s.merges("Acme/Game", [pr(1, 10, 20, base="Test-Staging")])
        self.s.runs("Acme/Game", [
            {"id": 10 + i, "status": "completed", "conclusion": c, "event": "push", "head_branch": "Test-Staging",
             "created_at": at(200), "updated_at": at(100)} for i, c in enumerate(["success", "failure", "success", "cancelled"])])
        self.s.runs("Acme/Game", [{"id": 30, "status": "completed", "conclusion": "failure", "event": "push",
                                   "head_branch": "some-feature", "created_at": at(200), "updated_at": at(100)}])
        g = self.f.compute(["Acme/Game"], {}, NOW)["all"]
        self.assertEqual(g["wait_p50"], 3 * 60)
        self.assertEqual(g["wait_p90"], 50 * 60)
        self.assertAlmostEqual(g["red_share"], 1 / 3)
        self.assertEqual(g["red_n"], 3)

    def test_empty_store(self):
        g = self.f.compute([], {}, NOW)["all"]
        self.assertIsNone(g["since_merge"])
        self.assertIsNone(g["lead_p50"])
        self.assertEqual(g["verdict"]["state"], "idle")

    def test_build_wait_uses_real_pr_job_times_and_observed_attempts(self):
        repo = "Acme/LifeShare"
        run = {"id": 1, "name": "PR check", "event": "pull_request", "created_at": at(220)}
        job = {"id": 1, "name": store.PR_BUILD, "status": "completed", "conclusion": "success",
               "runner_name": "node", "created_at": at(110), "started_at": at(100), "completed_at": at(70)}
        self.s.jobs(repo, run, [job, job, {**job, "id": 2, "created_at": at(190), "conclusion": "failure"},
                                {**job, "id": 3, "created_at": None}, {**job, "id": 4, "conclusion": "skipped"},
                                {**job, "id": 5, "name": "Cheap checks"}, {**job, "id": 6, "runner_name": ""},
                                {**job, "id": 7, "conclusion": "cancelled"}])
        self.s.jobs(repo, {**run, "event": "push"}, [{**job, "id": 8}])
        def build(current):
            return self.f.compute([repo], {}, NOW, build_queues={repo: current})["all"]["build_queues"][0]
        b = build({"times": [NOW - 1200, NOW - 2400]})
        self.assertEqual((b["samples"], b["median"], b["p90"], b["waiting"], b["oldest"]), (2, 3000, 5400, 2, 2400))
        self.assertIsNone(build(None)["waiting"])
        self.assertEqual(build({"times": []})["waiting"], 0)
        self.assertIsNone(build({"times": [None]})["oldest"])


if __name__ == "__main__":
    unittest.main()
