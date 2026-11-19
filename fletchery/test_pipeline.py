"""Where each change is in the whole process (pipeline.py): the six steps from checks, merges, staging runs and
releases; step times learned from what finished; and how the Live feed ties a machine's work to its change."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import devwatch  # noqa: E402
import pipeline  # noqa: E402
import test_devwatch as tdw  # noqa: E402

T0 = 1_800_000_000.0
REPO = "Acme/Game"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(cid, name, status="completed", conclusion="success", start=T0 - 3600, end=T0 - 600):
    c = {"id": cid, "name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
         "started_at": iso(start)}
    if status == "completed":
        c["completed_at"] = iso(end)
    return c


def pr(number=12, title="Town lots", draft=False, auto=True, base="Test-Staging", merged_at=None):
    p = {"number": number, "title": title, "html_url": f"https://github.com/{REPO}/pull/{number}", "draft": draft,
         "auto_merge": {"merge_method": "merge"} if auto else None, "base": {"ref": base}, "head": {"ref": "claude/x", "sha": "a" * 40},
         "labels": [], "updated_at": iso(T0 - 60)}
    if merged_at:
        p["merged_at"] = iso(merged_at)
    return p


def job(jid, status="in_progress", runner="MY-PC-unreal-pr", steps=()):
    return {"id": jid, "name": "Compile and test", "status": status, "conclusion": "success" if status == "completed" else None,
            "runner_name": runner, "steps": list(steps)}


def step(name, status, start=None, end=None, conclusion=None):
    s = {"name": name, "status": status, "conclusion": conclusion or ("success" if status == "completed" else None)}
    if start:
        s["started_at"] = iso(start)
    if end:
        s["completed_at"] = iso(end)
    return s


RULES = pipeline.Rules({"quick": ["^cheap"], "kinds": {"Town change": ["town", "road"]}})
MACHINE = lambda name: "Gaming PC" if name.startswith("MY-PC") else name   # noqa: E731


def states(c):
    return {s["id"]: s["state"] for s in c["stages"]}


class Rules(unittest.TestCase):
    def test_quick_checks_and_kinds(self):
        self.assertTrue(RULES.is_quick("Cheap checks"))
        self.assertFalse(RULES.is_quick("Compile and test"))
        self.assertTrue(pipeline.Rules().is_quick("Lint"))                 # the defaults
        self.assertEqual(RULES.kind("Parking lots", ["area: town"]), "Town change")
        self.assertEqual(RULES.kind("Shop signs", []), "Change")
        self.assertEqual(pipeline.Rules({"quick": ["(bad"]}).quick, [])   # a broken pattern is dropped, not fatal


class History(unittest.TestCase):
    def test_median_of_the_most_specific_level_with_enough_samples(self):
        h = pipeline.History()
        for i, secs in enumerate((600, 700, 800)):
            h.add(("build", REPO, "Town change", "Gaming PC"), secs, f"s{i}")
        h.add(("build", REPO, "Change", "Gaming PC"), 100, "x")
        self.assertEqual(h.estimate(("build", REPO, "Town change", "Gaming PC")), (700, 3))
        self.assertEqual(h.estimate(("build", REPO, "Town change", "NODE-01")), (700, 3))    # falls back to the kind
        self.assertEqual(h.estimate(("build", REPO, "Change", "Gaming PC"))[1], 4)            # 1 sample: the repo level
        self.assertEqual(h.estimate(("staging", REPO, "all")), (None, 0))

    def test_same_sample_once_last_twenty_and_sane_values(self):
        h = pipeline.History()
        h.add(("quick", REPO, "Change"), 60, "same")
        h.add(("quick", REPO, "Change"), 60, "same")
        h.add(("quick", REPO, "Change"), -5, "neg")
        h.add(("quick", REPO, "Change"), 3 * 86400, "days")
        self.assertEqual(h.estimate(("quick", REPO, "Change")), (60, 1))
        for i in range(30):
            h.add(("quick", REPO, "Change"), 100 + i, f"n{i}")
        self.assertEqual(len(h.samples[h.key("quick", REPO, "Change")]), pipeline.KEEP)

    def test_saved_atomically_and_reloaded_safely(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "step-times.json"
            h = pipeline.History(path)
            h.add(("build", REPO, "Change"), 900, "a")
            h.save()
            self.assertEqual(os.listdir(d), ["step-times.json"])
            self.assertEqual(pipeline.History(path).estimate(("build", REPO, "Change")), (900, 1))
            path.write_text(json.dumps({"samples": {"k|x": [["nope", 1], [5, "ok"]], "bad": "x"}}), encoding="utf-8")
            self.assertEqual(pipeline.History(path).samples, {"k|x": [[5.0, "ok"]]})
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(pipeline.History(path).samples, {})


class OpenChanges(unittest.TestCase):
    def setUp(self):
        self.h = pipeline.History()
        for i in range(3):
            self.h.add(("quick", REPO, "Town change"), 120, f"q{i}")
            self.h.add(("build", REPO, "Town change"), 2400, f"b{i}")
            self.h.add(("staging", REPO, "all"), 1800, f"s{i}")
            self.h.add(("step", REPO, "Compile and test", "Specs", "Town change"), 900, f"p{i}")

    def change(self, p, checks, jobs=None):
        return pipeline.open_change(self.h, RULES, REPO, p, checks, jobs or {}, MACHINE, T0)

    def test_building_with_parts_bars_and_time_left(self):
        jobs = {2: job(2, steps=[step("Set up job", "completed", T0 - 1200, T0 - 1190), step("Build editor", "completed", T0 - 1190, T0 - 700),
                                 step("Specs", "in_progress", T0 - 700), step("Town check", "queued")])}
        c = self.change(pr(), [check(1, "Cheap checks", start=T0 - 1400, end=T0 - 1300),
                               check(2, "Compile and test", status="in_progress", start=T0 - 1200)], jobs)
        self.assertEqual((c["stage"], c["status"], c["kind"]), ("build", "running", "Town change"))
        self.assertEqual(states(c), {"written": "done", "quick": "done", "build": "now", "merge": "todo", "staging": "todo", "release": "todo"})
        st = c["step"]
        self.assertEqual((st["elapsed"], st["avg"], st["pct"], st["left"], st["late"], st["machine"]), (1200, 2400, 50, 1200, False, "Gaming PC"))
        self.assertEqual([p["name"] for p in st["parts"]], ["Build editor", "Specs", "Town check"])     # no set-up noise
        self.assertEqual((st["part"], st["parts"][1]["avg"], st["part_elapsed"]), (1, 900, 700))
        # Overall: quick (120) done + half of build (1200 of 2400), of 120 + 2400 + 1800.
        self.assertEqual(c["overall"]["pct"], int(100 * 1320 / 4320))
        self.assertEqual(c["overall"]["left"], 1200 + 1800)
        self.assertEqual(c["stages"][3]["label"], "Into Test-Staging")

    def test_late_failed_rerun_and_passed(self):
        late = self.change(pr(), [check(1, "Cheap checks"), check(2, "Compile and test", status="in_progress", start=T0 - 4000)])
        self.assertTrue(late["step"]["late"])
        bad = self.change(pr(), [check(1, "Cheap checks"), check(2, "Compile and test", conclusion="failure")])
        self.assertEqual((bad["status"], states(bad)["build"]), ("failed", "failed"))
        self.assertIn("Compile and test", bad["note"])
        quick_bad = self.change(pr(), [check(1, "Cheap checks", conclusion="failure")])
        self.assertEqual((quick_bad["stage"], quick_bad["status"]), ("quick", "failed"))
        rerun = self.change(pr(), [check(1, "Cheap checks"), check(2, "Compile and test", conclusion="cancelled")])
        self.assertEqual(rerun["status"], "rerun")
        ok = self.change(pr(), [check(1, "Cheap checks"), check(2, "Compile and test")])
        self.assertEqual((ok["stage"], ok["status"], states(ok)["build"]), ("merge", "waiting", "done"))
        self.assertIn("by itself", ok["note"])
        self.assertIn("someone", self.change(pr(auto=False), [check(1, "Cheap checks"), check(2, "Compile and test")])["note"])

    def test_failed_step_clock_stops_when_it_failed(self):
        # #2263, 2026-11-19: Quick checks failed, yet the bar kept counting ("2 h 20 min of ~1 min", "Taking longer").
        c = self.change(pr(), [check(1, "Cheap checks", conclusion="failure", start=T0 - 9000, end=T0 - 8100)])
        st = c["step"]
        self.assertEqual((c["stage"], c["status"], st["elapsed"], st["late"], st["left"]), ("quick", "failed", 900, False, None))
        self.assertEqual((c["since"], st["stopped"]), (iso(T0 - 8100), iso(T0 - 8100)))
        gone = self.change(pr(), [check(1, "Cheap checks"), check(2, "Compile and test", conclusion="cancelled",
                                                                  start=T0 - 5000, end=T0 - 4000)])
        self.assertEqual((gone["status"], gone["step"]["elapsed"], gone["since"]), ("rerun", 1000, iso(T0 - 4000)))
        running = self.change(pr(), [check(1, "Cheap checks", status="in_progress", start=T0 - 60)])
        self.assertNotIn("since", running)

    def test_draft_no_checks_and_still_learning(self):
        self.assertEqual(self.change(pr(draft=True), [])["stage"], "written")
        c = self.change(pr(), [])
        self.assertEqual((c["stage"], c["status"]), ("quick", "queued"))
        fresh = pipeline.open_change(pipeline.History(), RULES, REPO, pr(title="Shop signs"), [check(1, "Compile and test", status="in_progress")],
                                     {}, MACHINE, T0)
        self.assertEqual((fresh["overall"]["pct"], fresh["overall"]["learning"], fresh["step"]["avg"]), (None, True, None))
        self.assertEqual(states(fresh)["quick"], "skipped")        # this repository has no quick checks


class MergedChanges(unittest.TestCase):
    def setUp(self):
        self.h = pipeline.History()

    def change(self, runs, releases=(), merged=T0 - 1200, jobs=None):
        return pipeline.merged_change(self.h, RULES, REPO, pr(merged_at=merged), runs, jobs or {}, list(releases), MACHINE, T0)

    def push_run(self, rid, status="in_progress", conclusion=None, created=T0 - 1100, branch="Test-Staging", updated=T0 - 60):
        return {"id": rid, "event": "push", "head_branch": branch, "status": status, "conclusion": conclusion,
                "created_at": iso(created), "run_started_at": iso(created), "updated_at": iso(updated)}

    def test_follows_the_staging_build_then_the_release(self):
        c = self.change([self.push_run(5, created=T0 - 5000), self.push_run(6), self.push_run(7, created=T0 - 900, branch="main")],
                        jobs={6: [job(60, runner="NODE-01-unreal-light", steps=[step("Build", "in_progress", T0 - 1000)])]})
        self.assertEqual((c["stage"], c["status"], c["run"], c["step"]["machine"]), ("staging", "running", 6, "NODE-01-unreal-light"))
        self.assertEqual(states(c)["merge"], "done")
        failed = self.change([self.push_run(6, status="completed", conclusion="failure")])
        self.assertEqual((failed["status"], states(failed)["staging"]), ("failed", "failed"))
        green = self.change([self.push_run(6, status="completed", conclusion="success", updated=T0 - 300)])
        self.assertEqual((green["stage"], green["status"]), ("release", "waiting"))
        shipped = self.change([self.push_run(6, status="completed", conclusion="success", updated=T0 - 300)],
                              releases=[{"name": "0.4.6", "published_at": iso(T0 - 100)}])
        self.assertEqual((shipped["status"], states(shipped)["release"], shipped["note"]), ("done", "done", "In 0.4.6."))

    def test_leaves_when_finished_long_ago_or_never_built(self):
        old_green = self.push_run(6, status="completed", conclusion="success", updated=T0 - 3 * 3600, created=T0 - 4 * 3600)
        self.assertIsNone(self.change([old_green], merged=T0 - 5 * 3600))
        self.assertIsNone(self.change([], merged=T0 - 3600))                 # no staging build for this branch
        self.assertEqual(self.change([], merged=T0 - 60)["status"], "queued")   # just merged: waiting for it to start
        self.assertIsNone(self.change([], merged=T0 - 2 * 86400))
        cancelled = self.push_run(6, status="completed", conclusion="cancelled")
        self.assertEqual(self.change([cancelled, self.push_run(8, created=T0 - 500)])["run"], 8)   # a cancelled run is skipped


class Learning(unittest.TestCase):
    def test_finished_checks_jobs_and_staging_runs_teach_step_times(self):
        h = pipeline.History()
        jobs = {2: job(2, status="completed", steps=[step("Specs", "completed", T0 - 1500, T0 - 900)])}
        cr = [check(1, "Cheap checks", start=T0 - 2000, end=T0 - 1880), check(2, "Compile and test", start=T0 - 1800, end=T0 - 600)]
        pipeline.learn_checks(h, REPO, "Town change", "a" * 40, cr[:1], cr[1:], jobs, MACHINE)
        pipeline.learn_checks(h, REPO, "Town change", "a" * 40, cr[:1], cr[1:], jobs, MACHINE)    # the next poll: no double count
        self.assertEqual(h.estimate(("quick", REPO, "Town change")), (120, 1))
        self.assertEqual(h.estimate(("build", REPO, "Town change", "Gaming PC")), (1200, 1))
        self.assertEqual(h.estimate(("step", REPO, "Compile and test", "Specs", "Town change")), (600, 1))
        running = [check(3, "Compile and test", status="in_progress")]
        pipeline.learn_checks(h, REPO, "Change", "b" * 40, [], running, {}, MACHINE)
        self.assertNotIn(h.key("build", REPO, "Change"), h.samples)                                  # nothing finished yet
        pipeline.learn_staging(h, REPO, {"id": 9, "status": "completed", "conclusion": "success", "run_started_at": iso(T0 - 2400),
                                         "updated_at": iso(T0)}, [])
        pipeline.learn_staging(h, REPO, {"id": 10, "status": "completed", "conclusion": "failure", "run_started_at": iso(T0 - 60),
                                         "updated_at": iso(T0)}, [])
        self.assertEqual(h.estimate(("staging", REPO, "all")), (2400, 1))


class Feed(unittest.TestCase):
    def test_changes_in_the_snapshot_and_machines_tied_to_them(self):
        merged = {"number": 9, "title": "Folk greetings", "html_url": "https://github.com/Acme/Game/pull/9", "merged_at": tdw.ago(20),
                  "base": {"ref": "main"}, "head": {"ref": "claude/greet"}, "labels": []}
        first = {f"repos/{tdw.REPO}/pulls?state=closed": [{**merged, "closed_at": tdw.ago(20)}],
                 f"repos/{tdw.REPO}/releases": []}
        with tempfile.TemporaryDirectory() as d:
            answers = dict(first)
            for k, v in tdw.world().items():
                answers.setdefault(k, v)
            w = devwatch.DevWatch({"names": {"ALEX-PC": "Gaming PC"}, "pipeline": {"kinds": {"Transit": ["bus"]}}},
                                  github=tdw.FakeGitHub(answers), state_dir=d)
            w.poll()
            s = w.snapshot()
            keys = {c["key"]: c for c in s["changes"]}
            self.assertEqual(set(keys), {"Acme/Game#12", "Acme/Game#13", "Acme/Game#9"})
            self.assertEqual((keys["Acme/Game#12"]["stage"], keys["Acme/Game#12"]["kind"]), ("build", "Transit"))
            self.assertEqual(keys["Acme/Game#9"]["merged"], True)
            self.assertEqual(s["changes"][0]["status"], "failed")                 # what needs attention first
            pc = next(m for m in s["machines"] if m["name"] == "Gaming PC")
            self.assertEqual([a.get("change") for a in pc["activities"]][:1], ["Acme/Game#12"])


if __name__ == "__main__":
    unittest.main()
