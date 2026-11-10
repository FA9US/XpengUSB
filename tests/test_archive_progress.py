"""Archive heartbeats represent advancing file work and remain operation-local."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xpengusb import archive


class ArchiveProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        self.source = self.snapshot / "XP_DCIM" / "clip.json"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"abcdefghij")
        self.nas = self.root / "nas"
        self.nas.mkdir()
        self.pings = []
        self.store = archive.ArchiveStore(self.root / "state", self.root / "spool",
                                         progress=lambda: self.pings.append("progress"))

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_hash_chunks_and_completed_files_report_progress_without_leaking_context(self):
        with mock.patch.object(archive, "CHUNK_SIZE", 4):
            self.store.ingest(self.snapshot, snapshot_id="one")
        # Three data chunks, then the completed file in the snapshot scan.
        self.assertEqual(len(self.pings), 4)
        archive._digest(self.source)
        self.assertEqual(len(self.pings), 4)

    def test_copy_and_nas_readback_report_chunk_progress(self):
        self.store.ingest(self.snapshot, snapshot_id="one")
        self.store.ingest(self.snapshot, snapshot_id="two")
        self.pings.clear()
        with mock.patch.object(archive, "CHUNK_SIZE", 4):
            self.store.archive_to(self.nas, now=100)
        # Source preverification, copy writes and NAS readback each advance.
        self.assertGreaterEqual(len(self.pings), 9)
        count = len(self.pings)
        archive._digest(self.source)
        self.assertEqual(len(self.pings), count)

    def test_failed_digest_does_not_claim_progress_and_existing_digest_mocks_work(self):
        with mock.patch.object(archive, "_digest", side_effect=OSError("read stalled")):
            with self.assertRaises(OSError):
                self.store.ingest(self.snapshot, snapshot_id="one")
        self.assertEqual(self.pings, [])
        self.assertEqual(self.store.status()["total"], 0)

    def test_callback_exception_restores_context_and_never_commits_a_capture(self):
        self.store.progress = mock.Mock(side_effect=RuntimeError("heartbeat unavailable"))
        with self.assertRaisesRegex(RuntimeError, "heartbeat unavailable"):
            self.store.ingest(self.snapshot, snapshot_id="one")
        self.assertEqual(self.store.status()["total"], 0)
        self.assertEqual(archive._digest(self.source)[1], 10)
        self.assertEqual(self.store.progress.call_count, 1)

    def test_recovery_uses_constructor_callback_during_content_verification(self):
        relative = "XP_DCIM/clip.json"
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        key = archive._spool_key(relative, digest)
        self.store._write_intent(relative, digest, 10, key)
        final = self.store._spool_path(key)
        final.parent.mkdir(parents=True)
        final.write_bytes(self.source.read_bytes())
        self.store.close()
        self.pings.clear()
        with mock.patch.object(archive, "CHUNK_SIZE", 4):
            self.store = archive.ArchiveStore(self.root / "state", self.root / "spool",
                                             progress=lambda: self.pings.append("recovery"))
        self.assertGreaterEqual(len(self.pings), 4)
        self.assertEqual(self.store.status()["total"], 1)
        count = len(self.pings)
        archive._digest(self.source)
        self.assertEqual(len(self.pings), count)

    def test_store_callbacks_do_not_cross_between_instances(self):
        other_pings = []
        with archive.ArchiveStore(self.root / "other-state", self.root / "other-spool",
                                  progress=lambda: other_pings.append("other")) as other:
            self.store.ingest(self.snapshot, snapshot_id="one")
            before = len(self.pings)
            other.ingest(self.snapshot, snapshot_id="one")
        self.assertEqual(len(self.pings), before)
        self.assertGreater(len(other_pings), 0)


if __name__ == "__main__":
    unittest.main()
