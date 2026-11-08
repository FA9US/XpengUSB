"""Regression checks for the HA access boundary and arrival timing."""
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from xpengusb import dashboard_control, notify
from xpengusb.nas_probe import mount_problem

spec = importlib.util.spec_from_file_location(
    "arrival", Path(__file__).resolve().parents[1] / "custom_components/xpengusb/arrival.py")
arrival_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arrival_module)


class ArrivalTests(unittest.TestCase):
    def test_initial_connection_and_single_missed_poll_do_not_notify(self):
        arrival = arrival_module.Arrival()
        self.assertFalse(arrival.ready(0, True))
        self.assertFalse(arrival.ready(301, True))
        arrival.disconnected()
        self.assertFalse(arrival.ready(350, True))
        self.assertFalse(arrival.ready(700, True))

    def test_return_requires_five_minutes_and_fires_once(self):
        arrival = arrival_module.Arrival()
        arrival.disconnected()
        arrival.disconnected()
        self.assertFalse(arrival.ready(0, True))
        self.assertFalse(arrival.ready(299, True))
        self.assertTrue(arrival.ready(300, True))
        arrival.sent()
        self.assertFalse(arrival.ready(900, True))

    def test_unparked_or_offline_resets_the_return_timer(self):
        arrival = arrival_module.Arrival()
        arrival.disconnected()
        arrival.disconnected()
        arrival.ready(10, True)
        self.assertFalse(arrival.ready(200, False))
        self.assertFalse(arrival.ready(300, True))
        self.assertFalse(arrival.ready(599, True))
        self.assertTrue(arrival.ready(600, True))
        arrival.retry(600)
        self.assertFalse(arrival.ready(1199, True))
        self.assertTrue(arrival.ready(1200, True))


class ProbeErrors(unittest.TestCase):
    def test_smb_errors_keep_actionable_distinctions(self):
        self.assertIn("login", mount_problem("mount error(13): Permission denied"))
        self.assertIn("share was not found", mount_problem("mount error(2)"))
        self.assertIn("SMB 3.1.1", mount_problem("mount error(95)"))
        self.assertIn("port 445", mount_problem("mount error(110)"))

    @patch.object(dashboard_control, "send_push_test", return_value={"message": "accepted"})
    @patch.object(dashboard_control, "save_push")
    @patch.object(dashboard_control, "read_json", return_value={})
    def test_test_button_does_not_save_and_accepts_disabled_notifications(self, read, save, send):
        fields = {"enabled": "0", "device": "phone", "token": "a" * 30, "user": "b" * 30}
        self.assertEqual(dashboard_control.dispatch({"action": "test-pushover", "fields": fields}),
                         {"message": "accepted"})
        save.assert_not_called()
        send.assert_called_once_with({"enabled": False, "device": "phone", "token": "a" * 30, "user": "b" * 30})

    @patch.object(notify, "build_opener")
    def test_pushover_error_explains_rejection_without_echoing_service_body(self, opener):
        opener.return_value.open.side_effect = HTTPError(
            "https://api.pushover.net/1/messages.json", 400, "bad request", {},
            io.BytesIO(b'{"errors":["application token invalid: secret-value"]}'),
        )
        with self.assertRaisesRegex(ValueError, "^Pushover rejected the application token\\.$"):
            notify.send({"token": "test", "user": "test"}, "Test", "Test")
