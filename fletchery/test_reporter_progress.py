"""Read-only PR check progress from the job's own reserved checkout."""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import reporter


class CheckObservations(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = int(time.time())
        self.acquired = self.now - 3600
        self.lease = {"workspaces": [str(self.root)], "acquiredUtc": self.iso(self.acquired)}

    @staticmethod
    def iso(stamp):
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def write(self, name, stamp, content=b"private log data"):
        path = self.root / "Saved" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (stamp, stamp))

    def latest(self):
        return reporter.latest_check_output(self.lease, self.now)

    def test_photo_count_tracks_written_images_and_gives_way_to_import(self):
        self.write("Logs/BuildTown_City.log", self.now - 500)
        self.write("LSHomeCardShots/City.log", self.now - 310)
        self.write("LSHomeCardShots/T_UI_Home_City_old.png", self.acquired - 10)
        self.write("LSHomeCardShots/T_UI_Home_City_future.png", self.now + 10)
        self.write("LSHomeCardShots/T_UI_Home_City_empty.png", self.now - 1, b"")
        self.write("LSHomeCardShots/T_UI_Home_Other_3.png", self.now - 1)
        self.write("LSHomeCardShots/T_UI_Home_City_A.png", self.now - 304)
        self.write("LSHomeCardShots/T_UI_Home_City_B.png", self.now - 300)
        self.assertEqual(self.latest(), {"label": "City home-selection photos (2 photos saved)",
                                         "at": self.iso(self.now - 300)})
        self.write("Logs/ImportHomeShots.log", self.now - 2)
        self.assertEqual(self.latest()["label"], "Importing home-selection photos")

    def test_scenario_output_ignores_archived_retries_and_other_reservations(self):
        self.write("LSFlowTest/flow_20261119_100000/Ava.log", self.acquired - 30)
        self.write("LSFlowTest/RunFlowTest_attempt1_flow_20261119_100000/Ben.log", self.now - 1)
        self.write("Logs/checks.log", self.now - 1)
        self.assertIsNone(self.latest())
        self.write("LSFourPlayerSmoke/four_123/Dana.log", self.now - 12)
        self.assertEqual(self.latest(), {"label": "Testing a four-player game", "at": self.iso(self.now - 12)})
