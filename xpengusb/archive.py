"""Content-verified archival, independent of USB gadget and mount control.

The controller supplies immutable, read-only snapshots. A file must have the
same content in two consecutive *different* snapshots before ingestion. MP4
checks validate box boundaries, not decodability or recording completeness.
Production callers must supply expected_source and gate the clock on NTP sync.
"""

import errno
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import struct
import time
import uuid


RECORDING_DIRS = ("XP_DCIM", "XP_EMER_DCIM")
CHUNK_SIZE = 1024 * 1024
DAY = 24 * 60 * 60
FILE_RETRY_SECONDS = 5 * 60
MAX_FILE_RETRY_SECONDS = 6 * 60 * 60
_PROGRESS_CALLBACK = ContextVar("xpengusb_archive_progress", default=None)
STATUS_QUERY = """SELECT COUNT(*) AS total,
    COALESCE(SUM(verified_at IS NULL),0) AS pending,
    COALESCE(SUM(CASE WHEN verified_at IS NULL THEN size ELSE 0 END),0) AS pending_bytes,
    COALESCE(SUM(verified_at IS NOT NULL),0) AS archived,
    COALESCE(SUM(purged=0),0) AS retained,
    COALESCE(SUM(purged=1),0) AS pruned,
    COALESCE(SUM(CASE WHEN purged=0 THEN size ELSE 0 END),0) AS retained_bytes
    FROM clips"""


def _report_progress():
    callback = _PROGRESS_CALLBACK.get()
    if callback is not None:
        callback()


def _with_progress(method):
    """Scope a store's heartbeat to its operation; helpers keep their API."""
    @wraps(method)
    def operation(self, *args, **kwargs):
        token = _PROGRESS_CALLBACK.set(self.progress)
        try:
            return method(self, *args, **kwargs)
        finally:
            _PROGRESS_CALLBACK.reset(token)
    return operation


class ArchiveError(RuntimeError):
    """An operation stopped without granting permission to delete recordings."""


class UnsafePathError(ArchiveError):
    pass


class IntegrityError(ArchiveError):
    pass


class InsufficientSpaceError(ArchiveError):
    pass


class ArchiveMountError(ArchiveError):
    pass


def is_transport_error(error):
    """Recognize network/session errors without treating permissions as outages.

    EIO/EREMOTEIO may also indicate a storage fault: this classification permits
    a controlled reconnect attempt, never a successful-copy receipt or deletion.
    Unknown errors are still propagated by archive operations.
    """
    codes = {getattr(errno, name, None) for name in (
        "ECONNABORTED", "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EHOSTDOWN",
        "EHOSTUNREACH", "ENETDOWN", "ENETUNREACH", "ENETRESET", "ENOTCONN",
        "ESTALE", "EPIPE", "EREMOTEIO", "EIO")}
    codes.discard(None)
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (TimeoutError, ConnectionError)):
            return True
        if isinstance(error, OSError) and error.errno in codes:
            return True
        error = error.__cause__ or error.__context__
    return False


def _isolated_file_error(error):
    if is_transport_error(error):
        return False
    if isinstance(error, (IntegrityError, UnsafePathError)):
        return True
    return isinstance(error, OSError) and error.errno in {
        errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.EISDIR,
        errno.ELOOP, errno.ENAMETOOLONG,
    }


def _file_error_reason(error):
    """Persist no paths, filenames, credentials or arbitrary exception text."""
    if isinstance(error, IntegrityError):
        return "Recording content could not be verified"
    if isinstance(error, UnsafePathError):
        return "Recording path is not a safe regular file or directory"
    if error.errno in (errno.EACCES, errno.EPERM):
        return "Recording access was denied"
    if error.errno == errno.ENOENT:
        return "Recording file or directory is missing"
    return "Recording path could not be accessed"


def _assert_no_symlinks(path):
    path = Path(os.path.abspath(path))
    for item in [*reversed(path.parents), path]:
        if item.is_symlink():
            raise UnsafePathError("Symbolic links are not allowed: {}".format(item))
    return path


def _directory(path, create=False, private=False):
    path = _assert_no_symlinks(path)
    if create:
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for current in reversed(missing):
            current.mkdir(mode=0o700, exist_ok=True)
            _fsync_dir(current)
            _fsync_dir(current.parent)
    if not path.is_dir():
        raise UnsafePathError("Expected a directory: {}".format(path))
    if private:
        os.chmod(path, 0o700)
    return path


def _regular_open(path):
    """Open a regular file without following a final symlink on POSIX."""
    path = _assert_no_symlinks(path)
    if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
        raise UnsafePathError("Expected a regular file: {}".format(path))
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafePathError("Expected a regular file: {}".format(path))
    return os.fdopen(fd, "rb")


def _digest(path, *, with_stat=False):
    digest = hashlib.sha256()
    size = 0
    with _regular_open(path) as stream:
        before = os.fstat(stream.fileno())
        while True:
            data = stream.read(CHUNK_SIZE)
            if not data:
                break
            digest.update(data)
            size += len(data)
            _report_progress()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise IntegrityError("Source changed while reading: {}".format(path))
    if size != before.st_size:
        raise IntegrityError("Short read: {}".format(path))
    result = (digest.hexdigest(), size)
    return (result, after) if with_stat else result


def mp4_has_complete_boxes(path):
    """Check outer ISO-BMFF bounds and nonempty ftyp, moov and mdat boxes.

    Size-zero boxes legally extend to EOF. This and stable content do not prove
    that every media sample is playable; in-car and copied-clip playback are
    separate acceptance tests.
    """
    with _regular_open(path) as stream:
        total = os.fstat(stream.fileno()).st_size
        position = 0
        present = set()
        boxes = 0
        while position < total:
            boxes += 1
            if boxes > 100000:
                return False
            if total - position < 8:
                return False
            stream.seek(position)
            header = stream.read(8)
            if len(header) != 8:
                return False
            length, kind = struct.unpack(">I4s", header)
            header_length = 8
            if length == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    return False
                length = struct.unpack(">Q", extended)[0]
                header_length = 16
            elif length == 0:
                length = total - position
            if length < header_length or position + length > total:
                return False
            if length > header_length:
                present.add(kind)
            position += length
            _report_progress()
        return {b"ftyp", b"moov", b"mdat"}.issubset(present)


def _fsync_dir(path):
    # Windows tests cannot open directories for fsync. Deployment is Linux.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def verify_archive_mount(nas_root, expected_source, mountinfo_path="/proc/self/mountinfo"):
    """Require the longest matching mount to be the configured CIFS share.

    expected_source=None is an explicit local-test seam. Runtime always passes
    //server/share. Check the path before mkdir so a missing mount cannot spill
    archive data onto the Pi's root filesystem.
    """
    root = _assert_no_symlinks(nas_root)
    if expected_source is None:
        return root
    if not expected_source.startswith("//") or "\n" in expected_source:
        raise ArchiveMountError("Expected source must be //server/share")
    matches = []
    try:
        with open(mountinfo_path, encoding="utf-8") as stream:
            for line in stream:
                fields = line.rstrip("\n").split(" ")
                separator = fields.index("-")
                mountpoint = Path(_unescape_mount(fields[4]))
                try:
                    root.relative_to(mountpoint)
                except ValueError:
                    continue
                matches.append((len(mountpoint.parts), fields[separator + 1],
                                _unescape_mount(fields[separator + 2]), fields[5],
                                _unescape_mount(fields[3])))
    except (OSError, ValueError, IndexError) as error:
        raise ArchiveMountError("Cannot verify the archive mount") from error
    if not matches:
        raise ArchiveMountError("Archive path is not mounted")
    _, filesystem, source, options, mount_root = max(matches, key=lambda entry: entry[0])
    if (filesystem != "cifs" or source.rstrip("/") != expected_source.rstrip("/")
            or mount_root != "/"):
        raise ArchiveMountError("Archive path is not the configured CIFS share")
    if "rw" not in options.split(","):
        raise ArchiveMountError("Archive share is not mounted read/write")
    return root


def _relative_path(value):
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or path.parts[0] not in RECORDING_DIRS
            or any(part in (".", "..", "") for part in path.parts)
            or "\\" in value or "\x00" in value):
        raise UnsafePathError("Invalid recording path")
    return path


def _spool_key(relative, digest):
    return hashlib.sha256((relative + "\0" + digest).encode("utf-8")).hexdigest()


def _short_name(text, maximum_bytes):
    return text.encode("utf-8")[:maximum_bytes].decode("utf-8", "ignore")


def archive_relative_path(relative, digest):
    """Preserve folders and playable extension; reused names cannot overwrite."""
    path = _relative_path(relative)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise IntegrityError("Invalid content digest")
    suffix = _short_name(path.suffix, 24)
    stem = _short_name(path.stem, 150)
    return path.parent / (stem + "--" + digest + suffix)


def _copy_verified(source, destination, expected_digest, expected_size,
                   min_free_bytes=0):
    """Write only a newly created temporary path; cleanup only that path."""
    _assert_no_symlinks(destination.parent)
    if shutil.disk_usage(destination.parent).free - expected_size < min_free_bytes:
        raise InsufficientSpaceError("Insufficient free space for a verified copy")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(destination, flags, 0o600)
    try:
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(fd, "wb") as target, _regular_open(source) as origin:
            while True:
                data = origin.read(CHUNK_SIZE)
                if not data:
                    break
                target.write(data)
                digest.update(data)
                count += len(data)
                _report_progress()
            target.flush()
            os.fsync(target.fileno())
        if count != expected_size or digest.hexdigest() != expected_digest:
            raise IntegrityError("Source changed during copy")
        contents, verified = _digest(destination, with_stat=True)
        if contents != (expected_digest, expected_size):
            raise IntegrityError("Copy readback does not match source")
        return verified
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise


def _publish_no_replace(temporary, final):
    """Atomic no-overwrite publication; require hard-link support on the NAS."""
    try:
        os.link(temporary, final)
    except FileExistsError:
        return False
    except OSError as error:
        if error.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
            raise ArchiveError("Archive filesystem must support atomic hard-link publication") from error
        raise
    _fsync_dir(final.parent)
    return True


def _matches_published_link(temporary, final, verified):
    """Check whether the new name still refers to the file we verified.

    The source remains linked until after this check. A successful hard link
    changes ctime/link count, so compare identity, size and modification time.
    Some SMB servers return different or unstable file IDs across opens. In
    that case the caller reads back the final file before granting a receipt.
    """
    with _regular_open(temporary) as origin, _regular_open(final) as target:
        original = os.fstat(origin.fileno())
        published = os.fstat(target.fileno())
    return (verified is not None and bool(verified.st_ino)
            and all(os.path.samestat(verified, item)
                    and (item.st_size, item.st_mtime_ns) == (verified.st_size, verified.st_mtime_ns)
                    for item in (original, published)))


class ArchiveStore:
    """Persistent content ledger; never modifies the car's snapshot or live disk."""

    def __init__(self, state_dir, spool_dir, retention_days=7, progress=None):
        if not isinstance(retention_days, (int, float)) or not math.isfinite(retention_days) or retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if progress is not None and not callable(progress):
            raise ValueError("progress must be callable or None")
        self.progress = progress
        self.retention_seconds = retention_days * DAY
        self.state_dir = _directory(state_dir, create=True, private=True)
        self.spool_dir = _directory(spool_dir, create=True, private=True)
        database = _assert_no_symlinks(self.state_dir / "archive.sqlite3")
        self.db = sqlite3.connect(str(database), timeout=30)
        self.db.row_factory = sqlite3.Row
        os.chmod(database, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
                generation INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL UNIQUE, manifest_sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates (
                relative TEXT PRIMARY KEY, digest TEXT NOT NULL, size INTEGER NOT NULL,
                generation INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY, relative TEXT NOT NULL, digest TEXT NOT NULL,
                size INTEGER NOT NULL, spool_key TEXT NOT NULL UNIQUE,
                verified_at REAL, purged INTEGER NOT NULL DEFAULT 0,
                UNIQUE(relative, digest));
            CREATE TABLE IF NOT EXISTS clip_failures (
                operation TEXT NOT NULL, clip_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL, retry_after REAL NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(operation, clip_id));
        """)
        self.db.commit()
        _fsync_dir(self.state_dir)
        self.intent_dir = _directory(self.state_dir / "ingest-intents", create=True, private=True)
        try:
            self._recover_intents()
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.db.close()

    def _spool_path(self, key):
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise IntegrityError("Invalid spool key in ledger")
        return _assert_no_symlinks(self.spool_dir / key[:2] / key)

    def _write_intent(self, relative, digest, size, key):
        """A durable receipt survives publication followed by sudden power loss."""
        intent = self.intent_dir / (key + ".json")
        temporary = self.intent_dir / (".partial-" + uuid.uuid4().hex)
        payload = {"relative": relative, "digest": digest, "size": size, "spool_key": key}
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            _assert_no_symlinks(intent)
            os.replace(temporary, intent)
            _fsync_dir(self.intent_dir)
        finally:
            temporary.unlink(missing_ok=True)
        return intent

    def _finish_intent(self, intent):
        intent.unlink(missing_ok=True)
        _fsync_dir(self.intent_dir)

    @_with_progress
    def _recover_intents(self):
        for intent in sorted(self.intent_dir.glob("*.json")):
            try:
                with _regular_open(intent) as stream:
                    payload = stream.read(65537)
                if len(payload) > 65536:
                    raise ValueError("oversized intent")
                data = json.loads(payload)
                relative, digest, size, key = (data["relative"], data["digest"], data["size"], data["spool_key"])
                _relative_path(relative)
                if (key != _spool_key(relative, digest) or intent.name != key + ".json"
                        or not isinstance(size, int) or size < 0
                        or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                    raise ValueError("invalid intent")
                final = self._spool_path(key)
                if final.exists():
                    if _digest(final) != (digest, size):
                        raise IntegrityError("Recovery found corrupt local spool content")
                    self.db.execute("INSERT OR IGNORE INTO clips(relative,digest,size,spool_key) VALUES(?,?,?,?)",
                                    (relative, digest, size, key))
                    self.db.commit()
                self._finish_intent(intent)
                _report_progress()
            except (ValueError, KeyError, TypeError) as error:
                raise IntegrityError("Invalid ingest recovery receipt") from error

    def _scan(self, root):
        root = _directory(root)
        entries = []
        for top in RECORDING_DIRS:
            directory = root / top
            _assert_no_symlinks(directory)
            if not directory.exists():
                continue
            if not directory.is_dir():
                raise UnsafePathError("Recording root is not a directory")
            for current, directories, filenames in os.walk(directory, followlinks=False):
                for name in directories:
                    _assert_no_symlinks(Path(current) / name)
                for name in filenames:
                    file = Path(current) / name
                    relative = file.relative_to(root).as_posix()
                    _relative_path(relative)
                    digest, size = _digest(file)
                    valid = file.suffix.lower() != ".mp4" or mp4_has_complete_boxes(file)
                    entries.append((relative, digest, size, valid))
                    _report_progress()
        return sorted(entries)

    @_with_progress
    def ingest(self, snapshot_root, *, snapshot_id=None, min_free_bytes=0):
        if not isinstance(min_free_bytes, int) or min_free_bytes < 0:
            raise ValueError("min_free_bytes must be a nonnegative integer")
        root = _directory(snapshot_root)
        snapshot_id = str(snapshot_id if snapshot_id is not None else root)
        if not snapshot_id or len(snapshot_id) > 4096:
            raise ValueError("Invalid snapshot_id")
        entries = self._scan(root)
        manifest = hashlib.sha256(json.dumps(entries, ensure_ascii=True).encode("utf-8")).hexdigest()
        result = {"observed": len(entries), "ingested": 0, "pending": 0,
                  "incomplete": sum(not entry[3] for entry in entries), "replayed": False,
                  "partial": False, "skipped_capacity": 0, "skipped_capacity_bytes": 0}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            seen = self.db.execute("SELECT manifest_sha FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if seen:
                if seen[0] != manifest:
                    raise IntegrityError("Snapshot ID reused with different content")
                saved = self.db.execute("SELECT value FROM metadata WHERE key='last_ingest_result'").fetchone()
                if saved:
                    saved = json.loads(saved[0])
                    if saved.get("snapshot_id") == snapshot_id:
                        for key in ("partial", "skipped_capacity", "skipped_capacity_bytes", "pending"):
                            result[key] = saved[key]
                result["replayed"] = True
                self.db.commit()
                return result
            previous = self.db.execute("SELECT COALESCE(MAX(generation), 0) FROM snapshots").fetchone()[0]
            stable = []
            for relative, digest, size, valid in entries:
                candidate = self.db.execute("SELECT digest, generation FROM candidates WHERE relative=?", (relative,)).fetchone()
                if valid and candidate and candidate[0] == digest and candidate[1] == previous:
                    old = self.db.execute("SELECT spool_key, purged FROM clips WHERE relative=? AND digest=?", (relative, digest)).fetchone()
                    if not old or (not old[1] and not self._spool_path(old[0]).exists()):
                        stable.append((relative, digest, size))
                elif valid:
                    result["pending"] += 1
            # Preserve incident footage first when capacity cannot fit every
            # new clip. Keep deterministic path ordering within each category.
            stable.sort(key=lambda entry: (not entry[0].startswith("XP_EMER_DCIM/"), entry[0]))
            for relative, digest, size in stable:
                key = _spool_key(relative, digest)
                final = self._spool_path(key)
                if not final.exists() and shutil.disk_usage(self.spool_dir).free - size < min_free_bytes:
                    result["skipped_capacity"] += 1
                    result["skipped_capacity_bytes"] += size
                    continue
                _directory(final.parent, create=True, private=True)
                intent = self._write_intent(relative, digest, size, key)
                if final.exists():
                    if _digest(final) != (digest, size):
                        raise IntegrityError("Existing local spool content is corrupt")
                else:
                    temporary = final.with_name(".xpengusb-partial-" + uuid.uuid4().hex)
                    try:
                        _copy_verified(root.joinpath(*PurePosixPath(relative).parts), temporary,
                                       digest, size, min_free_bytes)
                        _publish_no_replace(temporary, final)
                        if _digest(final) != (digest, size):
                            raise IntegrityError("Local spool publication failed readback")
                    except InsufficientSpaceError:
                        # Space can change after the first check while the car
                        # writes. The copy helper checks the same reserve again
                        # before creating its temporary file. Smaller later
                        # clips may still fit; never evict an existing copy.
                        self._finish_intent(intent)
                        result["skipped_capacity"] += 1
                        result["skipped_capacity_bytes"] += size
                        continue
                    finally:
                        temporary.unlink(missing_ok=True)
                self.db.execute("INSERT OR IGNORE INTO clips(relative,digest,size,spool_key) VALUES(?,?,?,?)",
                                (relative, digest, size, key))
                # Each durable capture stays registered if a later file fails.
                self.db.commit()
                self._finish_intent(intent)
                result["ingested"] += 1
                self.db.execute("BEGIN IMMEDIATE")
            result["partial"] = result["skipped_capacity"] > 0
            if result["partial"] and result["ingested"] == 0:
                raise InsufficientSpaceError(
                    "No new stable clip fits without breaching the recording reserve; "
                    "{} file(s), {} bytes deferred".format(
                        result["skipped_capacity"], result["skipped_capacity_bytes"]))
            cursor = self.db.execute("INSERT INTO snapshots(snapshot_id,manifest_sha) VALUES(?,?)", (snapshot_id, manifest))
            generation = cursor.lastrowid
            self.db.execute("DELETE FROM candidates")
            self.db.executemany("INSERT INTO candidates(relative,digest,size,generation) VALUES(?,?,?,?)",
                                [(r, d, s, generation) for r, d, s, _ in entries])
            self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('last_ingest_result',?)",
                            (json.dumps(dict(result, snapshot_id=snapshot_id)),))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _clock(self, now):
        value = time.time() if now is None else float(now)
        if not math.isfinite(value) or value < 0:
            raise ArchiveError("Invalid clock; archive retention is paused")
        previous = self.db.execute("SELECT value FROM metadata WHERE key='clock_highwater'").fetchone()
        if previous and value < float(previous[0]):
            raise ArchiveError("Clock moved backwards; archive retention is paused")
        self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('clock_highwater',?)", (str(value),))
        return value

    def _archive_path(self, nas_root, row):
        relative = archive_relative_path(row["relative"], row["digest"])
        return _assert_no_symlinks(nas_root.joinpath(*relative.parts))

    def _record_file_failure(self, operation, row, error, now):
        self.db.rollback()
        self.db.execute("BEGIN IMMEDIATE")
        current = self._clock(now)
        old = self.db.execute(
            "SELECT attempts FROM clip_failures WHERE operation=? AND clip_id=?",
            (operation, row["id"])).fetchone()
        attempts = old[0] + 1 if old else 1
        delay = min(MAX_FILE_RETRY_SECONDS, FILE_RETRY_SECONDS * 2 ** min(attempts - 1, 7))
        reason = _file_error_reason(error)
        self.db.execute(
            "INSERT OR REPLACE INTO clip_failures(operation,clip_id,attempts,retry_after,reason) "
            "VALUES(?,?,?,?,?)", (operation, row["id"], attempts, current + delay, reason))
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        return reason

    def _clear_file_failure(self, operation, row):
        self.db.execute("DELETE FROM clip_failures WHERE operation=? AND clip_id=?",
                        (operation, row["id"]))

    def _failure_summary(self, operation, current):
        eligibility = ("clips.verified_at IS NULL" if operation == "archive"
                       else "clips.verified_at IS NOT NULL AND clips.verified_at<=?")
        arguments = [operation, current]
        if operation == "prune":
            arguments.append(current - self.retention_seconds)
        row = self.db.execute(
            "SELECT COUNT(*), MIN(f.retry_after) FROM clip_failures f "
            "JOIN clips ON clips.id=f.clip_id WHERE f.operation=? AND f.retry_after>? "
            "AND clips.purged=0 AND " + eligibility, arguments).fetchone()
        return {"deferred": row[0], "next_retry_at": row[1]}

    @_with_progress
    def archive_to(self, nas_root, expected_source=None, now=None, max_files=None,
                   isolate_errors=False):
        if max_files is not None and (type(max_files) is not int or max_files < 1):
            raise ValueError("max_files must be a positive integer or None")
        root = verify_archive_mount(nas_root, expected_source)
        _directory(root, create=True)
        result = {"archived": 0, "already_verified": 0}
        if isolate_errors:
            result["failed"] = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self._clock(now)
            result["already_verified"] = self.db.execute(
                "SELECT COUNT(*) FROM clips WHERE purged=0 AND verified_at IS NOT NULL").fetchone()[0]
            rows = self.db.execute(
                "SELECT clips.* FROM clips LEFT JOIN clip_failures f "
                "ON f.clip_id=clips.id AND f.operation='archive' "
                "WHERE purged=0 AND verified_at IS NULL "
                "AND (?=0 OR f.retry_after IS NULL OR f.retry_after<=?) ORDER BY id LIMIT ?",
                (int(bool(isolate_errors)), current, -1 if max_files is None else max_files)).fetchall()
            for row in rows:
                try:
                    verify_archive_mount(root, expected_source)
                    final = self._archive_path(root, row)
                    _directory(final.parent, create=True)
                    expected = (row["digest"], row["size"])
                    if final.exists():
                        if _digest(final) != expected:
                            raise IntegrityError("Existing NAS recording has different content; refusing overwrite")
                        with _regular_open(final) as stream:
                            os.fsync(stream.fileno())
                        _fsync_dir(final.parent)
                        result["already_verified"] += 1
                    else:
                        source = self._spool_path(row["spool_key"])
                        if _digest(source) != expected:
                            raise IntegrityError("Local recording does not match its ledger")
                        temporary = final.with_name(".xpengusb-partial-" + uuid.uuid4().hex)
                        copy_error = None
                        try:
                            verified = _copy_verified(source, temporary, *expected)
                            verify_archive_mount(root, expected_source)
                            linked = _publish_no_replace(temporary, final)
                            if (not linked or not _matches_published_link(temporary, final, verified)) \
                                    and _digest(final) != expected:
                                raise IntegrityError("Existing NAS recording has different content; refusing overwrite")
                            _fsync_dir(final.parent)
                        except BaseException as error:
                            copy_error = error
                            raise
                        finally:
                            try:
                                temporary.unlink(missing_ok=True)
                            except OSError:
                                # Do not hide the cause needed for transport recovery.
                                # An incomplete temporary name is never a receipt.
                                if copy_error is None:
                                    raise
                        result["archived"] += 1
                    verify_archive_mount(root, expected_source)
                    verified_at = self._clock(now)
                    self.db.execute("UPDATE clips SET verified_at=COALESCE(verified_at,?) WHERE id=?",
                                    (verified_at, row["id"]))
                    self._clear_file_failure("archive", row)
                    self.db.commit()
                    self.db.execute("BEGIN IMMEDIATE")
                except Exception as error:
                    if not isolate_errors or not _isolated_file_error(error):
                        raise
                    result["last_error"] = self._record_file_failure("archive", row, error, now)
                    result["failed"] += 1
            if isolate_errors:
                result.update(self._failure_summary("archive", self._clock(now)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    @_with_progress
    def prune(self, nas_root, now=None, expected_source=None, max_files=None,
              isolate_errors=False):
        if max_files is not None and (type(max_files) is not int or max_files < 1):
            raise ValueError("max_files must be a positive integer or None")
        root = verify_archive_mount(nas_root, expected_source)
        _directory(root)
        result = {"pruned": 0, "kept": 0}
        if isolate_errors:
            result["failed"] = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self._clock(now)
            retained = self.db.execute("SELECT COUNT(*) FROM clips WHERE purged=0").fetchone()[0]
            rows = self.db.execute(
                "SELECT clips.* FROM clips LEFT JOIN clip_failures f "
                "ON f.clip_id=clips.id AND f.operation='prune' "
                "WHERE purged=0 AND verified_at IS NOT NULL AND verified_at<=? "
                "AND (?=0 OR f.retry_after IS NULL OR f.retry_after<=?) ORDER BY id LIMIT ?",
                (current - self.retention_seconds, int(bool(isolate_errors)), current,
                 -1 if max_files is None else max_files)).fetchall()
            for row in rows:
                try:
                    verify_archive_mount(root, expected_source)
                    final = self._archive_path(root, row)
                    if _digest(final) != (row["digest"], row["size"]):
                        raise IntegrityError("NAS verification failed; local retention is unchanged")
                    verify_archive_mount(root, expected_source)
                    local = self._spool_path(row["spool_key"])
                    local.unlink(missing_ok=True)
                    _fsync_dir(local.parent)
                    self.db.execute("UPDATE clips SET purged=1 WHERE id=?", (row["id"],))
                    self._clear_file_failure("prune", row)
                    self.db.commit()
                    result["pruned"] += 1
                    self.db.execute("BEGIN IMMEDIATE")
                except Exception as error:
                    if not isolate_errors or not _isolated_file_error(error):
                        raise
                    result["last_error"] = self._record_file_failure("prune", row, error, now)
                    result["failed"] += 1
            result["kept"] = retained - result["pruned"]
            if isolate_errors:
                result.update(self._failure_summary("prune", self._clock(now)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def status(self):
        row = self.db.execute(STATUS_QUERY).fetchone()
        return dict(row)

    @classmethod
    def read_status(cls, state_dir):
        """Read committed counts without initialization, recovery, or migrations.

        mode=ro reads the active WAL correctly; immutable=1 would ignore recent
        WAL commits. SQLite may use its normal shared-memory WAL coordination.
        """
        directory = _directory(state_dir)
        database = _assert_no_symlinks(directory / "archive.sqlite3")
        if not database.is_file():
            raise FileNotFoundError("Archive ledger has not been initialized")
        _assert_no_symlinks(directory / "archive.sqlite3-wal")
        _assert_no_symlinks(directory / "archive.sqlite3-shm")
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return dict(connection.execute(STATUS_QUERY).fetchone())
        finally:
            connection.close()
