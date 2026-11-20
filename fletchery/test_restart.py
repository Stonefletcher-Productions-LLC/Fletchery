"""Tests for restart.py and its wiring: python fletchery/test_restart.py"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import devwatch  # noqa: E402
import restart  # noqa: E402


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, at=1_800_000_000.0):
        self.at = at

    def __call__(self):
        return self.at


class RestartsTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.r = restart.Restarts(clock=self.clock)
        self.boot = iso(self.clock.at - 86400)

    def test_a_waiting_request_is_held_until_the_machine_is_idle(self):
        rec = self.r.request("NODE", "claude", wait=True)
        self.assertEqual(self.r.view("NODE")["state"], "waiting")
        self.assertIsNone(self.r.poll("NODE", self.boot, idle=False))            # still building
        self.assertIsNone(self.r.poll("NODE", self.boot, via="guard"))           # the guard never releases it
        self.clock.at += 3 * 3600
        self.assertFalse(self.r.view("NODE")["overdue"])                         # waiting is not "not back"
        cmd = self.r.poll("NODE", self.boot, idle=True)                          # the build ended
        self.assertEqual(cmd["id"], rec["id"])
        view = self.r.view("NODE")
        self.assertEqual((view["state"], view["age"]), ("requested", 0))         # pending and overdue count from now
        self.clock.at += 90
        self.assertIsNone(self.r.poll("NODE", iso(self.clock.at - 20), idle=True))
        self.assertEqual(self.r.view("NODE")["state"], "back")

    def test_a_waiting_request_can_be_withdrawn_and_is_given_up_after_half_a_day(self):
        self.r.request("NODE", wait=True)
        self.assertTrue(self.r.cancel("NODE"))
        self.assertIsNone(self.r.poll("NODE", self.boot, idle=True))
        self.r.request("NODE", wait=True)
        self.clock.at += restart.WAIT_MAX + 1
        self.assertIsNone(self.r.poll("NODE", self.boot, idle=True))
        self.assertEqual(self.r.view("NODE")["state"], "expired")

    def test_request_reaches_the_machine_and_is_marked_back_after_the_boot(self):
        rec = self.r.request("NODE", "the window")
        cmd = self.r.poll("NODE", self.boot)
        self.assertEqual(cmd["id"], rec["id"])
        self.assertEqual(self.r.view("NODE")["state"], "requested")
        self.clock.at += 10
        self.r.poll("NODE", self.boot, ack=rec["id"])
        self.assertEqual(self.r.view("NODE")["state"], "sent")
        self.clock.at += 120
        self.assertIsNone(self.r.poll("NODE", iso(self.clock.at - 30)))     # it booted after the request
        view = self.r.view("NODE")
        self.assertEqual((view["state"], view["took"], view["overdue"]), ("back", 130, False))
        self.assertIsNone(self.r.poll("NODE", iso(self.clock.at - 30)))     # and is never told again: no loop

    def test_other_machines_and_old_reporters_get_nothing(self):
        self.r.request("NODE", "x")
        self.assertIsNone(self.r.poll("PC", self.boot))
        self.assertIsNone(self.r.poll("NODE", ""))          # no boot time: an old reporter
        self.assertEqual(self.r.view("NODE")["state"], "requested")

    def test_uncollected_request_expires_and_cannot_fire_later(self):
        self.r.request("NODE", "x")
        self.clock.at += restart.PENDING + 1
        self.assertIsNone(self.r.poll("NODE", self.boot))
        self.assertEqual(self.r.view("NODE")["state"], "expired")

    def test_sent_request_keeps_coming_for_the_guard_and_goes_overdue(self):
        rec = self.r.request("NODE", "x")
        self.r.poll("NODE", self.boot, ack=rec["id"])
        self.clock.at += restart.OVERDUE + 1
        self.assertEqual(self.r.poll("NODE", self.boot, via="guard")["id"], rec["id"])
        self.assertTrue(self.r.view("NODE")["overdue"])

    def test_cancel_only_before_collection(self):
        rec = self.r.request("NODE", "x")
        self.assertTrue(self.r.cancel("NODE"))
        self.assertIsNone(self.r.poll("NODE", self.boot))
        rec = self.r.request("NODE", "x")
        self.r.poll("NODE", self.boot, ack=rec["id"])
        self.assertFalse(self.r.cancel("NODE"))

    def test_survives_a_backend_restart(self):
        with tempfile.TemporaryDirectory() as d:
            a = restart.Restarts(Path(d) / "restarts.json", clock=self.clock)
            rec = a.request("PC", "the phone (alex)")
            b = restart.Restarts(Path(d) / "restarts.json", clock=self.clock)
            self.assertEqual(b.poll("PC", self.boot)["id"], rec["id"])
            self.clock.at += 200
            b.poll("PC", iso(self.clock.at - 60))
            self.assertEqual(restart.Restarts(Path(d) / "restarts.json", clock=self.clock).view("PC")["state"], "back")


class MachineSideTest(unittest.TestCase):
    def test_actor_restarts_once_per_request(self):
        calls = []
        actor = restart.Actor(restart=lambda delay, note: (calls.append((delay, note)) or (True, "ok")))
        cmd = {"id": "a1", "delay": 20, "requested": 4_000_000_000, "by": "the window"}
        self.assertEqual(actor.handle(cmd), "a1")
        self.assertEqual(actor.handle(cmd), "a1")
        self.assertEqual(calls, [(20, "the window")])

    def test_actor_ignores_a_request_older_than_this_boot_and_junk(self):
        calls = []
        actor = restart.Actor(restart=lambda delay, note: (calls.append(1) or (True, "ok")))
        self.assertEqual(actor.handle({"id": "old", "requested": 1000.0}), "")
        self.assertEqual(actor.handle("restart"), "")
        self.assertEqual(actor.handle({}), "")
        self.assertEqual(calls, [])

    def test_refused_restart_is_tried_again(self):
        answers = [(False, "denied"), (True, "ok")]
        actor = restart.Actor(restart=lambda delay, note: answers.pop(0))
        cmd = {"id": "a1", "requested": 4_000_000_000}
        self.assertEqual(actor.handle(cmd), "")
        self.assertEqual(actor.handle(cmd), "a1")

    def test_restart_command_forces_and_accepts_already_scheduled(self):
        seen = []
        ok, _ = restart.restart_now(20, "the window", run=lambda argv: (seen.append(argv) or SimpleNamespace(returncode=1190, stdout="", stderr="")))
        self.assertTrue(ok)
        if restart.WINDOWS:
            self.assertEqual(seen[0][:5], ["shutdown", "/r", "/f", "/t", "20"])
        ok, _ = restart.restart_now(20, run=lambda argv: SimpleNamespace(returncode=5, stdout="", stderr="Access is denied."))
        self.assertFalse(ok)

    def test_boot_time_is_steady_and_in_the_past(self):
        a, b = restart.boot_time(), restart.boot_time()
        self.assertTrue(a)
        self.assertLessEqual(abs(restart._ts(a) - restart._ts(b)), 4)
        self.assertLess(restart._ts(a), datetime.now(timezone.utc).timestamp())

    def test_guard_goes_hard_when_the_machine_stays_up(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "keys" / "k"
            key.parent.mkdir()
            key.write_text("k")
            cfg = {"keyFile": str(key), "coordinator": "http://x", "machine": "NODE"}
            clock = Clock(4_000_000_100.0)
            hard, polite, acks, ticks = [], [], [], []
            actor = restart.Actor(restart=lambda delay, note: (polite.append(1) or (True, "ok")), clock=clock)

            def call(cfg, body):
                acks.append(body["ack"])
                return {"restart": {"id": "g1", "requested": 4_000_000_000.0, "delay": 20}}

            def sleep(_):      # the machine stays up: time passes between the guard's questions
                ticks.append(1)
                clock.at += restart.HARD_AFTER + 1
                if len(ticks) >= 2:
                    raise KeyboardInterrupt

            real = restart.boot_time
            restart.boot_time = lambda: iso(3_900_000_000.0)      # booted before the request
            try:
                with self.assertRaises(KeyboardInterrupt):
                    restart.guard(cfg, call=call, hard=lambda: (hard.append(1) or "asked"), clock=clock, sleep=sleep, actor=actor)
            finally:
                restart.boot_time = real
            self.assertEqual(polite, [1])
            self.assertEqual(hard, [1])
            self.assertEqual(acks[:2], ["", "g1"])
            self.assertIn("the hard way", (Path(d) / "fletchery-restart-guard.log").read_text())


class WiringTest(unittest.TestCase):
    def watch(self):
        w = devwatch.DevWatch({"names": {"ALEX-PC": "Gaming PC"}}, github=None)
        return w

    def report(self, w, name="GAMING-PC", **over):
        data = {"version": "6", "host": "ALEX-PC", "at": iso(datetime.now(timezone.utc).timestamp()), "lease": None, "queue": [],
                "activities": [], "procs": {}, "stats": {}, "busy": False, "boot": iso(datetime.now(timezone.utc).timestamp() - 3600),
                "restart_ack": "", "after_restart": {"signs_in": False, "guard": True}}
        data.update(over)
        return w.report(name, data, "127.0.0.1")

    def test_button_to_reporter_round_trip(self):
        w = self.watch()
        self.assertNotIn("restart", self.report(w))
        out = w.restart_machine("Gaming PC", "the window")          # by display name
        self.assertIn("does not sign in by itself", out["message"])
        answer = self.report(w)
        self.assertEqual(answer["restart"]["id"], out["id"])
        self.report(w, restart_ack=out["id"])
        machines = w._machines(datetime.now(timezone.utc).timestamp(), [], lambda *a, **k: None)
        m = machines[0]
        self.assertEqual((m["key"], m["can_restart"], m["restart"]["state"]), ("GAMING-PC", True, "sent"))
        self.assertEqual(m["after_restart"], {"signs_in": False, "guard": True})
        self.report(w, boot=iso(datetime.now(timezone.utc).timestamp() + 60))
        self.assertEqual(w.restarts.view("GAMING-PC")["state"], "back")

    def test_restart_when_idle_waits_for_the_build_and_can_be_withdrawn(self):
        w = self.watch()
        lease = {"purpose": "PR check #12 (GitHub run 5)", "since": iso(datetime.now(timezone.utc).timestamp() - 60), "alive": True}
        self.report(w, lease=lease, busy=True)
        out = w.restart_machine("GAMING-PC", "claude", when_idle=True)
        self.assertTrue(out["waiting"])
        self.assertNotIn("restart", self.report(w, lease=lease, busy=True))      # the build is still running
        self.assertNotIn("restart", self.report(w, busy=True))                   # a runner job with no lease counts too
        self.assertEqual(self.report(w)["restart"]["id"], out["id"])             # idle: now it is told
        w.restart_machine("GAMING-PC", "claude", force=True)
        self.report(w, lease=lease, busy=True)
        w.restart_machine("GAMING-PC", "claude", when_idle=True)
        self.assertTrue(w.restart_machine("GAMING-PC", "claude", cancel=True)["ok"])
        self.assertNotIn("restart", self.report(w))
        with self.assertRaisesRegex(ValueError, "no restart request"):
            w.restart_machine("GAMING-PC", "claude", cancel=True)

    def test_busy_machine_needs_force_and_unknown_or_old_machines_are_refused(self):
        w = self.watch()
        lease = {"purpose": "PR check #12 (GitHub run 5)", "since": iso(datetime.now(timezone.utc).timestamp() - 60), "alive": True}
        self.report(w, lease=lease)
        with self.assertRaisesRegex(ValueError, "A restart ends it"):
            w.restart_machine("GAMING-PC", "claude")
        self.assertTrue(w.restart_machine("GAMING-PC", "claude", force=True)["ok"])
        # a hold whose process is gone is exactly when a restart is wanted: no force needed
        w2 = self.watch()
        self.report(w2, lease={**lease, "alive": False})
        self.assertTrue(w2.restart_machine("alex-pc", "claude")["ok"])
        with self.assertRaisesRegex(ValueError, "No reporting machine"):
            w2.restart_machine("NOPE")
        w3 = self.watch()
        self.report(w3, boot="")
        with self.assertRaisesRegex(ValueError, "older reporter"):
            w3.restart_machine("GAMING-PC")

    def test_overdue_restart_is_flagged(self):
        w = self.watch()
        self.report(w)
        out = w.restart_machine("GAMING-PC", "the window")
        w.restarts.records["GAMING-PC"]["requested"] -= restart.OVERDUE + 60
        w.restarts.records["GAMING-PC"]["state"] = "sent"
        blocks = []
        w._machines(datetime.now(timezone.utc).timestamp(), [], lambda kind, title, detail="", **k: blocks.append((kind, title, k.get("key"))))
        hit = [b for b in blocks if str(b[2]).startswith("restart:")]
        self.assertEqual(len(hit), 1)
        self.assertRegex(hit[0][1], "Gaming PC was told to restart .* and is not back")

    def test_guard_endpoint(self):
        import machines
        with tempfile.TemporaryDirectory() as d:
            ms = machines.Machines(d)
            api = machines.WorkerApi(ms, "127.0.0.1:0", log=lambda *a: None)
            w = self.watch()
            api.reports = w
            self.report(w, name="NODE")
            out = w.restart_machine("NODE", "the window")
            boot = iso(datetime.now(timezone.utc).timestamp() - 3600)
            code, body = api._dispatch("/v1/restart", "NODE", "127.0.0.1", {"boot": boot, "ack": ""})
            self.assertEqual((code, body["restart"]["id"]), (200, out["id"]))
            code, body = api._dispatch("/v1/restart", "OTHER", "127.0.0.1", {"boot": boot})
            self.assertEqual((code, body), (200, {"restart": None}))


if __name__ == "__main__":
    unittest.main()
