"""XpengUSB worker: snapshot the recorder, retain files, and copy to SMB.

The car owns the live FAT volume. This module never mounts, repairs, or
disconnects that volume. Only a private Btrfs reflink is repaired and mounted.
Capture and NAS work are scheduled independently. NAS work yields after each
bounded batch so an upload backlog does not repeatedly rescan the camera disk.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .archive import is_transport_error


LOG = logging.getLogger("xpengusb")
GIB = 1024 ** 3
MIN_TRUSTED_TIME = 1735689600  # 2025-01-01; the Pi has no battery-backed clock.
SNAPSHOT_NAME = re.compile(r"^[0-9a-f]{32}$")
LOOP_NAME = re.compile(r"^/dev/loop[0-9]+$")
ARCHIVE_BATCH_FILES = 2
ARCHIVE_CONTINUE_SECONDS = 2
ARCHIVE_RETRY_SECONDS = 15
ARCHIVE_RETRY_MAX_SECONDS = 120


class RuntimeSafetyError(RuntimeError):
    """A prerequisite failed; keep the recorder connected and retry later."""


class Watchdog:
    """Feed systemd from work that is advancing, never from a background thread."""

    def __init__(self):
        address = os.environ.get("NOTIFY_SOCKET", "")
        usec = os.environ.get("WATCHDOG_USEC", "0")
        self.address = ("\0" + address[1:]) if address.startswith("@") else address
        self.interval = int(usec) / 3_000_000 if address and usec.isdecimal() else 0
        self.next_ping = 0.0

    def ping(self):
        if not self.interval or time.monotonic() < self.next_ping:
            return
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(b"WATCHDOG=1", self.address)
        self.next_ping = time.monotonic() + self.interval


@dataclass(frozen=True)
class Config:
    storage_dir: Path
    state_dir: Path
    spool_dir: Path
    snapshot_dir: Path
    camera_image: Path
    nas_mount: Path
    nas_source: str
    credentials_file: Path
    archive_device_id: str
    retention_days: int = 7
    interval_seconds: int = 300
    nas_directory: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        paths = ("storage_dir", "state_dir", "spool_dir", "snapshot_dir",
                 "camera_image", "nas_mount", "credentials_file")
        values = dict(data)
        for key in paths:
            if not isinstance(values.get(key), str):
                raise RuntimeSafetyError("Missing path setting: " + key)
            path = Path(values[key])
            if not path.is_absolute() or ".." in path.parts:
                raise RuntimeSafetyError("Paths must be absolute without '..': " + key)
            values[key] = path
        required = {field for field in cls.__dataclass_fields__}
        config = cls(**{key: val for key, val in values.items() if key in required})
        config.validate()
        return config

    def validate(self) -> None:
        if self.storage_dir == Path(self.storage_dir.anchor):
            raise RuntimeSafetyError("The storage directory cannot be a filesystem root")
        children = (self.state_dir, self.spool_dir, self.snapshot_dir, self.camera_image)
        if any(path.parent != self.storage_dir for path in children):
            raise RuntimeSafetyError("State, spool, snapshots and camera must be direct storage children")
        if len(set(children)) != 4:
            raise RuntimeSafetyError("Storage paths must be distinct")
        if self.nas_mount == Path(self.nas_mount.anchor) or self.nas_mount in children:
            raise RuntimeSafetyError("Invalid NAS mount point")
        if (self.nas_mount == self.storage_dir or self.storage_dir in self.nas_mount.parents
                or self.nas_mount in self.storage_dir.parents):
            raise RuntimeSafetyError("NAS mount cannot overlap recorder storage")
        if not re.fullmatch(r"//[A-Za-z0-9._-]+/[^/\\,\x00-\x1f]+", self.nas_source):
            raise RuntimeSafetyError("nas_source must be //hostname/share")
        if (not isinstance(self.nas_directory, str) or len(self.nas_directory) > 240
                or self.nas_directory and any(
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,79}", part)
                    or part.endswith((" ", "."))
                    for part in self.nas_directory.split("/"))):
            raise RuntimeSafetyError("Use a relative NAS subfolder such as cars/dashcam.")
        if any(char in str(self.credentials_file) for char in (",", "\n", "\r")):
            raise RuntimeSafetyError("Invalid credential file path")
        try:
            uuid.UUID(self.archive_device_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeSafetyError("archive_device_id must be a UUID") from exc
        if type(self.retention_days) is not int or not 0 <= self.retention_days <= 365:
            raise RuntimeSafetyError("retention_days must be 0 through 365; 0 clears verified local copies")
        if type(self.interval_seconds) is not int or self.interval_seconds < 30:
            raise RuntimeSafetyError("interval_seconds must be at least 30")

    @property
    def archive_root(self) -> Path:
        return self.nas_mount / self.nas_directory / "XpengUSB" / self.archive_device_id


@dataclass(frozen=True)
class Mount:
    target: Path
    source: str
    fstype: str
    options: frozenset
    root: str = "/"


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def parse_mountinfo(text: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        before, after = left.split(), right.split()
        if len(before) < 6 or len(after) < 3:
            continue
        mounts.append(Mount(Path(_unescape_mount(before[4])),
                            _unescape_mount(after[1]), after[0],
                            frozenset(before[5].split(",")),
                            _unescape_mount(before[3])))
    return mounts


def read_mounts() -> list[Mount]:
    return parse_mountinfo(Path("/proc/self/mountinfo").read_text())


def available_bytes(path: Path) -> int:
    result = os.statvfs(path)
    return result.f_bavail * result.f_frsize


def _no_symlinks(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RuntimeSafetyError("Managed paths must not contain symlinks")


class Runner:
    def run(self, args: list[str], *, timeout: int = 120, check: bool = True):
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeSafetyError(args[0] + " timed out") from exc
        if check and result.returncode:
            # Do not include tool output: mounting errors can include account data.
            raise RuntimeSafetyError(args[0] + " failed (exit " + str(result.returncode) + ")")
        return result


class Worker:
    def __init__(self, config: Config, store, *, runner=None,
                 mounts: Callable = read_mounts, free_bytes: Callable = available_bytes,
                 now: Callable = time.time, progress=None):
        self.config = config
        self.store = store
        self.runner = runner or Runner()
        self.mounts = mounts
        self.free_bytes = free_bytes
        self.now = now
        self.progress = progress
        self.nas_reconnect_needed = False

    def _run(self, *args, **kwargs):
        result = self.runner.run([str(arg) for arg in args], **kwargs)
        if self.progress:
            self.progress()
        return result

    def _mount_at(self, target: Path):
        found = [entry for entry in self.mounts() if entry.target == target]
        if len(found) > 1:
            raise RuntimeSafetyError("Stacked mounts at a managed path")
        return found[0] if found else None

    def trusted_time(self) -> float | None:
        result = self._run("timedatectl", "show", "-p", "NTPSynchronized", "--value",
                           timeout=10, check=False)
        now = self.now()
        if result.returncode == 0 and result.stdout.strip() == "yes" and now >= MIN_TRUSTED_TIME:
            return now
        # Some systemd versions print the boolean as true.
        if result.returncode == 0 and result.stdout.strip() == "true" and now >= MIN_TRUSTED_TIME:
            return now
        return None

    def _verify_nas(self) -> None:
        mount = self._mount_at(self.config.nas_mount)
        if (not mount or mount.fstype != "cifs" or mount.source != self.config.nas_source
                or mount.root != "/" or "rw" not in mount.options):
            raise RuntimeSafetyError("NAS mount is not the configured writable CIFS share")

    def ensure_nas(self) -> None:
        path = self.config.nas_mount
        if self._mount_at(path):
            self._verify_nas()
            if not self.nas_reconnect_needed:
                return
            # Archive calls have returned and closed their handles. Never force
            # or lazily detach a busy mount, and never detach an unrelated share.
            self._run("umount", "--", path, timeout=30)
            if self._mount_at(path):
                raise RuntimeSafetyError("Previous NAS session is still mounted")
        _no_symlinks(path)
        if not path.is_dir():
            raise RuntimeSafetyError("NAS mount directory is missing")
        if any(path.iterdir()):
            raise RuntimeSafetyError("Refusing to hide files below the NAS mount point")
        credentials = self.config.credentials_file
        _no_symlinks(credentials)
        info = credentials.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RuntimeSafetyError("SMB credentials must be a root-owned private regular file")
        options = ("credentials=" + str(credentials)
                   + ",vers=3.1.1,sec=ntlmssp,nodev,nosuid,noexec,cache=none,actimeo=0,"
                     "soft,echo_interval=10,nosharesock,uid=0,gid=0,file_mode=0600,dir_mode=0700")
        self._run("mount", "-t", "cifs", self.config.nas_source, path, "-o", options,
                  timeout=45)
        self._verify_nas()
        self.nas_reconnect_needed = False

    def archive(self, now: float) -> dict:
        self._verify_nas()
        # The store timestamps each completed verified transfer. Never pass a
        # cycle-start timestamp: copying over Wi-Fi can take hours.
        result = self.store.archive_to(self.config.archive_root,
                                       expected_source=self.config.nas_source,
                                       max_files=ARCHIVE_BATCH_FILES, isolate_errors=True)
        LOG.info("Archive: %s", result)
        now = self.trusted_time()
        if now is None:
            LOG.warning("Retention deferred: clock no longer synchronized")
            result["retention"] = "Waiting for synchronized clock"
        else:
            retention = self.store.prune(self.config.archive_root,
                                         expected_source=self.config.nas_source, now=now,
                                         max_files=ARCHIVE_BATCH_FILES, isolate_errors=True)
            LOG.info("Retention: %s", retention)
            result["pruned"] = retention["pruned"]
            if retention.get("failed") or retention.get("deferred"):
                result["retention"] = "Some expired files need NAS re-verification"
        return result

    def _check_storage(self) -> int:
        config = self.config
        for path in (config.storage_dir, config.state_dir, config.spool_dir,
                     config.snapshot_dir, config.camera_image):
            _no_symlinks(path)
        if not all(path.is_dir() for path in (config.storage_dir, config.state_dir,
                                             config.spool_dir, config.snapshot_dir)):
            raise RuntimeSafetyError("Recorder storage directories are missing")
        image_stat = config.camera_image.stat()
        if not stat.S_ISREG(image_stat.st_mode) or image_stat.st_nlink != 1:
            raise RuntimeSafetyError("Live camera image must be a regular file with one link")
        if image_stat.st_size != 32 * GIB:
            raise RuntimeSafetyError("Live camera image must be exactly 32 GiB")
        mount = self._mount_at(config.storage_dir)
        if not mount or mount.fstype != "btrfs" or "rw" not in mount.options:
            raise RuntimeSafetyError("Recorder storage must be a mounted writable Btrfs volume")
        filesystem = self._run("findmnt", "--noheadings", "--output", "FSTYPE",
                               "--target", config.storage_dir).stdout.strip()
        if filesystem != "btrfs":
            raise RuntimeSafetyError("Recorder storage must be Btrfs for atomic reflink snapshots")
        # Enough capacity for the car to rewrite its entire logical disk while
        # a snapshot pins old blocks, plus filesystem metadata/emergency margin.
        return image_stat.st_size + 8 * GIB

    def _loop_for(self, image: Path) -> str | None:
        result = self._run("losetup", "--json", "--list", "--output", "NAME,BACK-FILE")
        try:
            entries = json.loads(result.stdout).get("loopdevices", [])
        except (ValueError, AttributeError) as exc:
            raise RuntimeSafetyError("Could not inspect loop devices") from exc
        matches = [item.get("name") for item in entries
                   if item.get("back-file") == str(image)]
        if len(matches) > 1 or any(not LOOP_NAME.fullmatch(item or "") for item in matches):
            raise RuntimeSafetyError("Unexpected loop devices for snapshot")
        return matches[0] if matches else None

    def _workspace(self, reserve: int) -> Path:
        root = self.config.snapshot_dir
        entries = list(root.iterdir())
        if len(entries) > 1:
            raise RuntimeSafetyError("Multiple snapshot workspaces require inspection")
        if entries:
            work = entries[0]
            _no_symlinks(work)
            if not work.is_dir() or not SNAPSHOT_NAME.fullmatch(work.name):
                raise RuntimeSafetyError("Unexpected file in snapshot directory")
            names = {child.name for child in work.iterdir()}
            if not names <= {"cam.img", "mount", "cam.img.part"}:
                raise RuntimeSafetyError("Unexpected file in snapshot workspace")
            if names <= {"mount"}:
                target = work / "mount"
                _no_symlinks(target)
                if (self._loop_for(work / "cam.img") or
                        any(m.target == target or target in m.target.parents for m in self.mounts())):
                    raise RuntimeSafetyError("Empty snapshot workspace still in use")
                if target.exists():
                    target.rmdir()
                work.rmdir()
                return self._workspace(reserve)
            if (work / "cam.img.part").exists() and not (work / "cam.img").exists():
                partial, target = work / "cam.img.part", work / "mount"
                _no_symlinks(partial)
                _no_symlinks(target)
                if (self._loop_for(partial) or any(m.target == target or target in m.target.parents
                                                  for m in self.mounts())):
                    raise RuntimeSafetyError("Interrupted snapshot still in use; preserving it")
                if not partial.is_file():
                    raise RuntimeSafetyError("Unexpected incomplete snapshot type")
                partial.unlink()
                if target.exists():
                    target.rmdir()
                work.rmdir()
                return self._workspace(reserve)
            if not (work / "cam.img").is_file() or (work / "cam.img.part").exists():
                raise RuntimeSafetyError("Interrupted snapshot creation requires inspection")
            (work / "mount").mkdir(mode=0o700, exist_ok=True)
            return work
        if self.free_bytes(self.config.storage_dir) < reserve:
            raise RuntimeSafetyError("Low storage: keeping retained files; capture skipped")
        work = root / uuid.uuid4().hex
        work.mkdir(mode=0o700)
        (work / "mount").mkdir(mode=0o700)
        try:
            self._run("cp", "--reflink=always", "--sparse=auto", "--",
                      self.config.camera_image, work / "cam.img.part")
            os.replace(work / "cam.img.part", work / "cam.img")
        except BaseException:
            # cp never creates a mount. Remove only these exact temporary paths.
            for path in (work / "cam.img.part", work / "cam.img"):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            (work / "mount").rmdir()
            work.rmdir()
            raise
        return work

    def _check_snapshot_mount(self, work: Path, loop: str) -> None:
        mount = self._mount_at(work / "mount")
        if (not mount or mount.source != loop + "p1" or mount.fstype != "vfat"
                or not {"ro", "nosuid", "nodev", "noexec"} <= mount.options):
            raise RuntimeSafetyError("Snapshot mount is not the expected read-only FAT partition")

    def _release(self, work: Path, loop: str | None) -> None:
        _no_symlinks(work)
        if work.parent != self.config.snapshot_dir or not SNAPSHOT_NAME.fullmatch(work.name):
            raise RuntimeSafetyError("Refusing cleanup outside a managed snapshot")
        target, image = work / "mount", work / "cam.img"
        if self._mount_at(target):
            if not loop:
                raise RuntimeSafetyError("Cannot release unidentified snapshot mount")
            self._check_snapshot_mount(work, loop)
            self._run("umount", "--", target, timeout=30)
            if self._mount_at(target):
                raise RuntimeSafetyError("Snapshot still mounted; preserving image")
        if loop:
            # A surviving mount anywhere on this loop must stop deletion.
            if any(m.source == loop or m.source.startswith(loop + "p") for m in self.mounts()):
                raise RuntimeSafetyError("Snapshot still mounted elsewhere; preserving image")
            self._run("losetup", "--detach", loop)
            if self._loop_for(image):
                raise RuntimeSafetyError("Snapshot loop still active; preserving image")
        elif self._loop_for(image):
            raise RuntimeSafetyError("Unidentified active loop; preserving image")
        # No recursive deletion: extra files or mount points fail closed.
        if target.exists():
            target.rmdir()
        image.unlink()
        work.rmdir()

    def capture(self) -> dict:
        reserve = self._check_storage()
        work = self._workspace(reserve)
        image, target = work / "cam.img", work / "mount"
        _no_symlinks(image)
        _no_symlinks(target)
        image_stat, live_stat = image.stat(), self.config.camera_image.stat()
        if (not stat.S_ISREG(image_stat.st_mode) or image_stat.st_nlink != 1
                or (image_stat.st_dev, image_stat.st_ino) == (live_stat.st_dev, live_stat.st_ino)
                or image_stat.st_size != live_stat.st_size):
            raise RuntimeSafetyError("Snapshot is not an independent full-size regular image")
        loop = self._loop_for(image)
        try:
            if not loop:
                if self._mount_at(target):
                    raise RuntimeSafetyError("Snapshot mount has no matching loop")
                loop = self._run("losetup", "--find", "--show", "--partscan", image).stdout.strip()
                if not LOOP_NAME.fullmatch(loop):
                    loop = None
                    raise RuntimeSafetyError("Unexpected loop device name")
                self._run("udevadm", "settle", "--timeout=20", timeout=25)
            mounted = self._mount_at(target)
            if mounted:
                self._check_snapshot_mount(work, loop)
            else:
                if any(m.source == loop or m.source.startswith(loop + "p") for m in self.mounts()):
                    raise RuntimeSafetyError("Snapshot loop mounted at another path")
                result = self._run("fsck.fat", "-a", loop + "p1", check=False)
                if result.returncode not in (0, 1):
                    raise RuntimeSafetyError("Snapshot FAT repair failed; live image untouched")
                result = self._run("fsck.fat", "-n", loop + "p1", check=False)
                if result.returncode != 0:
                    raise RuntimeSafetyError("Snapshot FAT remains inconsistent after repair")
                self._run("mount", "-t", "vfat", "-o", "ro,nosuid,nodev,noexec",
                          loop + "p1", target)
                self._check_snapshot_mount(work, loop)
            result = self.store.ingest(target, snapshot_id=work.name, min_free_bytes=reserve)
            LOG.info("Snapshot ingestion: %s", result)
            capture = {key: result[key] for key in
                       ("partial", "skipped_capacity", "skipped_capacity_bytes") if key in result}
            try:
                usage = shutil.disk_usage(target)
            except OSError:
                return capture
            capture["usb_disk"] = {"total": usage.total, "free": usage.free}
            return capture
        except Exception:
            LOG.warning("Snapshot ingestion failed; committed spool is retained. "
                        "This snapshot will be released after a safe unmount; "
                        "recordings not yet ingested depend on the car's live volume.")
            raise
        finally:
            # Never leave an old snapshot pinning the car's COW blocks after a
            # recoverable ingestion error. Failed unmount/detach preserves it.
            self._release(work, loop)

    def cycle(self, *, capture_due=True, archive_due=True, report=None, nas_active=None) -> dict:
        outcomes = {}
        try:
            self._check_storage()
        except Exception as exc:
            LOG.warning("Storage unavailable: %s", exc)
            return {"archive": "storage unavailable", "capture": str(exc),
                    "capture_checked": time.time_ns(), "phase": "idle"}
        # Capture before network work so an existing NAS backlog cannot defer
        # the first snapshot indefinitely. A failed capture (including low
        # space) must still permit uploading and pruning retained footage.
        if capture_due:
            if report:
                report({"phase": "capturing"})
            try:
                outcomes.update(self.capture())
                outcomes["capture"] = ("Low storage: some clips could not be imported"
                                       if outcomes.get("partial") else "completed")
            except Exception as exc:
                outcomes["capture"] = str(exc)
                LOG.warning("Capture deferred: %s", exc)
            outcomes["capture_checked"] = time.time_ns()
        if archive_due:
            try:
                now = self.trusted_time()
                if now is None:
                    outcomes["archive"] = "waiting for synchronized clock"
                else:
                    outcomes["archive_attempt"] = time.time_ns()
                    if report:
                        report({**outcomes, "phase": "uploading"})
                    if nas_active:
                        nas_active.touch()
                    self.ensure_nas()
                    upload = self.archive(now)
                    outcomes["uploaded"] = upload["archived"]
                    outcomes["pruned"] = upload.get("pruned", 0)
                    outcomes["archive"] = "completed"
                    if upload.get("failed") or upload.get("deferred"):
                        outcomes["archive"] = upload.get("last_error") or "Some queued files need retry"
                    outcomes["retention"] = upload.get("retention", "completed")
            except Exception as exc:
                if is_transport_error(exc):
                    self.nas_reconnect_needed = True
                    outcomes["archive"] = "NAS connection interrupted; retry scheduled"
                else:
                    outcomes["archive"] = str(exc)
                LOG.warning("Archive deferred: %s", exc)
            if nas_active:
                nas_active.unlink(missing_ok=True)
        outcomes["phase"] = "idle"
        return outcomes


def archive_delay(result, pending, failures, interval):
    if (result.get("uploaded", 0) and pending) or result.get("pruned", 0) == ARCHIVE_BATCH_FILES:
        return ARCHIVE_CONTINUE_SECONDS, 0
    if result["archive"] == "waiting for synchronized clock":
        return min(30, interval), 0
    if result["archive"] != "completed":
        delay = min(ARCHIVE_RETRY_MAX_SECONDS, ARCHIVE_RETRY_SECONDS * 2 ** min(failures, 3))
        return min(delay, interval), failures + 1
    return interval, 0


@contextlib.contextmanager
def worker_lock(path: Path):
    import fcntl  # Linux only; pure controller tests remain portable.
    _no_symlinks(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeSafetyError("Another XpengUSB worker is already running") from exc
        yield
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/xpengusb/config.json"))
    parser.add_argument("--once", action="store_true", help="Run one snapshot/archive cycle")
    parser.add_argument("--status", action="store_true", help="Print local archive counts without mounting or copying recordings")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        _no_symlinks(args.config)
        config = Config.from_dict(json.loads(args.config.read_text()))
        from .archive import ArchiveStore
        if os.geteuid() != 0:
            raise RuntimeSafetyError("The worker must run as root")
        if args.status:
            Worker(config, None)._check_storage()
            print(json.dumps(ArchiveStore.read_status(config.state_dir), indent=2))
            return 0
        lock_dir = Path("/run/xpengusb")
        _no_symlinks(lock_dir)
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        lock_stat = lock_dir.stat()
        if lock_stat.st_uid != 0 or stat.S_IMODE(lock_stat.st_mode) & 0o077:
            raise RuntimeSafetyError("Worker lock directory must be root-owned and private")
        with worker_lock(lock_dir / "worker.lock"):
            # Do this before constructing the database, including for --status.
            # An unmounted storage volume must never create state on the OS disk.
            Worker(config, None)._check_storage()
            watchdog = Watchdog()
            watchdog.ping()
            with ArchiveStore(config.state_dir, config.spool_dir,
                              retention_days=config.retention_days, progress=watchdog.ping) as store:
                worker = Worker(config, store, progress=watchdog.ping)
                dashboard = Path("/etc/xpengusb/wifi.json").exists()
                sync_request = lock_dir / "sync-request"
                nas_active = lock_dir / "nas-active"

                def report(result):
                    if dashboard:
                        try:
                            from .dashboard_control import record_cycle
                            record_cycle(config, store, result)
                        except Exception:
                            LOG.warning("Could not refresh dashboard status")

                next_capture = next_archive = 0.0
                failures = 0
                if nas_active.exists():
                    worker.nas_reconnect_needed = True
                    report({"phase": "idle", "archive_attempt": time.time_ns(),
                            "archive": "NAS transfer was interrupted; retry scheduled"})
                while True:
                    watchdog.ping()
                    requested = dashboard and sync_request.exists()
                    if requested:
                        sync_request.unlink(missing_ok=True)
                    started = time.monotonic()
                    capture_due = requested or started >= next_capture
                    archive_due = requested or started >= next_archive
                    result = worker.cycle(capture_due=capture_due, archive_due=archive_due,
                                          report=report if dashboard else None, nas_active=nas_active)
                    report(result)
                    if args.once:
                        print(json.dumps(result, indent=2))
                        return 0 if result.get("capture") == "completed" else 1
                    finished = time.monotonic()
                    if capture_due:
                        next_capture = finished + config.interval_seconds
                    if "archive" in result:
                        delay, failures = archive_delay(result, store.status()["pending"], failures,
                                                        config.interval_seconds)
                        next_archive = finished + delay
                    elif capture_due and failures == 0 and store.status()["pending"]:
                        next_archive = finished
                    while not (dashboard and sync_request.exists()):
                        watchdog.ping()
                        remaining = min(next_capture, next_archive) - time.monotonic()
                        if remaining <= 0:
                            break
                        limit = 2 if dashboard else watchdog.interval or remaining
                        time.sleep(min(remaining, limit))
    except Exception as exc:
        LOG.error("Worker stopped: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
