"""Filesystem fixtures exercise failure and retention boundaries, not a car."""

import hashlib
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from xpengusb import archive


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mp4(payload=b"recording-one"):
    return box(b"ftyp", b"isom0000") + box(b"moov", b"metadata") + box(b"mdat", payload)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.nas = self.root / "nas"
        self.nas.mkdir()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def write(self, relative="XP_DCIM/nested/clip.mp4", content=None):
        path = self.snapshot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(mp4() if content is None else content)
        return path

    def capture_twice(self, first="1", second="2"):
        self.store.ingest(self.snapshot, snapshot_id=first)
        return self.store.ingest(self.snapshot, snapshot_id=second)

    def rows(self):
        return self.store.db.execute("SELECT * FROM clips ORDER BY id").fetchall()

    def final(self, row):
        return self.nas.joinpath(*archive.archive_relative_path(row["relative"], row["digest"]).parts)

    def test_two_distinct_stable_snapshots_required(self):
        self.write()
        first = self.store.ingest(self.snapshot, snapshot_id="a")
        self.assertEqual(first["ingested"], 0)
        self.assertEqual(first["pending"], 1)
        replay = self.store.ingest(self.snapshot, snapshot_id="a")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.status()["total"], 0)
        self.assertEqual(self.store.ingest(self.snapshot, snapshot_id="b")["ingested"], 1)

    def test_reused_snapshot_id_with_changed_content_rejected(self):
        self.write()
        self.store.ingest(self.snapshot, snapshot_id="a")
        self.write(content=mp4(b"different"))
        with self.assertRaises(archive.IntegrityError):
            self.store.ingest(self.snapshot, snapshot_id="a")
        self.assertEqual(self.store.status()["total"], 0)

    def test_same_size_and_timestamp_rollover_keeps_both_versions(self):
        path = self.write(content=mp4(b"AAAA"))
        timestamp = path.stat().st_mtime_ns
        self.capture_twice()
        self.store.archive_to(self.nas, now=100)
        path.write_bytes(mp4(b"BBBB"))
        os.utime(path, ns=(timestamp, timestamp))
        self.capture_twice("3", "4")
        self.store.archive_to(self.nas, now=200)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(self.final(rows[0]), self.final(rows[1]))
        self.assertEqual(self.final(rows[0]).read_bytes(), mp4(b"AAAA"))
        self.assertEqual(self.final(rows[1]).read_bytes(), mp4(b"BBBB"))

    def test_disappearance_resets_stability(self):
        path = self.write()
        self.store.ingest(self.snapshot, snapshot_id="1")
        path.unlink()
        self.store.ingest(self.snapshot, snapshot_id="2")
        self.write()
        self.assertEqual(self.store.ingest(self.snapshot, snapshot_id="3")["ingested"], 0)
        self.assertEqual(self.store.ingest(self.snapshot, snapshot_id="4")["ingested"], 1)

    def test_incomplete_mp4_not_ingested_but_sidecar_is(self):
        self.write(content=box(b"ftyp", b"isom0000") + box(b"mdat", b"video"))
        self.write("XP_EMER_DCIM/event/config.json", b'{"event":true}')
        result = self.capture_twice()
        self.assertEqual(result["incomplete"], 1)
        self.assertEqual(result["ingested"], 1)
        self.assertTrue(self.rows()[0]["relative"].endswith("config.json"))

    def test_outer_box_truncation_rejected(self):
        path = self.write(content=mp4()[:-1])
        self.assertFalse(archive.mp4_has_complete_boxes(path))
        path.write_bytes(mp4() + b"truncated")
        self.assertFalse(archive.mp4_has_complete_boxes(path))

    def test_size_zero_and_extended_box_supported(self):
        path = self.write(content=(box(b"ftyp", b"isom0000")
                                  + struct.pack(">I4sQ", 1, b"moov", 20) + b"meta"
                                  + struct.pack(">I4s", 0, b"mdat") + b"video"))
        self.assertTrue(archive.mp4_has_complete_boxes(path))

    def test_retention_exact_boundary_and_no_source_or_nas_deletion(self):
        source = self.write()
        original = source.read_bytes()
        self.capture_twice()
        self.store.archive_to(self.nas, now=1000)
        self.assertEqual(self.store.prune(self.nas, now=1000 + 7 * archive.DAY - 1)["pruned"], 0)
        self.assertEqual(self.store.prune(self.nas, now=1000 + 7 * archive.DAY)["pruned"], 1)
        row = self.rows()[0]
        self.assertFalse(self.store._spool_path(row["spool_key"]).exists())
        self.assertEqual(self.final(row).read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)

    def test_never_copied_is_never_pruned(self):
        self.write()
        self.capture_twice()
        self.assertEqual(self.store.prune(self.nas, now=10**9)["pruned"], 0)
        self.assertEqual(self.store.status()["retained"], 1)

    def test_repeat_copy_does_not_extend_first_success(self):
        self.write()
        self.capture_twice()
        self.store.archive_to(self.nas, now=100)
        self.store.archive_to(self.nas, now=200)
        self.assertEqual(self.rows()[0]["verified_at"], 100)

    def test_clock_rollback_refuses_pruning(self):
        self.write()
        self.capture_twice()
        self.store.archive_to(self.nas, now=1000)
        with self.assertRaises(archive.ArchiveError):
            self.store.prune(self.nas, now=999)
        self.assertEqual(self.store.status()["retained"], 1)

    def test_corrupt_nas_blocks_pruning_and_is_not_overwritten(self):
        self.write()
        self.capture_twice()
        self.store.archive_to(self.nas, now=1000)
        final = self.final(self.rows()[0])
        final.write_bytes(b"existing-different-content")
        with self.assertRaises(archive.IntegrityError):
            self.store.prune(self.nas, now=1000 + 7 * archive.DAY)
        # A completed archive is not reread on every Wi-Fi cycle. The expiry
        # check above is the required readback before local deletion.
        self.assertEqual(self.store.archive_to(self.nas, now=1001)["already_verified"], 1)
        self.assertEqual(final.read_bytes(), b"existing-different-content")
        self.assertEqual(self.store.status()["retained"], 1)

    def test_missing_nas_file_preserves_local_copy(self):
        self.write()
        self.capture_twice()
        self.store.archive_to(self.nas, now=1000)
        self.final(self.rows()[0]).unlink()
        with self.assertRaises(OSError):
            self.store.prune(self.nas, now=1000 + 7 * archive.DAY)
        self.assertEqual(self.store.status()["retained"], 1)

    def test_interrupted_publication_has_no_success_or_final(self):
        self.write()
        self.capture_twice()
        with mock.patch.object(archive, "_publish_no_replace", side_effect=OSError("connection lost")):
            with self.assertRaises(OSError):
                self.store.archive_to(self.nas, now=100)
        self.assertIsNone(self.rows()[0]["verified_at"])
        self.assertEqual([p for p in self.nas.rglob("*") if p.is_file()], [])
        self.assertEqual(self.store.archive_to(self.nas, now=200)["archived"], 1)

    def test_existing_different_nas_content_never_overwritten(self):
        self.write()
        self.capture_twice()
        final = self.final(self.rows()[0])
        final.parent.mkdir(parents=True)
        final.write_bytes(b"different")
        with self.assertRaises(archive.IntegrityError):
            self.store.archive_to(self.nas, now=100)
        self.assertEqual(final.read_bytes(), b"different")
        self.assertIsNone(self.rows()[0]["verified_at"])

    def test_verified_archive_is_not_rehashed_each_cycle(self):
        self.write()
        self.capture_twice()
        self.store.archive_to(self.nas, now=100)
        with mock.patch.object(archive, "_digest", side_effect=AssertionError("unnecessary Wi-Fi reread")):
            self.assertEqual(self.store.archive_to(self.nas, now=200)["already_verified"], 1)

    def test_archive_batch_leaves_remaining_pending_without_recopying_verified(self):
        for name in ("a", "b", "c"):
            self.write("XP_DCIM/" + name + ".mp4")
        self.capture_twice()
        self.assertEqual(self.store.archive_to(self.nas, now=100, max_files=2)["archived"], 2)
        self.assertEqual(self.store.status()["pending"], 1)
        result = self.store.archive_to(self.nas, now=200, max_files=2)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(result["already_verified"], 2)
        self.assertEqual(self.store.status()["pending"], 0)

    def test_bad_clip_is_retried_without_blocking_other_uploads(self):
        self.write("XP_DCIM/a.mp4")
        self.write("XP_DCIM/b.mp4")
        self.capture_twice()
        bad, good = self.rows()
        final = self.final(bad)
        final.parent.mkdir(parents=True)
        final.write_bytes(b"different NAS content")
        first = self.store.archive_to(self.nas, now=100, max_files=2, isolate_errors=True)
        self.assertEqual((1, 1), (first["archived"], first["failed"]))
        self.assertTrue(self.final(good).is_file())
        self.assertEqual(b"different NAS content", final.read_bytes())
        second = self.store.archive_to(self.nas, now=101, max_files=2, isolate_errors=True)
        self.assertEqual(1, second["deferred"])
        final.unlink()
        third = self.store.archive_to(self.nas, now=first["next_retry_at"], max_files=2,
                                      isolate_errors=True)
        self.assertEqual(1, third["archived"])
        self.assertEqual(0, self.store.status()["pending"])

    def test_missing_nas_copy_does_not_block_other_expired_files(self):
        self.write("XP_DCIM/a.mp4")
        self.write("XP_DCIM/b.mp4")
        self.capture_twice()
        self.store.archive_to(self.nas, now=100)
        missing, kept_on_nas = self.rows()
        self.final(missing).unlink()
        result = self.store.prune(self.nas, now=100 + 7 * archive.DAY, max_files=2,
                                  isolate_errors=True)
        self.assertEqual((1, 1), (result["pruned"], result["failed"]))
        self.assertTrue(self.store._spool_path(missing["spool_key"]).is_file())
        self.assertTrue(self.final(kept_on_nas).is_file())

    def test_prune_batch_bounds_eligible_readbacks_and_retains_the_rest(self):
        for name in ("a", "b", "c", "d"):
            self.write("XP_DCIM/" + name + ".mp4")
        self.capture_twice()
        self.store.archive_to(self.nas, now=100, max_files=3)
        self.store.archive_to(self.nas, now=200, max_files=1)
        result = self.store.prune(self.nas, now=100 + 7 * archive.DAY, max_files=2)
        self.assertEqual(result, {"pruned": 2, "kept": 2})
        result = self.store.prune(self.nas, now=100 + 7 * archive.DAY, max_files=2)
        self.assertEqual(result, {"pruned": 1, "kept": 1})
        self.assertEqual(len(list(self.nas.rglob("*.mp4"))), 4)

    def test_later_archive_failure_preserves_previous_success_receipt(self):
        self.write("XP_DCIM/a.mp4")
        self.write("XP_DCIM/b.mp4", mp4(b"second"))
        self.capture_twice()
        original = archive._copy_verified
        copies = 0

        def fail_second(*args, **kwargs):
            nonlocal copies
            copies += 1
            if copies == 2:
                raise OSError("NAS unavailable")
            return original(*args, **kwargs)

        with mock.patch.object(archive, "_copy_verified", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.store.archive_to(self.nas, now=100, max_files=2)
        self.assertEqual(self.store.status()["archived"], 1)
        self.assertEqual(self.rows()[0]["verified_at"], 100)
        self.assertEqual(self.store.status()["pending"], 1)

    def test_later_ingest_failure_retains_first_registered_capture(self):
        self.write("XP_DCIM/a.mp4")
        self.write("XP_DCIM/b.mp4", mp4(b"second"))
        self.store.ingest(self.snapshot, snapshot_id="1")
        original = archive._copy_verified
        copies = 0

        def fail_second(*args, **kwargs):
            nonlocal copies
            copies += 1
            if copies == 2:
                raise OSError("disk disappeared")
            return original(*args, **kwargs)

        with mock.patch.object(archive, "_copy_verified", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.store.ingest(self.snapshot, snapshot_id="2")
        self.assertEqual(self.store.status()["total"], 1)
        (self.snapshot / "XP_DCIM/a.mp4").unlink()
        self.assertEqual(self.store.archive_to(self.nas, now=100)["archived"], 1)

    def test_published_spool_recovers_after_crash_before_database_insert(self):
        source = self.write()
        self.store.ingest(self.snapshot, snapshot_id="1")
        original = archive._publish_no_replace

        def publish_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated sudden process exit")

        with mock.patch.object(archive, "_publish_no_replace", side_effect=publish_then_crash):
            with self.assertRaises(RuntimeError):
                self.store.ingest(self.snapshot, snapshot_id="2")
        self.assertEqual(self.store.status()["total"], 0)
        source.unlink()
        self.store.close()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool")
        self.assertEqual(self.store.status()["total"], 1)
        self.assertEqual(self.store.archive_to(self.nas, now=100)["archived"], 1)

    def test_readback_mismatch_cannot_grant_success(self):
        self.write()
        self.capture_twice()
        original = archive._copy_verified

        def damage_after_copy(source, destination, *args, **kwargs):
            original(source, destination, *args, **kwargs)
            destination.write_bytes(b"damaged-on-NAS")

        with mock.patch.object(archive, "_copy_verified", side_effect=damage_after_copy):
            with self.assertRaises(archive.IntegrityError):
                self.store.archive_to(self.nas, now=100)
        self.assertIsNone(self.rows()[0]["verified_at"])
        self.assertEqual(self.store.status()["retained"], 1)

    def test_low_space_does_not_advance_capture_or_delete_source(self):
        source = self.write()
        self.store.ingest(self.snapshot, snapshot_id="1")
        with self.assertRaises(archive.InsufficientSpaceError):
            self.store.ingest(self.snapshot, snapshot_id="2", min_free_bytes=10**20)
        self.assertEqual(self.store.status()["total"], 0)
        self.assertTrue(source.exists())
        self.assertEqual(self.store.ingest(self.snapshot, snapshot_id="2")["ingested"], 1)

    def test_restart_retains_candidates_and_success_time(self):
        self.write()
        self.store.ingest(self.snapshot, snapshot_id="first")
        self.store.close()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool")
        self.assertEqual(self.store.ingest(self.snapshot, snapshot_id="second")["ingested"], 1)
        self.store.archive_to(self.nas, now=100)
        self.store.close()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool")
        self.assertEqual(self.rows()[0]["verified_at"], 100)

    def test_read_status_sees_committed_wal_without_initialization(self):
        self.write()
        self.capture_twice()
        with mock.patch.object(archive.ArchiveStore, "__init__", side_effect=AssertionError("must not initialize")):
            self.assertEqual(archive.ArchiveStore.read_status(self.root / "state"), self.store.status())

    def test_read_status_does_not_create_missing_ledger(self):
        missing_state = self.root / "unused-state"
        missing_state.mkdir()
        with self.assertRaises(FileNotFoundError):
            archive.ArchiveStore.read_status(missing_state)
        self.assertEqual(list(missing_state.iterdir()), [])

    def test_identical_content_in_different_paths_has_independent_spool(self):
        self.write()
        self.write("XP_EMER_DCIM/clip.mp4")
        self.capture_twice()
        rows = self.rows()
        self.assertEqual(rows[0]["digest"], rows[1]["digest"])
        self.assertNotEqual(rows[0]["spool_key"], rows[1]["spool_key"])

    def test_deleted_spool_after_prune_crash_can_be_reconciled(self):
        self.write()
        self.capture_twice()
        self.store.archive_to(self.nas, now=100)
        row = self.rows()[0]
        self.store._spool_path(row["spool_key"]).unlink()
        self.assertEqual(self.store.prune(self.nas, now=100 + 7 * archive.DAY)["pruned"], 1)

    def test_missing_spool_restored_from_stable_capture(self):
        self.write()
        self.capture_twice()
        row = self.rows()[0]
        self.store._spool_path(row["spool_key"]).unlink()
        self.assertEqual(self.store.ingest(self.snapshot, snapshot_id="3")["ingested"], 1)
        self.assertTrue(self.store._spool_path(row["spool_key"]).exists())

    def test_arbitrary_hierarchy_and_spaces_are_preserved(self):
        self.write("XP_EMER_DCIM/event one/front view.MP4")
        self.capture_twice()
        self.store.archive_to(self.nas, now=100)
        final = self.final(self.rows()[0])
        self.assertEqual(final.parent.relative_to(self.nas).as_posix(), "XP_EMER_DCIM/event one")
        self.assertEqual(final.suffix, ".MP4")
        self.assertTrue(final.name.startswith("front view--"))

    def test_outside_recording_roots_is_not_ingested(self):
        self.write("other/file.mp4")
        self.assertEqual(self.capture_twice()["observed"], 0)

    def test_invalid_relative_paths_rejected(self):
        digest = "a" * 64
        for relative in ("../XP_DCIM/a.mp4", "/XP_DCIM/a.mp4", "XP_DCIM/../a.mp4", "XP_DCIM\\a.mp4", "other/a.mp4"):
            with self.subTest(relative=relative):
                with self.assertRaises(archive.UnsafePathError):
                    archive.archive_relative_path(relative, digest)

    @unittest.skipIf(os.name == "nt", "POSIX symlink and mode semantics are verified in Linux CI")
    def test_symlinks_rejected_in_snapshot_and_nas(self):
        self.write()
        outside = self.root / "outside"
        outside.mkdir()
        (self.snapshot / "XP_DCIM" / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(archive.UnsafePathError):
            self.store.ingest(self.snapshot, snapshot_id="1")
        (self.snapshot / "XP_DCIM" / "linked").unlink()
        self.capture_twice()
        (self.nas / "XP_DCIM").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(archive.UnsafePathError):
            self.store.archive_to(self.nas, now=100)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are verified in Linux CI")
    def test_state_and_spool_permissions_private(self):
        self.assertEqual(self.store.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.spool_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store.state_dir / "archive.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_store_never_uses_filename_timestamp_as_identity(self):
        self.write("XP_DCIM/not-a-date.mp4")
        self.assertEqual(self.capture_twice()["ingested"], 1)
        self.assertEqual(self.rows()[0]["digest"], hashlib.sha256(mp4()).hexdigest())


@unittest.skipIf(os.name == "nt", "Linux /proc mount identity is verified in Linux CI")
class MountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.mountinfo = self.root / "mountinfo"
        self.target = self.root / "nas" / "XpengUSB" / "device"

    def tearDown(self):
        self.temporary.cleanup()

    def line(self, filesystem="cifs", source="//server/share", target=None):
        target = target or self.root / "nas"
        encoded = str(target).replace(" ", "\\040")
        return "42 30 0:47 / {} rw,relatime - {} {} rw\n".format(encoded, filesystem, source)

    def test_expected_cifs_ancestor_accepted(self):
        self.mountinfo.write_text(self.line(), encoding="utf-8")
        self.assertEqual(archive.verify_archive_mount(self.target, "//server/share", self.mountinfo), self.target)

    def test_wrong_mount_or_nested_override_rejected(self):
        for content in (self.line(source="//server/other"), self.line(filesystem="ext4"),
                        self.line() + self.line(filesystem="tmpfs", source="tmpfs", target=self.target)):
            self.mountinfo.write_text(content, encoding="utf-8")
            with self.assertRaises(archive.ArchiveMountError):
                archive.verify_archive_mount(self.target, "//server/share", self.mountinfo)

    def test_missing_mount_cannot_create_local_archive_path(self):
        self.mountinfo.write_text("", encoding="utf-8")
        with self.assertRaises(archive.ArchiveMountError):
            archive.verify_archive_mount(self.target, "//server/share", self.mountinfo)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
