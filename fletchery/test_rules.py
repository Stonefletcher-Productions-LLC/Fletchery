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

    def test_fix_in_line_held_red_and_issue(self):
        issues = [{"number": 50, "title": "Test-Staging is red", "url": "u", "labels": ["tip-red"]}]
        prs = [pr(10, labels=["tip-fix"], checks="running", failed=()), pr(11, body="Fixes #50", checks="queued", failed=()),
               pr(12), pr(13, failed=("Something else",)), pr(14, draft=True), pr(15, base="main")]
        h = self.health(prs=prs, issues=issues)
        self.assertEqual([f["number"] for f in h["fixes"]], [10, 11])
        self.assertEqual(h["held"], 1)
        self.assertEqual(h["issues"][0]["number"], 50)

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
        notes = [{"annotation_level": "failure", "title": "LifeShare.Town.Fishing", "message": "x"},
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


if __name__ == "__main__":
    unittest.main()
