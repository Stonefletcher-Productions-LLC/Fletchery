"""The backend (app.py): one backend per state folder, builds kept across the upgrade, what only it flags on Live, and
the controls: each one logs who used it, and checks what it is given."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: E402
from config import Config  # noqa: E402


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_backend_per_state_folder(self):
        lock = app.take_lock(self.dir)
        self.assertEqual(lock.read_text().split()[0], str(os.getpid()))
        lock.write_text(f"{os.getppid()}\n")   # another process that is alive holds it
        with self.assertRaises(app.Busy):
            app.take_lock(self.dir)

    def test_a_lock_left_by_a_gone_process_is_taken_over(self):
        (self.dir / "fletchery.lock").write_text("999999999\n")
        self.assertEqual(app.take_lock(self.dir).read_text().split()[0], str(os.getpid()))
        (self.dir / "fletchery.lock").write_text("garbage")
        self.assertEqual(app.take_lock(self.dir).read_text().split()[0], str(os.getpid()))


class StateTests(unittest.TestCase):
    def test_builds_come_over_from_the_old_state_once(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "state.json").write_text(json.dumps({"builds": [{"id": "a"}], "tasks": {"1": {}}}), encoding="utf-8")
            s = app.State(d)
            self.assertEqual(s.builds, [{"id": "a"}])
            s.builds.append({"id": "b"})
            s.save()
            self.assertEqual([r["id"] for r in app.State(d).builds], ["a", "b"])


HOUR_AGO = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeMachines:
    def __init__(self):
        self.calls = []
        self.held = False

    def view(self, jobs=40):
        return {"machines": [{"name": "NODE-01", "online": False, "last_seen": HOUR_AGO},
                             {"name": "NODE-02", "online": True, "job": {"id": 9, "state": "running", "machine": "NODE-02", "recipe": "package",
                                                                         "params": {"version": "0.4.6"}, "ref": "main", "started": HOUR_AGO,
                                                                         "steps": [{"name": "BuildContent", "state": "passed"},
                                                                                   {"name": "Package 0.4.6", "state": "running"}]}}],
                "jobs": [{"id": 3, "state": "queued", "created": "2000-01-01T00:00:00Z", "requested_by": "build 0.4.6"}]}

    def cancel(self, job):
        self.calls.append(("cancel", job))
        return "stopping"

    def retry(self, job, by):
        self.calls.append(("retry", job, by))
        return job + 10

    def set_held(self, on):
        self.held = on
        return on

    def set_paused(self, name, kind, on):
        self.calls.append(("pause", name, kind, on))
        return [kind] if on else []


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config()
        cfg.name, cfg.repo, cfg.state_dir = "Harbor", "example/harbor", self.tmp.name
        self.app = app.App(cfg, log_print=lambda *a, **k: None)
        self.acts = self.app.actions()

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        return [r["line"] for r in self.app.lines]

    def test_restart_says_who_and_the_backend_exits_to_be_started_again(self):
        self.acts["/api/restart"]({"_from": "the phone (you@example.com)"})
        self.assertIn("restart requested from the phone (you@example.com)", self.lines())
        self.assertEqual(self.app.serve(check_every=0.01), app.RESTART_EXIT)

    def test_status_names_the_running_code(self):
        s = self.app.status()
        self.assertEqual((s["version"], len(s["version"])), (app.code_fingerprint()[:8], 8))
        self.assertTrue(s["started"])

    def test_machine_controls_need_build_machines(self):
        for path in ("/api/jobs/cancel", "/api/jobs/retry", "/api/machines/pause", "/api/machines/hold"):
            with self.assertRaises(ValueError, msg=path):
                self.acts[path]({"id": 1})

    def test_machine_controls_log_who_used_them(self):
        fake = FakeMachines()
        self.app.machines = SimpleNamespace(machines=fake)
        self.assertEqual(self.acts["/api/jobs/cancel"]({"id": 7, "_from": "the window"}), {"state": "stopping"})
        self.assertEqual(self.acts["/api/jobs/retry"]({"id": 7, "_from": "the window"}), {"id": 17})
        self.acts["/api/machines/pause"]({"name": "NODE-01", "on": True, "_from": "the window"})
        self.assertEqual(self.acts["/api/machines/hold"]({"on": True, "_from": "the window"}), {"held": True})
        self.assertEqual(fake.calls, [("cancel", 7), ("retry", 7, "run again from the window"), ("pause", "NODE-01", "builds", True)])
        self.assertIn("machines: job 7 stopping (from the window)", self.lines())
        self.assertIn("machines: NODE-01 paused (from the window)", self.lines())
        self.assertTrue(any(line.startswith("machines: all paused") for line in self.lines()))

    def test_actions_runs_only_in_watched_repositories(self):
        posted = []
        self.app.gh = SimpleNamespace(api=lambda *a: posted.append(a))
        self.app.portal = SimpleNamespace(live=SimpleNamespace(repos=[{"full_name": "example/harbor"}], _wake=SimpleNamespace(set=lambda: None)))
        for bad in ({"repo": "someone/else", "id": 5}, {"repo": "example/harbor", "id": 0}, {"repo": "example/harbor", "id": -3}):
            with self.assertRaises(ValueError, msg=bad):
                self.acts["/api/actions/cancel"](bad)
        self.acts["/api/actions/rerun"]({"repo": "example/harbor", "id": 5, "failed": True})
        self.acts["/api/actions/cancel"]({"repo": "example/harbor", "id": 6})
        self.acts["/api/actions/rerun-job"]({"repo": "example/harbor", "id": 77})
        with self.assertRaises(ValueError):
            self.acts["/api/actions/rerun-job"]({"repo": "someone/else", "id": 77})
        self.assertEqual([p[0] for p in posted], ["repos/example/harbor/actions/runs/5/rerun-failed-jobs",
                                                  "repos/example/harbor/actions/runs/6/cancel",
                                                  "repos/example/harbor/actions/jobs/77/rerun"])

    def test_snooze_an_alert(self):
        import alerts
        live = SimpleNamespace(alerts=alerts.Alerts())
        self.app.portal = SimpleNamespace(live=live)
        self.assertTrue(self.acts["/api/alerts/snooze"]({"key": "runner:N", "hours": 4})["until"])
        self.acts["/api/alerts/snooze"]({"group": "Acme/Game", "hours": 24})
        self.assertEqual(set(live.alerts.snoozed), {"runner:N", "group:Acme/Game"})
        for bad in ({"key": "x", "hours": 2}, {"hours": 1}, {"key": "x", "hours": "soon"}):
            with self.assertRaises(ValueError, msg=bad):
                self.acts["/api/alerts/snooze"](bad)
        self.acts["/api/alerts/snooze"]({"key": "runner:N", "hours": 0})
        self.assertEqual(set(live.alerts.snoozed), {"group:Acme/Game"})
        self.assertIn("alerts: alert runner:N snoozed 4 h (from the window)", self.lines())

    def test_a_new_build_ignores_who_sent_it(self):
        with self.assertRaises(ValueError):   # no build machines: refused, and `_from` never reaches the row
            self.acts["/api/builds/add"]({"version": "0.4.6", "_from": "the window"})

    def test_live_flags_a_silent_worker_and_a_job_nobody_takes(self):
        self.app.machines = SimpleNamespace(machines=FakeMachines())
        keys = [b["key"] for b in self.app.machine_blockers()]
        self.assertEqual(keys, ["worker:NODE-01", "job:3"])


    def test_live_shows_the_builds_this_backend_runs(self):
        self.app.machines = SimpleNamespace(machines=FakeMachines())
        self.assertEqual(self.app.machine_jobs(), [{"machine": "NODE-02", "kind": "building", "since": HOUR_AGO,
                                                    "title": "Fletchery build 0.4.6 (job #9)",
                                                    "detail": "step: Package 0.4.6 · from main"}])


if __name__ == "__main__":
    unittest.main()
