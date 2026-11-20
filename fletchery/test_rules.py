"""The rules (rules.py): base health (red since, suspects, fix in line, held red), test names from annotations, and
required checks from branch rules and protection."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules  # noqa: E402
import store  # noqa: E402

NOW = 1_800_000_000.0
REPO, BASE = "Acme/Game", "Test-Staging"


def at(minutes_ago):
    return datetime.fromtimestamp(NOW - minutes_ago * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(name, conclusion, minutes_ago):
    return {"name": name, "status": "completed", "conclusion": conclusion, "completed_at": at(minutes_ago)}


def merge(n, sha, minutes_ago, base=BASE, branch="feature"):
    return {"number": n, "title": f"Change {n}", "merged_at": at(minutes_ago), "created_at": at(minutes_ago + 60),
            "merge_commit_sha": sha, "base": {"ref": base}, "head": {"ref": branch}, "user": {"login": "ana"}}


def pr(n, checks="failed", failed=("Compile and test",), labels=(), body="", base=BASE, draft=False):
    return {"number": n, "title": f"PR {n}", "url": f"https://github.com/{REPO}/pull/{n}", "base": base, "labels": list(labels),
            "body": body, "draft": draft, "auto_merge": True, "checks": {"state": checks, "failed": list(failed)}}


class BaseHealthTest(unittest.TestCase):
    def setUp(self):
        self.s = store.Store(None)
        self.addCleanup(self.s.close)
        # g1 green (merge #1), then #2 and #3 merged; r1 (#2) red first, r2 (#3) still red
        self.s.merges(REPO, [merge(1, "g1", 400), merge(2, "r1", 300), merge(3, "r2", 200), merge(9, "x", 100, base="main")])
        self.s.base_checks(REPO, BASE, "g1", [check("Cheap checks", "success", 390), check("Compile and test", "success", 350)])
        self.s.base_checks(REPO, BASE, "r1", [check("Cheap checks", "success", 290), check("Compile and test", "failure", 240)])
        self.s.base_checks(REPO, BASE, "r2", [check("Cheap checks", "success", 190), check("Compile and test", "failure", 120),
                                              check("Lint docs", "failure", 180)])

    def health(self, **kw):
        return rules.base_health(self.s, REPO, BASE, now=NOW, **kw)

    def test_red_since_first_bad_and_suspects(self):
        h = self.health(notes={"Compile and test": ["LifeShare.Town.Fishing", "RouteScale"]})
        self.assertEqual(h["state"], "red")
        self.assertEqual(h["first_bad"], "r1")
        self.assertEqual(h["last_green"], "g1")
        self.assertEqual(h["since"], at(240))
        self.assertEqual(h["for"], 240 * 60)
        self.assertEqual([s["number"] for s in h["suspects"]], [2])
        self.assertTrue(h["suspects"][0]["first_bad"])
        self.assertEqual([f["name"] for f in h["failing"]], ["Compile and test", "Lint docs"])
        self.assertEqual(h["failing"][0]["tests"], ["LifeShare.Town.Fishing", "RouteScale"])

    def test_required_checks_only(self):
        h = self.health(required=["Compile and test"])
        self.assertEqual([f["name"] for f in h["failing"]], ["Compile and test"])
        self.assertTrue(h["required"])
        self.assertEqual(self.health(required=["Cheap checks"])["state"], "green")

    def test_all_required_pass_on_the_exact_tip(self):
        required = ["Cheap checks", "Compile and test"]
        tip = "r3"
        cheap = dict(check(required[0], "success", 5), head_sha=tip)
        foreign_compile = dict(check(required[1], "success", 4), head_sha="r2")
        def verdict(checks):
            return self.health(tip_sha=tip, tip_checks=checks, required=required)
        self.assertEqual(verdict([cheap, foreign_compile])["waiting"], [required[1]])
        for conclusion in ("skipped", "neutral", "cancelled", "stale", None):
            with self.subTest(conclusion=conclusion):
                compile_check = dict(check(required[1], conclusion, 3), status="completed" if conclusion else "in_progress")
                h = verdict([cheap, compile_check])
                self.assertEqual((h["sha"], h["state"], h["waiting"]), (tip, "checking", [required[1]]))
                self.assertNotIn("first_bad", h)
        self.assertEqual(verdict([cheap, check(required[1], "success", 1)])["state"], "green")
        self.assertEqual(verdict([])["waiting"], required)
        self.assertIsNone(self.health(tip_sha=tip, tip_checks=[]))

    def test_current_attempt_ignores_old_failures_on_same_commit(self):
        h = self.health(tip_sha="r2", tip_checks=[check("Cheap checks", "success", 1)],
                        required=["Cheap checks", "Compile and test"])
        self.assertEqual((h["state"], h["waiting"]), ("checking", ["Compile and test"]))
        self.assertNotIn("failing", h)

    def test_unprotected_terminal_checks_without_a_verdict_leave_no_notice(self):
        done = [check("Build", "cancelled", 6), check("Lint", "skipped", 4), check("Other", None, 2)]
        self.assertIsNone(self.health(tip_sha="r2", tip_checks=done))
        h = self.health(tip_sha="r2", tip_checks=done + [{"name": "Test", "status": "in_progress"}])
        self.assertEqual((h["state"], h["required"], h["waiting"]), ("checking", False, ["Test"]))

    def test_current_failure_and_unfinished_required_check_keep_first_bad_merge(self):
        self.s.base_checks(REPO, BASE, "g1", [check("Compile and test", "success", 1)])  # old commit reran after r2
        h = self.health(tip_sha="r2", tip_checks=[check("Compile and test", "failure", 6)],
                        required=["Compile and test", "Cheap checks"])
        self.assertEqual((h["state"], h["first_bad"], h["last_green"], h["since"]), ("red", "r1", "g1", at(240)))
        self.assertEqual(h["waiting"], ["Cheap checks"])
        self.assertEqual([p["number"] for p in h["suspects"] if p["first_bad"]], [2])

    def test_old_unverified_check_cannot_extend_a_pinned_app_incident(self):
        pins = {"Compile and test": {42}}
        live = [check("Compile and test", "failure", 2)]
        h = self.health(tip_sha="r2", tip_checks=live, required=list(pins), required_sources=pins)
        self.assertEqual(h["first_bad"], "r2")
        self.s.base_checks(REPO, BASE, "r1", [check("Compile and test", "failure", 240)], required_sources=pins)
        h = self.health(tip_sha="r2", tip_checks=live, required=list(pins), required_sources=pins)
        self.assertEqual((h["first_bad"], h["since"]), ("r1", at(240)))

    def test_fix_in_line_held_red_and_issue(self):
        issues = [{"number": 50, "title": "Test-Staging is red", "url": "u", "labels": ["tip-red"]},
                  {"number": 51, "title": "main is red", "labels": ["tip-red"]},
                  {"number": 52, "title": "archive/Test-Staging is red", "labels": ["tip-red"]}]
        prs = [pr(10, labels=["tip-fix"], checks="running", failed=()), pr(11, body="Fixes #50", checks="queued", failed=()),
               pr(12), pr(13, failed=("Something else",)), pr(14, draft=True), pr(15, base="main"),
               pr(16, body="Fixes #51", checks="queued", failed=())]
        h = self.health(prs=prs, issues=issues)
        self.assertEqual([f["number"] for f in h["fixes"]], [10, 11])
        self.assertEqual(h["held"], 1)
        self.assertEqual([i["number"] for i in h["issues"]], [50])

    def test_rerun_turns_green(self):
        self.s.base_checks(REPO, BASE, "r2", [check("Compile and test", "success", 10), check("Lint docs", "success", 9)])
        h = self.health()
        self.assertEqual(h["state"], "green")
        self.assertNotIn("since", h)

    def test_unknown_commits_are_skipped(self):
        self.s.base_checks(REPO, BASE, "c1", [check("Compile and test", "cancelled", 5)])
        h = self.health(checking=True)
        self.assertEqual(h["state"], "red")
        self.assertTrue(h["checking"])

    def test_nothing_known(self):
        self.assertIsNone(rules.base_health(self.s, REPO, "main", now=NOW))

    def test_suspects_without_merge_commits(self):
        s = store.Store(None)
        self.addCleanup(s.close)
        s.merges(REPO, [dict(merge(2, "", 300), merge_commit_sha=None)])
        s.base_checks(REPO, BASE, "g1", [check("Compile and test", "success", 350)])
        s.base_checks(REPO, BASE, "r1", [check("Compile and test", "failure", 240)])
        self.assertEqual([x["number"] for x in rules.base_health(s, REPO, BASE, now=NOW)["suspects"]], [2])


class HelpersTest(unittest.TestCase):
    def test_test_names(self):
        notes = [{"annotation_level": "warning", "message": "Node.js 20 is deprecated."},
                 {"annotation_level": "warning", "message": "No files were found with the provided path: result.json"},
                 {"annotation_level": "failure", "title": "LifeShare.Town.Fishing", "message": "x"},
                 {"annotation_level": "failure", "title": "", "message": "RouteScale pedestrian 5.0 ms\nmore"},
                 {"annotation_level": "failure", "title": "", "message": "Process completed with exit code 1."},
                 {"annotation_level": "notice", "title": "fyi"},
                 {"annotation_level": "failure", "title": "LifeShare.Town.Fishing"}]
        self.assertEqual(rules.test_names(notes), ["LifeShare.Town.Fishing", "RouteScale pedestrian 5.0 ms"])

    def test_required_checks(self):
        branch_rules = [{"type": "deletion"}, {"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "PR check / Cheap checks"}, {"context": "PR check / Compile and test"}]}}]
        prot = {"contexts": ["PR check / Cheap checks", "legacy"], "checks": [{"context": "legacy"}]}
        self.assertEqual(rules.required_checks(branch_rules, prot), ["PR check / Cheap checks", "PR check / Compile and test", "legacy"])
        self.assertEqual(rules.required_checks(None, None), [])

    def test_required_context_producers_combine_classic_and_ruleset_pins(self):
        contexts = [{"context": "compile", "integration_id": 77}, {"context": "test", "integration_id": -1}]
        branch_rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": contexts}}]
        protection = {"contexts": ["compile", "lint"], "checks": [{"context": "compile", "app_id": 88},
                                                                  {"context": "lint", "app_id": 33}]}
        self.assertEqual(rules.required_sources(branch_rules, protection), {"compile": {77, 88}, "test": {None}, "lint": {33}})


class CausesTest(unittest.TestCase):
    def test_classify_order(self):
        c = lambda concl="failure", **k: {"name": "PR check / Compile and test", "conclusion": concl, **k}
        base = {"compile and test": ["LifeShare.Town.Fishing"]}
        self.assertEqual(rules.classify(c("cancelled"), base_failing=base), "cancelled")
        self.assertEqual(rules.classify(c(no_result=True)), "machine")
        self.assertEqual(rules.classify(c("startup_failure")), "machine")
        self.assertEqual(rules.classify(c("timed_out")), "machine")
        self.assertEqual(rules.classify(c("timed_out"), failed_step="Run specs"), "own")
        self.assertEqual(rules.classify(c(), failed_step="Checkout"), "machine")
        self.assertEqual(rules.classify(c(), text="Error: No space left on device"), "machine")
        self.assertEqual(rules.classify(c(), text="ShaderCompileWorker crashed", machine_patterns=(r"shadercompileworker crashed",)), "machine")
        self.assertEqual(rules.classify(c(), base_failing=base), "base")                      # no tests to compare: the check
        self.assertEqual(rules.classify(c(), tests=["LifeShare.Town.Fishing"], base_failing=base), "base")
        self.assertEqual(rules.classify(c(), tests=["LifeShare.Folk.Hair"], base_failing=base), "own")   # a different test
        self.assertEqual(rules.classify(c(), retried_ok=True), "flaky")
        self.assertEqual(rules.classify(c()), "own")

    def test_problems_group_and_summary(self):
        f = lambda n, cause, tests=(), runner="", check="Compile and test": {
            "repo": REPO, "number": n, "title": f"PR {n}", "url": "u", "base": BASE, "check": check, "cause": cause,
            "tests": list(tests), "since": at(n), "runner": runner, "job": n}
        ps = rules.problems([f(1, "base"), f(2, "base"), f(3, "base"), f(4, "own", ["Folk.Hair LOD 3"]),
                             f(5, "own", ["Folk.Hair LOD 4"]), f(6, "own", ["Town.Roads"]), f(7, "machine", runner="NODE-pr"),
                             f(8, "machine", runner="NODE-pr"), f(1, "machine", runner="PC-pr", check="Cheap checks")])
        self.assertEqual([(g["cause"], len(g["prs"])) for g in ps],
                         [("own", 2), ("own", 1), ("base", 3), ("machine", 2), ("machine", 1)])
        self.assertEqual(ps[0]["title"], "2 PRs fail Compile and test (Folk.Hair LOD 3, Folk.Hair LOD 4)")
        self.assertEqual(ps[2]["title"], "3 PRs fail Compile and test because Test-Staging is red")
        sm = rules.summary(ps)
        self.assertEqual((sm["red"], sm["own"], sm["base"], sm["machine"], sm["problems_own"]), (8, 3, 3, 2, 2))


class QueueTest(unittest.TestCase):
    def test_schedule(self):
        # two workers: one free now, one in 10 min; three jobs of 30, 20 and unknown minutes, then one more
        got = rules.schedule([600, 0], [1800, 1200, None, 600])
        self.assertEqual(got[0], (0, 1800))
        self.assertEqual(got[1], (600, 1800))
        self.assertEqual(got[2], (1800, None))
        self.assertEqual(got[3], (None, None))
        self.assertEqual(rules.schedule([], [60]), [(None, None)])

    def test_cancel_reason(self):
        self.assertEqual(rules.cancel_reason([{"message": "The run was canceled by @ProjektPayne."}]), ("person", "ProjektPayne"))
        self.assertEqual(rules.cancel_reason([{"message": "Canceling since a higher priority waiting request for 'pr-12' exists"}]),
                         ("replaced", ""))
        self.assertEqual(rules.cancel_reason([{"message": "The operation was canceled."}]), ("", ""))

    def test_pool_of(self):
        self.assertEqual(rules.pool_of(["self-hosted", "Windows", "unreal-pr"], {"self-hosted", "windows"}), "unreal-pr")
        self.assertEqual(rules.pool_of([], set()), "any")

    def test_store_job_seconds_and_pool_stats(self):
        s = store.Store(None)
        self.addCleanup(s.close)
        run = {"id": 1, "name": "PR check", "created_at": at(300)}
        s.jobs(REPO, run, [{"id": i, "name": "Compile and test", "status": "completed", "conclusion": "success", "labels": ["self-hosted", "unreal-pr"],
                            "created_at": at(300), "started_at": at(300 - 10 * i), "completed_at": at(300 - 10 * i - d)}
                           for i, d in ((1, 40), (2, 60), (3, 50))])
        self.assertEqual(s.job_seconds(REPO, "PR check", "Compile and test"), 50 * 60)
        self.assertIsNone(s.job_seconds(REPO, "PR check", "Other"))
        st = s.pool_stats({"unreal-pr"}, 2, NOW)
        self.assertEqual((st["wait_p50"], st["wait_n"]), (20 * 60, 3))
        self.assertAlmostEqual(st["util"], 150 * 60 / (2 * 86400), places=3)
        self.assertEqual(s.pool_stats({"unreal-light"}, 1, NOW)["wait_n"], 0)


if __name__ == "__main__":
    unittest.main()
