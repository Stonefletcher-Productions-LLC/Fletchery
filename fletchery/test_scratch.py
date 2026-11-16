"""Scratch retention: real git repositories in temporary folders; nothing outside them is touched."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import scratch


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if done.returncode:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


class ScratchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fletchery-scratch-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.host = self.tmp / "host"          # the main clone, outside the scratch root
        git(self.tmp, "clone", "-q", str(self.remote), str(self.host))
        (self.host / ".gitignore").write_text("Intermediate/\nBinaries/\nSaved/\nDerivedDataCache/\nnotes.local\n", encoding="utf-8")
        (self.host / "Source.cpp").write_text("int x;\n", encoding="utf-8")
        git(self.host, "add", "-A")
        git(self.host, "commit", "-q", "-m", "base")
        git(self.host, "push", "-q", "origin", "HEAD:main")
        git(self.host, "fetch", "-q", "origin")
        self.root = self.tmp / "scratch"
        self.root.mkdir()
        self.later = time.time() + 30 * 86400   # "now" for a pass: everything made here is 30 days idle

    def worktree(self, name, pushed=True):
        path = self.root / name
        git(self.host, "worktree", "add", "-q", "-b", "claude/" + name.replace("/", "-"), str(path), "origin/main")
        (path / "Work.cpp").write_text(f"int y; // {name}\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "work")
        if pushed:
            git(path, "push", "-q", "origin", "HEAD:claude/" + name.replace("/", "-"))
            git(path, "fetch", "-q", "origin")
        for folder in ("Intermediate/Build", "Binaries/Win64", "Saved/Logs", "Saved/Cooked"):
            (path / folder).mkdir(parents=True)
            (path / folder / "big.bin").write_bytes(b"x" * 1000)
        return path

    def run_pass(self, **kw):
        kw.setdefault("lines", [])
        kw.setdefault("now", self.later)
        return scratch.retain([str(self.root)], **kw)

    def reason(self, summary, path):
        return next(k["reason"] for k in summary["kept"] if k["path"] == str(path))

    def test_trim_deletes_only_regenerable_output(self):
        wt = self.worktree("feature")
        summary = self.run_pass(mode="trim")
        self.assertEqual(summary["error"], "")
        self.assertFalse((wt / "Intermediate").exists())
        self.assertFalse((wt / "Binaries").exists())
        self.assertFalse((wt / "Saved" / "Cooked").exists())
        self.assertTrue((wt / "Saved" / "Logs" / "big.bin").exists())
        self.assertTrue((wt / "Work.cpp").exists())
        self.assertEqual(summary["freed_bytes"], 3000)
        self.assertEqual(summary["removed"], [])

    def test_report_and_dry_run_change_nothing(self):
        wt = self.worktree("feature")
        for kw in ({"mode": "report"}, {"mode": "remove", "dry_run": True}):
            summary = self.run_pass(**kw)
            self.assertTrue((wt / "Intermediate" / "Build" / "big.bin").exists())
            self.assertEqual(summary["freed_bytes"], 0)
        self.assertEqual(len(summary["removed"]), 1)   # the dry run says what it would do

    def test_remove_clean_pushed_worktree_keeps_branch_and_logs(self):
        wt = self.worktree("done")
        backup = self.tmp / "backup"
        summary = self.run_pass(mode="remove", backup_dir=str(backup))
        self.assertEqual(summary["error"], "")
        self.assertFalse(wt.exists())
        self.assertEqual(summary["removed"][0]["branch"], "claude/done")
        self.assertIn("claude/done", git(self.host, "branch", "--list", "claude/done"))
        self.assertEqual(len(list(backup.glob("done-*/big.bin"))), 1)
        self.assertNotIn(str(wt).replace("\\", "/"), git(self.host, "worktree", "list").replace("\\", "/"))

    def test_unpushed_dirty_untracked_and_unique_ignored_are_kept_for_a_person(self):
        unpushed = self.worktree("unpushed", pushed=False)
        dirty = self.worktree("dirty")
        (dirty / "Source.cpp").write_text("int z;\n", encoding="utf-8")
        untracked = self.worktree("untracked")
        (untracked / "New.cpp").write_text("new\n", encoding="utf-8")
        ignored = self.worktree("ignored")
        (ignored / "notes.local").write_text("only copy\n", encoding="utf-8")
        summary = self.run_pass(mode="remove")
        self.assertEqual(summary["removed"], [])
        for path, word in ((unpushed, "unpushed"), (dirty, "uncommitted"), (untracked, "uncommitted"), (ignored, "unique ignored")):
            self.assertTrue((path / "Work.cpp").exists())
            self.assertIn(word, self.reason(summary, path))
            self.assertFalse((path / "Intermediate").exists())   # still trimmed
        self.assertEqual({Path(a["path"]).name for a in summary["attention"]}, {"unpushed", "dirty", "untracked", "ignored"})

    def test_locked_and_nested_worktrees_are_kept(self):
        locked = self.worktree("locked")
        git(self.host, "worktree", "lock", str(locked))
        outer = self.worktree("outer")
        git(self.host, "worktree", "add", "-q", "-b", "claude/inner", str(outer / "Saved" / "inner"), "origin/main")
        summary = self.run_pass(mode="remove")
        self.assertEqual(summary["removed"], [])
        self.assertIn("locked", self.reason(summary, locked))
        self.assertIn("another worktree lives inside", self.reason(summary, outer))
        self.assertTrue((outer / "Saved" / "inner" / "Source.cpp").exists())

    def test_clone_with_attached_worktree_is_only_trimmed(self):
        clone = self.root / "before"
        git(self.tmp, "clone", "-q", str(self.remote), str(clone))
        (clone / "Intermediate").mkdir()
        (clone / "Intermediate" / "big.bin").write_bytes(b"x" * 10)
        git(clone, "worktree", "add", "-q", "-b", "ias", str(self.root / "ias-attached"), "origin/main")
        (self.root / "ias-attached" / "New.cpp").write_text("in progress\n", encoding="utf-8")
        summary = self.run_pass(mode="remove")
        self.assertTrue((clone / "Source.cpp").exists())
        self.assertFalse((clone / "Intermediate").exists())
        self.assertIn("never removed", self.reason(summary, clone))
        self.assertTrue((self.root / "ias-attached" / "Source.cpp").exists())

    def test_busy_recent_keep_protected_and_plain_folders_are_untouched(self):
        busy = self.worktree("busy")
        kept = self.worktree("LifeShareArt")
        guarded = self.worktree("foreman")
        (guarded / "state").mkdir()
        plain = self.root / "notes"
        plain.mkdir()
        (plain / "Intermediate").mkdir()
        summary = self.run_pass(mode="remove", keep=["lifeshareart", "rescue-*"], protected=[str(guarded / "state")],
                                lines=[f"unrealeditor.exe {str(busy).replace(os.sep, '/').lower()}/game.uproject"])
        self.assertEqual((summary["removed"], summary["trimmed"]), ([], []))
        self.assertIn("process", self.reason(summary, busy))
        self.assertIn("keep list", self.reason(summary, kept))
        self.assertIn("protected", self.reason(summary, guarded))
        self.assertIn("left alone", self.reason(summary, plain))
        recent = self.run_pass(mode="remove", now=time.time() + 3600)
        self.assertEqual((recent["removed"], recent["trimmed"]), ([], []))
        self.assertIn("Used", self.reason(recent, busy))

    def test_checkouts_inside_a_grouping_folder_are_found_but_not_deeper(self):
        (self.root / "wt").mkdir()
        nested = self.worktree("wt/petcorner")
        (self.root / "deep" / "er").mkdir(parents=True)
        deeper = self.worktree("deep/er/lost")
        (self.root / "LifeShareArt").mkdir()
        kept = self.worktree("LifeShareArt/owned")
        summary = self.run_pass(mode="trim", keep=["LifeShareArt"])
        self.assertFalse((nested / "Intermediate").exists())
        self.assertTrue((nested / "Work.cpp").exists())
        self.assertTrue((deeper / "Intermediate").exists())
        self.assertTrue((kept / "Intermediate").exists())
        self.assertIn("left alone", self.reason(summary, self.root / "deep"))

    def test_busy_match_needs_a_folder_boundary(self):
        self.assertTrue(scratch.named_by(Path("E:/s/round"), ['tail -f "e:/s/round/saved/logs/x.log"']))
        self.assertTrue(scratch.named_by(Path("E:/s/round"), ["bash /e/s/round/run.sh"]))
        self.assertFalse(scratch.named_by(Path("E:/s/round"), ["editor e:/s/round-2/game.uproject"]))
        self.assertTrue(scratch.named_by(Path("E:/s/town"), ["powershell drive.ps1 -log e:/s/town-logs/run.txt"]))
        self.assertFalse(scratch.named_by(Path("E:/s/town"), ["powershell drive.ps1 -log e:/s/town-logsx/run.txt"]))

    def test_deep_log_write_and_future_times_count_as_recent(self):
        wt = self.worktree("logging")
        log = wt / "Saved" / "Logs" / "big.bin"
        os.utime(log, (self.later - 600, self.later - 600))
        summary = self.run_pass(mode="remove")
        self.assertTrue((wt / "Intermediate").exists())
        self.assertIn("Used 0.2 h ago", self.reason(summary, wt))
        os.utime(log, (self.later + 9000, self.later + 9000))
        self.assertIn("Used 0.0 h ago", self.reason(self.run_pass(mode="remove"), wt))

    def test_urgent_trims_sooner_but_never_removes_sooner(self):
        wt = self.worktree("yesterday")
        at = time.time() + 5 * 3600
        self.assertEqual(self.run_pass(mode="remove", now=at)["trimmed"], [])
        self.assertEqual(scratch.TRIM_DIRS[-1], "Saved/ShaderDebugInfo")
        summary = self.run_pass(mode="remove", now=at, urgent=True)
        self.assertEqual(len(summary["trimmed"]), 3)
        self.assertTrue((wt / "Work.cpp").exists())

    def test_disposable_builds_and_tracked_build_folders(self):
        builds = self.root / "loadtimes-builds"
        (builds / "Windows").mkdir(parents=True)
        (builds / "Windows" / "Game.exe").write_bytes(b"x" * 500)
        wt = self.worktree("tracked")
        (wt / "Binaries" / "keep.dll").write_bytes(b"k")
        git(wt, "add", "-f", "Binaries/keep.dll")
        git(wt, "commit", "-q", "-m", "tracked binary")
        self.assertTrue(builds.exists() and self.run_pass(mode="trim", disposable=["*-builds"])["removed"] == [])
        summary = self.run_pass(mode="remove", disposable=["*-builds"])
        self.assertFalse(builds.exists())
        self.assertTrue((wt / "Binaries" / "keep.dll").exists())
        self.assertIn("tracked files", self.reason(summary, wt))

    def test_bad_mode_missing_root_and_unreadable_processes_fail_closed(self):
        wt = self.worktree("feature")
        self.assertIn("Unknown", scratch.retain([str(self.root)], mode="purge")["error"])
        self.assertIn("missing", scratch.retain([str(self.tmp / "nope")], lines=[])["kept"][0]["reason"])
        original = scratch.command_lines
        scratch.command_lines = lambda: (_ for _ in ()).throw(RuntimeError("Cannot list running processes"))
        try:
            summary = scratch.retain([str(self.root)], mode="remove", now=self.later)
        finally:
            scratch.command_lines = original
        self.assertIn("Cannot list", summary["error"])
        self.assertTrue((wt / "Intermediate").exists())

    def test_node_worker_pass_protects_its_own_folders_and_writes_a_receipt(self):
        import node_worker
        wt = self.worktree("feature")
        work = self.root / "node-work"
        (work / "fletchery" / "LifeShare" / "Intermediate").mkdir(parents=True)
        said = []
        original = scratch.command_lines
        scratch.command_lines = lambda: []
        clock = patch("scratch.time.time", return_value=self.later)
        clock.start()
        self.addCleanup(clock.stop)
        try:
            self.assertEqual(node_worker.scratch_pass({"workRoot": str(work)}, said.append), {})
            found = node_worker.scratch_pass({"workRoot": str(work), "scratchRoots": [str(self.root)]}, said.append)
        finally:
            scratch.command_lines = original
        self.assertEqual(found["error"], "")
        self.assertFalse((wt / "Intermediate").exists())
        self.assertTrue((work / "fletchery" / "LifeShare" / "Intermediate").exists())
        self.assertTrue((work / "fletchery" / "scratch-retention.json").exists())
        self.assertTrue(any("trimmed 3" in line for line in said))


if __name__ == "__main__":
    unittest.main()
