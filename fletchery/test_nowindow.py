"""No console windows from Fletchery's children, and stepping aside while a listed game runs (2026-11-16).
No agents or network."""
import os
import subprocess
import sys
import unittest

import nowindow


class FlagTests(unittest.TestCase):
    def test_no_window_is_added_unless_a_console_was_asked_for(self):
        self.assertEqual(nowindow.flags_for(0), nowindow.CREATE_NO_WINDOW)
        self.assertEqual(nowindow.flags_for(0x00004000), 0x00004000 | nowindow.CREATE_NO_WINDOW)   # below normal kept
        self.assertEqual(nowindow.flags_for(0x00000010), 0x00000010)   # CREATE_NEW_CONSOLE: on purpose
        self.assertEqual(nowindow.flags_for(0x00000008 | 0x200), 0x00000008 | 0x200)   # detached: no console anyway

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_install_patches_popen_and_children_still_run(self):
        self.assertTrue(nowindow.install())
        self.assertTrue(nowindow.install())   # twice is harmless
        out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "ok")

    def test_node_worker_hides_windows_the_same_way(self):
        import node_worker
        if os.name == "nt":
            self.assertTrue(getattr(subprocess.Popen, "_fletchery_no_window", False))
        self.assertEqual(node_worker.CREATE_NO_WINDOW, nowindow.CREATE_NO_WINDOW)


class YieldTests(unittest.TestCase):
    def test_busy_with_names_the_first_listed_program_running(self):
        names = {"wow.exe", "explorer.exe"}
        self.assertEqual(nowindow.busy_with(["Minecraft.exe", "Wow.exe"], names), "Wow.exe")
        self.assertEqual(nowindow.busy_with(["Minecraft.exe"], names), "")
        self.assertEqual(nowindow.busy_with([], names), "")
        self.assertEqual(nowindow.busy_with(None, names), "")

    def test_running_names_sees_this_python(self):
        names = nowindow.running_names()
        if not names:
            self.skipTest("process list unavailable here")
        self.assertIn(os.path.basename(sys.executable).lower(), names)

    def test_node_stats_show_paused_while_a_listed_game_runs(self):
        import node_worker
        me = os.path.basename(sys.executable)
        st = node_worker.stats({"yieldTo": [me], "workRoot": "."})
        if not nowindow.running_names():
            self.skipTest("process list unavailable here")
        self.assertTrue(st["paused"])
        self.assertTrue(st["pause_file"])      # counted like a person's pause: the coordinator never sees it free
        self.assertEqual(st["yield"], me)
        self.assertNotIn("yield", node_worker.stats({"yieldTo": ["no-such-game.exe"], "workRoot": "."}))


if __name__ == "__main__":
    unittest.main()
