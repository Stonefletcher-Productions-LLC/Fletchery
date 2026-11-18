"""Scheduled builds (builds.py): schedule, daily repeat, queueing the `package` job, following it, the controls (start
now, stop, build again) and the node's `package` recipe."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import builds  # noqa: E402
import machines  # noqa: E402
import node_worker  # noqa: E402
from builds import Builds  # noqa: E402

SHA = "b" * 40
NOW = datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)


class FakeState:
    def __init__(self):
        self.builds = []
        self.saves = 0

    def save(self):
        self.saves += 1


class FakeCoord:
    def __init__(self, m):
        self.machines = m
        self.resolved = []

    def resolve(self, ref):
        self.resolved.append(ref)
        return SHA


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.logs = []
        self.engine = SimpleNamespace(
            state=FakeState(), state_dir=Path(self.tmp.name), dry_run=False, wake=lambda: None,
            cfg=SimpleNamespace(base_branch="Test-Staging", repo="o/r", name="LifeShare", builds=None, heavy_lease_file=""),
            log=self.logs.append, gh=None)
        self.m = machines.Machines(self.tmp.name, "o/r")
        self.engine.machines = FakeCoord(self.m)
        self.b = Builds(self.engine)

    def tearDown(self):
        self.tmp.cleanup()

    def node(self, labels=("os:windows", "role:tests", "role:cook"), at=None):
        self.m.add_machine("NODE-01")
        self.m.seen("NODE-01", "100.1.1.1", list(labels), 24, "1", {})

    def add(self, **kw):
        data = {"version": "0.3.9", "at": NOW.isoformat(), **kw}
        return self.b.add(data)


class ScheduleTests(Base):
    def test_checks_input(self):
        for bad in ({"version": "0.3"}, {"version": "v1.0.0"}, {"version": "0.3.9", "ref": "a..b"},
                    {"version": "0.3.9", "repeat": "weekly"},
                    {"version": "0.3.9", "at": "2026-11-11T22:00"}):
            with self.assertRaises(ValueError, msg=bad):
                self.b.add(bad)

    def test_defaults_to_the_base_branch_and_waits_for_its_time(self):
        row = self.add()
        self.assertEqual((row["ref"], row["state"]), ("Test-Staging", "scheduled"))
        self.b.tick(NOW - timedelta(minutes=1))
        self.assertEqual(self.b.rows[0]["state"], "scheduled")
        self.assertEqual(self.engine.machines.resolved, [])

    def test_reads_the_branch_once_at_its_time(self):
        self.add()
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.assertEqual((row["sha"], row["state"], row["route"]), (SHA, "queued", "node"))
        self.assertEqual(self.engine.machines.resolved, ["Test-Staging"])
        self.b.tick(NOW + timedelta(minutes=5))
        self.assertEqual(self.engine.machines.resolved, ["Test-Staging"])  # not read again
        self.assertIn("none can take it right now", row["reason"])

    def test_daily_starts_a_copy_and_moves_on_a_day(self):
        self.add(repeat="daily")
        self.b.tick(NOW + timedelta(hours=3))
        sched, run = self.b.rows
        self.assertEqual(sched["state"], "scheduled")
        self.assertEqual(builds.utc(sched["at"]), NOW + timedelta(days=1))
        self.assertEqual((run["repeat"], run["parent"], run["state"]), ("", sched["id"], "queued"))
        self.b.tick(NOW + timedelta(hours=4))
        self.assertEqual(len(self.b.rows), 2)

    def test_cancel(self):
        row = self.add(repeat="daily")
        self.assertEqual(self.b.cancel(row["id"])["message"], "Repeat stopped.")
        self.b.tick(NOW + timedelta(days=2))
        self.assertEqual(len(self.b.rows), 1)
        with self.assertRaises(ValueError):
            self.b.cancel(row["id"])


class RoutingTests(Base):
    def test_queues_a_package_job_for_a_machine_that_can_package(self):
        self.node()
        self.add(tests="full")
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.assertEqual((row["route"], row["state"]), ("node", "queued"))
        job = self.m.job(row["job"])
        self.assertEqual((job["recipe"], job["sha"], job["params"]), ("package", SHA, {"version": "0.3.9", "tests": "full"}))
        self.assertIn("role:cook", machines.RECIPES["package"]["labels"])
        self.assertEqual(self.m.claim("NODE-01")["id"], row["job"])

    def test_follows_the_node_job_to_done(self):
        self.node()
        self.add()
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.m.claim("NODE-01")
        self.b.tick(NOW)
        self.assertEqual((row["state"], row["machine"]), ("running", "NODE-01"))
        self.m.finish("NODE-01", row["job"], 0, r"Packaged to E:\fletchery-builds\LifeShare-0.3.9 on NODE-01")
        self.b.tick(NOW)
        self.assertEqual((row["state"], row["output"]), ("done", r"E:\fletchery-builds\LifeShare-0.3.9"))

    def test_a_failed_node_build_fails(self):
        self.node()
        self.add()
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.m.claim("NODE-01")
        self.m.finish("NODE-01", row["job"], 1, "Package 0.3.9 failed (exit 1)")
        self.b.tick(NOW)
        self.assertEqual((row["state"], row["reason"]), ("failed", "Package 0.3.9 failed (exit 1)"))


    def test_extra_node_labels_are_required(self):
        self.engine.cfg.builds = {"nodeLabels": ["unreal:5.8.3"]}
        self.node()  # no unreal:5.8.3
        self.add()
        self.b.tick(NOW)
        self.assertIn("none can take it right now", self.b.rows[0]["reason"])
        self.assertIsNone(self.m.claim("NODE-01"))

    def test_an_old_pc_build_is_failed_on_start(self):
        self.engine.state.builds = [{"id": "x", "state": "running", "route": "pc", "version": "0.3.9"}]
        Builds(self.engine)
        self.assertEqual(self.engine.state.builds[0]["state"], "failed")


class ControlTests(Base):
    def test_start_now_moves_a_scheduled_build_to_now(self):
        row = self.add(at=(builds.utc() + timedelta(days=3)).isoformat())
        self.b.run_now(row["id"])
        self.assertLessEqual(builds.utc(self.b.rows[0]["at"]), builds.utc())
        with self.assertRaises(ValueError):
            self.b.run_now("nope")

    def test_stop_a_running_build_asks_the_machine(self):
        self.node()
        row = self.add()
        self.b.tick(NOW)
        self.m.claim("NODE-01")
        self.assertIn("next heartbeat", self.b.cancel(row["id"])["message"])

    def test_build_again_after_a_failure(self):
        self.node()
        self.add(tests="full")
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.m.claim("NODE-01")
        self.m.finish("NODE-01", row["job"], 1, "Package 0.3.9 failed (exit 1)")
        self.b.tick(NOW)
        again = self.b.retry(row["id"])
        self.assertEqual((again["version"], again["ref"], again["tests"], again["state"]), ("0.3.9", "Test-Staging", "full", "scheduled"))
        with self.assertRaises(ValueError):
            self.b.retry(again["id"])  # still scheduled


class RecipeTests(unittest.TestCase):
    def test_package_recipe_params_are_checked(self):
        self.assertEqual(machines.check_params("package", {"version": "0.3.9", "tests": "full", "cmd": "x"}),
                         {"version": "0.3.9", "tests": "full"})
        with self.assertRaises(ValueError):
            machines.check_params("package", {"version": "0.3.9;calc"})

    def test_node_steps(self):
        ws = Path("C:/w")
        steps = node_worker.recipe_steps("package", {"version": "0.3.9", "tests": "", "out_dir": r"E:\b\LifeShare-0.3.9"},
                                         ws, {"engine": r"E:\UE_5.8"}, Path("r.json"))
        names = [n for n, _, _ in steps]
        self.assertEqual(names[2:5], ["Build the editor", "BuildContent", "Package 0.3.9"])
        self.assertTrue(all(a for _, _, a in steps[-3:]))
        full = node_worker.recipe_steps("package", {"version": "0.3.9", "tests": "full", "out_dir": "o"}, ws, {}, Path("r"))
        self.assertIn("Build, BuildContent and Full tests", [n for n, _, _ in full])
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("package", {"version": "x", "out_dir": "o"}, ws, {}, Path("r"))

    def test_package_out_never_overwrites(self):
        with tempfile.TemporaryDirectory() as t:
            first = node_worker.package_out(t, "LifeShare", "0.3.9", SHA)
            self.assertEqual(first.name, "LifeShare-0.3.9")
            first.mkdir()
            second = node_worker.package_out(t, "LifeShare", "0.3.9", SHA)
            self.assertEqual(second.name, "LifeShare-0.3.9-" + SHA[:10])
            second.mkdir()
            self.assertEqual(node_worker.package_out(t, "LifeShare", "0.3.9", SHA).name, f"LifeShare-0.3.9-{SHA[:10]}-2")


if __name__ == "__main__":
    unittest.main()
