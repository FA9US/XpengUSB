"""Offline setup boundary tests; never format or mount storage."""

import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid


SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure", SOURCE / "configure.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class ConfigurationTests(unittest.TestCase):
    def test_nas_accepts_hostname_ipv4_and_share_spaces(self):
        self.assertEqual(configure.nas_source("nas.local", "dashcam"), "//nas.local/dashcam")
        self.assertEqual(configure.nas_source("192.0.2.5", "camera archive"), "//192.0.2.5/camera archive")

    def test_nas_rejects_argument_and_path_injection(self):
        for server in ("nas,guest", "nas/new", "nas\nother", "$(whoami)", "nas;true", "-o", "nas'", ""):
            with self.subTest(server=server), self.assertRaises(ValueError):
                configure.nas_source(server, "dashcam")
        for share in ("dashcam,guest", "dashcam/sub", "../other", "dashcam\r\n", "dashcam\\other", "x'", "", "..", "trailing "):
            with self.subTest(share=share), self.assertRaises(ValueError):
                configure.nas_source("nas.local", share)

    def test_credentials_preserve_special_characters_but_reject_new_lines(self):
        # Synthetic fixture, not an actual credential.
        fixture = "spaces ; $ = , ' \" allowed"
        self.assertEqual(configure.validate_credential(fixture, "Password"), fixture)
        for value in ("", "secret\nusername=other", "a\rb", "a\0b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configure.validate_credential(value, "Password")
        self.assertEqual(configure.validate_credential("", "Domain", True), "")

    def test_fixed_paths_retention_and_unique_device_id(self):
        first = configure.default_config()
        second = configure.default_config()
        self.assertEqual(first["retention_days"], 7)
        self.assertEqual(first["interval_seconds"], 300)
        self.assertEqual(first["state_dir"], first["storage_dir"] + "/state")
        self.assertEqual(first["spool_dir"], first["storage_dir"] + "/spool")
        self.assertNotEqual(first["archive_device_id"], second["archive_device_id"])
        self.assertEqual(str(uuid.UUID(first["archive_device_id"])), first["archive_device_id"])
        example = json.loads((SOURCE / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(set(first), set(example))
        self.assertNotIn("password", first)

    def test_reconfiguration_preserves_identity_and_nas(self):
        existing = configure.default_config()
        existing["nas_source"] = "//other.local/camera archive"
        updated = configure.default_config(existing)
        self.assertEqual(updated["archive_device_id"], existing["archive_device_id"])
        self.assertEqual(updated["nas_source"], existing["nas_source"])

    def test_reconfiguration_rejects_storage_redirection(self):
        for key in configure.FIXED_PATHS:
            existing = configure.default_config()
            existing[key] = "/tmp/unrelated"
            with self.subTest(key=key), self.assertRaises(ValueError):
                configure.default_config(existing)

    def test_nonroot_fails_before_service_or_filesystem_changes(self):
        with patch.object(configure.os, "geteuid", create=True, return_value=1000), patch.object(configure.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "sudo"):
                configure.configure()
            run.assert_not_called()

    def test_running_service_fails_before_credentials_prompt(self):
        with patch.object(configure.os, "geteuid", create=True, return_value=0), patch.object(configure.subprocess, "run") as run, patch("builtins.input") as prompt:
            run.return_value.returncode = 0
            with self.assertRaisesRegex(ValueError, "stop both"):
                configure.configure()
            prompt.assert_not_called()


@unittest.skipUnless(os.name == "posix", "POSIX file permissions and directory fsync require Linux")
class PrivateFileTests(unittest.TestCase):
    def test_private_atomic_write_and_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials"
            configure.atomic_private_write(path, "synthetic secret\n")
            self.assertEqual(path.read_text(), "synthetic secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            configure.atomic_private_write(path, "replacement\n")
            self.assertEqual(path.read_text(), "replacement\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_replace_preserves_previous_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials"
            configure.atomic_private_write(path, "previous\n")
            with patch.object(configure.os, "replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    configure.atomic_private_write(path, "replacement\n")
            self.assertEqual(path.read_text(), "previous\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_refuses_symlink_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing"
            target.write_text("keep\n")
            link = Path(directory) / "credentials"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                configure.atomic_private_write(link, "replacement")
            self.assertEqual(target.read_text(), "keep\n")

    def test_refuses_foreign_owned_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials"
            path.write_text("keep\n")
            with patch.object(configure.os, "geteuid", return_value=path.stat().st_uid + 1):
                with self.assertRaisesRegex(ValueError, "another user"):
                    configure.atomic_private_write(path, "replacement")
            self.assertEqual(path.read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main()
