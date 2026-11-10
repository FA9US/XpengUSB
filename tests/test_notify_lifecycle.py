"""Pushover lifecycle and outage tests using a disposable status file."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xpengusb import notify


class NotificationLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        self.status_file = directory / "sync.json"
        self.state_file = directory / "push-state.json"
        credentials = directory / "push.json"
        self.write(credentials, {"token": "fixture", "user": "fixture"})
        paths = patch.multiple(notify, PUSHOVER=credentials, SYNC_STATUS=self.status_file,
                               STATE_FILE=self.state_file, write_json=self.write)
        paths.start()
        self.addCleanup(paths.stop)
        sender = patch.object(notify, "send")
        self.send = sender.start()
        self.addCleanup(sender.stop)
        self.status = {"cycle": 1, "phase": "uploading", "archive_attempt": 100,
                       "pending": 4, "archived": 20, "capture": "completed",
                       "capture_checked": 10}

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def tick(self, **update):
        self.status.update(update)
        self.write(self.status_file, self.status)
        notify.tick(True)

    def titles(self):
        return [call.args[1] for call in self.send.call_args_list]

    def test_start_failure_retry_and_verified_completion(self):
        self.tick()
        self.tick(phase="idle", archive="NAS connection interrupted; retry scheduled",
                  archive_failure=100, archive_failure_reason="NAS connection interrupted")
        self.tick(phase="uploading", archive_attempt=200)
        self.tick(phase="idle", archive="completed", pending=0, archived=24)
        self.tick()
        self.assertEqual(["XpengUSB backup started", "XpengUSB backup needs attention",
                          "XpengUSB backup retry", "XpengUSB backup complete"], self.titles())
        self.assertIn("4 files verified", self.send.call_args.args[2])

    def test_failed_delivery_retains_order_without_suppressing_distinct_events(self):
        self.send.side_effect = OSError("fixture outage")
        with patch.object(notify.time, "time", return_value=1000):
            self.tick()
            self.tick(phase="idle", archive="completed", pending=0, archived=24)
        self.assertEqual(["XpengUSB backup started"], self.titles())
        self.send.side_effect = None
        with patch.object(notify.time, "time", return_value=1900):
            self.tick()
            self.tick()
        self.assertEqual(["XpengUSB backup started", "XpengUSB backup started",
                          "XpengUSB backup complete"], self.titles())

    def test_low_capacity_stays_visible_even_when_the_nas_queue_clears(self):
        self.tick(capture="Low storage: capture skipped")
        self.tick(phase="idle", archive="completed", pending=0, archived=24)
        self.tick()
        self.assertEqual(["XpengUSB backup started", "XpengUSB capture needs attention",
                          "XpengUSB backup complete"], self.titles())
        self.assertIn("still be on the USB volume", self.send.call_args.args[2])

    def test_offline_failure_survives_a_retry_completed_before_next_poll(self):
        self.tick()
        self.tick(phase="idle", archive="completed", pending=0, archived=24,
                  archive_attempt=300, archive_failure=200,
                  archive_failure_reason="NAS transfer was interrupted; retry scheduled")
        self.tick()
        self.tick()
        self.assertEqual(["XpengUSB backup started", "XpengUSB backup needs attention",
                          "XpengUSB backup retry", "XpengUSB backup complete"], self.titles())


if __name__ == "__main__":
    unittest.main()
