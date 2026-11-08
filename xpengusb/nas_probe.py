"""One-shot SMB check in a private mount namespace."""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import uuid

from .runtime import read_mounts


def mount_problem(stderr):
    if "error(13)" in stderr or "error(126)" in stderr:
        return "The NAS rejected the login or denied access to this share. Check the account, password and domain."
    if "error(2)" in stderr or "BAD_NETWORK_NAME" in stderr:
        return "The SMB share was not found. Check the share name."
    if "error(95)" in stderr:
        return "The NAS did not accept SMB 3.1.1. Check the NAS SMB protocol settings."
    if "error(30)" in stderr:
        return "The SMB share is read-only."
    if "error(110)" in stderr or "error(113)" in stderr or "error(111)" in stderr:
        return "The Pi could not reach SMB on the NAS. Check that the NAS is on and TCP port 445 is reachable."
    return "The NAS could not be mounted over SMB 3.1.1. Check the share settings and SMB server log."


def check(request):
    source = request["source"]
    server = source[2:].split("/", 1)[0]
    try:
        socket.getaddrinfo(server, 445, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError("The Pi could not resolve the NAS hostname.") from error
    with tempfile.TemporaryDirectory(prefix="xpengusb-nas-test-", dir="/run") as work:
        mount = Path(work) / "share"
        mount.mkdir()
        credentials = Path(work) / "credentials"
        with credentials.open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(request["credentials"])
        result = subprocess.run(
            ["mount", "-t", "cifs", source, str(mount), "-o",
             f"credentials={credentials},vers=3.1.1,sec=ntlmssp,nodev,nosuid,noexec,cache=none"],
            text=True, capture_output=True, timeout=20, check=False,
        )
        if result.returncode:
            raise ValueError(mount_problem(result.stderr))
        try:
            mounts = [entry for entry in read_mounts() if entry.target == mount]
            if (len(mounts) != 1 or mounts[0].source != source or mounts[0].fstype != "cifs"
                    or "rw" not in mounts[0].options):
                raise ValueError("The Pi did not see the expected writable SMB mount.")
            target = mount
            parts = request["directory"].split("/") if request["directory"] else []
            for part in parts:
                target = target / part
                if target.is_symlink() or not target.is_dir():
                    raise ValueError("The configured NAS subfolder is missing or inaccessible. Check the path.")
            for part in ("XpengUSB", request["device_id"]):
                candidate = target / part
                if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
                    raise ValueError("The XpengUSB archive path is blocked by a file or link.")
                if not candidate.is_dir():
                    break
                target = candidate
            path = target / (".xpengusb-test-" + uuid.uuid4().hex)
            linked = path.with_name(path.name + "-link")
            created = []
            try:
                with path.open("xb") as stream:
                    created.append(path)
                    stream.write(b"XpengUSB test\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(path, linked)
                except OSError as error:
                    raise ValueError("The NAS rejected hard links, which verified XpengUSB uploads require.") from error
                created.append(linked)
                with linked.open("rb") as stream:
                    if stream.read(32) != b"XpengUSB test\n":
                        raise ValueError("The NAS test file could not be read back correctly.")
            except OSError as error:
                if error.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise ValueError("The NAS is out of space or the account quota is full.") from error
                if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                    raise ValueError("SMB login worked, but the account cannot write to the destination.") from error
                raise ValueError("The NAS test file could not be written or read back.") from error
            finally:
                cleanup_failed = False
                for temporary in reversed(created):
                    try:
                        temporary.unlink()
                    except OSError:
                        cleanup_failed = True
                if cleanup_failed:
                    raise ValueError("The test file was created but cleanup failed. Check delete permissions.")
        finally:
            subprocess.run(["umount", "--", str(mount)], check=True, timeout=10, capture_output=True)
    return "NAS login and destination passed. Write, hard link, readback and cleanup worked."


if __name__ == "__main__":
    os.umask(0o077)
    try:
        result = {"message": check(json.loads(sys.stdin.read(8192)))}
    except ValueError as error:
        result = {"error": str(error)}
    except (OSError, subprocess.SubprocessError):
        result = {"error": "The NAS check could not finish. Check connectivity and try again."}
    print(json.dumps(result), flush=True)
