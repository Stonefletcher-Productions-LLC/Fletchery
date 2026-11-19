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
import pipeline  # noqa: E402
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

    def test_long_check_and_second_page_of_open_prs(self):
        runs = f"repos/{REPO}/actions/runs?"
        pulls = f"repos/{REPO}/pulls?state=open&sort=updated&direction=desc&per_page=50"
        active = world()[f"repos/{REPO}/actions/runs"]["workflow_runs"][0]
        w = self.watch_first({
            runs + "per_page=20": {"workflow_runs": [{"id": i, "status": "completed"} for i in range(50, 70)]},
            runs + "status=in_progress": {"workflow_runs": [active]},
            runs + "status=queued": {"workflow_runs": []},
            pulls + "&page=2": [{"number": 51}],
            pulls: [{"number": i} for i in range(1, 51)],
        })
        s = w.snapshot()
        self.assertEqual(s["open_pr_counts"][REPO], 51)
        self.assertEqual(s["counts"]["runs"], 1)
        self.assertEqual(s["building"][0]["steps_done"], 1)
        self.assertEqual(s["building"][0]["step"], "Build editor")

    def test_inner_phase_is_for_the_current_run_attempt_and_machine(self):
        run = world()[f"repos/{REPO}/actions/runs"]["workflow_runs"][0]
        job = {"id": 80, "name": "Compile and test", "runner_name": "ALEX-PC-unreal-pr", "status": "in_progress",
               "started_at": ago(20), "steps": [{"name": "Build and test", "status": "in_progress", "started_at": ago(15)}]}
        w = self.watch_first({f"repos/{REPO}/actions/runs/7/jobs": {"jobs": [job]},
                              f"repos/{REPO}/actions/runs?": {"workflow_runs": [{**run, "run_attempt": 2}]}})
        phase = {"kind": "pr-phase", "title": REPO + "@7/2", "detail": "Running specs", "since": ago(4)}
        for title, since, expect in ((REPO + "@7/1", ago(4), False), (OLD + "@7/2", ago(4), False),
                                     (phase["title"], ago(25), False), (phase["title"], ago(4), True)):
            w.report("pc", {"host": "ALEX-PC", "activities": [{**phase, "title": title, "since": since}]})
            s = w.snapshot()
            self.assertEqual("phase" in s["runs"][0]["progress"], expect)
            self.assertEqual(len(s["building"]), 1)
            self.assertEqual(s["building"][0]["progress"], s["runs"][0]["progress"])
        w.reports["pc"]["at"] = ago(10)
        self.assertNotIn("phase", w.snapshot()["building"][0]["progress"])
        for age, within_phase in ((8, False), (2, True)):
            output = {"label": "Testing a four-player game", "at": ago(age)}
            w.report("pc", {"host": "ALEX-PC", "activities": [phase],
                            "lease": {"purpose": "PR check #12 (GitHub run 7)", "since": ago(12),
                                      "alive": True, "last_output": output}})
            progress = w.snapshot()["building"][0]["progress"]
            self.assertEqual(progress["phase"]["label"], "Running specs")
            self.assertEqual("last_output" in progress, within_phase)
        w.report("pc", {"host": "ALEX-PC", "queue": [{"purpose": "PR check #12 (GitHub run 7)", "since": ago(2)}]})
        waiting = w.snapshot()["building"][0]["progress"]
        self.assertTrue(waiting["waiting_machine"])
        self.assertEqual(waiting["phase"]["label"], "Waiting for the machine reservation")

    def test_machine_wait_uses_actual_holder_and_ordered_local_tickets(self):
        w = self.watch()
        dispatch = {"id": 91, "workflow_id": 56, "event": "workflow_dispatch", "status": "in_progress",
                    "display_title": "Node checks " + "a"*40 + " LakePictures"}
        job = {"name": "Capture", "runner_name": "ALEX-PC", "status": "in_progress",
               "steps": [{"name": "Photograph lake", "status": "in_progress", "started_at": ago(20)}]}
        past = {"id": 90, "display_title": "Node checks " + "b"*40 + " LakePictures", "conclusion": "failure"}
        old_step = {"name": "Photograph lake", "started_at": ago(100), "completed_at": ago(60), "conclusion": "failure"}
        route = f"repos/{REPO}/actions/workflows/56/runs"
        w.gh.answers = {route: {"workflow_runs": [past, {**past, "id": 89, "conclusion": "cancelled"}]},
                        f"repos/{REPO}/actions/runs/90/jobs": {"jobs": [{**job, "steps": [old_step]}]}}
        holder = {"purpose": "Scenery lake pictures: Photograph the lake", "alive": True, "since": ago(10)}
        ticket = {"purpose": "local run PR check #12 (GitHub run 7)", "since": ago(7)}
        w.report("pc", {"host": "ALEX-PC", "lease": holder, "queue": [ticket]})
        w._read_holder_times(REPO, [dispatch], {91: [job]})
        w._read_holder_times(REPO, [dispatch], {91: [job]})
        self.assertEqual(sum(path.startswith(route) for path in w.gh.asked), 1)
        w.runs[REPO].append(dispatch)
        w.jobs[91] = [job]
        w.jobs[7][0]["steps"] = [{"status": "in_progress", "name": "Build and test", "started_at": ago(8)}]

        def reservation():
            return next(r for r in w.snapshot()["runs"] if r["id"] == 7)["progress"]["reservation"]

        info = reservation()
        self.assertEqual((info["holder"], info["ahead"], info["release"]["failed"]), (holder["purpose"], 0, 1))
        self.assertAlmostEqual(info["start_in"], 20*60, delta=3)
        w.report("pc", {"host": "ALEX-PC", "lease": holder, "queue": [{"purpose": "local run Earlier"}, ticket]})
        self.assertEqual(reservation()["ahead"], 1)
        self.assertIsNone(reservation()["start_in"])
        w.history.add(devwatch._reservation_key("ALEX-PC", "Earlier"), 360, "earlier-hold")
        self.assertAlmostEqual(reservation()["start_in"], 26*60, delta=3)
        w.report("pc", {"host": "ALEX-PC", "queue": [ticket]})
        self.assertIsNone(reservation()["start_in"])
        self.assertNotIn("holder", reservation())
        seconds, n = w.history.remaining_exact(devwatch._reservation_key("ALEX-PC", holder["purpose"]), 60)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(seconds, 540, delta=3)

    def test_time_left_uses_job_runtime_and_excludes_shorter_or_cancelled_jobs(self):
        ledger = self.watch().store
        for i, (minutes, pr, result) in enumerate([(1, 12, "success"), (30, 12, "failure"), (50, 13, "success"), (80, 12, "cancelled")], 100):
            run = {"id": i, "status": "completed", "updated_at": ago(1), "pull_requests": [{"number": pr}]}
            ledger.runs(REPO, [run])
            ledger.jobs(REPO, run, [{"id": i, "status": "completed", "conclusion": result, "name": "Build",
                                     "created_at": ago(400), "started_at": ago(100), "completed_at": ago(100 - minutes)}])
        self.assertEqual(ledger.job_remaining(REPO, "Build", 12, 20*60), {"samples": 1, "seconds": 600, "same_pr": True})
        self.assertEqual(ledger.job_remaining(REPO, "Build", 12, 40*60)["seconds"], 600)
        self.assertIsNone(ledger.job_remaining(REPO, "Build", 12, 55*60)["seconds"])

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

    def test_base_ignores_statuses_that_link_nowhere(self):
        # Main with no check runs, only statuses: a retired local runner's failures (no link) do not turn it red;
        # a status that links to its run still counts.
        main = "d" * 40
        legacy = [{"id": 90 + i, "context": name, "state": "failure", "description": "Failed on the build PC (run 9000000001)",
                   "created_at": ago(20), "updated_at": ago(20)} for i, name in enumerate(("Nightly full tests (node)", "preview-receipt", "sync"))]
        w = self.watch_first({f"repos/{REPO}/commits/main/check-runs": {"check_runs": []},
                              f"repos/{REPO}/commits/main/status": {"sha": main, "statuses": legacy}})
        s = w.snapshot()
        self.assertNotIn("red", [b.get("state") for b in s["bases"]])
        self.assertNotIn("base", [b["kind"] for b in s["blockers"]])
        linked = dict(legacy[0], id=99, context="Nightly full", target_url="https://github.com/Acme/Game/actions/runs/77")
        w = self.watch_first({f"repos/{REPO}/commits/main/check-runs": {"check_runs": []},
                              f"repos/{REPO}/commits/main/status": {"sha": main, "statuses": legacy + [linked]}})
        base = w.snapshot()["bases"][0]
        self.assertEqual((base["state"], [f["name"] for f in base["failing"]]), ("red", ["Nightly full"]))

    def test_existing_history_reconciles_only_confirmed_unlinked_statuses_on_this_tip(self):
        sha = "d" * 40
        nightly = "Nightly full tests (node)"
        check = {"head_sha": sha, "status": "completed", "conclusion": "failure", "name": nightly, "completed_at": ago(20)}
        names = [nightly, "preview-receipt", "sync", "Published report"]
        legacy = [dict(check, name=name) for name in names]
        statuses = [{"context": name, "state": "failure", "target_url": "https://example.org/report" if name == "Published report" else None}
                    for name in names]
        check_path = f"repos/{REPO}/commits/main/check-runs"
        status_path = f"repos/{REPO}/commits/{sha}/status"
        w = self.watch_first({check_path: {"total_count": 2, "check_runs": [check]},
                              status_path: {"sha": sha, "statuses": statuses}})
        w.store.base_checks(REPO, "main", sha, legacy)
        w.store.base_checks(REPO, "other", sha, legacy)
        w.poll()  # One of two real checks was returned. The absent one might share an unlinked status name.
        self.assertEqual({f["name"] for f in w.snapshot()["bases"][0]["failing"]}, set(names))
        w.gh.answers[check_path]["total_count"] = 1
        w.poll()
        self.assertEqual({f["name"] for f in w.snapshot()["bases"][0]["failing"]}, {nightly, "Published report"})
        self.assertEqual(w.store.query("SELECT COUNT(*) FROM base_checks WHERE branch='other'"), [(4,)])

    def test_a_deleted_base_branch_is_not_a_feed_error(self):
        # The last watched base had a failure in history before it was deleted.
        path = f"repos/{REPO}/commits/claude%2Fold-stack"
        check = {"name": "Compile", "head_sha": "c" * 40, "status": "completed", "conclusion": "failure", "completed_at": ago(40)}
        merged = {"number": 30, "title": "Stacked", "merged_at": ago(60), "closed_at": ago(60), "base": {"ref": "claude/old-stack"},
                  "head": {"ref": "claude/top"}, "html_url": "https://github.com/Acme/Game/pull/30"}
        w = self.watch_first({f"repos/{REPO}/pulls?state=open": [], f"repos/{REPO}/pulls?state=closed": [merged],
                              path: {"check_runs": [check]}})
        self.assertEqual(w.snapshot()["bases"][0]["state"], "red")
        w.gh.answers[path] = devwatch.GitHubError(422, "No commit found for SHA: claude/old-stack")
        w.poll()
        self.assertEqual(w.snapshot()["bases"], [])
        w.poll()  # a cached missing ref must also hide the branch
        self.assertEqual(w.error, "")
        self.assertFalse({"base", "feed"} & {b["kind"] for b in w.snapshot()["blockers"]})
        # A generic permission failure is inconclusive. Preserve the last known check in that case.
        w.skip.clear()
        w.gh.answers[path] = devwatch.GitHubError(403, "Forbidden")
        w.poll()
        self.assertEqual(w.snapshot()["bases"][0]["state"], "red")

    def test_base_failure_details_belong_to_the_current_check_run(self):
        def check(cid):
            return {"check_runs": [{"id": cid, "name": "Compile", "status": "completed", "conclusion": "failure",
                                    "head_sha": str(cid) * 40, "completed_at": ago(10), "output": {"annotations_count": 1}}]}

        main_path = f"repos/{REPO}/commits/main/check-runs"
        w = self.watch_first({main_path: check(1), f"repos/{REPO}/commits/staging/check-runs": check(2),
                              **{f"repos/{REPO}/check-runs/{i}/annotations": [{"annotation_level": "failure", "title": f"Test {i}"}]
                                 for i in (1, 2, 3)}})
        w.bases_conf[REPO.lower()] = ["staging"]
        w.poll()
        def details():
            return {b["branch"]: b["failing"][0]["tests"] for b in w.snapshot()["bases"]}
        self.assertEqual(details(), {"main": ["Test 1"], "staging": ["Test 2"]})
        w.gh.answers[main_path] = check(3)
        w.poll()
        self.assertEqual(details(), {"main": ["Test 3"], "staging": ["Test 2"]})

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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:     # Windows: the history file stays open
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
        self.assertAlmostEqual(w.skip[path][0] - time.time(), devwatch.RUNNER_REFUSED_RETRY, delta=5)
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

    def test_latest_output_remains_during_a_quiet_lease_and_expires_with_the_report(self):
        w = devwatch.DevWatch(github=FakeGitHub({}))
        lease = {"purpose": "PR check #12 (GitHub run 7)", "since": ago(45), "alive": True}
        out = {"label": "Checking navigation", "at": ago(12)}
        for timestamp, expected in ((out["at"], True), (ago(50), False)):
            w.report("node", {"host": "node", "lease": {**lease, "last_output": {**out, "at": timestamp}}})
            self.assertEqual("last_output" in w.snapshot()["building"][0], expected)
        w.report("node", {"host": "node", "lease": {**lease, "last_output": out}})
        w.reports["node"]["at"] = ago(2)
        self.assertNotIn("last_output", w.snapshot()["building"][0])

    def test_reporter_alone_identifies_a_waiting_pr_and_its_holder(self):
        w = devwatch.DevWatch(github=FakeGitHub({}))
        w.report("node", {"host": "node", "lease": {"purpose": "City photos", "since": ago(30), "alive": True},
                          "queue": [{"purpose": "local run PR check #12 (GitHub run 7)", "since": ago(25)}]})
        row = next(a for a in w.snapshot()["building"] if a["kind"] == "queued")
        self.assertEqual(row["progress"]["reservation"]["holder"], "City photos")
        self.assertEqual(row["progress"]["reservation"]["ahead"], 0)
        self.assertIsNone(row["progress"]["reservation"]["start_in"])

    def test_upload_count_survives_sync_and_quiet_periods_without_a_stale_eta(self):
        job = {"name": "Compile and test", "runner_name": "ALEX-PC-unreal-pr", "status": "in_progress",
               "steps": [{"name": "Build and test", "status": "in_progress", "started_at": ago(15)}]}
        w = self.watch_first({f"repos/{REPO}/actions/runs/7/jobs": {"jobs": [job]}})
        upload = {"done": 2160, "total": 11391, "eta_seconds": 1080}
        lease = {"purpose": "PR check #12 (GitHub run 7)", "since": ago(12), "alive": True}
        for age, raw, expected in ((0, {**upload, "local_path": "private"}, upload),
                                   (4, upload, {**upload, "eta_seconds": None}),
                                   (0, {**upload, "done": 11500}, None)):
            out = {"label": "Uploading generated assets", "at": ago(age), "upload": raw}
            w.report("pc", {"host": "ALEX-PC", "lease": {**lease, "last_output": out}})
            state = w.snapshot()
            row = next(x for x in state["building"] if x.get("reservation_holder"))
            self.assertEqual(row["last_output"].get("upload"), expected)
            run = next(x for x in state["runs"] if x["id"] == 7)
            self.assertEqual(run["progress"]["last_output"], row["last_output"])

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

    def test_a_filling_drive_is_forecast_and_alerts_wait_their_turn(self):
        w = self.watch()
        now = time.time()
        for i in range(8):       # losing 20 GB every 10 min: full in about 2 h
            w.store.disk("SF-NODE-01", "E:", 300 - 20 * i, now - 4200 + 600 * i)
        w.report("NODE-01", {"host": "SF-NODE-01", "stats": {"disks": [{"name": "E:", "free_gb": 160, "total_gb": 500}]}})
        s = w.snapshot()
        m = next(x for x in s["machines"] if x["host"] == "SF-NODE-01")
        self.assertTrue(3600 < m["disks"][0]["full_in"] < 3 * 3600)
        fc = next(b for b in s["blockers"] if b["key"] == "diskfc:SF-NODE-01:E:")
        self.assertEqual(fc["kind"], "disk")
        self.assertIn("will be full in about", fc["title"])
        self.assertEqual(fc["alert"]["state"], "pending")          # a disk waits 5 min before it alerts
        self.assertEqual(fc["alert"]["group"], "machine:SF-NODE-01")
        self.assertIn("diskfc:SF-NODE-01:E:", {a["key"] for a in s["alerts"]["pending"]})
        self.assertTrue(all(b["alert"]["state"] in ("firing", "pending", "off") for b in s["blockers"]))

    def test_step_times_are_learned_from_past_runs_once(self):
        runs = {"workflow_runs": [
            {"id": 100 + i, "name": "PR check", "event": "pull_request", "status": "completed", "conclusion": "success",
             "display_title": "Town surfaces", "head_branch": "claude/town", "pull_requests": [{"number": 50 + i}],
             "created_at": ago(300 + i), "run_started_at": ago(300 + i), "updated_at": ago(240 + i)} for i in range(4)]
            + [{"id": 99, "name": "Docs", "event": "schedule", "status": "completed", "conclusion": "success"}]}
        answers = {f"repos/{REPO}/actions/runs/{100 + i}/jobs": {"jobs": [
            {"id": 1000 + i, "name": "Cheap checks", "status": "completed", "conclusion": "success", "runner_name": "ALEX-PC-unreal-light",
             "started_at": ago(300), "completed_at": ago(296)},
            {"id": 2000 + i, "name": "Compile and test", "status": "completed", "conclusion": "success", "runner_name": "ALEX-PC-unreal-pr",
             "started_at": ago(295), "completed_at": ago(255), "steps": [{"name": "Build editor", "status": "completed", "conclusion": "success",
                                                                         "started_at": ago(294), "completed_at": ago(270)}]}]} for i in range(4)}
        answers[f"repos/{REPO}/actions/runs"] = runs
        w = devwatch.DevWatch({}, github=FakeGitHub(answers))
        w._backfill(REPO)
        avg, n = w.history.estimate(("build", REPO))
        self.assertEqual(n, 4)
        self.assertAlmostEqual(avg, 40 * 60, delta=2)
        self.assertEqual(w.history.estimate(("quick", REPO))[0], 4 * 60)
        self.assertEqual(w.store.job_seconds(REPO, "PR check", "Compile and test"), 40 * 60)
        self.assertIsNone(w.history.estimate(("build", "Acme/Other"))[0])

    def test_a_town_build_is_never_learned_as_two_minutes(self):
        """Shaped like LifeShare on 2026-11-19 (job names, steps and times copied from real runs): every green
        "Compile and test" was a 2-minute skip for a tooling change; the town and game builds that really built all
        ended red after 13 to 45 minutes; others were cancelled or failed in set-up. A town change's Build & test must
        come out at 10 minutes or more, and say it is for a town change."""
        def run(i, title, branch, conclusion):
            return {"id": 300 + i, "name": "PR check", "event": "pull_request", "status": "completed", "conclusion": conclusion,
                    "display_title": f"#{2300 + i} {title}", "head_branch": branch, "pull_requests": [{"number": 2300 + i}]}

        def jobs(i, build_min, conclusion, failed_step="Build and test (Tools\\CI\\PrCheckJob.ps1)"):
            steps = [{"name": "Set up job", "status": "completed", "conclusion": "success", "started_at": ago(build_min + 1), "completed_at": ago(build_min + 1)},
                     {"name": "Check out the merge commit in this machine's standing checkout", "status": "completed",
                      "conclusion": "failure" if failed_step.startswith("Check out") else "success",
                      "started_at": ago(build_min + 1), "completed_at": ago(build_min)},
                     {"name": "Build and test (Tools\\CI\\PrCheckJob.ps1)", "status": "completed",
                      "conclusion": conclusion if failed_step.startswith("Build") else "skipped", "started_at": ago(build_min), "completed_at": ago(0.2)}]
            return {"jobs": [
                {"id": 4000 + i, "name": "PR check / Cheap checks", "status": "completed", "conclusion": "success",
                 "runner_name": "SF-NODE-01-unreal-light", "started_at": ago(build_min + 20), "completed_at": ago(build_min + 10)},
                {"id": 5000 + i, "name": "PR check / Compile and test", "status": "completed", "conclusion": conclusion,
                 "runner_name": "SF-NODE-01-unreal-pr", "created_at": ago(build_min + 60), "started_at": ago(build_min + 1),
                 "completed_at": ago(0), "steps": steps},
                {"id": 6000 + i, "name": "PR check / Tip status", "status": "completed", "conclusion": "skipped",
                 "started_at": ago(0), "completed_at": ago(0)}]}
        answers, runs = {}, []
        for i in range(15):          # tooling changes: the build machine is skipped after 2 minutes, green
            runs.append(run(i, "Nightly housekeeping sweeps finished town worktrees" if i == 0 else "PR check tooling", "claude/tools", "success"))
            answers[f"repos/{REPO}/actions/runs/{300 + i}/jobs"] = jobs(i, 2, "success")
        for i, (title, mins) in enumerate([("City rebuild checks (RouteScale, Woods)", 27), ("Town surfaces: level car parks", 45),
                                           ("Ground town sidewalks and parking lots", 38)], start=20):
            runs.append(run(i, title, "codex/town", "failure"))
            answers[f"repos/{REPO}/actions/runs/{300 + i}/jobs"] = jobs(i, mins, "failure")
        runs.append(run(30, "Town square corners", "codex/town", "failure"))      # the machine broke: no time
        answers[f"repos/{REPO}/actions/runs/330/jobs"] = jobs(30, 1, "failure", failed_step="Check out")
        runs.append(run(31, "Build Alderwick Marina", "codex/marina", "cancelled"))   # superseded: never read
        answers[f"repos/{REPO}/actions/runs"] = {"workflow_runs": runs}
        kinds = {"Town change": ["town ", "town:", "city rebuild", "/town", "marina"]}
        w = devwatch.DevWatch({"pipeline": {"kinds": kinds}}, github=FakeGitHub(answers))
        w._backfill(REPO)
        self.assertNotIn(f"repos/{REPO}/actions/runs/331/jobs", w.gh.asked)
        avg, n = w.history.estimate(("build", REPO, "Town change"), own_from=3)
        self.assertGreaterEqual(avg, 10 * 60, "a town Build & test is never ~2 min")
        self.assertAlmostEqual(avg, 33.5 * 60, delta=60)      # 3 (a tooling PR that says "town"), 28, 39, 46 min
        pr = {"number": 2340, "title": "Town surfaces: level car parks and park paving", "head": {"ref": "claude/town", "sha": "c" * 40},
              "base": {"ref": "Test-Staging"}, "html_url": "", "updated_at": ago(1)}
        checks = [{"id": 1, "name": "PR check / Cheap checks", "status": "completed", "conclusion": "success",
                   "started_at": ago(90), "completed_at": ago(80)},
                  {"id": 2, "name": "PR check / Compile and test", "status": "in_progress", "started_at": ago(39)}]
        c = pipeline.open_change(w.history, w.rules, REPO, pr, checks, {}, w._runner_machine, time.time())
        self.assertEqual((c["kind"], c["stage"]), ("Town change", "build"))
        self.assertGreaterEqual(c["step"]["avg"], 10 * 60)
        self.assertEqual(c["step"]["scope"], "kind")
        self.assertFalse(c["step"]["late"])

    def test_old_green_only_build_times_are_dropped(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            f = Path(d) / "steps.json"
            f.write_text(json.dumps({"version": 2, "samples": {"build|Acme/Game": [[129, "a"]], "own|Acme/Game|7|build": [[129, "b"]],
                                                                "quick|Acme/Game": [[300, "c"]], "staging|Acme/Game|all": [[900, "d"]]}}), encoding="utf-8")
            h = pipeline.History(f)
            self.assertEqual(sorted(h.samples), ["quick|Acme/Game", "staging|Acme/Game|all"])

    def test_nothing_merging_is_a_signal(self):
        w = self.watch()
        w.flow_now = lambda prs, at: {"all": {}, "repos": {REPO: {"since_merge": 4 * 3600, "last": [{"at": at - 4 * 3600}],
                                                                  "verdict": {"state": "stopped", "why": "Nothing merged for 4 h while 3 changes wait."}}}}
        b = next(x for x in w.snapshot()["blockers"] if x["kind"] == "flow")
        self.assertEqual((b["key"], b["repo"]), (f"flow:{REPO}", REPO))
        self.assertIn("for 4 h", b["title"])

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
    def test_only_recent_output_in_this_checkout_and_lease(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "Saved" / "Logs"
            logs.mkdir(parents=True)
            f = logs / "ImportEnvironment.log"
            f.touch()
            now = time.time()
            os.utime(f, (now-2, now-2))
            lease = {"acquiredUtc": ago(2), "workspaces": [d]}
            self.assertEqual(reporter.latest_check_output(lease, now)["label"], "Environment import")
            self.assertEqual(reporter.latest_check_output({**lease, "workspaces": d}, now)["label"], "Environment import")
            os.utime(f, (now-200, now-200))
            self.assertIsNone(reporter.latest_check_output(lease, now))

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
