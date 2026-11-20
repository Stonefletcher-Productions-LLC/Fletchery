"""Tests for recheck.py: python fletchery/test_recheck.py"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import devwatch  # noqa: E402
import recheck  # noqa: E402

REPO = "Acme/Game"


class FakeGitHub:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        self.calls.append((method, path))
        if path in self.fail:
            raise RuntimeError("403")
        return {}


def group(cause, runner, prs, tests=("No result: the machine stayed busy",)):
    return {"cause": cause, "check": "PR check / Compile and test", "repo": REPO, "runner": runner, "tests": list(tests),
            "prs": [{"repo": REPO, "number": n, "job": j, "check": "PR check / Compile and test"} for n, j in prs]}


def snap(problems, blockers=(), machines=None, partial=False):
    return {"pr_count_status": {"partial": partial}, "fresh": {"error": ""}, "problems": list(problems), "blockers": list(blockers),
            "runners": [{"name": "MY-PC-unreal-pr", "machine": "Gaming PC"}, {"name": "NODE-unreal-pr", "machine": "NODE"}],
            "machines": machines or [{"name": "Gaming PC", "host": "MY-PC", "key": "GAMING-PC", "online": True},
                                     {"name": "NODE", "host": "NODE", "key": "NODE", "online": True}]}


class RecheckTest(unittest.TestCase):
    def make(self, state, gh=None, **k):
        self.gh = gh or FakeGitHub()
        self.lines = []
        self.at = [1_800_000_000.0]
        return recheck.Recheck(self.gh, self.lines.append, snapshot=lambda: state["snap"], clock=lambda: self.at[0], **k)

    def test_reruns_machine_failures_only_and_once_per_job(self):
        state = {"snap": snap([group("machine", "NODE-unreal-pr", [(11, 101), (12, 102)]),
                               group("own", "NODE-unreal-pr", [(13, 103)]), group("cancelled", "", [(14, 104)]),
                               group("base", "NODE-unreal-pr", [(15, 105)])])}
        r = self.make(state)
        out = r.round()
        self.assertEqual([(d["pr"], d["job"]) for d in out], [(11, 101), (12, 102)])
        self.assertEqual(self.gh.calls, [("POST", f"repos/{REPO}/actions/jobs/101/rerun"), ("POST", f"repos/{REPO}/actions/jobs/102/rerun")])
        self.assertEqual(r.round(), [])                     # the same failed job is never re-run twice
        self.assertIn("re-run: #11", self.lines[0])

    def test_leaves_checks_alone_while_their_machine_is_in_trouble(self):
        dead = {"key": "deadhold:MY-PC:2026-11-20T17:32:29Z", "title": "Gaming PC: a run holds the machine but its process is gone"}
        state = {"snap": snap([group("machine", "MY-PC-unreal-pr", [(11, 101)], tests=("Cleanup failed",)),
                               group("machine", "NODE-unreal-pr", [(12, 102)])], blockers=[dead])}
        r = self.make(state)
        self.assertEqual([d["pr"] for d in r.round()], [12])
        self.assertRegex(r.view()["waiting"][0]["why"], "Gaming PC is in trouble")
        state["snap"] = snap([group("machine", "MY-PC-unreal-pr", [(11, 101)])])     # the hold cleared
        self.assertEqual([d["pr"] for d in r.round()], [11])

    def test_waits_while_the_base_branch_is_red(self):
        s = snap([{**group("machine", "NODE-unreal-pr", [(11, 101)]), "base": "Test-Staging"}])
        s["bases"] = [{"repo": REPO, "branch": "Test-Staging", "state": "red", "required": True}]
        state = {"snap": s}
        r = self.make(state)
        self.assertEqual(r.round(), [])
        self.assertRegex(r.view()["waiting"][0]["why"], "Test-Staging is red")
        s["bases"][0]["state"] = "green"
        self.assertEqual([d["pr"] for d in r.round()], [11])

    def test_restarting_or_silent_machine_gets_no_reruns(self):
        machines = [{"name": "Gaming PC", "host": "MY-PC", "key": "GAMING-PC", "online": True, "restart": {"state": "sent"}},
                    {"name": "NODE", "host": "NODE", "key": "NODE", "online": False}]
        state = {"snap": snap([group("machine", "MY-PC-unreal-pr", [(11, 101)]), group("machine", "NODE-unreal-pr", [(12, 102)])], machines=machines)}
        self.assertEqual(self.make(state).round(), [])

    def test_limits_per_round_and_per_pr_per_day(self):
        state = {"snap": snap([group("machine", "NODE-unreal-pr", [(n, 100 + n) for n in range(1, 6)])])}
        r = self.make(state)
        self.assertEqual(len(r.round()), recheck.PER_TICK)
        self.assertEqual(len(r.round()), 2)
        # PR 1 fails for the machine again and again: two automatic re-runs a day, then it waits for a person
        state["snap"] = snap([group("machine", "NODE-unreal-pr", [(1, 201)])])
        self.assertEqual(len(r.round()), 1)
        state["snap"] = snap([group("machine", "NODE-unreal-pr", [(1, 202)])])
        self.assertEqual(r.round(), [])
        self.assertRegex(r.view()["waiting"][0]["why"], "needs a look")
        self.at[0] += recheck.DAY + 1
        self.assertEqual(len(r.round()), 1)

    def test_partial_read_decides_nothing_and_off_switch(self):
        state = {"snap": snap([group("machine", "NODE-unreal-pr", [(11, 101)])], partial=True)}
        self.assertEqual(self.make(state).round(), [])
        state["snap"] = snap([group("machine", "NODE-unreal-pr", [(11, 101)])])
        r = self.make(state, enabled=False)
        r.tick()
        self.assertEqual(self.gh.calls, [])

    def test_refused_rerun_is_asked_once_and_state_survives(self):
        with tempfile.TemporaryDirectory() as d:
            state = {"snap": snap([group("machine", "NODE-unreal-pr", [(11, 101), (12, 102)])])}
            gh = FakeGitHub(fail={f"repos/{REPO}/actions/jobs/101/rerun"})
            r = self.make(state, gh=gh, state_dir=d)
            self.assertEqual([x["pr"] for x in r.round()], [12])
            again = recheck.Recheck(gh, self.lines.append, state_dir=d, snapshot=lambda: state["snap"], clock=lambda: self.at[0])
            self.assertEqual(again.round(), [])
            self.assertEqual(len(gh.calls), 2)

    def test_tick_runs_a_round_every_five_minutes(self):
        state = {"snap": snap([group("machine", "NODE-unreal-pr", [(11, 101)])])}
        r = self.make(state)
        r.tick()
        state["snap"] = snap([group("machine", "NODE-unreal-pr", [(12, 102)])])
        r.tick()
        self.assertEqual(len(self.gh.calls), 1)
        self.at[0] += recheck.EVERY + 1
        r.tick()
        self.assertEqual(len(self.gh.calls), 2)


class FlowFreshTest(unittest.TestCase):
    def test_untouched_prs_do_not_count_as_waiting(self):
        w = devwatch.DevWatch(github=None)
        seen = {}
        w.flow.compute = lambda repos, waiting, at, **k: seen.update(waiting) or {"all": {}, "repos": {}}
        at = 1_800_000_000.0
        iso = lambda ts: devwatch.now_iso_at(ts)
        pr = lambda repo, age_days: {"repo": repo, "draft": False, "checks": {"state": "passed"}, "updated": iso(at - age_days * 86400)}
        w.flow_now([pr("A/quiet", 20), pr("A/quiet", 30), pr("A/busy", 1), pr("A/busy", 10)], at)
        self.assertEqual(seen, {"A/busy": 1})


if __name__ == "__main__":
    unittest.main()
