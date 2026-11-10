"""Partial ingestion must use available capacity without reducing its reserve."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from xpengusb import archive


class IngestCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool")
        self.reserve = 1000

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def write(self, relative, size):
        source = self.snapshot / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"x" * size)
        return source

    def used_bytes(self):
        return sum(path.stat().st_size for path in self.store.spool_dir.rglob("*") if path.is_file())

    def disk_usage(self, capacity):
        return mock.patch.object(archive.shutil, "disk_usage", side_effect=lambda _: SimpleNamespace(
            free=self.reserve + capacity - self.used_bytes()))

    def records(self):
        return [row[0] for row in self.store.db.execute("SELECT relative FROM clips ORDER BY id")]

    def test_large_batch_keeps_smaller_fitting_clips_without_crossing_reserve(self):
        self.write("XP_DCIM/a-large.json", 200)
        self.write("XP_DCIM/b-small.json", 60)
        self.write("XP_DCIM/c-small.json", 30)
        self.store.ingest(self.snapshot, snapshot_id="first")
        with self.disk_usage(90):
            result = self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        self.assertEqual(result["ingested"], 2)
        self.assertTrue(result["partial"])
        self.assertEqual(result["skipped_capacity"], 1)
        self.assertEqual(result["skipped_capacity_bytes"], 200)
        self.assertEqual(self.used_bytes(), 90)
        self.assertEqual(self.records(), ["XP_DCIM/b-small.json", "XP_DCIM/c-small.json"])
        self.assertEqual(len(list(self.snapshot.rglob("*.json"))), 3)

    def test_emergency_clips_have_capacity_priority(self):
        self.write("XP_DCIM/regular.json", 60)
        self.write("XP_EMER_DCIM/emergency.json", 60)
        self.store.ingest(self.snapshot, snapshot_id="first")
        with self.disk_usage(60):
            result = self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        self.assertEqual(self.records(), ["XP_EMER_DCIM/emergency.json"])
        self.assertEqual(result["skipped_capacity"], 1)
        self.assertEqual(self.used_bytes(), 60)

    def test_unfittable_emergency_does_not_block_smaller_regular_clip(self):
        self.write("XP_EMER_DCIM/large.json", 200)
        self.write("XP_DCIM/small.json", 60)
        self.store.ingest(self.snapshot, snapshot_id="first")
        with self.disk_usage(60):
            result = self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        self.assertEqual(self.records(), ["XP_DCIM/small.json"])
        self.assertEqual(result["skipped_capacity_bytes"], 200)

    def test_no_clip_fitting_keeps_deferred_error_and_replay_retry(self):
        source = self.write("XP_DCIM/large.json", 200)
        self.store.ingest(self.snapshot, snapshot_id="first")
        with self.disk_usage(100), self.assertRaises(archive.InsufficientSpaceError):
            self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.used_bytes(), 0)
        self.assertEqual(source.read_bytes(), b"x" * 200)
        with self.disk_usage(200):
            result = self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        self.assertEqual(result["ingested"], 1)
        self.assertFalse(result["partial"])

    def test_later_capture_can_take_previously_skipped_clip(self):
        self.write("XP_DCIM/a-large.json", 200)
        self.write("XP_DCIM/b-small.json", 60)
        self.store.ingest(self.snapshot, snapshot_id="first")
        with self.disk_usage(60):
            self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        with self.disk_usage(260):
            result = self.store.ingest(self.snapshot, snapshot_id="third", min_free_bytes=self.reserve)
        self.assertEqual(result["ingested"], 1)
        self.assertFalse(result["partial"])
        self.assertEqual(self.used_bytes(), 260)
        self.assertEqual(len(self.records()), 2)

    def test_replaying_latest_partial_snapshot_preserves_warning_without_duplicate_ingest(self):
        self.write("XP_DCIM/a-large.json", 200)
        self.write("XP_DCIM/b-small.json", 60)
        self.store.ingest(self.snapshot, snapshot_id="first")
        with self.disk_usage(60):
            self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        result = self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        self.assertTrue(result["replayed"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped_capacity"], 1)
        self.assertEqual(self.used_bytes(), 60)

    def test_space_change_during_copy_check_skips_clip_but_keeps_later_small_clip(self):
        self.write("XP_DCIM/a-large.json", 200)
        self.write("XP_DCIM/b-small.json", 60)
        self.store.ingest(self.snapshot, snapshot_id="first")
        original = archive._copy_verified

        def changed_capacity(source, *args, **kwargs):
            if Path(source).name == "a-large.json":
                raise archive.InsufficientSpaceError("Reserve changed after initial check")
            return original(source, *args, **kwargs)

        with self.disk_usage(260), mock.patch.object(archive, "_copy_verified", side_effect=changed_capacity):
            result = self.store.ingest(self.snapshot, snapshot_id="second", min_free_bytes=self.reserve)
        self.assertTrue(result["partial"])
        self.assertEqual(self.records(), ["XP_DCIM/b-small.json"])
        self.assertEqual(list(self.store.intent_dir.glob("*.json")), [])
        self.assertEqual(self.used_bytes(), 60)


if __name__ == "__main__":
    unittest.main()
