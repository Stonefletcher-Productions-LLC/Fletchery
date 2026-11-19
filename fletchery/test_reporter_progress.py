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


class RunnerUpload(unittest.TestCase):
    job = "00000000-0000-0000-0000-000000000001"
    step = "00000000-0000-0000-0000-000000000002"
    mirror = "00000000-0000-0000-0000-000000000003"
    marker = "Holding the PC for PR check #12 (GitHub run 345): Tools\\CI\\PrCheckJob.ps1"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pages = self.root / "_diag" / "pages"
        self.pages.mkdir(parents=True)
        self.now = int(time.time())
        self.lease = {"workspaces": [str(self.root)], "acquiredUtc": self.iso(-300),
                      "purpose": "PR check #12 (GitHub run 345): Tools\\CI\\PrCheckJob.ps1"}
        self.runner = reporter.RunnerLogProgress(str(self.root))

    def iso(self, offset):
        return datetime.fromtimestamp(self.now + offset, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def page(self, step, number, rows, partial=""):
        path = self.pages / f"{self.job}_{step}_{number}.log"
        data = "".join(f"{self.iso(offset)} {message}\r\n" for offset, message in rows)
        path.write_bytes((data + partial).encode())
        return path

    def latest(self, offset=0):
        return reporter.latest_check_output(self.lease, self.now + offset, self.runner)

    def test_mirrors_partial_records_measured_eta_and_rotation(self):
        anchor = self.page(self.step, 1, [(-300, self.marker), (-80, "uploaded 40 of 200"),
                                          (-60, "uploaded 80 of 200")])
        duplicate = self.page(self.mirror, 1, [(-300, self.marker), (-79.999, "uploaded 40 of 200"),
                                                (-59.999, "uploaded 80 of 200")])
        page2 = self.page(self.step, 2, [(-40, "uploaded 120 of 200")], self.iso(-5) + " uploaded 160 of 20")
        self.assertEqual(self.latest()["upload"], {"done": 120, "total": 200, "eta_seconds": 40})
        anchor.unlink()
        duplicate.unlink()
        page2.unlink()
        stale = self.latest(200)
        self.assertEqual(stale["upload"], {"done": 120, "total": 200, "eta_seconds": None})
        self.assertEqual(stale["at"], CheckObservations.iso(self.now - 40))
        self.page(self.step, 3, [(201, "uploaded 200 of 200")])
        finished = self.latest(201)
        self.assertEqual(finished["label"], "Uploaded generated assets")
        self.assertEqual(finished["upload"]["eta_seconds"], 0)
        self.page(self.step, 4, [(202, "== Game scenarios and navigation mapped to the paths (RunAllTests -Set Auto)")])
        self.assertNotIn("upload", self.latest(202))
        saved = self.root / "Saved" / "Logs" / "Specs.log"
        saved.parent.mkdir(parents=True)
        saved.touch()
        os.utime(saved, (self.now + 203, self.now + 203))
        self.assertEqual(self.latest(203)["label"], "Automation specs")

    def test_exact_lease_and_stream_required_and_no_old_state_after_handoff(self):
        self.page(self.step, 1, [(-300, self.marker.replace("345", "3456")), (-10, "uploaded 40 of 90")])
        self.page(self.mirror, 1, [(-600, self.marker), (-10, "uploaded 80 of 90")])
        self.assertIsNone(self.latest())
        self.page(self.step, 1, [(-300, self.marker), (-6, "LFS: 90 file(s) to upload, in batches of 40.")])
        self.assertEqual(self.latest()["upload"], {"done": 0, "total": 90, "eta_seconds": None})
        self.page(self.step, 2, [(-5, "::error::LFS upload kept failing at file 40 of 90. Nothing was pushed.")])
        self.assertEqual(self.latest()["label"], "Generated asset upload failed")
        self.lease["acquiredUtc"] = self.iso(-4)
        self.assertIsNone(self.latest())
