"""Alerts (alerts.py): the wait before firing, the hold after, groups, snoozes, saved state; and what reaches the phone
(push.py marks and bundle) and the disk forecast (rules.disk_forecast)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import alerts  # noqa: E402
import push  # noqa: E402
import rules  # noqa: E402
import store  # noqa: E402

T = 1_800_000_000.0


def sig(key, kind="stuck", repo="", title=None):
    return {"key": key, "kind": kind, "repo": repo, "title": title or key, "detail": "d"}


class RulesTest(unittest.TestCase):
    def test_pending_then_firing(self):
        a = alerts.Alerts()
        out = a.evaluate([sig("disklow:NODE", "disk")], T)
        self.assertEqual([r["key"] for r in out["pending"]], ["disklow:NODE"])
        self.assertEqual(out["pending"][0]["fires_in"], 300)
        self.assertEqual(out["firing"], [])
        out = a.evaluate([sig("disklow:NODE", "disk")], T + 299)
        self.assertEqual(out["firing"], [])
        out = a.evaluate([sig("disklow:NODE", "disk")], T + 300)
        self.assertEqual([r["key"] for r in out["firing"]], ["disklow:NODE"])
        self.assertEqual(out["firing"][0]["group"], "machine:NODE")

    def test_a_blip_never_fires(self):
        a = alerts.Alerts()
        a.evaluate([sig("feed", "feed")], T)
        out = a.evaluate([], T + 60)
        self.assertEqual(out, {"firing": [], "pending": [], "cleared": []})

    def test_keep_firing_then_cleared_then_forgotten(self):
        a = alerts.Alerts()
        a.evaluate([sig("step:r:1", "stuck", "Acme/Game")], T)
        self.assertEqual(len(a.evaluate([], T + 60)["firing"]), 1)          # held 120 s after it goes
        self.assertEqual(len(a.evaluate([sig("step:r:1", "stuck", "Acme/Game")], T + 90)["firing"]), 1)   # came back: same alert
        out = a.evaluate([], T + 90 + 120)
        self.assertEqual([r["key"] for r in out["cleared"]], ["step:r:1"])
        self.assertEqual(out["cleared"][0]["group"], "Acme/Game")
        self.assertEqual(a.evaluate([], T + 90 + 120 + alerts.RESOLVED_SHOWN + 1)["cleared"], [])

    def test_fires_again_after_clearing(self):
        a = alerts.Alerts()
        a.evaluate([sig("k", "check")], T)
        a.evaluate([], T + 10)
        out = a.evaluate([sig("k", "check")], T + 20)
        self.assertEqual(out["firing"][0]["fired"], T + 20)

    def test_profile_rules(self):
        a = alerts.Alerts(conf={"check": {"for": 2}, "runner": {"off": True}, "flow": {"notify": False}, "disk": {"for": "x"}},
                          log=(lines := []).append)
        out = a.evaluate([sig("c", "check"), sig("runner:N", "runner"), sig("flow:r", "flow")], T)
        self.assertEqual([r["key"] for r in out["pending"]], ["c"])
        self.assertEqual([r["notify"] for r in out["firing"]], [False])
        self.assertEqual(a.rule("disk")["for"], 300)
        self.assertTrue(any("disk" in x for x in lines))

    def test_snooze_one_and_group(self):
        a = alerts.Alerts()
        a.evaluate([sig("a", "check", "Acme/Game"), sig("b", "check", "Acme/Game"), sig("c", "check", "Acme/Tool")], T)
        a.snooze(key="a", hours=1, now=T)
        out = a.evaluate([sig("a", "check", "Acme/Game"), sig("b", "check", "Acme/Game"), sig("c", "check", "Acme/Tool")], T + 10)
        snoozed = {r["key"] for r in out["firing"] if r.get("snoozed_until")}
        self.assertEqual(snoozed, {"a"})
        a.snooze(group="Acme/Game", hours=4, now=T)
        out = a.evaluate([sig("a", "check", "Acme/Game"), sig("b", "check", "Acme/Game"), sig("c", "check", "Acme/Tool")], T + 3601)
        self.assertEqual({r["key"] for r in out["firing"] if r.get("snoozed_until")}, {"a", "b"})
        a.snooze(group="Acme/Game", hours=0, now=T)
        out = a.evaluate([sig("a", "check", "Acme/Game")], T + 3602)
        self.assertNotIn("snoozed_until", out["firing"][0])
        with self.assertRaises(ValueError):
            a.snooze(key="a", hours=3)
        with self.assertRaises(ValueError):
            a.snooze(hours=1)

    def test_state_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "alerts.json"
            s = store.Store(None)
            self.addCleanup(s.close)
            a = alerts.Alerts(path, store=s)
            a.evaluate([sig("machine:N", "machine")], T)
            a.evaluate([sig("machine:N", "machine")], T + 200)
            a.snooze(key="machine:N", hours=24, now=T)
            b = alerts.Alerts(path)
            out = b.evaluate([sig("machine:N", "machine")], T + 300)
            self.assertEqual(out["firing"][0]["fired"], T + 200)       # not fired again
            self.assertTrue(out["firing"][0]["snoozed_until"])
            self.assertEqual([r[0] for r in s.query("SELECT kind FROM events")], ["alert-fired"])
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(alerts.Alerts(path).state, {})

    def test_groups(self):
        self.assertEqual(alerts.group_of({"repo": "Acme/Game", "key": "x"}), "Acme/Game")
        self.assertEqual(alerts.group_of({"key": "diskfc:NODE:E:"}), "machine:NODE")
        self.assertEqual(alerts.group_of({"key": "feed", "kind": "feed"}), "feed")


class PhoneTest(unittest.TestCase):
    def test_marks_and_bundle(self):
        view = {"live_alerts": [{"kind": "disk", "key": "disklow:N", "group": "machine:N", "title": "N: E: has 30 GB free", "detail": "d"},
                                {"kind": "runner", "key": "runner:N-pr", "group": "machine:N", "title": "Runner N-pr is offline", "detail": ""},
                                {"kind": "check", "key": "problem:x", "group": "Acme/Game", "title": "PR #4 fails", "detail": ""}],
                "live_cleared": [{"kind": "feed", "key": "feed", "group": "feed", "title": "GitHub can't be read", "cleared": T}]}
        m = push.marks(view)
        self.assertEqual(set(m), {"stuck:disklow:N", "stuck:runner:N-pr", "stuck:problem:x", f"resolved:feed:{int(T)}"})
        self.assertEqual(m["stuck:disklow:N"]["title"], "Disk")
        out = dict(push.bundle(list(m.items())))
        self.assertEqual(set(out), {"stuck-group:machine:N", "stuck:problem:x", f"resolved:feed:{int(T)}"})
        self.assertEqual(out["stuck-group:machine:N"]["title"], "2 alerts: N")
        self.assertIn("Runner N-pr is offline", out["stuck-group:machine:N"]["body"])
        self.assertIn("resolved", push.EVENTS)


class ForecastTest(unittest.TestCase):
    def test_filling_drive(self):
        # 100 GB free, losing 10 GB every 10 min: 5 GB floor in 95 min
        r = [(T - 3600 + i * 600, 100 + 60 - i * 10) for i in range(7)]
        self.assertAlmostEqual(rules.disk_forecast(r, T), 95 * 60, delta=1)

    def test_not_enough_or_not_filling(self):
        self.assertIsNone(rules.disk_forecast([(T - 60 * i, 50) for i in range(10)], T))           # flat
        self.assertIsNone(rules.disk_forecast([(T - 60 * i, 50 + i) for i in range(4)], T))       # too few
        self.assertIsNone(rules.disk_forecast([(T - 60 * i, 50 + i) for i in range(5)], T))       # too short a span
        self.assertIsNone(rules.disk_forecast([(T - 600 * i, 50 - i) for i in range(6)], T))      # freeing up
        self.assertEqual(rules.disk_forecast([(T - 600 * i, 4 + i) for i in range(6)], T), 0.0)   # already under the floor


if __name__ == "__main__":
    unittest.main()
