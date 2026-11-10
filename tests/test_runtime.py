"""Controller failure tests; no mounts, device operations, network or root needed."""

import json
import contextlib
import errno
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from xpengusb.runtime import (Config, GIB, Mount, RuntimeSafetyError, Worker,
                              archive_delay, main, parse_mountinfo)

# Scale storage units in controller tests: these are fixture files, not disk
# performance or real sparse-file tests. Windows truncate can allocate zeros.
GIB = 1024


class FakeStore:
    def __init__(self):
        self.calls = []
        self.ingest_error = False

    def ingest(self, path, **kwargs):
        self.calls.append(("ingest", path, kwargs))
        if self.ingest_error:
            raise RuntimeError("simulated full spool")
        return {"ingested": 1, "pending": 0}

    def archive_to(self, path, **kwargs):
        self.calls.append(("archive", path, kwargs))
        return {"archived": 1}

    def prune(self, path, **kwargs):
        self.calls.append(("prune", path, kwargs))
        return {"pruned": 0}


class FakeRunner:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.mounts = [Mount(config.storage_dir, "/dev/loop0", "btrfs", frozenset({"rw"}))]
        self.loops = {}
        self.fail = set()
        self.clock = "yes"
        self.fsck_code = 0
        self.verify_code = 0
        self.filesystem = "btrfs"

    def run(self, args, *, timeout=120, check=True):
        self.calls.append(args)
        if args[0] in self.fail:
            raise RuntimeSafetyError("simulated " + args[0] + " failure")
        stdout = ""
        code = 0
        if args[0] == "timedatectl":
            stdout = self.clock
        elif args[0] == "findmnt":
            stdout = self.filesystem
        elif args[0] == "cp":
            with open(args[-1], "wb") as stream:
                stream.truncate(Path(args[-2]).stat().st_size)
        elif args[:2] == ["losetup", "--json"]:
            stdout = json.dumps({"loopdevices": [{"name": name, "back-file": path}
                                                 for name, path in self.loops.items()]})
        elif args[:2] == ["losetup", "--find"]:
            self.loops["/dev/loop7"] = args[-1]
            stdout = "/dev/loop7\n"
        elif args[:2] == ["losetup", "--detach"]:
            if "detach" in self.fail:
                raise RuntimeSafetyError("simulated detach failure")
            del self.loops[args[-1]]
        elif args[0] == "fsck.fat":
            code = self.verify_code if args[1] == "-n" else self.fsck_code
        elif args[0] == "udevadm":
            pass
        elif args[:3] == ["mount", "-t", "vfat"]:
            self.mounts.append(Mount(Path(args[-1]), args[-2], "vfat",
                                     frozenset({"ro", "nosuid", "nodev", "noexec"})))
        elif args[:3] == ["mount", "-t", "cifs"]:
            self.mounts.append(Mount(Path(args[4]), args[3], "cifs", frozenset({"rw"})))
        elif args[0] == "umount":
            self.mounts[:] = [m for m in self.mounts if str(m.target) != args[-1]]
        else:
            raise AssertionError("Unexpected command: " + repr(args))
        return subprocess.CompletedProcess(args, code, stdout, "")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        units = patch("xpengusb.runtime.GIB", GIB)
        units.start()
        self.addCleanup(units.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        storage = self.root / "storage"
        storage.mkdir()
        config_dict = {
            "storage_dir": str(storage), "state_dir": str(storage / "state"),
            "spool_dir": str(storage / "spool"), "snapshot_dir": str(storage / "snapshots"),
            "camera_image": str(storage / "cam.img"), "nas_mount": str(self.root / "nas"),
            "nas_source": "//nas.local/dashcam", "credentials_file": str(self.root / "smb.credentials"),
            "archive_device_id": str(uuid.uuid4()), "retention_days": 7, "interval_seconds": 300,
        }
        self.config_dict = config_dict
        self.config = Config.from_dict(config_dict)
        for path in (self.config.state_dir, self.config.spool_dir,
                     self.config.snapshot_dir, self.config.nas_mount):
            path.mkdir()
        with self.config.camera_image.open("wb") as stream:
            stream.truncate(32 * GIB)
        self.config.credentials_file.write_text("username=test\npassword=fixture-only\n")
        self.config.credentials_file.chmod(0o600)
        self.store = FakeStore()
        self.runner = FakeRunner(self.config)
        self.worker = Worker(self.config, self.store, runner=self.runner,
                             mounts=lambda: self.runner.mounts,
                             free_bytes=lambda path: 80 * GIB,
                             now=lambda: 1800000000)

    def tearDown(self):
        self.temp.cleanup()

    def add_nas(self, source=None):
        self.runner.mounts.append(Mount(self.config.nas_mount,
                                       source or self.config.nas_source, "cifs", frozenset({"rw"})))

    def existing_snapshot(self):
        work = self.config.snapshot_dir / uuid.uuid4().hex
        work.mkdir()
        (work / "mount").mkdir()
        with (work / "cam.img").open("wb") as stream:
            stream.truncate(32 * GIB)
        return work

    def test_only_private_snapshot_is_repaired_and_mounted(self):
        self.worker.capture()
        self.assertEqual([], list(self.config.snapshot_dir.iterdir()))
        self.assertEqual({}, self.runner.loops)
        self.assertEqual(["btrfs"], [mount.fstype for mount in self.runner.mounts])
        commands = self.runner.calls
        self.assertEqual(1, len([cmd for cmd in commands if cmd[0] == "cp"]))
        for command in commands:
            if str(self.config.camera_image) in command:
                self.assertEqual("cp", command[0])
            self.assertNotIn("disable_gadget", " ".join(command))
        self.assertIn(["fsck.fat", "-a", "/dev/loop7p1"], commands)
        ingest = [call for call in self.store.calls if call[0] == "ingest"][0]
        self.assertEqual(40 * GIB, ingest[2]["min_free_bytes"])

    def test_ingest_failure_releases_snapshot_and_keeps_spool(self):
        retained = self.config.spool_dir / "retained.mp4"
        retained.write_bytes(b"existing recording")
        self.store.ingest_error = True
        with self.assertRaisesRegex(RuntimeError, "full spool"):
            self.worker.capture()
        self.assertEqual(b"existing recording", retained.read_bytes())
        self.assertEqual([], list(self.config.snapshot_dir.iterdir()))

    def test_unmount_failure_preserves_snapshot_and_loop(self):
        self.runner.fail.add("umount")
        with self.assertRaisesRegex(RuntimeSafetyError, "umount"):
            self.worker.capture()
        self.assertEqual(1, len(list(self.config.snapshot_dir.glob("*/cam.img"))))
        self.assertTrue(self.runner.loops)
        self.assertFalse(any(cmd[:2] == ["losetup", "--detach"] for cmd in self.runner.calls))

    def test_detach_failure_preserves_snapshot(self):
        self.runner.fail.add("detach")
        with self.assertRaisesRegex(RuntimeSafetyError, "detach"):
            self.worker.capture()
        self.assertEqual(1, len(list(self.config.snapshot_dir.glob("*/cam.img"))))
        self.assertEqual(["btrfs"], [mount.fstype for mount in self.runner.mounts])

    def test_fsck_failure_never_mounts_bad_image(self):
        self.runner.fsck_code = 8
        with self.assertRaisesRegex(RuntimeSafetyError, "repair failed"):
            self.worker.capture()
        self.assertFalse(any(cmd[0] == "mount" for cmd in self.runner.calls))
        self.assertEqual([], list(self.config.snapshot_dir.iterdir()))

    def test_repair_exit_one_is_accepted(self):
        self.runner.fsck_code = 1
        self.worker.capture()
        self.assertEqual("ingest", self.store.calls[0][0])

    def test_repair_must_be_followed_by_clean_check(self):
        self.runner.fsck_code = 1
        self.runner.verify_code = 1
        with self.assertRaisesRegex(RuntimeSafetyError, "remains inconsistent"):
            self.worker.capture()
        self.assertFalse(any(cmd[0] == "mount" for cmd in self.runner.calls))
        self.assertEqual([], self.store.calls)

    def test_incomplete_unmounted_snapshot_is_replaced_safely(self):
        work = self.config.snapshot_dir / uuid.uuid4().hex
        work.mkdir()
        (work / "mount").mkdir()
        (work / "cam.img.part").write_bytes(b"incomplete")
        self.worker.capture()
        self.assertFalse(work.exists())
        self.assertEqual(1, len([cmd for cmd in self.runner.calls if cmd[0] == "cp"]))

    def test_empty_workspace_left_by_cleanup_is_recovered(self):
        work = self.config.snapshot_dir / uuid.uuid4().hex
        work.mkdir()
        self.worker.capture()
        self.assertFalse(work.exists())
        self.assertEqual(1, len([cmd for cmd in self.runner.calls if cmd[0] == "cp"]))

    def test_unmounted_storage_never_captures(self):
        self.runner.mounts.clear()
        with self.assertRaisesRegex(RuntimeSafetyError, "mounted writable"):
            self.worker.capture()
        self.assertEqual([], self.store.calls)

    def test_recovers_existing_mounted_snapshot_without_second_snapshot(self):
        work = self.existing_snapshot()
        self.runner.loops["/dev/loop7"] = str(work / "cam.img")
        self.runner.mounts.append(Mount(work / "mount", "/dev/loop7p1", "vfat",
                                       frozenset({"ro", "nodev", "nosuid", "noexec"})))
        self.worker.capture()
        self.assertFalse(any(cmd[0] in {"cp", "fsck.fat", "mount"} for cmd in self.runner.calls))
        self.assertEqual(work.name, self.store.calls[0][2]["snapshot_id"])
        self.assertFalse(work.exists())

    def test_unknown_snapshot_content_is_preserved(self):
        work = self.existing_snapshot()
        (work / "unexpected.txt").write_text("preserve")
        with self.assertRaisesRegex(RuntimeSafetyError, "Unexpected file"):
            self.worker.capture()
        self.assertTrue((work / "cam.img").exists())
        self.assertFalse(any(cmd[0] == "cp" for cmd in self.runner.calls))

    def test_low_space_does_not_block_nas_copy_and_retention(self):
        self.add_nas()
        self.worker.free_bytes = lambda path: 39 * GIB
        result = self.worker.cycle()
        self.assertEqual("completed", result["archive"])
        self.assertIn("Low storage", result["capture"])
        self.assertEqual(["archive", "prune"], [call[0] for call in self.store.calls])
        self.assertEqual(2, self.store.calls[0][2]["max_files"])
        self.assertEqual(2, self.store.calls[1][2]["max_files"])
        self.assertFalse(any(cmd[0] == "cp" for cmd in self.runner.calls))

    def test_backlog_is_batched_after_capture_once_per_cycle(self):
        self.add_nas()
        backlog = {"pending": 20, "expired": 20}
        original_archive, original_prune = self.store.archive_to, self.store.prune

        def transfer(*args, **kwargs):
            amount = min(backlog["pending"], kwargs["max_files"])
            backlog["pending"] -= amount
            original_archive(*args, **kwargs)
            return {"archived": amount}

        def expire(*args, **kwargs):
            amount = min(backlog["expired"], kwargs["max_files"])
            backlog["expired"] -= amount
            original_prune(*args, **kwargs)
            return {"pruned": amount}

        self.store.archive_to, self.store.prune = transfer, expire
        for expected_remaining in (18, 16):
            result = self.worker.cycle()
            self.assertEqual("completed", result["capture"])
            self.assertEqual("completed", result["archive"])
            self.assertEqual(expected_remaining, backlog["pending"])
            self.assertEqual(expected_remaining, backlog["expired"])
        self.assertEqual(["ingest", "archive", "prune"] * 2,
                         [call[0] for call in self.store.calls])

    def test_upload_only_cycle_reports_attempt_without_rescanning_camera(self):
        self.add_nas()
        progress = []
        result = self.worker.cycle(capture_due=False, report=progress.append)
        self.assertEqual(["archive", "prune"], [call[0] for call in self.store.calls])
        self.assertNotIn("capture", result)
        self.assertEqual("uploading", progress[0]["phase"])
        self.assertEqual(progress[0]["archive_attempt"], result["archive_attempt"])
        self.assertEqual("idle", result["phase"])
        self.assertEqual((2, 0), archive_delay(result, pending=18, failures=2, interval=300))

    def test_nas_transport_failure_retries_with_new_mount_without_touching_snapshot(self):
        self.add_nas()
        failed = OSError(errno.EHOSTDOWN, "NAS no longer responds")
        with patch.object(self.store, "archive_to", side_effect=failed):
            result = self.worker.cycle(capture_due=False)
        self.assertIn("retry scheduled", result["archive"])
        self.assertEqual((15, 1), archive_delay(result, pending=18, failures=0, interval=300))
        self.assertEqual((120, 9), archive_delay(result, pending=18, failures=8, interval=300))
        original_stat = Path.stat

        def root_credentials(path, *args, **kwargs):
            if path == self.config.credentials_file:
                return SimpleNamespace(st_uid=0, st_mode=0o100600)
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", root_credentials):
            retried = self.worker.cycle(capture_due=False)
        self.assertEqual("completed", retried["archive"])
        self.assertFalse(self.worker.nas_reconnect_needed)
        self.assertIn(["umount", "--", str(self.config.nas_mount)], self.runner.calls)
        mount = next(cmd for cmd in self.runner.calls if cmd[:3] == ["mount", "-t", "cifs"])
        self.assertIn("soft,echo_interval=10", mount[-1])
        self.assertFalse(any(cmd[0] == "cp" for cmd in self.runner.calls))

    def test_reconnect_never_unmounts_an_unexpected_share(self):
        self.add_nas("//other/share")
        self.worker.nas_reconnect_needed = True
        result = self.worker.cycle(capture_due=False)
        self.assertIn("configured writable CIFS", result["archive"])
        self.assertFalse(any(cmd[0] == "umount" for cmd in self.runner.calls))

    def test_unsynchronized_clock_still_ingests_but_never_archives_or_prunes(self):
        self.runner.clock = "no"
        self.add_nas()
        result = self.worker.cycle()
        self.assertEqual("completed", result["capture"])
        self.assertEqual("waiting for synchronized clock", result["archive"])
        self.assertEqual(["ingest"], [call[0] for call in self.store.calls])

    def test_wrong_nas_source_never_writes_but_still_captures(self):
        self.add_nas("//other/share")
        result = self.worker.cycle()
        self.assertIn("configured writable CIFS", result["archive"])
        self.assertEqual("completed", result["capture"])
        self.assertEqual(["ingest"], [call[0] for call in self.store.calls])

    def test_prune_clock_is_sampled_after_long_transfer(self):
        self.add_nas()
        current = [1800000000]
        self.worker.now = lambda: current[0]
        original_archive = self.store.archive_to

        def long_archive(*args, **kwargs):
            current[0] += 7200
            return original_archive(*args, **kwargs)

        self.store.archive_to = long_archive
        self.worker.archive(1800000000)
        self.assertNotIn("now", self.store.calls[0][2])
        self.assertEqual(1800007200, self.store.calls[1][2]["now"])

    def test_lost_clock_sync_after_transfer_prevents_prune(self):
        self.add_nas()
        original_archive = self.store.archive_to

        def archive_then_lose_clock(*args, **kwargs):
            self.runner.clock = "no"
            return original_archive(*args, **kwargs)

        self.store.archive_to = archive_then_lose_clock
        self.worker.archive(1800000000)
        self.assertEqual(["archive"], [call[0] for call in self.store.calls])

    def test_existing_nas_directory_without_mount_is_never_used_as_archive(self):
        (self.config.nas_mount / "local-file").write_text("keep")
        with self.assertRaisesRegex(RuntimeSafetyError, "hide files"):
            self.worker.ensure_nas()
        self.assertEqual([], self.store.calls)

    def test_non_btrfs_storage_never_snapshots(self):
        self.runner.filesystem = "ext4"
        with self.assertRaisesRegex(RuntimeSafetyError, "must be Btrfs"):
            self.worker.capture()
        self.assertFalse(any(cmd[0] == "cp" for cmd in self.runner.calls))

    def test_cp_failure_removes_only_new_temporary_workspace(self):
        self.runner.fail.add("cp")
        with self.assertRaisesRegex(RuntimeSafetyError, "cp failure"):
            self.worker.capture()
        self.assertTrue(self.config.camera_image.exists())
        self.assertEqual([], list(self.config.snapshot_dir.iterdir()))

    def test_unsafe_configuration_is_rejected(self):
        for patch in ({"spool_dir": self.config_dict["state_dir"]},
                      {"camera_image": str(self.root / "other.img")},
                      {"nas_source": "//nas.local/dashcam,guest"},
                      {"nas_mount": str(self.root)},
                      {"retention_days": 0}, {"interval_seconds": 0}):
            with self.subTest(patch=patch), self.assertRaises(RuntimeSafetyError):
                Config.from_dict(dict(self.config_dict, **patch))

    def test_mountinfo_decodes_escaped_paths_and_options(self):
        mounts = parse_mountinfo("7 1 0:9 / /mnt/some\\040space rw,nosuid,nodev,noexec - cifs //nas.local/dashcam rw\n")
        self.assertEqual(Path("/mnt/some space"), mounts[0].target)
        self.assertEqual("//nas.local/dashcam", mounts[0].source)
        self.assertIn("noexec", mounts[0].options)

    def test_status_is_read_only_and_does_not_need_worker_lock(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config_dict))
        with patch("xpengusb.runtime.os.geteuid", return_value=0, create=True), \
                patch("xpengusb.runtime.Worker._check_storage"), \
                patch("xpengusb.runtime.worker_lock", side_effect=AssertionError("must not lock")), \
                patch("xpengusb.archive.ArchiveStore.__init__", side_effect=AssertionError("must not initialize")), \
                patch("xpengusb.archive.ArchiveStore.read_status", return_value={"retained": 3}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--config", str(config_path), "--status"])
        self.assertEqual(0, code)
        self.assertEqual({"retained": 3}, json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
