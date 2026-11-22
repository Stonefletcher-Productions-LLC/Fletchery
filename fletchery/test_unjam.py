"""Tests for unjam.py: python fletchery/test_unjam.py"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import recheck  # noqa: E402
import unjam  # noqa: E402

REPO = "Acme/Game"
T0 = 1_800_000_000.0


def iso(ts):
    return unjam.datetime.fromtimestamp(ts, unjam.timezone.utc).isoformat()


class FakeGitHub:
    """runs: run id -> (status, seconds after T0 it ended) for a run with one attempt and one job, or
    run id -> {attempt number: [(job status, seconds after T0 the job ended), ...]} for a run with re-runs.
    The run says what its newest attempt says, as GitHub does; `says` overrides that for one run id."""
    def __init__(self, runs):
        self.runs, self.calls, self.says, self.broken, self.unlisted = dict(runs), [], {}, "", 0

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        self.calls.append(path)
        repo, rest = path.split("/actions/runs/")
        run, _, tail = rest.partition("/")
        if repo != f"repos/{REPO}" or run not in self.runs or (self.broken and self.broken in path):
            raise RuntimeError("404")
        attempts = self.runs[run] if isinstance(self.runs[run], dict) else {1: [self.runs[run]]}
        newest = max(attempts)
        if not tail:
            done = all(status == "completed" for status, _ in attempts[newest])
            return {"status": self.says.get(run) or ("completed" if done else "in_progress"), "run_attempt": newest,
                    "updated_at": iso(T0 + max(at for _, at in attempts[newest]))}
        n = int(re.fullmatch(r"attempts/(\d+)/jobs\?per_page=100", tail).group(1))
        return {"total_count": len(attempts[n]) + self.unlisted,
                "jobs": [{"status": status, "run_attempt": n, "completed_at": iso(T0 + at) if status == "completed" else None}
                         for status, at in attempts[n]]}


def machine(name, title, signs_in=True, restart=None, online=True, procs=None, since="2026-11-21T03:40:07Z"):
    return {"name": name, "online": online, "after_restart": {} if signs_in is None else {"signs_in": signs_in},
            "restart": restart, "procs": procs or {}, "runners": [f"{name}-unreal-pr"],
            "activities": [{"kind": "queued", "title": "local run Tools\\BuildTown.ps1", "position": 1},
                           {"kind": "check", "title": title, "since": since,
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
        return unjam.Unjam(self.gh, self.lines.append, repos=lambda: ["Other/Repo", REPO], clock=lambda: self.at[0],
                           snapshot=lambda: {"fresh": {"github": iso(self.at[0] - 20), "error": ""}, **state["snap"]}, **k)

    def test_a_running_check_or_one_that_just_finished_is_left_alone(self):
        state = {"snap": {"machines": [machine("NODE", HELD)]}}
        u = self.make(state, {"77": ("in_progress", 0)})
        self.assertEqual(u.round(), [])
        self.gh.runs["77"] = ("completed", 3600 - 300)          # finished five minutes ago: its own cleanup may still run
        self.assertEqual(u.round(), [])
        self.assertEqual((u.blockers(), self.restarts), ([], []))

    def test_a_machine_that_signs_in_is_restarted_on_the_second_sighting_and_only_once(self):
        # The editor the check left behind is what a restart clears, and another machine's work is not this one's.
        state = {"snap": {"machines": [machine("NODE", HELD, procs={"unreal": 1, "claude": 2}), machine("Gaming PC", "Tools\\BuildTown.ps1")],
                          "runners": [{"name": "Gaming PC-unreal-pr", "busy": True, "job": {"id": 9}},
                                      {"name": "NODE-unreal-pr", "busy": False, "job": None}]}}
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

    def test_a_rerun_is_judged_by_its_latest_attempt_not_by_an_earlier_one_that_finished(self):
        # 2026-11-22 (#284): attempt 16 ended on the node and was seen there once, in its own cleanup. Two hours later
        # attempt 17 was compiling on the PC under the same run id, and the PC was restarted for "finished 125 min ago".
        state = {"snap": {"machines": [machine("NODE", HELD)]}}
        u = self.make(state, {"77": {16: [("completed", 3600 - 60)]}})
        self.assertEqual(u.round(), [])
        self.gh.runs["77"] = {16: [("completed", 3600 - 60)], 17: [("completed", 100), ("in_progress", 0)]}
        state["snap"] = {"machines": [machine("NODE", "Tools\\BuildTown.ps1"), machine("PC", HELD, since=iso(T0 + 3600 + 7000))]}
        self.at[0] += 125 * 60
        for _ in range(3):
            self.assertEqual(u.round(), [])
            self.at[0] += unjam.EVERY
        self.assertEqual((u.blockers(), self.restarts, self.lines), ([], [], []))

        ended = self.at[0] - T0                                  # attempt 17 ends: only now does the wait for a jam start
        self.gh.runs["77"][17] = [("completed", 100), ("completed", ended)]
        self.at[0] += unjam.QUIET - 60
        self.assertEqual(u.round(), [])
        self.at[0] += unjam.EVERY
        jam, = u.round()
        self.assertEqual((jam["machine"], jam["attempt"], jam["finished"]), ("PC", 17, T0 + ended))
        self.assertIn("finished 11 min ago", u.blockers()[0]["title"])

    def test_what_github_cannot_prove_finished_is_not_a_jam(self):
        state = {"snap": {"machines": [machine("NODE", HELD)]}}
        u = self.make(state, {"77": {1: [("completed", 0)], 2: [("completed", 0), ("queued", 0)]}})
        self.gh.says["77"] = "completed"                         # the run says finished while a job of its latest attempt waits
        self.assertEqual(u.round(), [])
        self.gh.runs["77"] = ("completed", 0)
        self.gh.broken = "/jobs"                                 # the jobs cannot be read
        self.assertEqual(u.round(), [])
        self.gh.broken, self.gh.unlisted = "", 1                 # not every job was listed
        self.assertEqual(u.round(), [])
        self.gh.unlisted = 0
        state["snap"] = {"machines": [machine("NODE", HELD, since=iso(T0 + 60))]}   # held since after that attempt ended
        self.assertEqual(u.round(), [])
        self.assertEqual((u.blockers(), self.restarts), ([], []))
        state["snap"] = {"machines": [machine("NODE", HELD)]}
        self.assertEqual(len(u.round()), 1)

    def test_a_jammed_machine_is_not_restarted_while_anything_works_on_it(self):
        idle = {"name": "NODE-unreal-pr", "busy": False, "job": None}
        for what, change in (("Its runner NODE-unreal-pr is running a job", {"runners": [{**idle, "busy": True}]}),
                             ("Its runner NODE-unreal-pr is running a job", {"runners": [{**idle, "job": {"id": 5}}]}),
                             ("Runner.Worker", {"machines": [machine("NODE", HELD, procs={"runner": 1})]}),
                             ("A compiler is running", {"machines": [machine("NODE", HELD, procs={"compiler": 3})]}),
                             ("GitHub cannot be read", {"fresh": {"github": iso(T0 + 3600), "error": "Acme/Game: timed out"}}),
                             ("GitHub cannot be read", {"fresh": {"github": iso(T0 + 3600 - unjam.STALE - 60), "error": ""}})):
            quiet = {"machines": [machine("NODE", HELD)], "runners": [idle]}
            state = {"snap": {**quiet, **change}}
            u = self.make(state, {"77": ("completed", 0)})
            for _ in range(3):
                jam, = u.round()
                self.at[0] += unjam.EVERY
            self.assertEqual((self.restarts, jam["rounds"], jam["idle"]), ([], 3, 0), what)
            detail = u.blockers()[0]["detail"]
            self.assertIn(what, detail)
            self.assertIn("will not restart it until nothing runs there", detail)
            self.assertLessEqual(len(detail), 300, what)             # Live cuts a longer one short
            self.assertEqual(len([line for line in self.lines if "not restarting NODE" in line]), 1, what)
            state["snap"] = quiet                                # it stops: idle must be seen twice in a row from here
            u.round()
            self.assertEqual(self.restarts, [], what)
            self.assertIn("on the next check", u.blockers()[0]["detail"])
            self.at[0] += unjam.EVERY
            u.round()
            self.assertEqual(len(self.restarts), 1, what)

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
