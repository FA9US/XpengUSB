"""Exercise the installer embedded boot-config planner against real layouts."""

import os
from pathlib import Path
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
PROGRAM = INSTALLER.read_text(encoding="utf-8").split("<<'XPENGUSB_BOOT_CONFIG'\n", 1)[1].split("\nXPENGUSB_BOOT_CONFIG", 1)[0]
NAMESPACE = {"__name__": "xpengusb_boot_config_test"}
exec(compile(PROGRAM, str(INSTALLER) + ":boot-config", "exec"), NAMESPACE)
plan = NAMESPACE["plan_boot_config"]
configure = NAMESPACE["configure_boot"]

# Relevant unchanged lines from the official Bookworm pi-gen boot config:
# https://raw.githubusercontent.com/RPi-Distro/pi-gen/bookworm/stage1/00-boot-files/files/config.txt
BOOKWORM = """dtparam=audio=on
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
[cm4]
otg_mode=1
[cm5]
dtoverlay=dwc2,dr_mode=host
[all]
"""


class BootConfigTests(unittest.TestCase):
    def test_fresh_bookworm_ignores_cm5_host_mode_and_preserves_it(self):
        result = plan(BOOKWORM)
        self.assertTrue(result.startswith(BOOKWORM))
        self.assertIn("[cm5]\ndtoverlay=dwc2,dr_mode=host\n", result)
        self.assertIn("[all]\n[pi0w]\ndtoverlay=dwc2,dr_mode=peripheral\n[all]\n", result)
        self.assertEqual(plan(result), result)

    def test_existing_correct_active_config_is_unchanged(self):
        for section in ("", "[all]\n", "[pi0]\n", "[pi0w]\n"):
            text = BOOKWORM + section + "dtoverlay=dwc2,dr_mode=peripheral # existing\n"
            with self.subTest(section=section):
                self.assertEqual(plan(text), text)

    def test_inactive_correct_config_does_not_satisfy_zero_w(self):
        text = "[cm5]\ndtoverlay=dwc2,dr_mode=peripheral\n"
        self.assertIn("[pi0w]\ndtoverlay=dwc2,dr_mode=peripheral", plan(text))

    def test_active_wrong_or_missing_mode_refuses_change(self):
        for section in ("", "[all]\n", "[pi0]\n", "[pi0w]\n"):
            for overlay in ("dwc2", "dwc2,dr_mode=host", "dwc2,dr_mode=otg", "dwc2,dr_mode=peripheral,dr_mode=host"):
                with self.subTest(section=section, overlay=overlay), self.assertRaisesRegex(ValueError, "Active Zero W"):
                    plan(BOOKWORM + section + "dtoverlay=" + overlay + "\n")

    def test_multiple_active_overlays_refused(self):
        with self.assertRaisesRegex(ValueError, "Multiple active"):
            plan("dtoverlay=dwc2,dr_mode=peripheral\n[pi0w]\ndtoverlay=dwc2,dr_mode=peripheral\n")

    def test_parameter_override_after_correct_overlay_refused(self):
        for parameter in ("dr_mode=host", "dr_mode=otg", "foo=1, dr_mode = host", "dr_mode"):
            with self.subTest(parameter=parameter), self.assertRaisesRegex(ValueError, "dtparam=dr_mode"):
                plan("[pi0w]\ndtoverlay=dwc2,dr_mode=peripheral\ndtparam=" + parameter + "\n")
        inactive = BOOKWORM.replace("[all]\n", "dtparam=dr_mode=host\n[all]\n")
        self.assertTrue(plan(inactive).startswith(inactive))

    def test_uncertain_filter_and_include_refused(self):
        for text in ("[gpio4=1]\ndtoverlay=dwc2,dr_mode=host\n",
                     "[gpio4=1]\n[pi0w]\ndtoverlay=dwc2,dr_mode=peripheral\n",
                     "include custom.txt\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "manual review"):
                plan(text)

    def test_all_resets_unknown_filter(self):
        text = "[gpio4=1]\ndtparam=i2c_arm=on\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n"
        self.assertEqual(plan(text), text)

    @unittest.skipUnless(os.name == "posix", "Durable boot-file replacement is a Linux installation operation")
    def test_atomic_install_preserves_backup_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path, backup = Path(directory) / "config.txt", Path(directory) / "before.txt"
            path.write_text(BOOKWORM)
            configure(path, backup)
            first = path.read_bytes()
            self.assertEqual(backup.read_text(), BOOKWORM)
            configure(path, backup)
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(backup.read_text(), BOOKWORM)

    @unittest.skipUnless(os.name == "posix", "Durable boot-file replacement is a Linux installation operation")
    def test_wrong_mode_changes_neither_file_nor_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path, backup = Path(directory) / "config.txt", Path(directory) / "before.txt"
            original = BOOKWORM + "[pi0w]\ndtoverlay=dwc2,dr_mode=host\n"
            path.write_text(original)
            with self.assertRaises(ValueError):
                configure(path, backup)
            self.assertEqual(path.read_text(), original)
            self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
