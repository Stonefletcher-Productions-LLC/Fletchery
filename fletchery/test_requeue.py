"""Tests for requeue.py: python fletchery/test_requeue.py"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import requeue  # noqa: E402

REPO = "Acme/Game"
T0 = 1_800_000_000.0


def iso(after):
    return datetime.fromtimestamp(T0 + after, timezone.utc).isoformat()


def run(rid, sha, conclusion="cancelled", after=0):
    return {"id": rid, "head_sha": sha, "status": "completed", "conclusion": conclusion, "updated_at": iso(after)}


def pull(number, sha, auto=True, draft=False):
    return {"number": number, "draft": draft, "auto_merge": {"merge_method": "merge"} if auto else None, "head": {"sha": sha}}


def jobs(runner=""):
    return [{"name": "PR check / Cheap checks", "conclusion": "success", "runner_name": "light"},
            {"name": "PR check / Compile and test", "conclusion": "cancelled", "runner_name": runner}]


class FakeGitHub:
    def __init__(self, runs, pulls, jobs_of=None, moving=False, refuse=()):
        self.runs, self.pulls, self.jobs_of, self.moving, self.refuse = runs, pulls, jobs_of or {}, moving, set(refuse)
        self.posts = []

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        assert path.startswith(f"repos/{REPO}/"), path
        rest = path[len(f"repos/{REPO}/"):]
        if method == "POST":
            rid = int(rest.split("/")[2])
            self.posts.append(rid)
            if rid in self.refuse:
                raise RuntimeError("403")
            return None
        if rest.startswith("actions/workflows/tip-fix-first.yml/runs"):
            return {"workflow_runs": [{"id": 1}] if self.moving and "in_progress" in rest else []}
        if rest.startswith("actions/workflows/pr-check.yml/runs?status=cancelled"):
            return {"workflow_runs": [r for r in self.runs if r["conclusion"] == "cancelled"]}
        if rest.startswith("actions/workflows/pr-check.yml/runs?head_sha="):
            sha = rest.split("head_sha=")[1].split("&")[0]
            return {"workflow_runs": [r for r in self.runs if r["head_sha"] == sha]}
        if rest.startswith("pulls?"):
            return list(self.pulls)
        if rest.startswith("actions/runs/"):
            return {"jobs": self.jobs_of.get(int(rest.split("/")[2]), jobs())}
        raise RuntimeError("404 " + path)


class RequeueTest(unittest.TestCase):
    def make(self, gh, state=None):
        self.lines, self.at = [], [T0 + 3600]
        return requeue.Requeue(gh, self.lines.append, state, repos=lambda: [REPO], clock=lambda: self.at[0])

    def test_requeues_only_a_stranded_check(self):
        gh = FakeGitHub(
            runs=[run(1, "a"),                       # stranded: re-queued
                  run(2, "b"), run(3, "b", "success"),   # a newer check on the same commit
                  run(4, "c"),                       # its PR has no auto-merge
                  run(5, "d"),                       # a draft
                  run(6, "e"),                       # was building when cancelled
                  run(7, "f", after=3600 - 60),      # cancelled a minute ago: whatever cancelled it may still act
                  run(8, "gone"),                    # no open PR on that commit
                  run(9, "h", after=-4 * 86400)],    # history
            pulls=[pull(10, "a"), pull(11, "b"), pull(12, "c", auto=False), pull(13, "d", draft=True), pull(14, "e"),
                   pull(15, "f"), pull(16, "h")],
            jobs_of={6: jobs("SF-NODE-01-unreal-pr")})
        r = self.make(gh)
        self.assertEqual(r.round(), [{"repo": REPO, "pr": 10, "run": 1}])
        self.assertEqual(gh.posts, [1])
        self.assertIn("#10", self.lines[0])

    def test_waits_for_a_tip_fix_that_is_moving_builds(self):
        gh = FakeGitHub([run(1, "a")], [pull(10, "a")], moving=True)
        r = self.make(gh)
        self.assertEqual(r.round(), [])
        self.assertEqual(gh.posts, [])
        self.assertIn("wait for the tip fix", r.note)

    def test_a_check_cancelled_again_is_asked_twice_a_day_and_no_more(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGitHub([run(1, "a")], [pull(10, "a")])
            r = self.make(gh, d)
            for _ in range(3):
                r.round()
            self.assertEqual(gh.posts, [1, 1])
            self.assertIn("needs a look", r.waiting[0]["why"])
            again = self.make(gh, d)                 # the count survives a restart of Fletchery
            again.round()
            self.assertEqual(gh.posts, [1, 1])
            self.at[0] += 86400 + 1
            gh.runs[0]["updated_at"] = iso(86400)
            again.round()
            self.assertEqual(gh.posts, [1, 1, 1])

    def test_a_round_is_capped_and_a_refusal_is_not_fatal(self):
        n = requeue.PER_TICK + 2
        gh = FakeGitHub([run(i, f"s{i}") for i in range(1, n + 1)], [pull(100 + i, f"s{i}") for i in range(1, n + 1)], refuse=[2])
        r = self.make(gh)
        got = r.round()
        self.assertEqual([g["run"] for g in got], [1, 3, 4, 5, 6])
        self.assertEqual(sorted(w["run"] for w in r.waiting), [2, 7])

    def test_tick_is_paced_and_can_be_turned_off(self):
        gh = FakeGitHub([run(1, "a")], [pull(10, "a")])
        r = self.make(gh)
        r.tick()
        r.tick()
        self.assertEqual(gh.posts, [1])
        off = requeue.Requeue(FakeGitHub([run(1, "a")], [pull(10, "a")]), self.lines.append, None, repos=lambda: [REPO],
                              clock=lambda: self.at[0], enabled=False)
        off.tick()
        self.assertEqual(off.gh.posts, [])

    def test_a_repository_without_the_workflow_is_skipped(self):
        class Bare:
            def api(self, *a, **k):
                raise RuntimeError("404")
        r = self.make(Bare())
        self.assertEqual(r.round(), [])
        self.assertEqual(r.note, "no waiting PR has a cancelled check")


if __name__ == "__main__":
    unittest.main()
