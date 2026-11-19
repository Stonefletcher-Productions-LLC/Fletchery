"""Backend sign-in selection, with two simulated GitHub CLI credentials."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).parent))
from accounts import Accounts


class AccountTests(TestCase):
    def test_selected_login_survives_reload_and_drives_github_transports(self):
        calls = []

        def gh(args, env):
            calls.append(args)
            if args[1] == "status":
                return json.dumps({"hosts": {"github.com": [
                    {"login": "first", "state": "success"}, {"login": "second", "state": "success"}]}})
            if args[1] == "token":
                self.assertEqual(args[-1], "second")
                self.assertNotIn("GH_TOKEN", env)
                return "fixture-second-credential"
            return "second" if (env or {}).get("GH_TOKEN") == "fixture-second-credential" else "first"

        with TemporaryDirectory() as d, patch("accounts._gh", side_effect=gh), patch.dict(os.environ, {"GH_TOKEN": "fixture-inherited"}):
            original = Accounts(d)
            original.identify()
            self.assertEqual(original.login, "first")
            original.select("second")
            self.assertEqual(original.login, "first")  # pending until the supervised reload
            self.assertEqual(json.loads(original.path.read_text()), {"original": "first", "login": "second"})
            active = Accounts(d)
            self.assertEqual(active.login, "second")
            self.assertEqual(active.token(), "fixture-second-credential")
            self.assertEqual(active.cli_env()["GH_TOKEN"], active.token())
            self.assertEqual(os.environ["GH_TOKEN"], "fixture-inherited")
            self.assertNotEqual(active.live_directory(), Path(d))
            with self.assertRaises(ValueError):
                active.select("unknown")
            self.assertEqual(Accounts(d).login, "second")
            with patch("accounts._gh", side_effect=ValueError("unavailable")), self.assertRaises(ValueError):
                Accounts(d).cli_env()
