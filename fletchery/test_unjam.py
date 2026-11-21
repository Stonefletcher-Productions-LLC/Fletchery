"""Tests for unjam.py: python fletchery/test_unjam.py"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import recheck  # noqa: E402
import unjam  # noqa: E402

REPO = "Acme/Game"
T0 = 1_800_000_000.0


class FakeGitHub:
    """runs: run id -> (status, seconds after T0 it was last updated)."""
    def __init__(self, runs):
        self.runs, self.calls = dict(runs), []

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        self.calls.append(path)
        repo, run = path.split("/actions/runs/")
        if repo != f"repos/{REPO}" or run not in self.runs:
            raise RuntimeError("404")
        status, at = self.runs[run]
        return {"status": status, "updated_at": unjam.datetime.fromtimestamp(T0 + at, unjam.timezone.utc).isoformat()}


def machine(name, title, signs_in=True, restart=None, online=True):
    return {"name": name, "online": online, "after_restart": {} if signs_in is None else {"signs_in": signs_in},
            "restart": restart, "activities": [{"kind": "queued", "title": "local run Tools\\BuildTown.ps1", "position": 1},
                                               {"kind": "check", "title": title, "since": "2026-11-21T03:40:07Z",
                                                "reservation_holder": True, "alive": True}]}


HELD = "PR check #2399 (GitHub run 77): Tools\\CI\\PrCheckJob.ps1"


class UnjamTest(unittest.TestCase):
    def make(self, state, runs, **k):
        self.gh = FakeGitHub(runs)
        self.lines, self.restarts, self.at = [], [], [T0 + 3600]

        def restart(name, reason):
            self.restarts.append((name, reason))
            return f"{name} restarts in under a minute."
        k.setdefault("restart", restart)
        return unjam.Unjam(self.gh, self.lines.append, snapshot=lambda: state["snap"], repos=lambda: ["Other/Repo", REPO],
                           clock=lambda: self.at[0], **k)

    def test_a_running_check_or_one_that_just_finished_is_left_alone(self):
        state = {"snap": {"machines": [machine("NODE", HELD)]}}
        u = self.make(state, {"77": ("in_progress", 0)})
        self.assertEqual(u.round(), [])
        self.gh.runs["77"] = ("completed", 3600 - 300)          # finished five minutes ago: its own cleanup may still run
        self.assertEqual(u.round(), [])
        self.assertEqual((u.blockers(), self.restarts), ([], []))

    def test_a_machine_that_signs_in_is_restarted_on_the_second_sighting_and_only_once(self):
        state = {"snap": {"machines": [machine("NODE", HELD), machine("Gaming PC", "Tools\\BuildTown.ps1")]}}
        u = self.make(state, {"77": ("completed", 0)})
        first = u.round()
        self.assertEqual([(j["machine"], j["pr"], j["rounds"]) for j in first], [("NODE", 2399, 1)])
        self.assertEqual(self.restarts, [])                      # one sighting is not enough to restart a machine
        self.at[0] += unjam.EVERY
        u.round()
        self.assertEqual(self.restarts, [("NODE", "PR check #2399 finished but still holds the machine")])
        row, = u.blockers()
        self.assertEqual((row["kind"], row["key"], row["repo"]), ("stuck", "jam:node:77", REPO))
        self.assertIn("finished 62 min ago but still holds the machine", row["title"])
        self.assertIn("Fletchery is restarting it", row["detail"])
        self.at[0] += unjam.EVERY                                # held again after the restart: say so, do not loop
        u.round()
        self.assertEqual(len(self.restarts), 1)
        self.assertIn("will not restart it a second time", u.blockers()[0]["detail"])
        self.at[0] += unjam.AGAIN
        u.round()
        self.assertEqual(len(self.restarts), 2)

    def test_a_machine_that_does_not_sign_in_is_flagged_and_never_restarted(self):
        for signs_in in (False, None):
            state = {"snap": {"machines": [machine("Gaming PC", HELD, signs_in=signs_in)]}}
            u = self.make(state, {"77": ("completed", 0)})
            for _ in range(3):
                u.round()
                self.at[0] += unjam.EVERY
            self.assertEqual(self.restarts, [])
            self.assertIn("elevated Task Manager", u.blockers()[0]["detail"])

    def test_off_switch_a_restart_under_way_and_a_refused_restart(self):
        state = {"snap": {"machines": [machine("NODE", HELD)]}}
        u = self.make(state, {"77": ("completed", 0)}, enabled=False)
        u.round(); u.round()
        self.assertEqual(self.restarts, [])
        self.assertIn("turned off", u.blockers()[0]["detail"])

        state["snap"] = {"machines": [machine("NODE", HELD, restart={"state": "sent"})]}
        u = self.make(state, {"77": ("completed", 0)})
        u.round(); u.round()
        self.assertEqual(self.restarts, [])
        self.assertIn("It is restarting", u.blockers()[0]["detail"])

        def refuse(name, reason):
            raise ValueError("NODE is not reporting")
        state["snap"] = {"machines": [machine("NODE", HELD)]}
        u = self.make(state, {"77": ("completed", 0)}, restart=refuse)
        u.round(); u.round()
        self.assertIn("could not restart it", u.blockers()[0]["detail"])
        self.assertEqual(u.restarted, {})                        # a refused restart does not use up the one allowed

    def test_cleared_jam_goes_away_and_is_logged_and_state_survives_a_restart_of_fletchery(self):
        state = {"snap": {"machines": [machine("NODE", HELD)]}}
        with tempfile.TemporaryDirectory() as tmp:
            u = self.make(state, {"77": ("completed", 0)}, state_dir=tmp)
            u.round()
            self.at[0] += unjam.EVERY
            u.round()
            state["snap"] = {"machines": [machine("NODE", "PR check #2400 (GitHub run 78): Tools\\CI\\PrCheckJob.ps1")]}
            self.gh.runs["78"] = ("in_progress", 0)
            self.assertEqual(u.round(), [])
            self.assertEqual(u.blockers(), [])
            self.assertTrue(any("no longer held" in line for line in self.lines))
            again = unjam.Unjam(self.gh, self.lines.append, state_dir=tmp)
            self.assertEqual(list(again.restarted), ["node"])

    def test_a_rerun_of_the_same_run_is_not_a_jam_and_tick_keeps_its_pace(self):
        state = {"snap": {"machines": [machine("NODE", HELD)]}}
        u = self.make(state, {"77": ("in_progress", 0)})
        u.tick(); u.tick()
        self.assertEqual(len([c for c in self.gh.calls if c.endswith("/77")]), 2)   # one round: Other/Repo, then Acme/Game
        self.assertEqual(u.jams, {})

    def test_recheck_holds_reruns_for_a_jammed_machine(self):
        snap = {"machines": [{"name": "NODE", "host": "NODE", "key": "NODE", "online": True}],
                "blockers": [{"key": "jam:node:77", "title": "NODE: PR check #2399 finished 62 min ago but still holds the machine"}]}
        self.assertIn("node", recheck.Recheck._unfit(snap))


if __name__ == "__main__":
    unittest.main()
