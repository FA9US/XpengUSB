"""Zero-day cleanup still requires fresh proof that the NAS copy is intact."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xpengusb import archive


class ZeroRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.nas = self.root / "nas"
        self.nas.mkdir()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool", retention_days=0)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def ingest(self, count=1):
        directory = self.snapshot / "XP_DCIM"
        directory.mkdir(exist_ok=True)
        for index in range(count):
            (directory / (str(index) + ".json")).write_bytes(b'{"fixture":' + str(index).encode() + b"}")
        self.store.ingest(self.snapshot, snapshot_id="first")
        self.store.ingest(self.snapshot, snapshot_id="second")
        return self.store.db.execute("SELECT * FROM clips ORDER BY id").fetchall()

    def final(self, row):
        return self.nas.joinpath(*archive.archive_relative_path(row["relative"], row["digest"]).parts)

    def test_new_copy_is_immediately_eligible_and_read_back_again(self):
        row = self.ingest()[0]
        source = self.snapshot / row["relative"]
        content = source.read_bytes()
        self.store.archive_to(self.nas, now=100)
        with mock.patch.object(archive, "_digest", wraps=archive._digest) as digest:
            result = self.store.prune(self.nas, now=100)
        self.assertEqual(result["pruned"], 1)
        digest.assert_called_once_with(self.final(row))
        self.assertFalse(self.store._spool_path(row["spool_key"]).exists())
        self.assertEqual(source.read_bytes(), content)
        self.assertEqual(self.final(row).read_bytes(), content)
        self.assertEqual(self.store.status()["pruned"], 1)

    def test_never_verified_copy_is_kept(self):
        row = self.ingest()[0]
        self.assertEqual(self.store.prune(self.nas, now=100)["pruned"], 0)
        self.assertTrue(self.store._spool_path(row["spool_key"]).exists())

    def test_switch_from_default_retention_cleans_previously_verified_copy(self):
        self.store.close()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool")
        row = self.ingest()[0]
        self.assertEqual(self.store.retention_seconds, 7 * archive.DAY)
        self.store.archive_to(self.nas, now=100)
        self.assertEqual(self.store.prune(self.nas, now=101)["pruned"], 0)
        self.store.close()
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool", retention_days=0)
        self.assertEqual(self.store.prune(self.nas, now=102)["pruned"], 1)
        self.assertTrue(self.final(row).exists())

    def test_missing_and_corrupt_nas_copies_keep_local_data(self):
        rows = self.ingest(count=2)
        self.store.archive_to(self.nas, now=100)
        self.final(rows[0]).unlink()
        self.final(rows[1]).write_bytes(b"changed NAS data")
        result = self.store.prune(self.nas, now=100, isolate_errors=True)
        self.assertEqual(result["pruned"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["deferred"], 2)
        self.assertEqual(self.store.status()["retained"], 2)
        for row in rows:
            self.assertTrue(self.store._spool_path(row["spool_key"]).exists())
        self.assertFalse(self.final(rows[0]).exists())
        self.assertEqual(self.final(rows[1]).read_bytes(), b"changed NAS data")

    def test_newest_verified_batch_precedes_missing_historical_receipts(self):
        rows = self.ingest(count=4)
        self.store.archive_to(self.nas, now=100, max_files=2)
        for row in rows[:2]:
            self.final(row).unlink()
        self.store.archive_to(self.nas, now=101, max_files=2)
        result = self.store.prune(self.nas, now=101, max_files=2, isolate_errors=True)
        self.assertEqual(result["pruned"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.store.status()["retained"], 2)
        for row in rows[:2]:
            self.assertTrue(self.store._spool_path(row["spool_key"]).exists())
        result = self.store.prune(self.nas, now=102, max_files=2, isolate_errors=True)
        self.assertEqual(result["pruned"], 0)
        self.assertEqual(result["failed"], 2)

    def test_clock_rollback_does_not_bypass_zero_day_safeguards(self):
        row = self.ingest()[0]
        self.store.archive_to(self.nas, now=100)
        with self.assertRaisesRegex(archive.ArchiveError, "backwards"):
            self.store.prune(self.nas, now=99)
        self.assertTrue(self.store._spool_path(row["spool_key"]).exists())

    def test_invalid_retention_rejected_before_creating_state(self):
        for value in (-1, True, False, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                archive.ArchiveStore(self.root / "invalid", self.root / "unused", retention_days=value)
        self.assertFalse((self.root / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
