"""Stacked dependents (Fletchery #183): a task may start on its one open dependency's PR branch once that PR is ready.

Run: python fletchery/test_stacking.py"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

import graph  # noqa: E402
from graph import Task, readiness  # noqa: E402
from github import PullRequest  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def task(n, body="", status="queued", pr=None):
    t = Task(n, f"Task {n}", body, ["foreman", f"fletchery:{status}"])
    t.pr_number = pr
    return t


def test_graph():
    base = {1: task(1, status="pr", pr=11), 2: task(2, "Depends on: #1")}
    rd = readiness(dict(base), set(), set(), {1: "claude/fletchery-1-task-1"})
    check([t.number for t in rd.ready] == [2], "a task whose only open dependency is stackable is ready")
    check(base[2].stack_on == "claude/fletchery-1-task-1" and base[2].stack_parent == 1 and base[2].stack_pr == 11,
          "it records the branch, parent and parent PR to start on")
    check("Stacked on #1" in graph.stack_note(base[2]) and "PR #11" in graph.stack_note(base[2]),
          "the worker and reviewer are told what they build on")

    rd = readiness({1: task(1, status="pr", pr=11), 2: task(2, "Depends on: #1")}, set(), set(), None)
    check(rd.ready == [] and "waits for #1" in rd.waiting[2], "without stacking it waits as before")

    rd = readiness({1: task(1, status="pr", pr=11), 2: task(2, "Depends on (merged): #1")}, set(), set(),
                   {1: "b1"})
    check(rd.ready == [] and 2 in rd.waiting, "`Depends on (merged):` never stacks")
    check(task(2, "Depends on (merged): #1, #5").depends_on == [1, 5], "the merged form still counts as dependencies")
    check(task(2, "Depends on (merged): #1\nDepends on: #3").merged_dependencies == {1}, "only that line is strict")

    rd = readiness({1: task(1, status="pr", pr=11), 3: task(3, status="pr", pr=13), 2: task(2, "Depends on: #1, #3")},
                   set(), set(), {1: "b1", 3: "b3"})
    check(rd.ready == [], "two open dependencies: a branch has one starting point, so it waits")

    rd = readiness({1: task(1, "Depends on: #9", status="pr", pr=11), 2: task(2, "Depends on: #1, #9")},
                   {9}, set(), {1: "b1"})
    check([t.number for t in rd.ready] == [2], "closed dependencies beside the stackable one don't matter")

    rd = readiness({0: task(0), 1: task(1, "Depends on: #0", status="pr", pr=11), 2: task(2, "Depends on: #1")},
                   set(), set(), {1: "b1"})
    check(2 not in [t.number for t in rd.ready], "one level only: a parent with open work under it is no base")

    t = task(2, "Depends on: #1")
    t.stack_on = "old"
    rd = readiness({2: t}, {1}, set(), {})
    check(rd.ready == [t] and t.stack_on == "", "a task whose parent landed starts on the base again")


def test_pr_body():
    import pipeline
    t = task(2, "Depends on: #1")
    t.stack_on, t.stack_parent, t.stack_pr, t.stack_sha = "b1", 1, 11, "abc1234"
    cfg = SimpleNamespace()
    body = pipeline.pr_body(t, {"summary": "s"}, [], {"verdict": "approve"}, cfg)
    m = graph.STACK_MARK.search(body)
    check(bool(m) and m.groups() == ("1", "11", "abc1234"), "the PR carries a stack marker with parent and start")
    check("Stacked on PR #11" in body, "and a line people can read")
    plain = pipeline.pr_body(task(3), {"summary": "s"}, [], {"verdict": "approve"}, cfg)
    check(not graph.STACK_MARK.search(plain), "an ordinary PR has no marker")


def test_engine_helpers():
    import loop
    pr = lambda n, head, labels=("ready",), body="", sha="h": PullRequest(n, "t", head, "Test-Staging", sha, list(labels), body)
    t1, t2, t3, t4 = task(1, status="pr", pr=11), task(2, status="pr", pr=12), task(3, status="pr", pr=13), task(4, status="pr", pr=14)
    prs = {11: pr(11, t1.branch), 12: pr(12, t2.branch, labels=()), 13: pr(13, t3.branch, sha="x"),
           14: pr(14, t4.branch, body="<!-- fletchery-stacked-on:9:19:abc1234 -->")}
    eng = SimpleNamespace(state=SimpleNamespace(merge_excluded={"13": {"head": "x"}}, merge_parked={}),
                          ctx=SimpleNamespace(cfg=SimpleNamespace(merge_label="ready")))
    out = loop.Engine._stackable(eng, {1: t1, 2: t2, 3: t3, 4: t4}, prs)
    check(out == {1: t1.branch}, "only ready, not left out, not themselves stacked PRs are bases")

    saved = []
    infos = {19: {"state": "closed", "merged": False}, 29: {"state": "closed", "merged": True}, 39: {"state": "open"}}
    eng = SimpleNamespace(state=SimpleNamespace(merge_excluded={}, save=lambda p: saved.append(p)),
                          ctx=SimpleNamespace(gh=SimpleNamespace(issue_info=infos.get), cfg=SimpleNamespace(base_branch="Test-Staging"),
                                              log=lambda m: None), state_dir=Path("."))
    ready = [pr(21, "a", body="<!-- fletchery-stacked-on:9:19:abc1234 -->"),
             pr(22, "b", body="<!-- fletchery-stacked-on:9:29:abc1234 -->"),
             pr(23, "c", body="<!-- fletchery-stacked-on:9:39:abc1234 -->"), pr(24, "d")]
    keep = loop.Engine._without_orphaned_stacks(eng, ready)
    check([p.number for p in keep] == [22, 23, 24], "a PR whose stack parent closed unmerged is left out")
    check("closed without merging" in eng.state.merge_excluded["21"]["why"] and saved, "with a reason its fixer reads")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_repo():
    import repo as repo_mod
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        remote, seed = d / "remote.git", d / "seed"
        git(d, "init", "-q", "--bare", str(remote))
        git(d, "init", "-q", "-b", "Test-Staging", str(seed))
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            git(seed, "config", k, v)
        (seed / "a.txt").write_text("a\n")
        git(seed, "add", "-A"); git(seed, "commit", "-qm", "base")
        git(seed, "remote", "add", "origin", str(remote)); git(seed, "push", "-q", "origin", "Test-Staging")
        git(seed, "checkout", "-qb", "claude/fletchery-1-parent")
        (seed / "p.txt").write_text("parent\n")
        git(seed, "add", "-A"); git(seed, "commit", "-qm", "parent")
        git(seed, "push", "-q", "origin", "claude/fletchery-1-parent")
        parent_head = git(seed, "rev-parse", "HEAD")
        root = d / "host"
        git(d, "clone", "-q", str(remote), str(root))
        r = repo_mod.Repo(root, d / "wt", "Test-Staging")
        r.fetch_base()
        wt = r.worktree(2, "child", "claude/fletchery-2-child", "", "claude/fletchery-1-parent")
        check((wt / "p.txt").exists(), "a stacked task's checkout starts with the parent's work")
        check(r.stack_base(wt) == parent_head, "it remembers the parent head it started on")
        check(not r.has_commits(wt) and r.changed_files(wt) == [], "the parent's commits are not the task's own")
        (wt / "c.txt").write_text("child\n")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "child")
        check(r.has_commits(wt) and r.changed_files(wt) == ["c.txt"], "only the task's own changes count")
        try:
            r.stack_start("Test-Staging")
            check(False, "stacking on the base is refused")
        except RuntimeError:
            check(True, "stacking on the base is refused")
        # the parent lands on the base: own changes are measured from the base again
        git(seed, "checkout", "-q", "Test-Staging"); git(seed, "merge", "-q", "--no-ff", "-m", "land", "claude/fletchery-1-parent")
        git(seed, "push", "-q", "origin", "Test-Staging")
        r.fetch_base()
        check(r.stack_base(wt) == "" and r.changed_files(wt) == ["c.txt"], "after the parent lands, the base is the base")


if __name__ == "__main__":
    test_graph()
    test_pr_body()
    test_engine_helpers()
    test_repo()
    print(f"RESULT: {'FAIL' if FAILS else 'PASS'} (stacked dependents, {len(FAILS)} failed)")
    sys.exit(1 if FAILS else 0)
