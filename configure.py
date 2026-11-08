#!/usr/bin/env python3
"""Create XpengUSB configuration without putting secrets in shell arguments."""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid


CONFIG_DIR = Path("/etc/xpengusb")
CONFIG_PATH = CONFIG_DIR / "config.json"
SMB_CREDENTIAL_FILES = (CONFIG_DIR / "smb.credentials", CONFIG_DIR / "smb.next.credentials")
FIXED_PATHS = {
    "storage_dir": "/var/lib/xpengusb/storage",
    "camera_image": "/var/lib/xpengusb/storage/cam.img",
    "state_dir": "/var/lib/xpengusb/storage/state",
    "spool_dir": "/var/lib/xpengusb/storage/spool",
    "snapshot_dir": "/var/lib/xpengusb/storage/snapshots",
    "nas_mount": "/mnt/xpengusb-nas",
    "credentials_file": "/etc/xpengusb/smb.credentials",
}


def nas_source(server, share):
    """Accept a hostname/IP and one SMB share; never shell syntax or subpaths."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", server):
        raise ValueError("Use a DNS hostname or IPv4 address for the NAS.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,79}", share):
        raise ValueError("Use the SMB share name only, without slashes or special characters.")
    if share.endswith(" ") or share in {".", ".."}:
        raise ValueError("Invalid SMB share name.")
    return "//" + server + "/" + share


def validate_credential(value, name, allow_empty=False):
    if (not allow_empty and not value) or any(c in value for c in "\r\n\0"):
        raise ValueError(name + " must be nonempty and must not contain line breaks or NUL.")
    return value


def atomic_private_write(path, content):
    """Replace only an ordinary owned config file, with a durable 0600 file."""
    if path.parent.is_symlink() or path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Refusing unexpected configuration path: " + str(path))
    if path.exists() and path.stat().st_uid != os.geteuid():
        raise ValueError("Refusing configuration owned by another user.")
    descriptor, temporary = tempfile.mkstemp(prefix=".xpengusb-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def default_config(existing=None):
    config = dict(FIXED_PATHS)
    config.update({
        "nas_source": "//nas.local/dashcam",
        "archive_device_id": str(uuid.uuid4()),
        "retention_days": 7,
        "interval_seconds": 300,
    })
    if existing:
        for key, value in FIXED_PATHS.items():
            allowed = tuple(path.as_posix() for path in SMB_CREDENTIAL_FILES
                            ) if key == "credentials_file" else (value,)
            if existing.get(key) not in allowed:
                raise ValueError("Existing configuration has an unexpected " + key + ".")
        config["credentials_file"] = existing["credentials_file"]
        config["archive_device_id"] = str(uuid.UUID(existing["archive_device_id"]))
        config["nas_source"] = existing["nas_source"]
        if "nas_directory" in existing:
            config["nas_directory"] = existing["nas_directory"]
    return config


def configure():
    if os.geteuid() != 0:
        raise ValueError("Run with sudo on the Raspberry Pi.")
    if subprocess.run(
        ["systemctl", "is-active", "--quiet", "xpengusb.service", "xpengusb-gadget.service"],
        check=False,
    ).returncode == 0:
        raise ValueError("Safe-eject USB in the car and stop both XpengUSB services before reconfiguring.")
    if CONFIG_DIR.is_symlink():
        raise ValueError("Refusing a symlink at /etc/xpengusb.")
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    if CONFIG_PATH.is_symlink():
        raise ValueError("Refusing a symlink configuration file.")
    existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else None
    config = default_config(existing)
    parts = config["nas_source"][2:].split("/", 1)
    if len(parts) != 2:
        raise ValueError("Invalid existing NAS source.")
    server = input("NAS hostname [" + parts[0] + "]: ").strip() or parts[0]
    share = input("SMB share [" + parts[1] + "]: ").strip() or parts[1]
    config["nas_source"] = nas_source(server, share)
    username = validate_credential(input("NAS username [xpeng]: ").strip() or "xpeng", "Username")
    password = validate_credential(getpass.getpass("NAS password (hidden): "), "Password")
    domain = validate_credential(input("SMB domain (usually blank): ").strip(), "Domain", True)
    credentials = "username=" + username + "\npassword=" + password + "\n"
    if domain:
        credentials += "domain=" + domain + "\n"
    atomic_private_write(Path(config["credentials_file"]), credentials)
    atomic_private_write(CONFIG_PATH, json.dumps(config, indent=2) + "\n")
    print("Saved private configuration. No NAS connection or service start was made.")
    print("Seven-day retention starts after each verified NAS copy. Device ID: " + config["archive_device_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        configure()
    except (OSError, ValueError, KeyError, EOFError, KeyboardInterrupt) as error:
        # Never echo credential/config content in diagnostics.
        if isinstance(error, (EOFError, KeyboardInterrupt)):
            print("Configuration cancelled.")
        elif isinstance(error, (OSError, KeyError, json.JSONDecodeError)):
            print("Configuration failed; check file permissions and existing config.json.")
        else:
            print("Configuration failed: " + str(error))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
