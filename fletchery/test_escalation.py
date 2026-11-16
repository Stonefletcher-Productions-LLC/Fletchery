"""The escalation desk: a task about to say "needs a person" goes to opus (then fable) first. No real services.

LifeShare 2026-11-15: 31 items in Needs you, every worker idle. Cheap fixers gave up, nothing stronger ever looked."""
import json
import unittest
from datetime import datetime, timedelta, timezone

import escalation
import test_dispatch_resilience as fixtures
from agents import AgentPool, AgentSpec
from github import PullRequest
from graph import Task, QUEUED, label_for
from loop import EXHAUSTED_RETRY_HOURS, Engine


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


class PureTests(unittest.TestCase):
    def test_tiers_follow_failures(self):
        k = escalation.failure_key("a" * 40, "")
        self.assertEqual(escalation.next_tier(None, k, 2), 0)
        self.assertEqual(escalation.next_tier({"key": "other", "tier": 1, "at": ago(0)}, k, 2), 0)
        self.assertEqual(escalation.next_tier({"key": k, "tier": 0, "verdict": "failed", "at": ago(0)}, k, 2), 1)
        self.assertIsNone(escalation.next_tier({"key": k, "tier": 1, "verdict": "failed", "at": ago(1)}, k, 2))
        self.assertEqual(escalation.next_tier({"key": k, "tier": 1, "verdict": "failed",
                                               "at": ago(escalation.REESCALATE_HOURS + 1)}, k, 2), 0)
        self.assertIsNone(escalation.next_tier({"key": k, "tier": 0, "running": True}, k, 2))
        self.assertIsNone(escalation.next_tier({"key": k, "tier": 0, "verdict": "needs_alex", "at": ago(99)}, k, 2))
        self.assertIsNone(escalation.next_tier(None, k, 0))

    def test_only_owner_actions_count(self):
        self.assertTrue(escalation.accept_owner_action("Add `Bash(x:*)` to settings.json", "settings"))
        self.assertFalse(escalation.accept_owner_action("Run ImportHomeKit on NODE-01", "lane"))
        self.assertFalse(escalation.accept_owner_action("", "github"))

    def test_wait_text_marks_only_owner_actions_and_spent_tiers_as_needs_you(self):
        k = "k"
        tiers = ["opus", "fable"]
        self.assertTrue(escalation.wait_text({"key": k, "verdict": "needs_alex", "action": "Click Merge"}, k, tiers)
                        .startswith(escalation.NEEDS_ALEX))
        self.assertFalse(escalation.wait_text({"key": k, "tier": 0, "verdict": "failed", "at": ago(0)}, k, tiers)
                         .startswith(escalation.NEEDS_ALEX))
        self.assertTrue(escalation.wait_text({"key": k, "tier": 1, "verdict": "failed", "at": ago(0)}, k, tiers)
                        .startswith(escalation.NEEDS_ALEX))

    def test_agents_in_order_and_present(self):
        pool = AgentPool([AgentSpec("sonnet", kind="fake"), AgentSpec("fable", kind="fake"), AgentSpec("opus", kind="fake")],
                         {}, lambda _: None)
        self.assertEqual(escalation.agents(pool), ["opus", "fable"])
        pool.routing["escalationAgents"] = ["fable"]
        self.assertEqual(escalation.agents(pool), ["fable"])


class DeskTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, fixer_answer=None):
        task = Task(1, "Feature with a spent PR", "<!-- fletchery-blocker-applied:abc123 -->", [label_for(QUEUED)])
        engine, gh, rt = fixtures.DispatchResilienceTests.engine(self, {1: task}, max_parallel=2)
        if fixer_answer is not None:
            rt.script[("fixer", 1)] = fixer_answer
        pool = AgentPool([AgentSpec("haiku", kind="fake", tier=1), AgentSpec("opus", kind="fake", tier=3)], {},
                         lambda _: rt)
        engine.ctx.agents = pool
        engine = Engine(engine.ctx, engine.state_dir)
        engine.check_accounts = lambda force=False: None
        engine.state.repair_continuations["12"] = "abc123"
        engine.ctx.run_gates = lambda *a, **kw: []
        pr = PullRequest(12, "PR", task.branch, "Test-Staging", "a" * 40, ["ready"], "Closes #1")
        gh.prs = [pr]
        # Repairs spent and the fresh one used too: before the desk, this said "needs a person" for good.
        engine.state.merge_excluded["12"] = {"head": pr.head_sha, "why": "Conflicts in LSBuild.h", "at": ago(EXHAUSTED_RETRY_HOURS + 2),
                                             "round": 82, "retried": ago(1)}
        engine.state.fix_attempts["12"] = engine.cfg.fixer_attempts
        engine.state.seen["12"] = ago(1)
        return engine, gh, rt, task, pr

    def why(self, engine):
        return {r["number"]: r["why"] for r in engine.snapshot["tasks"]}[1]

    async def test_spent_pr_goes_to_opus_not_needs_you(self):
        engine, gh, rt, task, pr = self.setup({"summary": "Merged Test-Staging, fixed LSBuild.h", "pushed": False})
        await engine.once()
        fixer_prompts = [p for role, n, p in rt.calls if role == "fixer" and n == 1]
        self.assertTrue(fixer_prompts, rt.calls)
        self.assertIn("ESCALATION (opus)", fixer_prompts[-1])
        rec = engine.state.escalations["1"]
        self.assertEqual((rec["agent"], rec["verdict"], rec["running"]), ("opus", "fixed", False))

    async def test_owner_only_verdict_reaches_needs_you_with_the_action(self):
        engine, gh, rt, task, pr = self.setup({"summary": "Blocked by a permission prompt", "pushed": False,
                                               "needs_alex": "Add \"Bash(gh pr merge:*)\" to permissions.allow",
                                               "needs_alex_kind": "settings"})
        await engine.once()
        self.assertEqual(engine.state.escalations["1"]["verdict"], "needs_alex")
        task.labels = [label_for(QUEUED)]
        engine.dispatch()
        why = self.why(engine)
        self.assertTrue(why.startswith("needs a person: only you can do this"), why)
        self.assertIn("permissions.allow", why)
        self.assertTrue(any("this needs you" in w for w in gh.writes), gh.writes)

    async def test_non_owner_verdict_is_not_accepted(self):
        engine, gh, rt, task, pr = self.setup({"summary": "Needs a node import", "pushed": False,
                                               "needs_alex": "Run ImportHomeKit", "needs_alex_kind": "lane"})
        await engine.once()
        self.assertEqual(engine.state.escalations["1"]["verdict"], "failed")
        # Only one tier configured (no fable here): spent, so it shows as needing a person with the summary.
        task.labels = [label_for(QUEUED)]
        engine.dispatch()
        self.assertIn("opus could not fix or classify it", self.why(engine))

    async def test_close_verdict_closes_issue_and_pr(self):
        engine, gh, rt, task, pr = self.setup({"summary": "Landed by #2044", "pushed": False, "close": "PR #2044"})
        await engine.once()
        self.assertEqual(engine.state.escalations["1"]["verdict"], "closed")
        self.assertIn("close PR #12", gh.writes)
        self.assertIn(1, gh.closed)

    async def test_hold_file_keeps_the_desk_off_a_pr(self):
        engine, gh, rt, task, pr = self.setup()
        (engine.state_dir).mkdir(parents=True, exist_ok=True)
        (engine.state_dir / "escalation-hold.json").write_text(json.dumps({"prs": [12]}), encoding="utf-8")
        await engine.once()
        self.assertNotIn("1", engine.state.escalations)
        self.assertTrue(self.why(engine).startswith("needs a person"))


class StrongRetryTests(unittest.TestCase):
    def thread(self, body):
        return {"body": body, "comments": [{"fletchery": True, "id": 1, "body": "Fletchery fixer (attempt 2) could not fix PR #5: lock"}]}

    def test_strong_planner_gets_one_retry_past_the_caps(self):
        import blockers
        body = "x " + " ".join(f"{blockers.RETRY}k{i} -->" for i in range(3))
        plan = {"retry": "Merge Test-Staging and fix LSBuild.h", "issues": [], "reuse": [], "needs_human": ""}
        self.assertTrue(blockers.constrain(plan, self.thread(body))["needs_human"])
        strong = blockers.constrain(plan, self.thread(body), "opus")
        self.assertEqual((strong["retry"], strong["strong"]), (plan["retry"], "opus"))
        used = body + f" {blockers.STRONG_RETRY}{blockers.failure_key(self.thread(body))} -->"
        self.assertTrue(blockers.constrain(plan, self.thread(used), "opus")["needs_human"])


if __name__ == "__main__":
    unittest.main()
