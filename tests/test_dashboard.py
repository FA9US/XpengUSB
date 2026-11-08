"""Exercise the dashboard's access boundary and failure recovery."""
import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from configure import default_config
from xpengusb import dashboard, dashboard_control as control
from xpengusb.runtime import Config, RuntimeSafetyError


class DashboardRequests(unittest.TestCase):
    def setUp(self):
        self.server = dashboard.HTTPServer(("127.0.0.1", 0), dashboard.Dashboard)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        auth = patch.object(dashboard, "AUTH", b"Basic test")
        auth.start()
        self.addCleanup(auth.stop)

    def request(self, method, path, body=None, auth=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if auth:
            headers["Authorization"] = "Basic test"
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        response.read()
        return response.status

    @patch.object(dashboard, "broker")
    def test_anonymous_requests_cannot_read_or_write(self, broker):
        self.assertEqual(self.request("GET", "/", auth=False), 401)
        self.assertEqual(self.request("POST", "/retry", "csrf=" + dashboard.CSRF, auth=False), 401)
        broker.assert_not_called()

    @patch.object(dashboard, "broker")
    def test_authenticated_post_requires_csrf_and_rejects_duplicates(self, broker):
        self.assertEqual(self.request("POST", "/retry", "csrf=bad"), 403)
        self.assertEqual(self.request("POST", "/retry", "csrf=bad&csrf=" + dashboard.CSRF), 400)
        broker.assert_not_called()

    @patch.object(dashboard, "broker")
    def test_valid_post_dispatches_only_the_requested_action(self, broker):
        def respond(handler, message):
            handler.respond(200, "saved")
        with patch.object(dashboard.Dashboard, "show", respond):
            self.assertEqual(self.request("POST", "/retry", "csrf=" + dashboard.CSRF), 200)
        broker.assert_called_once_with("retry", {})


class WifiFallback(unittest.TestCase):
    def setup_tick(self, current, *, home=control.HOME, action="", peers=""):
        settings = {"home": home, "home_ssid": "Home", "ap_ssid": "XpengUSB",
                    "action": action, "last_retry": 0}
        mocks = [
            patch.object(control, "read_json", side_effect=lambda path, default=None:
                         dict(settings) if path == control.WIFI else {}),
            patch.object(control, "write_json"),
            patch.object(control, "active_wifi", side_effect=current),
            patch.object(control, "wifi_ssid", return_value="Network"),
            patch.object(control, "nm"),
            patch.object(control, "run"),
            patch.object(control.time, "time", return_value=1000),
        ]
        values = []
        for mock in mocks:
            values.append(mock.start())
            self.addCleanup(mock.stop)
        values[5].return_value.returncode = 0
        values[5].return_value.stdout = peers
        return values[4]

    def test_disconnected_pi_starts_the_hotspot(self):
        nm = self.setup_tick(["", control.AP])
        self.assertFalse(control.network_tick())
        nm.assert_called_once_with("connection", "up", "uuid", control.AP,
                                   "ifname", control.IFACE, check=False, wait=20)

    def test_keeps_hotspot_while_a_phone_is_connected(self):
        nm = self.setup_tick([control.AP], peers="Station ab:cd:ef:01:23:45")
        self.assertFalse(control.network_tick())
        nm.assert_not_called()

    def test_failed_home_retry_restores_hotspot(self):
        nm = self.setup_tick([control.AP, "", control.AP])
        self.assertFalse(control.network_tick())
        self.assertEqual([call.args[3] for call in nm.call_args_list], [control.HOME, control.AP])

    def test_valid_home_connection_takes_over_from_idle_hotspot(self):
        nm = self.setup_tick([control.AP, control.HOME])
        self.assertTrue(control.network_tick())
        self.assertEqual(nm.call_args.args[3], control.HOME)

    def test_keyfile_inputs_cannot_add_sections_or_options(self):
        for ssid, password in (("home\n[ipv4]", "longpass"), ("home", "pass\nword"),
                               ("1;2;", "longpass")):
            with self.assertRaises(ValueError):
                control.profile_text(ssid, password, False)
        profile = control.profile_text("Home WiFi", "key with spaces", False)
        self.assertIn("ssid=Home\\sWiFi\n", profile)
        self.assertIn("psk=key\\swith\\sspaces\n", profile)


class NasSettings(unittest.TestCase):
    def config(self, root):
        storage = root / "storage"
        return dict(default_config(), storage_dir=str(storage), camera_image=str(storage / "cam.img"),
                    state_dir=str(storage / "state"), spool_dir=str(storage / "spool"),
                    snapshot_dir=str(storage / "snapshots"), nas_mount=str(root / "nas"),
                    credentials_file=str(root / "smb.credentials"))

    def test_share_subfolder_does_not_allow_mount_escape(self):
        config = self.config(Path.cwd())
        self.assertEqual(Config.from_dict(dict(config, nas_directory="cars/dashcam")).archive_root.parent.name,
                         "XpengUSB")
        for path in ("../other", "/outside", "folder//other", "a/..", "a\\b"):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeSafetyError, "relative NAS subfolder"):
                Config.from_dict(dict(config, nas_directory=path))

    def test_nas_host_change_does_not_send_previous_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            with patch.object(control, "CONFIG_PATH", config_path), \
                    patch.object(control, "SMB_CREDENTIAL_FILES",
                                 (root / "smb.credentials", root / "smb.next.credentials")), \
                    patch.object(control, "read_json", return_value=self.config(root)), \
                    patch.object(control, "nas_credentials", return_value={"username": "xpeng", "password": "old"}), \
                    patch.object(control, "run") as run:
                with self.assertRaisesRegex(ValueError, "Enter the SMB password"):
                    control.set_nas(dict(server="other.local", share="dashcam", directory="",
                                         username="xpeng", password="", domain=""))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
