#!/usr/bin/env python3
"""Real Linux Btrfs/FAT/CIFS integration, for an explicitly disposable runner.

No physical disk, USB gadget, host user, or permanent Samba configuration is
changed. This uses small file-backed devices and a loopback-only Samba server.
Missing kernel modules/capabilities are failures, not skipped tests. The MP4
fixtures test file integrity, not video decoding or vehicle compatibility.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xpengusb.archive import ArchiveMountError, ArchiveStore, DAY, verify_archive_mount
from xpengusb.runtime import read_mounts


LOOP = re.compile(r"/dev/loop[0-9]+")


def run(*arguments, input_text=None, check=True, timeout=60):
    result = subprocess.run([str(arg) for arg in arguments], input=input_text,
                            text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        # Only synthetic fixtures are used; command errors aid CI diagnosis.
        raise RuntimeError(f"{arguments[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def mp4(payload):
    def box(kind, data):
        return struct.pack(">I4s", len(data) + 8, kind) + data
    return box(b"ftyp", b"isom0000") + box(b"moov", b"metadata") + box(b"mdat", payload)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Fixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xpengusb-storage-smoke-", dir="/tmp")
        # Automatic recursive cleanup is unsafe if unmount fails. Take explicit
        # ownership; cleanup() below only deletes after proving no mounts/loops.
        self.temporary._finalizer.detach()
        self.root = Path(self.temporary.name).resolve()
        self.storage = self.root / "storage"
        self.camera = self.storage / "cam.img"
        self.samba = None
        self.log = None
        self.counter = 0
        self.nas_mount = self.root / "nas-mount"
        self.share = "xpengusb_" + uuid.uuid4().hex
        self.source = "//127.0.0.1/" + self.share

    def loops(self):
        entries = json.loads(run("losetup", "--json", "--list", "--output", "NAME,BACK-FILE").stdout)
        return [(item["name"], Path(item["back-file"]))
                for item in entries.get("loopdevices", [])
                if item.get("back-file") and Path(item["back-file"]).is_relative_to(self.root)]

    def setup(self):
        self.storage.mkdir()
        container = self.root / "storage.btrfs"
        run("fallocate", "-l", "512M", container)
        run("mkfs.btrfs", container)
        outer = run("losetup", "--find", "--show", container).stdout.strip()
        require(LOOP.fullmatch(outer), "Unexpected outer loop device")
        run("mount", "-t", "btrfs", "-o", "noatime", outer, self.storage)
        run("truncate", "-s", "128M", self.camera)
        run("sfdisk", self.camera, input_text="label: dos\nstart=2048, type=c\n")
        loop = run("losetup", "--find", "--show", "--partscan", self.camera).stdout.strip()
        require(LOOP.fullmatch(loop), "Unexpected camera loop device")
        run("udevadm", "settle", "--timeout=10")
        run("mkfs.vfat", "-F", "32", "-n", "XPENGTEST", loop + "p1")
        run("losetup", "--detach", loop)
        self.write_camera(mp4(b"AAAA"))
        self.start_samba()

    @contextmanager
    def image_mount(self, image, *, read_only):
        self.counter += 1
        target = self.root / ("image-mount-" + str(self.counter))
        target.mkdir()
        loop = run("losetup", "--find", "--show", "--partscan", image).stdout.strip()
        require(LOOP.fullmatch(loop), "Unexpected image loop device")
        run("udevadm", "settle", "--timeout=10")
        try:
            if read_only:
                require(image != self.camera, "Refusing to repair original camera image")
                checked = run("fsck.fat", "-a", loop + "p1", check=False)
                require(checked.returncode in (0, 1), "Snapshot fsck failed: " + checked.stderr)
            options = ("ro" if read_only else "rw") + ",nodev,nosuid,noexec"
            run("mount", "-t", "vfat", "-o", options, loop + "p1", target)
            mounts = [item for item in read_mounts() if item.target == target]
            require(len(mounts) == 1 and mounts[0].source == loop + "p1", "Unexpected FAT mount")
            require(mounts[0].fstype == "vfat", "Unexpected snapshot filesystem")
            if read_only:
                require({"ro", "nodev", "nosuid", "noexec"} <= mounts[0].options, "Snapshot must be read-only")
                try:
                    (target / "must-not-write").write_bytes(b"no")
                except OSError:
                    pass
                else:
                    raise AssertionError("Read-only snapshot accepted a write")
            yield target
        finally:
            if any(item.target == target for item in read_mounts()):
                run("umount", target)
            require(not any(item.target == target for item in read_mounts()), "Mount remains active")
            require(not any(item.source == loop or item.source.startswith(loop + "p")
                            for item in read_mounts()), "Loop has another active mount")
            run("losetup", "--detach", loop)
            target.rmdir()

    def write_camera(self, regular):
        # Simulate the car only while no snapshot or gadget uses this fixture.
        with self.image_mount(self.camera, read_only=False) as target:
            (target / "XP_DCIM" / "nested").mkdir(parents=True, exist_ok=True)
            (target / "XP_EMER_DCIM").mkdir(exist_ok=True)
            regular_path = target / "XP_DCIM" / "nested" / "loop.mp4"
            regular_path.write_bytes(regular)
            # Keep FAT's coarse timestamp identical for filename-reuse coverage.
            os.utime(regular_path, (1700000000, 1700000000))
            (target / "XP_EMER_DCIM" / "event.mp4").write_bytes(mp4(b"EVENT"))
            os.sync()

    def snapshot(self, store, identity):
        image = self.storage / ("snapshot-" + identity + ".img")
        before = digest(self.camera)
        run("cp", "--reflink=always", "--sparse=auto", "--", self.camera, image)
        require(digest(image) == before, "Reflink does not match source")
        with self.image_mount(image, read_only=True) as target:
            result = store.ingest(target, snapshot_id=identity)
        require(digest(self.camera) == before, "Snapshot processing modified the camera image")
        image.unlink()
        return result

    def start_samba(self):
        samba_root = self.root / "samba"
        samba_root.mkdir()
        for name in ("lock", "state", "cache", "private", "pid", "ncalrpc", "share"):
            (samba_root / name).mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        configuration = samba_root / "smb.conf"
        configuration.write_text(f"""[global]
    server role = standalone server
    security = user
    map to guest = Bad User
    guest account = nobody
    interfaces = 127.0.0.1
    bind interfaces only = yes
    smb ports = {port}
    server min protocol = SMB3_11
    lock directory = {samba_root / 'lock'}
    state directory = {samba_root / 'state'}
    cache directory = {samba_root / 'cache'}
    private dir = {samba_root / 'private'}
    pid directory = {samba_root / 'pid'}
    ncalrpc dir = {samba_root / 'ncalrpc'}
    log file = {samba_root / 'samba.log'}
    load printers = no
    disable spoolss = yes
    printing = bsd
    printcap name = /dev/null
[{self.share}]
    path = {samba_root / 'share'}
    read only = no
    guest ok = yes
    guest only = yes
    force user = root
    follow symlinks = no
    wide links = no
""", encoding="utf-8")
        run("testparm", "--suppress-prompt", configuration)
        self.log = (samba_root / "foreground.log").open("wb")
        # Foreground smbd watches stdin pipes for EOF and treats it as parent
        # death. Keep a private pipe open instead of inheriting CI's closed stdin.
        # https://github.com/samba-team/samba/blob/master/source3/smbd/server.c
        self.samba = subprocess.Popen(["smbd", "--foreground", "--no-process-group",
                                      "--debug-stdout", "--debuglevel=3", "--configfile", str(configuration)],
                                      stdin=subprocess.PIPE, stdout=self.log, stderr=self.log,
                                      start_new_session=True)
        deadline = time.monotonic() + 15
        while True:
            returncode = self.samba.poll()
            require(returncode is None, f"Isolated Samba exited with code {returncode}; fixture logs follow")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Isolated loopback Samba did not start")
                time.sleep(0.1)
        self.nas_mount.mkdir()
        # Synthetic guest login, local test only. Production always uses a
        # root-private credential file and never enables a guest share.
        options = (f"guest,username=xpengusb_test_{uuid.uuid4().hex},port={port},vers=3.1.1,"
                   "nodev,nosuid,noexec,cache=none,actimeo=0,uid=0,gid=0,file_mode=0600,dir_mode=0700")
        run("mount", "-t", "cifs", self.source, self.nas_mount, "-o", options)
        verify_archive_mount(self.nas_mount, self.source)

    def diagnostics(self):
        """Keep bounded synthetic-only Samba evidence before fixture cleanup."""
        if self.samba is not None:
            print("Isolated smbd return code: " + str(self.samba.poll()), file=sys.stderr)
        if self.log is not None and not self.log.closed:
            self.log.flush()
        for name in ("foreground.log", "samba.log", "log.smbd"):
            path = self.root / "samba" / name
            if not path.is_file() or path.is_symlink():
                continue
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 16384))
                tail = stream.read().decode("utf-8", errors="replace")
            print(f"--- isolated Samba {name} (last 80 lines, at most 16 KiB) ---", file=sys.stderr)
            print("\n".join(tail.splitlines()[-80:]), file=sys.stderr)

    def cleanup(self):
        # Never use lazy/forced unmount or recursively delete a mounted tree.
        for mount in sorted(read_mounts(), key=lambda item: len(item.target.parts), reverse=True):
            if mount.target.is_relative_to(self.root) and mount.target != self.storage:
                run("umount", mount.target)
        for loop, backing in self.loops():
            if backing.is_relative_to(self.storage):
                require(not any(item.source == loop or item.source.startswith(loop + "p")
                                for item in read_mounts()), "Test loop remains mounted")
                run("losetup", "--detach", loop)
        if any(item.target == self.storage for item in read_mounts()):
            run("umount", self.storage)
        for loop, _ in self.loops():
            require(not any(item.source == loop or item.source.startswith(loop + "p")
                            for item in read_mounts()), "Outer test loop remains mounted")
            run("losetup", "--detach", loop)
        if self.samba is not None:
            try:
                os.killpg(self.samba.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.samba.wait(timeout=15)
            if self.samba.stdin is not None:
                self.samba.stdin.close()
        if self.log is not None:
            self.log.close()
        require(not any(item.target.is_relative_to(self.root) for item in read_mounts()), "Refusing cleanup: a test mount remains")
        require(not self.loops(), "Refusing cleanup: a test loop device remains")
        self.temporary.cleanup()


def exercise(fixture):
    fixture.setup()
    original = digest(fixture.camera)
    archive_root = fixture.nas_mount / "XpengUSB" / str(uuid.uuid4())
    with ArchiveStore(fixture.storage / "state", fixture.storage / "spool") as store:
        require(fixture.snapshot(store, "one")["ingested"] == 0, "First observation ingested too early")
        require(fixture.snapshot(store, "two")["ingested"] == 2, "Second stable snapshot did not capture both folders")
        try:
            store.archive_to(archive_root, expected_source="//wrong.example/share", now=1000)
        except ArchiveMountError:
            pass
        else:
            raise AssertionError("Wrong NAS source was accepted")
        require(not archive_root.exists(), "Wrong mount check created archive directories")
        require(store.archive_to(archive_root, expected_source=fixture.source, now=1000)["archived"] == 2,
                "CIFS copy/readback/hard-link publication failed")
        require(digest(fixture.camera) == original, "Archival modified original image")

        fixture.write_camera(mp4(b"BBBB"))
        rewritten = digest(fixture.camera)
        require(rewritten != original, "Filename reuse fixture did not change content")
        require(fixture.snapshot(store, "three")["ingested"] == 0, "Changed recording ingested without stability")
        require(fixture.snapshot(store, "four")["ingested"] == 1, "Reused filename was not retained as a new version")
        require(store.archive_to(archive_root, expected_source=fixture.source, now=2000)["archived"] == 1,
                "New file version failed CIFS archival")
        files = list(archive_root.rglob("*.mp4"))
        require(len(files) == 3, "Archive overwrote a filename or duplicated stable data")
        contents = {path.read_bytes() for path in files}
        require(contents == {mp4(b"AAAA"), mp4(b"BBBB"), mp4(b"EVENT")}, "NAS readback content mismatch")
        require(store.prune(archive_root, expected_source=fixture.source, now=1000 + 7 * DAY - 1)["pruned"] == 0,
                "Local retention expired before seven days")
        require(store.prune(archive_root, expected_source=fixture.source, now=1000 + 7 * DAY)["pruned"] == 2,
                "Exact seven-day retention boundary failed")
        require(store.status()["retained"] == 1, "Newer version lost its own retention interval")
        require(store.prune(archive_root, expected_source=fixture.source, now=2000 + 7 * DAY)["pruned"] == 1,
                "Later copy retention boundary failed")
        require({path.read_bytes() for path in archive_root.rglob("*.mp4")} == contents,
                "Local pruning modified NAS archive")
        require(digest(fixture.camera) == rewritten, "Retention modified original camera image")

        with ArchiveStore(fixture.storage / "zero-state", fixture.storage / "zero-spool",
                          retention_days=0) as zero_store:
            fixture.snapshot(zero_store, "zero-first")
            fixture.snapshot(zero_store, "zero-second")
            zero_root = fixture.nas_mount / "XpengUSB" / str(uuid.uuid4())
            require(zero_store.archive_to(zero_root, expected_source=fixture.source,
                                         now=3000)["archived"] == 2, "Zero-day upload failed")
            require(zero_store.prune(zero_root, expected_source=fixture.source, now=3000)["pruned"] == 2,
                    "Verified zero-day copies were not cleared immediately")
            require(zero_store.status()["retained"] == 0, "Zero-day cleanup left a retained copy")
            require({path.read_bytes() for path in zero_root.rglob("*.mp4")} == {mp4(b"BBBB"), mp4(b"EVENT")},
                    "Zero-day cleanup changed a NAS copy")
            require(digest(fixture.camera) == rewritten, "Zero-day cleanup modified the camera image")

        run("umount", fixture.nas_mount)
        try:
            store.archive_to(archive_root, expected_source=fixture.source, now=2001 + 7 * DAY)
        except ArchiveMountError:
            pass
        else:
            raise AssertionError("Missing CIFS mount was accepted")
        require(not any(fixture.nas_mount.iterdir()), "Missing NAS spilled archive files onto local root")
    print("PASS: real Btrfs reflinks, snapshot-only FAT repair/RO mounts, CIFS publication/readback, filename reuse, seven-day retention, missing/wrong mount guards")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-test", action="store_true", required=True,
                        help="Acknowledge this is a disposable Linux test runner")
    parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("This integration requires root on a disposable Linux runner.")
    for command in ("fallocate", "mkfs.btrfs", "mount", "umount", "truncate", "sfdisk",
                    "losetup", "udevadm", "mkfs.vfat", "fsck.fat", "cp", "smbd", "testparm"):
        if shutil.which(command) is None:
            raise SystemExit("Required command is unavailable: " + command)
    fixture = Fixture()
    try:
        exercise(fixture)
    except BaseException:
        fixture.diagnostics()
        raise
    finally:
        try:
            fixture.cleanup()
        except Exception as error:
            print("CLEANUP STOPPED; preserving " + str(fixture.root) + ": " + str(error), file=sys.stderr)
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
