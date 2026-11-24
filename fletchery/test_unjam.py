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


def stuck_machine(name, locked=1, presence=None, procs=None, signs_in=True, restart=None):
    """A machine whose PR checkout's Saved\\StuckFiles holds `locked` files a process will not let go of (LifeShare
    #2530), with nothing holding its lease: the check that hit it moved them aside and went on."""
    return {"name": name, "online": True, "after_restart": {"signs_in": signs_in}, "restart": restart, "procs": procs or {},
            "runners": [f"{name}-unreal-pr"], "activities": [], "presence": presence,
            "stuck": {"locked": locked, "files": locked, "unknown": 0, "sample": "20261123T204400Z/Binaries/Win64/x.dll"}}


AWAY = {"attended": True, "input_idle": unjam.AWAY + 60, "games": []}


class StuckProcessTest(unittest.TestCase):
    """The second jam signal (files a process that cannot be ended still holds) and the guard on an attended PC."""

    def make(self, machines, runners=()):
        self.lines, self.restarts, self.at = [], [], [T0]
        self.snap = {"machines": machines, "runners": list(runners)}

        def restart(name, reason):
            self.restarts.append((name, reason))
            return f"{name} restarts as soon as it has finished what it is running."
        return unjam.Unjam(FakeGitHub({}), self.lines.append, restart=restart, repos=lambda: [REPO], clock=lambda: self.at[0],
                           snapshot=lambda: {"fresh": {"github": iso(self.at[0] - 20), "error": ""}, **self.snap})

    def rounds(self, u, n):
        out = None
        for _ in range(n):
            out = u.round()
            self.at[0] += unjam.EVERY
        return out

    def test_files_held_for_ten_minutes_are_a_jam_and_the_node_is_restarted_when_idle(self):
        u = self.make([stuck_machine("SF-NODE-01")])
        self.assertEqual(self.rounds(u, 5), [])                  # 0 to 8 min: the job's own cleanup may still free them
        self.assertEqual(self.restarts, [])
        jams = self.rounds(u, 1)                                 # 10 min: a jam, first sighting
        self.assertEqual([(j["machine"], j["kind"], j["locked"]) for j in jams], [("SF-NODE-01", "stuck-files", 1)])
        self.assertEqual(self.restarts, [])
        self.rounds(u, 1)
        self.assertEqual(len(self.restarts), 1)
        self.assertIn("still held by a process that cannot be ended", self.restarts[0][1])
        row, = u.blockers()
        self.assertEqual(row["key"], "jam:sf-node-01:stuck")
        self.assertIn("has held 1 file(s) of its PR checkout", row["title"])
        self.assertIn("Fletchery is restarting it", row["detail"])
        self.assertIn("sf-node-01", u.notes())
        self.rounds(u, 3)                                        # still there after the restart: once per AGAIN, no loop
        self.assertEqual(len(self.restarts), 1)

    def test_files_that_are_let_go_clear_the_jam_and_its_clock(self):
        u = self.make([stuck_machine("SF-NODE-01")])
        self.rounds(u, 4)
        self.snap["machines"] = [stuck_machine("SF-NODE-01", locked=0)]
        self.assertEqual(self.rounds(u, 1), [])
        self.snap["machines"] = [stuck_machine("SF-NODE-01")]
        self.assertEqual(self.rounds(u, 5), [])                  # the ten minutes start again
        self.assertEqual(self.restarts, [])

    def test_a_running_job_is_never_ended(self):
        u = self.make([stuck_machine("SF-NODE-01", procs={"runner": 1})])
        self.rounds(u, 10)
        self.assertEqual(self.restarts, [])
        self.assertIn("Runner.Worker", u.blockers()[0]["detail"])

    def test_gaming_pc_recent_input_means_no_restart(self):
        used = {"attended": True, "input_idle": 4 * 60, "games": []}
        u = self.make([stuck_machine("Alex_Gaming_PC", presence=used)])
        self.rounds(u, 10)
        self.assertEqual(self.restarts, [])
        row, = u.blockers()
        self.assertIn("Waiting until you are away: this PC was used 4 min ago", row["detail"])
        self.assertIn("Waiting until you are away", u.notes()["alex_gaming_pc"])
        self.assertEqual(sum("not restarting Alex_Gaming_PC yet" in line for line in self.lines), 1)   # said once

    def test_gaming_pc_game_running_means_no_restart(self):
        playing = {"attended": True, "input_idle": 3 * 3600, "games": ["eu5"]}
        u = self.make([stuck_machine("Alex_Gaming_PC", presence=playing)])
        self.rounds(u, 10)
        self.assertEqual(self.restarts, [])
        self.assertIn("Waiting until you are away: eu5 is running.", u.blockers()[0]["detail"])

    def test_gaming_pc_input_that_cannot_be_read_means_no_restart(self):
        u = self.make([stuck_machine("Alex_Gaming_PC", presence={"attended": True, "input_idle": None, "games": []})])
        self.rounds(u, 10)
        self.assertEqual(self.restarts, [])

    def test_gaming_pc_away_means_restart_requested(self):
        u = self.make([stuck_machine("Alex_Gaming_PC", presence={"attended": True, "input_idle": 4 * 60, "games": []})])
        self.rounds(u, 8)
        self.assertEqual(self.restarts, [])
        self.snap["machines"] = [stuck_machine("Alex_Gaming_PC", presence=AWAY)]   # 31 min without input, no game
        self.rounds(u, 2)
        self.assertEqual([name for name, _ in self.restarts], ["Alex_Gaming_PC"])

    def test_the_guard_also_holds_the_finished_check_jam(self):
        used = {"attended": True, "input_idle": 60, "games": []}
        m = machine("Alex_Gaming_PC", HELD)
        m["presence"] = used
        self.make([m])
        u = unjam.Unjam(FakeGitHub({"77": ("completed", 0)}), self.lines.append, restart=lambda n, r: self.restarts.append(n) or "ok",
                        repos=lambda: [REPO], clock=lambda: self.at[0],
                        snapshot=lambda: {"fresh": {"github": iso(self.at[0] - 20), "error": ""}, **self.snap})
        self.at[0] = T0 + 3600
        self.rounds(u, 4)
        self.assertEqual(self.restarts, [])
        m["presence"] = AWAY
        self.rounds(u, 2)
        self.assertEqual(self.restarts, ["Alex_Gaming_PC"])

    def test_off_switch_flags_only(self):
        u = self.make([stuck_machine("SF-NODE-01")])
        u.enabled = False
        self.rounds(u, 10)
        self.assertEqual(self.restarts, [])
        self.assertIn("Automatic restart is turned off", u.blockers()[0]["detail"])


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class StuckFilesOnDiskTest(unittest.TestCase):
    """reporter.stuck_files against a real mapped DLL, the way an editor that cannot be ended holds one."""

    HOLDER = ("Add-Type -Namespace FT -Name Native -MemberDefinition '[DllImport(\"kernel32.dll\", CharSet=CharSet.Unicode)] "
              "public static extern IntPtr LoadLibraryEx(string p, IntPtr f, uint x);'; "
              "if ([FT.Native]::LoadLibraryEx($env:FT_DLL, [IntPtr]::Zero, 1) -eq [IntPtr]::Zero) { exit 3 }; "
              "Set-Content -LiteralPath $env:FT_READY -Value ready; Start-Sleep -Seconds 120")

    def test_a_mapped_file_counts_and_a_free_one_does_not(self):
        import os
        import shutil
        import subprocess
        import time
        import reporter
        with tempfile.TemporaryDirectory() as tmp:
            co = Path(tmp) / "LifeShare"
            aside = co / "Saved" / "StuckFiles" / "20261123T204400Z" / "Binaries" / "Win64"
            aside.mkdir(parents=True)
            held, free = aside / "UnrealEditor-LSBoot.dll", aside / "UnrealEditor-Free.dll"
            shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "version.dll", held)
            free.write_bytes(b"x")
            self.assertIsNone(reporter.stuck_files([Path(tmp) / "elsewhere"]))
            ready = Path(tmp) / "ready"
            env = {**os.environ, "FT_DLL": str(held), "FT_READY": str(ready)}
            child = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", self.HOLDER], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
            try:
                deadline = time.monotonic() + 60
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                self.assertTrue(ready.exists(), "the holder could not map the DLL")
                got = reporter.stuck_files([co])
                self.assertEqual((got["locked"], got["files"]), (1, 2), got)
                self.assertTrue(got["sample"].endswith("UnrealEditor-LSBoot.dll"), got)
                self.assertEqual(free.read_bytes(), b"x")       # looking changes nothing
            finally:
                child.kill()
                child.wait(timeout=30)
            for _ in range(50):                                 # the mapping goes with the process
                if reporter.stuck_files([co])["locked"] == 0:
                    break
                time.sleep(0.2)
            self.assertEqual(reporter.stuck_files([co])["locked"], 0)


class PresenceTest(unittest.TestCase):
    def test_games_come_from_machine_json_or_the_throttle_defaults(self):
        import json
        import reporter
        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp)
            self.assertFalse(reporter.presence(lease, ["eu5.exe"], {})["attended"])     # no game throttle: not someone's PC
            (lease / "game-throttle.last").write_text("2026-11-23T20:00:00Z", encoding="utf-8")
            here = reporter.presence(lease, ["explorer.exe", "eu5.exe"], {})
            self.assertEqual((here["attended"], here["games"]), (True, ["eu5"]))
            (lease / "machine.json").write_text(json.dumps({"yieldTo": ["Factorio"]}), encoding="utf-8")
            self.assertEqual(reporter.presence(lease, ["eu5.exe", "factorio.exe"], {})["games"], ["Factorio"])
            self.assertFalse(reporter.presence(lease, [], {"attended": False})["attended"])


if __name__ == "__main__":
    unittest.main()
