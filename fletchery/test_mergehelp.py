"""The merge helper (mergehelp.py): backend choice and spend limits, which PR it picks, and a real conflict resolved in
a real git repository with a stand-in for Claude. It never force-pushes and pushes nothing when a conflict is left."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import mergehelp  # noqa: E402


def cfg(state, **mh):
    return SimpleNamespace(state_dir=str(state), repo="o/r", base_branch="Test-Staging", merge_helper=mh)


class FakeGH:
    def __init__(self, prs=(), full=None, checks=None, pushed="2026-01-01T00:00:00Z"):
        self.prs, self.full, self.checks, self.pushed = list(prs), full or {}, checks or {}, pushed
        self.posts = []

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        if method in ("POST", "PUT"):
            self.posts.append((path, body))
            return {}
        if "/pulls?" in path:
            return self.prs
        if "/check-runs" in path:
            return self.checks.get(path.split("/commits/")[1].split("/")[0], {"check_runs": []})
        if "/commits/" in path:
            return {"commit": {"committer": {"date": self.pushed}}}
        if "/pulls/" in path:
            return self.full.get(int(path.rsplit("/", 1)[1]), {})
        return {}


def pr(n, sha="a" * 40, ref="feature", draft=False, labels=()):
    return {"number": n, "title": f"PR {n}", "body": "", "draft": draft, "labels": [{"name": x} for x in labels],
            "head": {"ref": ref, "sha": sha, "repo": {"full_name": "o/r"}}}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def helper(self, key="sk-ant-test", **mh):
        return mergehelp.MergeHelper(cfg(self.dir, **mh), FakeGH(), lambda *_: None, key_reader=lambda: key)

    def test_page_choices_are_checked_and_kept(self):
        h = self.helper()
        self.assertEqual(h.backend(), "api")
        h.set({"backend": "subscription", "model": "claude-sonnet-5-5", "enabled": 0})
        h2 = self.helper()
        self.assertEqual((h2.backend(), h2.setting("model"), h2.setting("enabled")), ("subscription", "claude-sonnet-5-5", False))
        with self.assertRaises(ValueError):
            h.set({"backend": "cheap"})
        with self.assertRaises(ValueError):
            h.set({"model": "gpt; rm -rf"})

    def test_after_the_credit_expires_it_uses_the_subscription(self):
        h = self.helper(budgetUntil=(date.today() - timedelta(days=1)).isoformat())
        self.assertEqual(h.backend(), "subscription")
        self.assertEqual(self.helper(budgetUntil=(date.today() + timedelta(days=3)).isoformat()).backend(), "api")

    def test_api_waits_for_a_key_and_stops_at_the_limits(self):
        self.assertIn("API key", self.helper(key="").why_not())
        h = self.helper(dailyUsd=5, budgetUsd=8, budgetFrom="2000-01-01")
        self.assertEqual(h.why_not(), "")
        h._record({"at": mergehelp.now_iso(), "backend": "api", "cost_usd": 5.5})
        self.assertIn("Today's API limit", h.why_not())
        h._record({"at": "2020-01-01T00:00:00Z", "backend": "api", "cost_usd": 3})
        self.assertIn("used up", h.why_not())
        s = h.spend()
        self.assertEqual((s["api_total_usd"], s["api_today_usd"], s["budget_left_usd"]), (8.5, 5.5, 0.0))
        h.set({"backend": "subscription"})
        self.assertEqual(h.why_not(), "")

    def test_the_key_reaches_only_the_api_backend(self):
        base = {"PATH": "x", "ANTHROPIC_API_KEY": "old", "FLETCHERY_ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t"}
        sub = mergehelp.claude_env("subscription", "", base)
        self.assertNotIn("ANTHROPIC_API_KEY", sub)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", sub)
        self.assertNotIn("FLETCHERY_ANTHROPIC_API_KEY", sub)
        self.assertEqual(mergehelp.claude_env("api", "sk-ant-new", base)["ANTHROPIC_API_KEY"], "sk-ant-new")

    def test_cache_ttl_is_pinned_on_the_api_only(self):
        base = {"PATH": "x", "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}
        self.assertEqual(mergehelp.claude_env("api", "k", base)["CLAUDE_CODE_PROMPT_CACHE_TTL"], "5m")
        self.assertEqual(mergehelp.claude_env("api", "k", base, cache_ttl="1h")["CLAUDE_CODE_PROMPT_CACHE_TTL"], "1h")
        self.assertEqual(mergehelp.claude_env("api", "k", base, cache_ttl="9d")["CLAUDE_CODE_PROMPT_CACHE_TTL"], "5m")
        self.assertEqual(mergehelp.claude_env("subscription", "", base)["CLAUDE_CODE_PROMPT_CACHE_TTL"], "1h")
        self.assertEqual(mergehelp.claude_env("api", "k", base)["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    def test_view_never_carries_the_key(self):
        v = json.dumps(self.helper(key="sk-ant-secret-value").view())
        self.assertNotIn("secret", v)
        self.assertIn('"key_set": true', v)


class PickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_conflicts_first_then_failed_quick_checks_once_per_head(self):
        gh = FakeGH(prs=[pr(1, "1" * 40, "a"), pr(2, "2" * 40, "b"), pr(3, "3" * 40, "c", draft=True), pr(4, "4" * 40, "d", labels=["no-claude"])],
                    full={1: {"mergeable_state": "unstable"}, 2: {"mergeable_state": "dirty"}, 3: {"mergeable_state": "dirty"}, 4: {"mergeable_state": "dirty"}},
                    checks={"1" * 40: {"check_runs": [
                        {"name": "PR check / Cheap checks", "status": "completed", "conclusion": "failure", "started_at": "2", "id": 9},
                        {"name": "PR check / Compile and test", "status": "completed", "conclusion": "failure", "started_at": "2", "id": 8}]}})
        h = mergehelp.MergeHelper(cfg(self.dir), gh, lambda *_: None, key_reader=lambda: "k")
        job = h.next_job()
        self.assertEqual((job["number"], job["kind"]), (2, "conflict"))
        h.tried["2:" + "2" * 40 + ":conflict"] = "x"
        job = h.next_job()
        self.assertEqual((job["number"], job["kind"], job["check"]["id"]), (1, "fix", 9))
        h.tried["1:" + "1" * 40 + ":fix"] = "x"
        self.assertIsNone(h.next_job())

    def test_any_failed_check_is_taken_unless_limited_to_named_ones(self):
        checks = {"5" * 40: {"check_runs": [
            {"name": "PR check / Cheap checks", "status": "completed", "conclusion": "success", "started_at": "2", "id": 1},
            {"name": "PR check / Tip status", "status": "completed", "conclusion": "skipped", "started_at": "2", "id": 2},
            {"name": "PR check / Compile and test", "status": "completed", "conclusion": "failure", "started_at": "1", "id": 3},
            {"name": "PR check / Compile and test", "status": "completed", "conclusion": "success", "started_at": "0", "id": 4}]}}
        gh = FakeGH(prs=[pr(5, "5" * 40, "e")], full={5: {"mergeable_state": "blocked"}}, checks=checks)
        job = mergehelp.MergeHelper(cfg(self.dir), gh, lambda *_: None, key_reader=lambda: "k").next_job()
        self.assertEqual((job["kind"], job["check"]["id"], job["check"]["name"]), ("fix", 3, "PR check / Compile and test"))
        named = mergehelp.MergeHelper(cfg(self.dir, fixAllChecks=False), gh, lambda *_: None, key_reader=lambda: "k")
        self.assertIsNone(named.next_job())

    def test_a_check_running_again_is_not_taken(self):
        checks = {"6" * 40: {"check_runs": [
            {"name": "PR check / Compile and test", "status": "completed", "conclusion": "failure", "started_at": "1", "id": 3},
            {"name": "PR check / Compile and test", "status": "in_progress", "conclusion": None, "started_at": "2", "id": 4}]}}
        gh = FakeGH(prs=[pr(6, "6" * 40, "f")], full={6: {"mergeable_state": "blocked"}}, checks=checks)
        self.assertIsNone(mergehelp.MergeHelper(cfg(self.dir), gh, lambda *_: None, key_reader=lambda: "k").next_job())

    def test_the_log_excerpt_keeps_the_failed_lines_far_above_the_end(self):
        log = "\n".join(["start"] + ["noise %d" % i for i in range(2000)] + ["Error: library -> pharmacy: 6 lane samples on the asphalt"]
                        + ["cleanup %d" % i for i in range(600)])
        out = mergehelp.log_excerpt(log)
        self.assertIn("library -> pharmacy", out)
        self.assertTrue(out.endswith("cleanup 599"))
        self.assertNotIn("noise 5\n", out)

    def test_a_branch_pushed_minutes_ago_is_left_alone(self):
        gh = FakeGH(prs=[pr(2)], full={2: {"mergeable_state": "dirty"}}, pushed=mergehelp.now_iso())
        self.assertIsNone(mergehelp.MergeHelper(cfg(self.dir), gh, lambda *_: None, key_reader=lambda: "k").next_job())


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class ConflictTests(unittest.TestCase):
    """A real repository: origin (bare), a base branch and a PR branch that both changed the same line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        self.origin = d / "origin.git"
        git(d, "init", "--bare", "-b", "Test-Staging", str(self.origin))
        seed = d / "seed"
        git(d, "clone", str(self.origin), str(seed))
        (seed / "a.txt").write_text("one\n")
        git(seed, "add", "."); git(seed, "commit", "-m", "base"); git(seed, "push", "origin", "HEAD:Test-Staging")
        git(seed, "checkout", "-b", "feature")
        (seed / "a.txt").write_text("one from the PR\n")
        git(seed, "commit", "-am", "pr"); git(seed, "push", "origin", "feature")
        self.sha = git(seed, "rev-parse", "HEAD")
        git(seed, "checkout", "Test-Staging")
        (seed / "a.txt").write_text("one from the base\n")
        git(seed, "commit", "-am", "base change"); git(seed, "push", "origin", "Test-Staging")
        self.state = d / "state"
        work = self.state / "merge-helper"
        work.mkdir(parents=True)
        git(work, "clone", "--no-checkout", str(self.origin), str(work / "repo"))
        self.gh = FakeGH()

    def tearDown(self):
        self.tmp.cleanup()

    def helper(self, resolve):
        real = subprocess.run
        calls = []

        def run(cmd, **kw):
            if cmd[0] == "claude":
                calls.append(cmd)
                if resolve:
                    Path(kw["cwd"], "a.txt").write_text("one from the base and the PR\n")
                    real(["git", "add", "a.txt"], cwd=kw["cwd"], check=True)
                out = {"total_cost_usd": 0.42, "num_turns": 3, "is_error": False, "result": "Kept both.", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 120, "cache_creation": {"ephemeral_5m_input_tokens": 120, "ephemeral_1h_input_tokens": 0}}}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
            return real(cmd, **kw)
        h = mergehelp.MergeHelper(cfg(self.state), self.gh, lambda *_: None, run=run, key_reader=lambda: "sk-ant-k")
        h.calls = calls
        return h

    def job(self):
        return {"number": 7, "title": "t", "body": "", "head": "feature", "sha": self.sha, "kind": "conflict"}

    def test_resolved_merge_is_pushed_to_the_pr_branch(self):
        h = self.helper(resolve=True)
        self.assertEqual(h.do(self.job()), "pushed")
        tip = git(self.origin, "rev-parse", "feature")
        self.assertNotEqual(tip, self.sha)
        self.assertEqual(git(self.origin, "show", f"{tip}:a.txt"), "one from the base and the PR")
        self.assertEqual(len(git(self.origin, "rev-list", "--parents", "-n1", tip).split()), 3)   # a merge, not a rewrite
        cmd = h.calls[0]
        self.assertIn("--max-budget-usd", cmd)
        self.assertIn("claude-opus-5-5", cmd)
        for flag in mergehelp.CLAUDE_CACHE_FLAGS:
            self.assertIn(flag, cmd)
        run = h.runs()[-1]
        self.assertEqual((run["result"], run["cost_usd"], run["backend"]), ("pushed", 0.42, "api"))
        self.assertEqual((run["cache_read_tokens"], run["cache_write_tokens"], run["cache_write_1h_tokens"]), (900, 120, 0))
        self.assertIn("resolved the conflicts", self.gh.posts[0][1]["body"])
        self.assertFalse((self.state / "merge-helper" / "pr-7").exists())

    def test_a_conflict_left_pushes_nothing(self):
        h = self.helper(resolve=False)
        self.assertEqual(h.do(self.job()), "gave up")
        self.assertEqual(git(self.origin, "rev-parse", "feature"), self.sha)
        self.assertIn("needs a person", self.gh.posts[0][1]["body"])

    def test_a_try_the_pc_broke_is_tried_again_later_and_told_only_at_the_last(self):
        h = self.helper(resolve=True)
        key = f"7:{self.sha}:conflict"

        def broken():
            raise OSError("the clone could not be fetched")
        real_clone, h.clone = h.clone, broken
        self.assertEqual(h.do(self.job()), "error")
        self.assertEqual(self.gh.posts, [])                       # one bad moment is not told on the PR
        self.assertFalse(h.may_try(key))                          # not straight away...
        h.errors[key]["at"] = "2000-01-01T00:00:00Z"
        self.assertTrue(h.may_try(key))                           # ...but after retryMinutes
        saved = json.loads((self.state / "merge-helper.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["_errors"][key]["n"], 1)           # and a restart keeps the count
        again = mergehelp.MergeHelper(cfg(self.state), self.gh, lambda *_: None, key_reader=lambda: "k")
        self.assertEqual(again.errors[key]["n"], 1)
        h.clone = real_clone
        self.assertEqual(h.do(self.job()), "pushed")              # the retry lands
        self.assertNotIn(key, h.errors)
        self.assertFalse(h.may_try(key))

    def test_a_try_that_keeps_breaking_is_told_once_and_left(self):
        h = self.helper(resolve=True)
        key = f"7:{self.sha}:conflict"

        def broken():
            raise OSError("disk full")
        h.clone = broken
        for _ in range(2):
            self.assertEqual(h.do(self.job()), "error")
            h.errors[key]["at"] = "2000-01-01T00:00:00Z"
        self.assertEqual(h.do(self.job()), "gave up")
        self.assertEqual(len(self.gh.posts), 1)
        self.assertIn("broke 3 times", self.gh.posts[0][1]["body"])
        h.errors[key]["at"] = "2000-01-01T00:00:00Z"
        self.assertFalse(h.may_try(key))



class UnionMergeTest(unittest.TestCase):
    def table(self, keys, end):
        return b"".join(b"%s,Text %s,note" % (k.encode(), k.encode()) + end for k in ["Key"] + keys)

    def test_doubled_file_is_seen_and_rebuilt(self):
        old = [f"K{i}" for i in range(400)]
        base = self.table(old, b"\r\n")
        theirs = self.table(old + ["Tip.A", "Tip.B"], b"\r\n")
        mine = self.table(old + ["Mine.A"], b"\n")            # this branch rewrote the file with LF endings
        merged = theirs + mine                                # what merge=union made of it
        self.assertTrue(mergehelp.union_doubled(merged, mine, theirs))
        out = mergehelp.union_rebuild(base, mine, theirs)
        self.assertEqual(out, theirs + b"Mine.A,Text Mine.A,note\r\n")
        self.assertFalse(mergehelp.union_doubled(out, mine, theirs))

    def test_a_proper_union_merge_is_left_alone(self):
        old = [f"K{i}" for i in range(400)]
        theirs = self.table(old + ["Tip.A"], b"\r\n")
        mine = self.table(old + ["Mine.A"], b"\r\n")
        merged = self.table(old + ["Tip.A", "Mine.A"], b"\r\n")
        self.assertFalse(mergehelp.union_doubled(merged, mine, theirs))

    def test_rebuild_keeps_the_tips_bytes_and_adds_a_final_line_ending(self):
        theirs = b"a\r\nb\nc"
        out = mergehelp.union_rebuild(b"a\r\n", b"a\nx\nx\n\n", theirs)
        self.assertEqual(out, b"a\r\nb\nc\r\nx\r\n")


    def test_real_git_union_merge_is_repaired_before_the_push(self):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("git"):
            self.skipTest("no git")
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)

            def git(*a):
                return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d,
                                      capture_output=True, text=True, check=True)
            old = [f"K{i}" for i in range(300)]
            git("init", "-q", "-b", "tip")
            (wt / ".gitattributes").write_bytes(b"*.csv -text merge=union\n")
            (wt / "s.csv").write_bytes(self.table(old, b"\r\n"))
            git("add", "-A"); git("commit", "-q", "-m", "base")
            git("checkout", "-q", "-b", "pr")
            (wt / "s.csv").write_bytes(self.table(old + ["Mine.A"], b"\n"))
            git("commit", "-q", "-am", "mine, with LF endings")
            git("checkout", "-q", "tip")
            (wt / "s.csv").write_bytes(self.table(old + ["Tip.A"], b"\r\n"))
            git("commit", "-q", "-am", "tip")
            git("checkout", "-q", "pr")
            git("merge", "-q", "--no-edit", "--no-ff", "tip")
            self.assertGreater((wt / "s.csv").read_bytes().count(b"K7,"), 1)      # the union merge doubled it
            helper = object.__new__(mergehelp.MergeHelper)
            helper._run, lines = subprocess.run, []
            helper.log = lines.append
            self.assertEqual(helper._repair_union(wt), ["s.csv"])
            self.assertEqual((wt / "s.csv").read_bytes(), self.table(old + ["Tip.A"], b"\r\n") + b"Mine.A,Text Mine.A,note\r\n")
            self.assertEqual(git("status", "--porcelain").stdout.strip(), "")
            self.assertEqual(len(git("rev-list", "--parents", "-1", "HEAD").stdout.split()), 3)   # still the merge commit
            self.assertEqual(helper._repair_union(wt), [])


class AutoMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def helper(self, gh, **mh):
        return mergehelp.MergeHelper(cfg(self.dir, **mh), gh, lambda *_: None, key_reader=lambda: "sk-ant-test")

    @staticmethod
    def full(n, state, sha="a" * 40):
        return {"number": n, "state": "open", "mergeable_state": state, "node_id": f"PR_{n}",
                "head": {"sha": sha, "repo": {"full_name": "o/r"}}}

    def test_a_blocked_pr_gets_auto_merge_and_a_clean_one_is_merged(self):
        gh = FakeGH([pr(1), pr(2)], {1: self.full(1, "blocked"), 2: self.full(2, "clean")})
        self.helper(gh).arm_merges()
        self.assertEqual(gh.posts[0][0], "graphql")
        self.assertEqual(gh.posts[0][1]["variables"], {"id": "PR_1"})
        self.assertEqual(gh.posts[1], ("repos/o/r/pulls/2/merge", {"merge_method": "merge", "sha": "a" * 40}))

    def test_drafts_opted_out_and_already_armed_prs_are_left(self):
        armed = dict(pr(3), auto_merge={"merge_method": "merge"})
        gh = FakeGH([pr(1, draft=True), pr(2, labels=("do-not-merge",)), armed],
                    {n: self.full(n, "blocked") for n in (1, 2, 3)})
        self.helper(gh).arm_merges()
        self.assertEqual(gh.posts, [])

    def test_the_same_head_is_armed_once_and_a_new_head_again(self):
        gh = FakeGH([pr(1)], {1: self.full(1, "blocked")})
        h = self.helper(gh)
        h.arm_merges()
        h.arm_merges()
        self.assertEqual(len(gh.posts), 1)
        gh.prs, gh.full = [pr(1, sha="b" * 40)], {1: self.full(1, "blocked", sha="b" * 40)}
        self.helper(gh).arm_merges()
        self.assertEqual(len(gh.posts), 2)

    def test_an_undecided_pr_waits_for_github(self):
        gh = FakeGH([pr(1)], {1: self.full(1, "unknown")})
        h = self.helper(gh)
        h.arm_merges()
        self.assertEqual(gh.posts, [])
        gh.full = {1: self.full(1, "behind")}
        h.arm_merges()
        self.assertEqual(len(gh.posts), 1)

    def test_a_refused_auto_merge_is_retried_after_the_wait_and_cleared_on_success(self):
        class RefuseOnce(FakeGH):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.refuse = True

            def api(self, path, method="GET", body=None, paginate=False, fresh=False):
                if self.refuse and method == "POST" and path == "graphql":
                    self.refuse = False
                    raise RuntimeError("Pull request is in unstable status")
                return super().api(path, method, body, paginate=paginate, fresh=fresh)

        gh = RefuseOnce([pr(1)], {1: self.full(1, "blocked")})
        h = self.helper(gh)
        h.arm_merges()
        self.assertEqual(gh.posts, [])
        key = "1:" + "a" * 40 + ":merge"
        self.assertEqual(h.errors[key]["n"], 1)
        h.arm_merges()                                  # still inside the retry window
        self.assertEqual(gh.posts, [])
        old = (datetime.now(timezone.utc) - timedelta(minutes=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
        h.errors[key]["at"] = old
        h.arm_merges()
        self.assertEqual(len(gh.posts), 1)
        self.assertNotIn(key, h.errors)

    def test_it_runs_even_when_the_api_limits_pause_fixing(self):
        gh = FakeGH([pr(1)], {1: self.full(1, "blocked")})
        h = mergehelp.MergeHelper(cfg(self.dir), gh, lambda *_: None, key_reader=lambda: "")
        h._pass()
        self.assertEqual(len(gh.posts), 1)
        self.assertIn("API key", h.last_note)


if __name__ == "__main__":
    unittest.main()
