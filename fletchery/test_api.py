"""The stable read API (api.py: history, Prometheus metrics), agents and projects from the profile (devwatch.py), and
Flow per project (flow.py)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import api  # noqa: E402
import devwatch  # noqa: E402
import flow  # noqa: E402
import presence  # noqa: E402
import store  # noqa: E402
from test_devwatch import FakeGitHub, REPO, world  # noqa: E402
from test_rules import at, merge  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent.parent / "ui" / "test-live-fixture.json").read_text(encoding="utf-8"))
NOW = 1_800_000_000.0


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.s = store.Store(None)
        self.addCleanup(self.s.close)
        self.s.merges("Acme/Game", [merge(1, "a", 300), merge(2, "b", 200), merge(3, "c", 100)])
        self.s.merges("Acme/Tool", [merge(9, "x", 150)])

    def test_rows_oldest_first_and_paged(self):
        out = api.history(self.s, "merges", limit=2)
        self.assertEqual([r["number"] for r in out["rows"]], [1, 2])
        self.assertEqual(out["schema"], 2)
        nxt = api.history(self.s, "merges", since=out["next"] + 0.001, limit=3)
        self.assertEqual([r["number"] for r in nxt["rows"]], [9, 3])
        self.assertIsNone(nxt["next"])
        self.assertEqual([r["number"] for r in api.history(self.s, "merges", repo="Acme/Tool")["rows"]], [9])

    def test_bad_questions(self):
        for kw in ({"table": "sqlite_master"}, {"table": "merges", "repo": "x'; drop"}, {"table": "disks", "repo": "Acme/Game"},
                   {"table": "merges", "since": "yesterday"}):
            with self.assertRaises(ValueError, msg=kw):
                api.history(self.s, **kw)


class MetricsTest(unittest.TestCase):
    def test_fixture_as_prometheus_text(self):
        text = api.metrics(FIXTURE)
        self.assertIn("# TYPE fletchery_up gauge\nfletchery_up 1\n", text)
        self.assertIn('fletchery_disk_full_in_seconds{machine="SF-NODE-01",drive="E:"} 9000', text)
        self.assertIn('fletchery_runner_online{runner="MY-PC-unreal-pr",machine="Gaming PC"} 1', text)
        self.assertIn('fletchery_pool_workers{pool="unreal-pr"} 2', text)
        self.assertIn('fletchery_base_red{repo="Acme/Game",branch="Test-Staging"} 1', text)
        self.assertIn('fletchery_merges_last_day{repo="Acme/Game"}', text)
        for line in text.splitlines():           # every sample line is name{labels} number
            if not line.startswith("#"):
                self.assertRegex(line, r'^[a-z0-9_]+(\{[^}]*\})? -?[0-9.e+]+$')
        self.assertEqual(text.count("# TYPE fletchery_disk_free_gb"), 1)

    def test_label_values_are_escaped(self):
        text = api.metrics({"machines": [{"name": 'A "quoted"\nname', "online": True}]})
        self.assertIn('machine="A \\"quoted\\" name"', text)


class ProfileTest(unittest.TestCase):
    def tearDown(self):
        devwatch.AGENTS = (("claude/", "Claude"), ("codex/", "Codex"))
        devwatch.AGENT_LOGINS = (("codex", "Codex"),)
        presence.AGENTS.pop("devin", None)

    def test_agents_from_the_profile(self):
        notes = devwatch.set_agents([{"name": "Claude", "branches": ["claude/", "cc/"]},
                                     {"name": "Devin", "logins": ["devin-ai-integration[bot]"]}, {"name": "<b>"}])
        self.assertEqual(len(notes), 1)
        self.assertEqual(devwatch._agent_of("cc/fix-roads", ""), "Claude")
        self.assertEqual(devwatch._agent_of("devin/x", ""), "Devin")             # default branch prefix: name/
        self.assertEqual(devwatch._agent_of("feature", "devin-ai-integration[bot]"), "Devin")
        self.assertEqual(devwatch._agent_of("codex/x", "codex"), "")               # replaced, not added
        self.assertIn("devin", presence.AGENTS)
        self.assertEqual(devwatch.set_agents(None), [])

    def test_projects(self):
        w = devwatch.DevWatch({"projects": {"Game": [REPO, "Acme/Art"], "bad": "x"}}, github=FakeGitHub(world()), log=(lines := []).append)
        w.poll()
        w.repos.append({"full_name": "Acme/Art", "url": ""})
        w.repos.append({"full_name": "Acme/Site", "url": ""})
        self.assertEqual(w.projects_now(), [{"name": "Game", "repos": [REPO, "Acme/Art"]}, {"name": "Site", "repos": ["Acme/Site"]}])
        self.assertTrue(any("bad" in x for x in lines))
        snap = w.snapshot()
        self.assertEqual(snap["schema"], 2)
        self.assertIn("Game", snap["flow"]["projects"])
        self.assertTrue(all(b.get("project") == "Game" for b in snap["blockers"] if b["repo"] == REPO))
        self.assertTrue(all(b["alert"]["group"] == "Game" for b in snap["blockers"] if b["repo"] == REPO))

    def test_flow_per_project(self):
        s = store.Store(None)
        self.addCleanup(s.close)
        s.merges("Acme/Game", [merge(1, "a", 30)])
        s.merges("Acme/Art", [merge(2, "b", 20)])
        s.merges("Acme/Site", [merge(3, "c", 10)])
        out = flow.Flow(s).compute(["Acme/Game", "Acme/Art", "Acme/Site"], {"Acme/Game": 1}, NOW, projects={"Game": ["Acme/Game", "Acme/Art"]})
        self.assertEqual(out["projects"]["Game"]["merges_hour"], 2)
        self.assertEqual(out["projects"]["Game"]["since_merge"], 20 * 60)
        self.assertEqual(out["projects"]["Game"]["waiting"], 1)
        self.assertEqual(out["all"]["merges_hour"], 3)
        self.assertTrue(at(0))


if __name__ == "__main__":
    unittest.main()
