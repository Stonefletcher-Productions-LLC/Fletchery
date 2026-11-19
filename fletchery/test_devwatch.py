"""The Live tab's feed (devwatch.py) and the machines' reporter (reporter.py): what is shown, what counts as stuck, the
free 304s, and the /v1/report route. No real GitHub: a fake answers by path, and a local server stands in for ETags."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import devwatch  # noqa: E402
import reporter  # noqa: E402


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


ORG, REPO, OLD = "Acme", "Acme/Game", "Acme/Old"


class FakeGitHub:
    remaining, calls, free = 4000, 0, 0

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, path):
        self.asked.append(path)
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise devwatch.GitHubError(404, "Not Found")


def world(**over):
    sha_ok, sha_bad = "a" * 40, "b" * 40
    answers = {
        "user/repos": [
            {"full_name": REPO, "owner": {"login": ORG, "type": "Organization"}, "pushed_at": ago(30), "html_url": "https://github.com/Acme/Game"},
            {"full_name": OLD, "owner": {"login": ORG, "type": "Organization"}, "pushed_at": ago(60 * 24 * 90)},
            {"full_name": "Acme/Gone", "owner": {"login": ORG, "type": "Organization"}, "pushed_at": ago(5), "archived": True}],
        f"repos/{REPO}/actions/runs/7/jobs": {"jobs": [{"name": "Compile and test", "status": "in_progress", "runner_name": "ALEX-PC-unreal-pr",
                                                         "steps": [{"name": "Checkout", "status": "completed"},
                                                                   {"name": "Build editor", "status": "in_progress", "started_at": ago(160)},
                                                                   {"name": "Specs", "status": "queued"}]}]},
        f"repos/{REPO}/actions/runs": {"workflow_runs": [
            {"id": 7, "name": "PR check", "status": "in_progress", "event": "pull_request", "created_at": ago(80), "run_started_at": ago(80),
             "updated_at": ago(1), "html_url": "https://github.com/Acme/Game/actions/runs/7", "pull_requests": [{"number": 12}]},
            {"id": 8, "name": "PR check", "status": "queued", "created_at": ago(30), "updated_at": ago(30), "pull_requests": [{"number": 13}]},
            {"id": 6, "name": "PR check", "status": "completed", "conclusion": "success", "created_at": ago(200), "updated_at": ago(180)}]},
        f"repos/{REPO}/pulls": [
            {"number": 12, "title": "Bus lines", "html_url": "https://github.com/Acme/Game/pull/12", "head": {"sha": sha_ok, "ref": "bus"}, "base": {"ref": "main"},
             "user": {"login": "dev"}, "updated_at": ago(2), "labels": []},
            {"number": 13, "title": "Weather", "html_url": "https://github.com/Acme/Game/pull/13", "head": {"sha": sha_bad, "ref": "wx"}, "base": {"ref": "main"},
             "user": {"login": "dev"}, "updated_at": ago(5), "labels": [{"name": "status:needs-alex"}]}],
        f"repos/{REPO}/commits/{sha_ok}/check-runs": {"check_runs": [{"name": "Compile and test", "status": "in_progress", "started_at": ago(20)}]},
        f"repos/{REPO}/commits/{sha_bad}/check-runs": {"check_runs": [{"name": "Compile and test", "status": "completed", "conclusion": "failure",
                                                                       "completed_at": ago(4), "html_url": "https://github.com/Acme/Game/runs/1"}]},
        f"repos/{REPO}/issues": [{"number": 40, "title": "Bench clipping", "html_url": "https://github.com/Acme/Game/issues/40", "labels": [{"name": "foreman:blocked"}],
                                  "created_at": ago(3), "updated_at": ago(2)},
                                 {"number": 41, "title": "Pick a pack", "html_url": "https://github.com/Acme/Game/issues/41", "labels": [{"name": "needs-alex"}],
                                  "created_at": ago(3), "updated_at": ago(2)},
                                 {"number": 13, "title": "Weather", "pull_request": {}, "labels": []}],
        f"orgs/{ORG}/actions/runners": {"runners": [
            {"id": 1, "name": "ALEX-PC-unreal-pr", "status": "online", "busy": True, "labels": [{"name": "self-hosted", "type": "read-only"}, {"name": "unreal-pr", "type": "custom"}]},
            {"id": 2, "name": "NODE-unreal-light", "status": "offline", "busy": False, "labels": [{"name": "unreal-light", "type": "custom"}]}]},
    }
    answers.update(over)
    return answers


class Feed(unittest.TestCase):
    def watch(self, **over):
        w = devwatch.DevWatch({"names": {"ALEX-PC": "Gaming PC"}}, github=FakeGitHub(world(**over)))
        w.poll()
        return w

    def test_finds_repositories_by_itself(self):
        w = self.watch()
        self.assertEqual([r["full_name"] for r in w.repos], [REPO])     # old and archived ones are left out
        self.assertEqual(w.interval, w.fast)                             # something runs: ask often

    def test_snapshot(self):
        s = self.watch().snapshot()
        run = s["runs"][0]
        self.assertEqual((run["id"], run["runner"], run["step"], run["steps_done"], run["steps_total"]), (7, "ALEX-PC-unreal-pr", "Build editor", 1, 3))
        self.assertEqual(sorted(i["number"] for i in s["issues"]), [40, 41])      # PRs are not issues
        self.assertEqual(s["prs"][0]["checks"]["state"], "failed")
        pc = next(m for m in s["machines"] if m["name"] == "Gaming PC")
        self.assertTrue(pc["busy"])
        self.assertIn("PR check · PR #12", pc["activities"][0]["title"])
        node = next(r for r in s["runners"] if r["name"] == "NODE-unreal-light")
        self.assertEqual((node["machine"], node["online"]), ("NODE", False))

    def test_stuck_and_blocked(self):
        titles = " | ".join(b["title"] for b in self.watch().snapshot()["blockers"])
        for want in ("waited 30 min for a runner", "\"Build editor\" has run 160 min", "PR #13 fails Compile and test",
                     "Runner NODE-unreal-light is offline", "PR #13 is marked blocked", "#41 needs you"):
            self.assertIn(want, titles)

    def test_issue_blocked_on_other_work_is_parked_not_stuck(self):
        s = self.watch().snapshot()
        self.assertNotIn("#40", " | ".join(b["title"] for b in s["blockers"]))
        self.assertEqual([p["title"] for p in s["parked"]], ["#40 Bench clipping"])
        self.assertEqual(s["counts"]["parked"], 1)
        self.assertEqual(s["counts"]["blockers"], len(s["blockers"]))

    def test_status_only_commit_counts_as_checked(self):
        # A commit the PR check pushed with its own token has no check runs, only the statuses the job posted on it.
        sha = "b" * 40
        w = self.watch_first({f"repos/{REPO}/commits/{sha}/check-runs": {"check_runs": []},
                              f"repos/{REPO}/commits/{sha}/status": {"statuses": [
                                  {"id": 5, "context": "PR check / Compile and test", "state": "failure", "description": "Checked with its generated content",
                                   "target_url": "https://github.com/Acme/Game/actions/runs/9", "created_at": ago(4), "updated_at": ago(4)},
                                  {"id": 6, "context": "PR check / Cheap checks", "state": "success", "created_at": ago(4), "updated_at": ago(4)}]}})
        s = w.snapshot()
        pr = next(p for p in s["prs"] if p["number"] == 13)
        self.assertEqual((pr["checks"]["state"], pr["checks"]["failed"]), ("failed", ["PR check / Compile and test"]))
        self.assertIn("PR #13 fails PR check / Compile and test", " | ".join(b["title"] for b in s["blockers"]))

    def test_red_base_branch_is_a_banner_and_one_blocker(self):
        main = "c" * 40
        w = self.watch_first({
            f"repos/{REPO}/commits/main/check-runs": {"check_runs": [
                {"id": 70, "name": "Compile and test", "status": "completed", "conclusion": "failure", "head_sha": main,
                 "completed_at": ago(50), "output": {"annotations_count": 1}}]},
            f"repos/{REPO}/check-runs/70/annotations": [{"annotation_level": "failure", "title": "LifeShare.Town.Fishing"}],
            f"repos/{REPO}/rules/branches/main": [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "Compile and test"}]}}]})
        s = w.snapshot()
        base = s["bases"][0]
        self.assertEqual((base["branch"], base["state"], base["required"]), ("main", "red", True))
        self.assertEqual(base["failing"], [{"name": "Compile and test", "tests": ["LifeShare.Town.Fishing"]}])
        self.assertEqual(base["held"], 1)          # PR #13 fails the same check
        blk = [b for b in s["blockers"] if b["kind"] == "base"]
        self.assertEqual(len(blk), 1)
        self.assertEqual(blk[0]["key"], f"base:{REPO}:main")
        self.assertEqual(s["blockers"][0]["kind"], "base")
        w.poll()                                    # the same commit again changes nothing
        self.assertEqual(w.store.query("SELECT COUNT(*) FROM base_checks"), [(1,)])

    def test_red_checks_grouped_by_cause(self):
        # PR #13 fails the same check as its red base (inherited); PR #12 fails in a setup step (the machine).
        main, sha_ok, sha_bad = "c" * 40, "a" * 40, "b" * 40
        w = self.watch_first({
            f"repos/{REPO}/commits/main/check-runs": {"check_runs": [
                {"id": 70, "name": "Compile and test", "status": "completed", "conclusion": "failure", "head_sha": main, "completed_at": ago(50)}]},
            f"repos/{REPO}/commits/{sha_ok}/check-runs": {"check_runs": [
                {"id": 81, "name": "Cheap checks", "status": "completed", "conclusion": "failure", "completed_at": ago(9),
                 "details_url": "https://github.com/Acme/Game/actions/runs/1/job/81"}]},
            f"repos/{REPO}/actions/jobs/81": {"runner_name": "ALEX-PC-unreal-light", "steps": [
                {"name": "Set up job", "conclusion": "success"}, {"name": "Checkout with LFS", "conclusion": "failure"}]}})
        s = w.snapshot()
        by = {(g["cause"], g["prs"][0]["number"]) for g in s["problems"]}
        self.assertEqual(by, {("base", 13), ("machine", 12)})
        self.assertEqual(s["causes"], {"own": 0, "base": 1, "machine": 1, "flaky": 0, "cancelled": 0, "red": 2, "problems_own": 0})
        self.assertNotIn("check", {b["kind"] for b in s["blockers"]})     # nothing of their own to fix
        self.assertEqual({p["number"]: p["checks"].get("cause") for p in s["prs"]}, {12: "machine", 13: "base"})
        g = next(g for g in s["problems"] if g["cause"] == "machine")
        self.assertEqual(g["runner"], "ALEX-PC-unreal-light")

    def test_queue_pools_waiting_and_who_cancelled(self):
        runs = world()[f"repos/{REPO}/actions/runs"]["workflow_runs"] + [
            {"id": 9, "name": "PR check", "status": "completed", "conclusion": "cancelled", "event": "pull_request",
             "created_at": ago(70), "updated_at": ago(60), "pull_requests": [{"number": 12}]}]
        w = self.watch_first({
            f"repos/{REPO}/actions/runs/8/jobs": {"jobs": [{"id": 80, "name": "Compile and test", "status": "queued",
                                                            "labels": ["self-hosted", "unreal-pr"], "created_at": ago(30)}]},
            f"repos/{REPO}/actions/runs/9/jobs": {"jobs": [{"id": 90, "name": "Compile and test", "status": "completed", "conclusion": "cancelled"}]},
            f"repos/{REPO}/check-runs/90/annotations": [{"message": "The run was canceled by @ProjektPayne."}]},
            **{f"repos/{REPO}/actions/runs": {"workflow_runs": runs}})
        q = w.snapshot()["queue"]
        pr_pool = next(p for p in q["pools"] if p["name"] == "unreal-pr")
        self.assertEqual((pr_pool["online"], pr_pool["busy"], pr_pool["waiting_n"]), (1, 1, 1))
        self.assertEqual(pr_pool["waiting"][0]["pr"], 13)
        self.assertIsNone(pr_pool["waiting"][0]["start_in"])          # nothing learned yet: no made-up time
        self.assertEqual(pr_pool["running"][0]["pr"], 12)
        self.assertEqual([(c["run"], c["how"], c["who"]) for c in q["cancelled"]], [(9, "person", "ProjektPayne")])
        w.note_cancel(REPO, 9, "from the phone")
        w._base_cache = (0.0, None)
        self.assertEqual(w.snapshot()["queue"]["cancelled"][0]["how"], "fletchery")

    def watch_first(self, first: dict, **over):
        """A world whose `first` answers win over the usual ones (FakeGitHub answers the first matching prefix)."""
        answers = dict(first)
        for k, v in world(**over).items():
            answers.setdefault(k, v)
        w = devwatch.DevWatch({"names": {"ALEX-PC": "Gaming PC"}}, github=FakeGitHub(answers))
        w.poll()
        return w

    def test_only_real_blockers(self):
        sha_bad = "b" * 40
        pr_labels = [{"name": "sev: blocker"}]
        first = {
            # run 8 waits for unreal-pr, whose only runner is busy: a queue, not stuck
            f"repos/{REPO}/actions/runs/8/jobs": {"jobs": [{"name": "Compile and test", "status": "queued", "labels": ["self-hosted", "unreal-pr"]}]},
            # the old failure was re-run on the same commit and is running; a cancelled check and a "No result" one need a re-run
            f"repos/{REPO}/commits/{sha_bad}/check-runs": {"check_runs": [
                {"id": 1, "name": "Compile and test", "status": "completed", "conclusion": "failure", "completed_at": ago(40)},
                {"id": 5, "name": "Compile and test", "status": "in_progress", "started_at": ago(3)},
                {"id": 6, "name": "Cheap checks", "status": "completed", "conclusion": "cancelled"},
                {"id": 7, "name": "Import check", "status": "completed", "conclusion": "failure", "output": {"annotations_count": 1}}]},
            f"repos/{REPO}/check-runs/7/annotations": [{"message": "No result: the machine stayed busy. Re-run this check."}],
        }
        w = self.watch_first(first)
        w.prs[REPO][1]["labels"] = pr_labels                  # "sev: blocker" is how bad a bug is, not "blocked"
        s = w.snapshot()
        titles = " | ".join(b["title"] for b in s["blockers"])
        self.assertNotIn("for a runner", titles)
        self.assertNotIn("PR #13 fails", titles)
        self.assertNotIn("marked blocked", titles)
        # the cancelled and "No result" checks are problems with their cause, but nobody needs to fix code for them
        causes = {g["cause"]: g for g in s["problems"]}
        self.assertEqual(set(causes), {"cancelled", "machine"})
        self.assertEqual((s["causes"]["red"], s["causes"]["own"]), (1, 0))
        pr = next(p for p in s["prs"] if p["number"] == 13)
        self.assertEqual(pr["checks"]["state"], "running")
        self.assertEqual([(x["name"], x["why"], x["job"]) for x in pr["checks"]["rerun"]],
                         [("Cheap checks", "cancelled", 6), ("Import check", "no result", 7)])
        self.assertEqual(sum(1 for q in w.gh.asked if "/annotations" in q), 1)
        w.poll()                                              # annotations are asked once per check run
        self.assertEqual(sum(1 for q in w.gh.asked if "/annotations" in q), 1)

        # the same queued run, with the runner idle: stuck
        w.runners = {k: [{**r, "busy": False} for r in v] for k, v in w.runners.items()}
        w.runs[REPO] = [r for r in w.runs[REPO] if r["id"] != 7]      # nothing running on it either
        titles = " | ".join(b["title"] for b in w.snapshot()["blockers"])
        self.assertIn("although a runner with its labels is idle", titles)

    def test_agents_one_row_per_active_branch(self):
        first = {f"repos/{REPO}/events": [
            {"type": "PushEvent", "created_at": ago(5), "actor": {"login": "dev"}, "payload": {"ref": "refs/heads/claude/new-idea"}},
            {"type": "PushEvent", "created_at": ago(3), "actor": {"login": "dev"}, "payload": {"ref": "refs/heads/claude/bus"}},
            {"type": "PushEvent", "created_at": ago(60 * 30), "actor": {"login": "dev"}, "payload": {"ref": "refs/heads/claude/old"}},
            {"type": "PushEvent", "created_at": ago(2), "actor": {"login": "dev"}, "payload": {"ref": "refs/heads/main"}},
            {"type": "IssuesEvent", "created_at": ago(1)}]}
        w = self.watch_first(first)
        w.prs[REPO][0]["head"]["ref"] = "claude/bus"             # PR #12: checks running
        w.prs[REPO][1]["user"] = {"login": "chatgpt-codex-connector[bot]"}   # PR #13 by Codex: its check failed
        s = w.snapshot()
        rows = {(a["agent"], a["number"] or a["title"]): a["state"] for a in s["agents"]}
        self.assertEqual(rows, {("Claude", 12): "Waiting on checks", ("Codex", 13): "Checks failing: Compile and test",
                                ("Claude", "claude/new-idea"): "Coding, no PR yet"})   # main and a branch quiet 30 h: not agents
        self.assertEqual(s["counts"]["agents"], 3)
        self.assertEqual(s["agents"][0]["number"], 12)            # newest activity first (a push 3 min ago)

    def test_agents_active_idle_and_done(self):
        first = {
            f"repos/{REPO}/pulls?state=closed": [
                {"number": 20, "head": {"ref": "claude/merged-work"}, "closed_at": ago(30), "merged_at": ago(30)}],
            f"repos/{REPO}/events": [
                {"type": "PushEvent", "created_at": ago(40), "payload": {"ref": "refs/heads/claude/merged-work"}},   # merged after: gone
                {"type": "PushEvent", "created_at": ago(60 * 5), "payload": {"ref": "refs/heads/claude/quiet"}},     # 5 h ago: quiet
                {"type": "PushEvent", "created_at": ago(10), "payload": {"ref": "refs/heads/codex/fresh"}}]}         # active
        w = self.watch_first(first)
        s = w.snapshot()
        rows = [a["title"] for a in s["agents"] if not a["number"]]
        self.assertEqual(rows, ["codex/fresh"])                  # only what moved in the last 15 min is listed
        self.assertEqual((s["counts"]["agents"], s["counts"]["agents_idle"]), (len(s["agents"]), 1))   # the quiet branch is counted

    def test_heartbeats_lead_and_cover_the_github_guess(self):
        """What agents say beats what GitHub suggests (docs/AGENT-PRESENCE.md)."""
        first = {f"repos/{REPO}/events": [
            {"type": "PushEvent", "created_at": ago(4), "payload": {"ref": "refs/heads/claude/bus"}},
            {"type": "PushEvent", "created_at": ago(6), "payload": {"ref": "refs/heads/codex/shy"}}]}
        w = self.watch_first(first)
        w.prs[REPO][0]["head"]["ref"] = "claude/bus"                 # PR #12, pushed 4 min ago
        w.presence.beat("ALEX-PC", {"agent": "claude", "session": "wt-1", "work": "Acme/Game#12", "branch": "claude/bus",
                                    "step": "Fixing the importer", "state": "working"})
        w.presence.beat("ALEX-PC", {"agent": "codex", "session": "wt-2", "work": "Acme/Game#40", "step": "Reading the issue",
                                    "state": "waiting"})
        w.presence.beat("ALEX-PC", {"agent": "claude", "session": "wt-3", "work": "Other/Repo#5", "step": "Elsewhere"})
        s = w.snapshot()
        rows = {(a["source"], a["number"] or a["title"]): a for a in s["agents"]}
        self.assertEqual(set(rows), {("heartbeat", 12), ("heartbeat", 40), ("heartbeat", 5), ("github", "codex/shy")})
        pr = rows[("heartbeat", 12)]                                  # linked to the PR, its title and checks
        self.assertEqual((pr["title"], pr["url"], pr["step"], pr["where"], pr["state"], pr["checks"]),
                         ("Bus lines", "https://github.com/Acme/Game/pull/12", "Fixing the importer", "Gaming PC", "Working", "running"))
        issue = rows[("heartbeat", 40)]
        self.assertEqual((issue["state"], issue["tone"], issue["url"]), ("Waiting", "wait", "https://github.com/Acme/Game/issues/40"))
        self.assertEqual(rows[("heartbeat", 5)]["url"], "")           # not a watched repository: never linked
        self.assertEqual(s["agents"][-1]["source"], "github")         # the guess comes after what agents say
        self.assertEqual(s["counts"]["agents"], 4)

    def test_heartbeat_done_quiet_and_closed(self):
        first = {
            f"repos/{REPO}/pulls?state=closed": [{"number": 21, "head": {"ref": "claude/landed"}, "closed_at": ago(2)}],
            f"repos/{REPO}/events": [{"type": "PushEvent", "created_at": ago(8), "payload": {"ref": "refs/heads/claude/bus"}}]}
        w = self.watch_first(first)
        w.prs[REPO][0]["head"]["ref"] = "claude/bus"                 # pushed 8 min ago, then the agent said done (5 min)
        beat = lambda **kw: w.presence.beat("ALEX-PC", {"agent": "claude", "step": "x", **kw})   # noqa: E731
        beat(session="done", work="Acme/Game#12", branch="claude/bus", state="done")   # said done: gone, and covers #12
        beat(session="landed", work="Acme/Game#21")                                    # its PR merged after: gone
        beat(session="quiet", work="Acme/Game#40")
        for r in w.presence.records.values():
            r["at"] -= 5 * 60
        w.presence.records[next(k for k in w.presence.records if k.endswith("|quiet"))]["at"] -= 20 * 60   # 25 min quiet
        s = w.snapshot()
        self.assertEqual(s["agents"], [])
        self.assertEqual(s["counts"]["agents_idle"], 1)               # the quiet one is counted, not listed
        # A push after the agent said done is new work: the GitHub guess comes back.
        w.pushes[REPO] = [{"branch": "claude/bus", "at": ago(1), "actor": "dev"}]
        self.assertEqual([(a["source"], a["number"]) for a in w.snapshot()["agents"]], [("github", 12)])

    def test_status_issue_comments_are_read_once_an_hour_with_etags(self):
        marker = '<!-- fletchery-agent {"agent":"claude","session":"thread-1","work":"#12","step":"Planning","state":"working"} -->'
        first = {f"repos/{REPO}/issues/9/comments": [
            {"id": 1, "body": marker + "\n**Claude** on #12: Planning", "author_association": "MEMBER", "updated_at": ago(2)},
            {"id": 2, "body": marker.replace("thread-1", "spoof"), "author_association": "NONE", "updated_at": ago(1)}]}
        answers = dict(first)
        for k, v in world().items():
            answers.setdefault(k, v)
        w = devwatch.DevWatch({"agentIssues": {"acme/game": 9, "Bad Key": "x"}}, github=FakeGitHub(answers))
        w.poll()
        asked = [a for a in w.gh.asked if "/comments" in a]
        self.assertEqual(len(asked), 1)
        since = asked[0].split("since=")[1].split("&")[0]
        self.assertTrue(since.endswith(":00:00Z"), since)              # rounded to the hour: the same URL all hour
        rows = [a for a in w.snapshot()["agents"] if a["source"] == "heartbeat"]
        self.assertEqual([(a["number"], a["where"], a["step"]) for a in rows], [(12, "cloud", "Planning")])

    def test_agent_presence_is_kept_in_the_state_folder(self):
        with tempfile.TemporaryDirectory() as d:
            w = devwatch.DevWatch({}, github=FakeGitHub(world()), state_dir=d)
            w.agent("ALEX-PC", {"agent": "codex", "session": "s", "step": "Building"})
            again = devwatch.DevWatch({}, github=FakeGitHub(world()), state_dir=d)
            self.assertEqual([r["session"] for r in again.presence.current()], ["s"])

    def test_blocked_labels_match_the_whole_name(self):
        m = devwatch.BLOCKED_LABEL.match
        for yes in ("blocked", "foreman:blocked", "status:needs-alex", "needs-you", "Blocked: upstream", "waiting-on-art"):
            self.assertTrue(m(yes), yes)
        for no in ("sev: blocker", "blocker", "unblocks-2263", "needs-tests"):
            self.assertFalse(m(no), no)

    def test_one_bad_repo_keeps_the_rest(self):
        w = self.watch()
        w.gh.answers[f"repos/{REPO}/pulls"] = devwatch.GitHubError(502, "Bad Gateway")
        with self.assertRaises(devwatch.GitHubError):   # every repo failed: the feed says so, the last answers stay
            w.poll()
        self.assertEqual(len(w.prs[REPO]), 2)
        self.assertIn("feed", [b["kind"] for b in w.snapshot()["blockers"]])

    def test_no_admin_scope_still_shows_runners_from_jobs(self):
        w = self.watch(**{f"orgs/{ORG}/actions/runners": devwatch.GitHubError(403, "Must have admin rights")})
        s = w.snapshot()
        self.assertIn("admin:org", s["fresh"]["runner_note"])
        self.assertEqual([r["name"] for r in s["runners"]], ["ALEX-PC-unreal-pr"])

    def test_runners_added_to_a_repository_are_listed_without_admin_org(self):
        """LifeShare's four runners are registered on the repository, not the organisation (2026-11-18: Live showed one)."""
        repo_runners = {"runners": [
            {"id": 11, "name": f"{pc}-{lab}", "status": "online", "busy": pc == "ALEX-PC" and lab == "unreal-pr",
             "labels": [{"name": "self-hosted", "type": "read-only"}, {"name": lab, "type": "custom"}]}
            for pc in ("ALEX-PC", "SF-NODE-01") for lab in ("unreal-light", "unreal-pr")]}
        for i, r in enumerate(repo_runners["runners"]):
            r["id"] = 100 + i
        w = self.watch(**{f"orgs/{ORG}/actions/runners": devwatch.GitHubError(403, "Must have admin rights"),
                          f"repos/{REPO}/actions/runners": repo_runners})
        s = w.snapshot()
        self.assertEqual(sorted(r["name"] for r in s["runners"]),
                         ["ALEX-PC-unreal-light", "ALEX-PC-unreal-pr", "SF-NODE-01-unreal-light", "SF-NODE-01-unreal-pr"])
        busy = next(r for r in s["runners"] if r["name"] == "ALEX-PC-unreal-pr")
        self.assertEqual((busy["job"]["id"], busy["job"]["step"]), (7, "Build editor"))

    def test_a_refused_runner_list_is_asked_again_after_an_hour(self):
        w = self.watch(**{f"orgs/{ORG}/actions/runners": devwatch.GitHubError(403, "Must have admin rights")})
        path = f"orgs/{ORG}/actions/runners?per_page=100"
        self.assertAlmostEqual(w.skip[path] - time.time(), devwatch.RUNNER_REFUSED_RETRY, delta=5)
        self.assertEqual(devwatch.RUNNER_REFUSED_RETRY, 3600)

    def test_a_run_for_a_closed_pull_request_is_flagged(self):
        w = self.watch()
        w.gh.answers[f"repos/{REPO}/pulls"] = [p for p in w.gh.answers[f"repos/{REPO}/pulls"] if p["number"] != 12]
        w.poll()
        s = w.snapshot()
        self.assertTrue(next(r for r in s["runs"] if r["id"] == 7)["pr_closed"])
        self.assertIn("PR check · PR #12 is still running, but its pull request is closed", [b["title"] for b in s["blockers"]])
        pc = next(r for r in s["runners"] if r["name"] == "ALEX-PC-unreal-pr")
        self.assertTrue(pc["job"]["pr_closed"])
        self.assertFalse(next(r for r in self.watch().snapshot()["runs"] if r["id"] == 7)["pr_closed"])

    def test_building_now_lists_every_build_on_every_machine(self):
        w = self.watch()
        w.own_builds = lambda: [{"machine": "SF-NODE-01", "kind": "building", "title": "Fletchery build 0.4.6 (job #9)", "since": ago(20),
                           "detail": "step: Package 0.4.6"}]
        s = w.snapshot()
        node = next(m for m in s["machines"] if m["name"] == "SF-NODE-01")
        self.assertTrue(node["busy"])
        self.assertEqual(node["activities"][0]["title"], "Fletchery build 0.4.6 (job #9)")
        self.assertEqual([(b["machine"], b["kind"]) for b in s["building"]], [("Gaming PC", "check"), ("SF-NODE-01", "building")])
        self.assertEqual(s["counts"]["building"], 2)
        w.own_builds = lambda: 1 / 0   # a broken hook never loses the feed
        self.assertEqual(len(w.snapshot()["building"]), 1)

    def test_finished_checks_are_not_asked_again(self):
        w = self.watch()
        w.gh.asked.clear()
        w.poll()
        self.assertFalse(any("b" * 40 in p for p in w.gh.asked))
        self.assertTrue(any("a" * 40 in p for p in w.gh.asked))

    def test_machine_reports(self):
        w = self.watch()
        w.report("GAMING-PC", {"host": "ALEX-PC", "busy": True, "idle_seconds": 900, "procs": {"claude": 2, "unreal": 1},
                               "lease": {"purpose": r"Hand merge batch 7: E:\scratch\Build.ps1", "since": ago(30), "alive": True},
                               "queue": [{"purpose": "local run PR check #14", "since": ago(100)}],
                               "stats": {"cpu": 2.0, "ram_used_gb": 20, "ram_total_gb": 32, "disks": [{"name": "E:", "free_gb": 140}]}})
        w.report("NODE-01", {"host": "SF-NODE-01", "lease": {"purpose": "Proof run #2263", "since": ago(10), "alive": False}})
        w.reports["NODE-01"]["at"] = ago(2)
        s = w.snapshot()
        pc = next(m for m in s["machines"] if m["name"] == "Gaming PC")
        kinds = [a["kind"] for a in pc["activities"]]
        self.assertIn("merging", kinds)
        self.assertNotIn("coding", kinds)          # a count of claude.exe processes said nothing (Agents card instead)
        self.assertNotIn("E:\\scratch", json.dumps(s))                  # folder paths stay on the machine
        self.assertIn("ALEX-PC-unreal-pr", pc["runners"])                 # runner and report land on one machine
        titles = " | ".join(b["title"] for b in s["blockers"])
        for want in ("CPU has been idle 15 min", "waited 100 min in the queue", "SF-NODE-01 stopped reporting",
                     "its process is gone"):
            self.assertIn(want, titles)

    def test_the_disk_guard_shows_on_the_machine_and_flags_low_and_full(self):
        guard = {"machine": "SF-NODE-01", "drive": "E:", "freeGB": 31.5, "totalGB": 250, "state": "low", "wantGB": 40, "minGB": 20,
                 "warnGB": 50, "updatedUtc": ago(4), "runnerStopped": [],
                 "holders": [{"path": r"E:\lifeshare-scratch\wt-bus-lines", "gb": 61.2}, {"path": r"E:\GitHubRunner\_work", "gb": 40}]}
        w = self.watch()
        w.report("NODE-01", {"host": "SF-NODE-01", "disk_guard": guard,
                             "stats": {"disks": [{"name": "E:", "free_gb": 12, "total_gb": 250}, {"name": "C:", "free_gb": 80, "total_gb": 230}]}})
        s = w.snapshot()
        m = next(x for x in s["machines"] if x["host"] == "SF-NODE-01")
        self.assertTrue(m["disk_guard"]["low"])
        self.assertEqual(m["disk_guard"]["holders"][0], {"name": "lifeshare-scratch\\wt-bus-lines", "gb": 61.2})
        self.assertNotIn("E:\\lifeshare-scratch", json.dumps(s))          # folder names, not the machine's paths
        self.assertEqual(m["disks"][0]["total_gb"], 250)
        disk = [b for b in s["blockers"] if "SF-NODE-01" in b["title"]]
        self.assertEqual([b["kind"] for b in disk], ["disk"])              # the guard's drive once, not again from stats
        self.assertIn("has 32 GB free", disk[0]["title"])

        w.report("NODE-01", {"host": "SF-NODE-01", "disk_guard": {**guard, "freeGB": 4, "state": "full", "runnerStopped": ["actions.runner.unreal-pr"]}})
        s = w.snapshot()
        full = [b for b in s["blockers"] if "SF-NODE-01" in b["title"]]
        self.assertEqual([(b["kind"], b["title"]) for b in full], [("stuck", "SF-NODE-01: PR runner offline: disk full")])
        self.assertIn("actions.runner.unreal-pr", full[0]["detail"])

        w.report("NODE-01", {"host": "SF-NODE-01", "disk_guard": {**guard, "freeGB": 120, "state": "ok"}})
        self.assertFalse([b for b in w.snapshot()["blockers"] if "SF-NODE-01" in b["title"]])

    def test_runner_without_labels_joins_its_machine(self):
        w = self.watch(**{f"orgs/{ORG}/actions/runners": {"runners": [{"id": 9, "name": "ALEX_GAMING_PC-unreal-pr", "status": "online", "busy": False, "labels": []}]}})
        w.report("GAMING-PC", {"host": "Alex_Gaming_PC"})
        s = w.snapshot()
        pc = next(m for m in s["machines"] if m["host"] == "Alex_Gaming_PC")
        self.assertIn("ALEX_GAMING_PC-unreal-pr", pc["runners"])
        self.assertEqual(next(r for r in s["runners"] if r["name"] == "ALEX_GAMING_PC-unreal-pr")["machine"], "Alex_Gaming_PC")

    def test_runner_seen_without_reporter_or_labels_is_named_by_its_computer(self):
        w = self.watch(**{f"orgs/{ORG}/actions/runners": {"runners": [{"id": 9, "name": "SF-NODE-01-unreal-light", "status": "online", "busy": False, "labels": []}]}})
        self.assertEqual(next(r for r in w.snapshot()["runners"] if r["name"] == "SF-NODE-01-unreal-light")["machine"], "SF-NODE-01")

    def test_bounds(self):
        w = devwatch.DevWatch({}, github=FakeGitHub({}))
        with self.assertRaises(ValueError):
            w.report("X", {"blob": "x" * 70000})

    def test_kinds(self):
        for text, kind in (("PR check #2290 (GitHub run 1)", "check"), ("Hand merge batch 7", "merging"),
                           ("NavCheck proof for #12", "proof"), ("Build 0.4.5", "building"), ("something", "working")):
            self.assertEqual(devwatch.kind_of(text), kind, text)


class StuckNotifications(unittest.TestCase):
    def test_keys_are_stable_while_minutes_change(self):
        w = devwatch.DevWatch({}, github=FakeGitHub(world()))
        w.poll()
        first = {b["key"] for b in w.snapshot()["blockers"]}
        self.assertIn("runner:NODE-unreal-light", first)
        self.assertTrue(all(b["repo"] == REPO for b in w.snapshot()["blockers"] if b["kind"] in ("check", "label")))
        self.assertEqual(first, {b["key"] for b in w.snapshot()["blockers"]})

    def test_push_says_each_stuck_thing_once_and_skips_blips(self):
        import push
        view = {"live_blockers": [{"kind": "stuck", "key": "step:r:7:Build", "title": "PR check: step Build has run 75 min", "detail": "r", "since": ago(75)},
                                  {"kind": "feed", "key": "feed", "title": "GitHub can't be read right now", "since": ago(1)},
                                  {"kind": "machine", "key": "machine:N", "title": "N stopped reporting", "since": ago(5)}]}
        marks = push.marks(view)
        self.assertIn("stuck:step:r:7:Build", marks)
        self.assertIn("stuck:machine:N", marks)
        self.assertNotIn("stuck:feed", marks)       # one minute old: may be a blip
        self.assertEqual(marks["stuck:step:r:7:Build"]["tab"], "live")
        self.assertIn("stuck", push.EVENTS)

    def test_token_is_read_again(self):
        calls = []
        gh = devwatch.GitHub(lambda: calls.append(1) or f"t{len(calls)}")
        self.assertEqual(gh.token(), "t1")
        self.assertEqual(gh.token(), "t1")
        gh._token_at -= 601
        self.assertEqual(gh.token(), "t2")


class Etags(unittest.TestCase):
    def test_304_is_free_and_answers_from_memory(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.headers.get("If-None-Match"))
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.send_header("X-RateLimit-Remaining", "4999")
                    self.end_headers()
                    return
                body = b'{"ok": 1}'
                self.send_response(200)
                self.send_header("ETag", '"v1"')
                self.send_header("X-RateLimit-Remaining", "4998")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            gh = devwatch.GitHub(lambda: "t")
            url = f"http://127.0.0.1:{srv.server_address[1]}/x"
            self.assertEqual(_plain(gh, url), {"ok": 1})
            self.assertEqual(_plain(gh, url), {"ok": 1})
            self.assertEqual((gh.calls, gh.free, gh.remaining), (1, 1, 4999))
            self.assertEqual(hits, [None, '"v1"'])
        finally:
            srv.shutdown()


class GhFallback(unittest.TestCase):
    def test_switches_to_gh_when_sockets_fail_and_keeps_etags(self):
        gh = devwatch.GitHub(lambda: "t")
        def no_socket(url, etag):
            raise devwatch.GitHubError(0, "GitHub could not be reached: [WinError 10013] forbidden")
        asked = []
        def via_gh(url, etag):
            asked.append(etag)
            if etag:
                return devwatch.parse_gh_output(b'HTTP/2.0 304 Not Modified\r\nEtag: "e1"\r\n\r\n', b"gh: HTTP 304")
            return devwatch.parse_gh_output(b'HTTP/2.0 200 OK\r\nEtag: "e1"\r\nX-Ratelimit-Remaining: 4990\r\n\r\n[{"a": 1}]')
        gh._urllib, gh._gh = no_socket, via_gh
        self.assertEqual(gh.get("user/repos"), [{"a": 1}])
        self.assertEqual(gh.via, "gh")
        self.assertEqual(gh.get("user/repos"), [{"a": 1}])
        self.assertEqual((asked, gh.calls, gh.free, gh.remaining), (["", '"e1"'], 1, 1, 4990))

    def test_gh_errors(self):
        self.assertEqual(devwatch.parse_gh_output(b"", b"gh: Not Found (HTTP 404)")[0], 404)
        with self.assertRaises(devwatch.GitHubError):
            devwatch.parse_gh_output(b"", b"error connecting to api.github.com")


def _plain(gh, url):
    old = devwatch.API
    try:
        devwatch.API = url.rsplit("/", 1)[0]
        return gh.get("x")
    finally:
        devwatch.API = old


class Reporter(unittest.TestCase):
    def test_reads_the_lease_queue_and_activities(self):
        with tempfile.TemporaryDirectory() as d:
            lease = Path(d) / "heavy.json"
            lease.write_text(json.dumps({"owner": "local", "pid": os.getpid(), "purpose": "Hand merge batch 7", "acquiredUtc": ago(3)}), encoding="utf-8")
            (Path(d) / "heavy.queue").mkdir()
            (Path(d) / "heavy.queue" / "2-20261118T000000000000Z-abc.json").write_text(json.dumps({"purpose": "local run PR check #9", "queuedUtc": ago(1)}))
            old = Path(d) / "heavy.queue" / "2-20261118T000000000000Z-old.json"
            old.write_text(json.dumps({"purpose": "dead run"}))
            os.utime(old, (time.time() - 600, time.time() - 600))
            (Path(d) / "activity").mkdir()
            (Path(d) / "activity" / "proof.json").write_text(json.dumps({"title": "Proof run for #2263", "kind": "proof"}))
            r = reporter.Reporter({"leaseFile": str(lease), "disks": [d], "keyFile": "k", "coordinator": "x", "machine": "m"})
            rep = r.collect()
        self.assertEqual((rep["lease"]["purpose"], rep["lease"]["alive"]), ("Hand merge batch 7", True))
        self.assertEqual([q["purpose"] for q in rep["queue"]], ["local run PR check #9"])
        self.assertEqual(rep["activities"][0]["title"], "Proof run for #2263")
        self.assertTrue(rep["busy"])
        self.assertTrue(rep["stats"]["disks"])

    def test_sends_the_disk_guard_report_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            lease = Path(d) / "heavy.json"
            r = reporter.Reporter({"leaseFile": str(lease), "disks": [d], "keyFile": "k", "coordinator": "x", "machine": "m"})
            self.assertIsNone(r.collect()["disk_guard"])
            (Path(d) / "disk.json").write_text(json.dumps({"drive": "E:", "freeGB": 12.5, "totalGB": 250, "state": "full",
                                                           "runnerStopped": "actions.runner.x", "holders": [{"path": "E:\\a", "gb": 9}] * 20}))
            g = r.collect()["disk_guard"]
            self.assertEqual((g["freeGB"], g["state"], g["runnerStopped"], len(g["holders"])), (12.5, "full", ["actions.runner.x"], 10))
            os.utime(Path(d) / "disk.json", (time.time() - 7200, time.time() - 7200))
            self.assertIsNone(r.collect()["disk_guard"])          # a guard that stopped writing is not believed

    def test_dead_pid(self):
        self.assertFalse(reporter.alive(2 ** 22 + 12345))
        self.assertIsNone(reporter.alive("x"))

    def test_route(self):
        import machines
        with tempfile.TemporaryDirectory() as d:
            m = machines.Machines(d, "Acme/Game")
            key = m.add_machine("NODE-01")
            api = machines.WorkerApi(m, "127.0.0.1:0")
            self.assertEqual(api.handle("/v1/report", "NODE-01", key, "1.2.3.4", {})[0], 503)
            api.reports = devwatch.DevWatch({}, github=FakeGitHub({}))
            self.assertEqual(api.handle("/v1/report", "NODE-01", key, "1.2.3.4", {"host": "SF-NODE-01"}), (200, {"ok": True, "every": 10}))
            self.assertEqual(api.handle("/v1/report", "NODE-01", "wrong", "1.2.3.4", {})[0], 401)
            self.assertIn("NODE-01", api.reports.reports)


if __name__ == "__main__":
    unittest.main()
