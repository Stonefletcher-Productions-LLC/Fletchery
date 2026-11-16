"""Dead ends: queued work nothing would ever start is repaired or surfaced (deadends.py). No real services.

LifeShare 2026-11-14: 91 queued tasks, a healthy backend and no worker coding for hours. 22 depended on issues that had
lost the task label, were done by hand, or were PRs closed unmerged ("depends on missing issue"), and 16 had PRs that
failed their PR check, whose fixer ended without a new commit ("waits for new feedback")."""
import unittest
from datetime import datetime, timedelta, timezone

import deadends
import graph
import test_dispatch_resilience as fixtures
from github import PullRequest
from graph import Task, QUEUED, label_for


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def why_of(engine, number):
    return {r['number']: r for r in engine.snapshot['tasks']}[number]


class PrCheckDeadEndTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, check_hours, retried=False, seen_after=True, passed=False):
        # Like the live tasks: queued with a saved blocker plan already applied, so the task moves only through fixers.
        task = Task(1, 'Feature whose PR failed its PR check', '<!-- fletchery-blocker-applied:abc123 -->',
                    [label_for(QUEUED)])
        engine, gh, rt = fixtures.DispatchResilienceTests.engine(self, {1: task}, max_parallel=2)
        engine.state.repair_continuations['12'] = 'abc123'
        engine.ctx.run_gates = lambda *a, **kw: []
        pr = PullRequest(12, 'PR', task.branch, 'Test-Staging', 'a' * 40, [graph.PREFIX], 'Closes #1')
        gh.prs = [pr]
        engine.state.pr_checks['12'] = {'head': pr.head_sha, 'passed': passed, 'at': ago(check_hours),
                                        'output': 'Merge the PR onto the base failed (exit 1)', 'machine': 'NODE-01'}
        if retried:
            engine.state.pr_checks['12']['retried'] = ago(1)
        engine.state.fix_attempts['12'] = 1
        if seen_after:
            engine.state.seen['12'] = ago(max(0, check_hours - 0.5))
        return engine, gh, rt, task, pr

    async def test_recent_failure_says_when_the_fresh_repair_starts(self):
        engine, gh, rt, task, pr = self.setup(check_hours=1)
        engine.dispatch()
        self.assertFalse(engine.jobs)
        row = why_of(engine, 1)
        self.assertIn('failed its PR check', row['why'])
        self.assertIn('one fresh repair starts', row['why'])
        self.assertFalse(row['needs_person'])

    async def test_old_failure_gets_exactly_one_fresh_fixer_then_needs_a_person(self):
        engine, gh, rt, task, pr = self.setup(check_hours=deadends.CHECK_RETRY_HOURS + 1)
        await engine.once()
        self.assertIn(('fixer', 1), [(role, n) for role, n, _ in rt.calls])
        self.assertTrue(engine.state.pr_checks['12'].get('retried'))
        calls = len(rt.calls)
        task.labels = [label_for(QUEUED)]
        engine.state.seen['12'] = ago(0)
        await engine.once()
        self.assertEqual(len(rt.calls), calls)
        row = why_of(engine, 1)
        self.assertTrue(row['why'].startswith('needs a person'), row['why'])
        self.assertTrue(row['needs_person'])

    async def test_report_not_yet_taken_is_left_to_the_normal_fixer(self):
        engine, gh, rt, task, pr = self.setup(check_hours=10, seen_after=False)
        self.assertFalse(deadends.check_repair_due(pr, engine.state.pr_checks['12'], None))
        self.assertEqual(deadends.check_stuck_wait(pr, engine.state.pr_checks['12'], None), '')

    def test_new_head_or_pass_clears_it(self):
        pr = PullRequest(12, 'PR', 'b', 'Test-Staging', 'b' * 40, [])
        self.assertEqual(deadends.check_stuck_wait(pr, {'head': 'a' * 40, 'passed': False, 'at': ago(9)}, ago(1)), '')
        pr = PullRequest(12, 'PR', 'b', 'Test-Staging', 'a' * 40, [])
        self.assertEqual(deadends.check_stuck_wait(pr, {'head': 'a' * 40, 'passed': True, 'at': ago(9)}, ago(1)), '')


class DependencyDeadEndTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, info):
        task = Task(1, 'Feature that waits on #50', 'Depends on: #50', [label_for(QUEUED)])
        engine, gh, rt = fixtures.DispatchResilienceTests.engine(self, {1: task}, max_parallel=0)
        gh.infos[50] = info
        return engine, gh, task

    def test_lost_task_label_is_given_back(self):
        engine, gh, task = self.setup({'number': 50, 'state': 'open', 'labels': [{'name': label_for(QUEUED)}]})
        engine.dispatch()
        self.assertIn(f'labels #50: {graph.PREFIX}', gh.writes)
        self.assertIn('label back', gh.comments[50][0])
        row = why_of(engine, 1)
        self.assertIn('#50 (got its', row['why'])
        self.assertFalse(row['needs_person'])

    def test_closed_unmerged_pr_needs_a_person_and_is_reported_once(self):
        engine, gh, task = self.setup({'number': 50, 'state': 'closed', 'pull_request': {}, 'merged': False, 'labels': []})
        engine.dispatch()
        row = why_of(engine, 1)
        self.assertTrue(row['needs_person'])
        self.assertIn('closed without merging', row['why'])
        self.assertEqual(len(gh.comments.get(1, [])), 1)
        self.assertIn('Depends on', gh.comments[1][0])
        engine.dispatch()
        self.assertEqual(len(gh.comments.get(1, [])), 1)
        self.assertIn('Queued but stuck', engine.status_text({1: task}, {}, {1: row['why']}, []) if False else
                      '\n'.join(engine.status_text({1: task}, {}, {1: row['why']}, []).splitlines()))

    def test_open_issue_without_any_label_needs_a_person(self):
        engine, gh, task = self.setup({'number': 50, 'state': 'open', 'labels': [{'name': 'area: town'}]})
        engine.dispatch()
        row = why_of(engine, 1)
        self.assertTrue(row['needs_person'])
        self.assertIn('not a Fletchery task', row['why'])
        self.assertNotIn(f'labels #50: {graph.PREFIX}', gh.writes)

    def test_open_pr_is_a_real_wait(self):
        engine, gh, task = self.setup({'number': 50, 'state': 'open', 'pull_request': {}, 'labels': []})
        engine.dispatch()
        row = why_of(engine, 1)
        self.assertFalse(row['needs_person'])
        self.assertIn('open PR', row['why'])
        self.assertFalse(gh.comments.get(1))


if __name__ == '__main__':
    unittest.main()
