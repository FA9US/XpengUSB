#!/bin/bash
# Fresh Raspberry Pi OS Lite Bookworm installation only. Never partitions a disk.
set -euo pipefail
umask 077

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SOURCE
readonly BASE=/var/lib/xpengusb
readonly STORAGE="$BASE/storage"
readonly CONTAINER="$BASE/storage.btrfs"
readonly LAYOUT="$BASE/storage-layout.json"
readonly IMAGE="$STORAGE/cam.img"
readonly DEST=/usr/local/lib/xpengusb
readonly CONTAINER_BYTES=103079215104  # 96 GiB, physically allocated on ext4
readonly IMAGE_BYTES=34359738368      # 32 GiB camera volume, sparse within Btrfs
readonly RESERVE_BYTES=8589934592     # leave at least 8 GiB on the OS filesystem
readonly FSTAB_ENTRY="$CONTAINER $STORAGE btrfs loop,noatime,nofail 0 0"

die() { printf 'XpengUSB installer: %s\n' "$*" >&2; exit 1; }
usage() {
    printf '%s\n' 'Usage: sudo bash install.sh [--prepare-storage]'
    printf '%s\n' 'First install requires --prepare-storage and at least 104 GiB free on ext4.'
    printf '%s\n' 'Existing storage is never resized, reformatted, or erased.'
}

prepare=false
case ${1:-} in
    --prepare-storage) prepare=true ;;
    --help|-h) usage; exit 0 ;;
    '') ;;
    *) usage; exit 1 ;;
esac
[[ $# -le 1 ]] || { usage; exit 1; }
[[ $EUID -eq 0 ]] || die 'Run with sudo on the Raspberry Pi.'
[[ -r /proc/device-tree/model ]] || die 'This installer only supports the original Raspberry Pi Zero W.'
model=$(tr -d '\0' < /proc/device-tree/model)
[[ $model == 'Raspberry Pi Zero W '* && $(uname -m) == armv6l ]] || die 'Expected original Pi Zero W and 32-bit ARMv6 OS.'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == raspbian && ${VERSION_CODENAME:-} == bookworm ]] || die 'Use Raspberry Pi OS Lite Bookworm (32-bit).'
[[ $(findmnt -rn -o FSTYPE --target /var/lib) == ext4 ]] || die '/var/lib must be on the fresh OS ext4 filesystem.'
[[ -f $SOURCE/xpengusb/runtime.py && -f $SOURCE/xpengusb/archive.py ]] || die 'Run from a complete reviewed XpengUSB checkout.'

for path in "$BASE" "$STORAGE" "$CONTAINER" "$LAYOUT" "$DEST" /etc/xpengusb /mnt/xpengusb-nas; do
    [[ ! -L $path ]] || die "Refusing symlink path: $path"
done
if systemctl is-active --quiet xpengusb.service || systemctl is-active --quiet xpengusb-gadget.service; then
    die 'Safe-eject USB in the car and stop both XpengUSB services before installing an update.'
fi
if [[ -d /sys/kernel/config/usb_gadget/xpengusb ]]; then
    die 'USB gadget still exists. Safe-eject it in the car and stop its service first.'
fi

if [[ -e $LAYOUT ]]; then
    [[ -f $LAYOUT && -f $CONTAINER && -d $STORAGE ]] || die 'Existing storage is incomplete; refusing changes.'
    python3 - "$LAYOUT" <<'PY'
import json, sys
expected = {"format": 1, "container_bytes": 103079215104, "camera_bytes": 34359738368}
try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        actual = json.load(stream)
    if actual != expected:
        raise ValueError()
except (OSError, ValueError):
    sys.exit("Unrecognized storage layout; no storage changes were made.")
PY
    [[ $(stat -c '%s' "$CONTAINER") == "$CONTAINER_BYTES" ]] || die 'Container size differs; refusing changes.'
else
    $prepare || die 'First install requires --prepare-storage. See README before running it.'
    [[ ! -e $CONTAINER && ! -e $IMAGE ]] || die 'Unrecognized or incomplete storage exists; no formatting will be attempted.'
    ! mountpoint -q "$STORAGE" || die 'Storage mountpoint is already in use.'
    if [[ -d $STORAGE && -n $(find "$STORAGE" -mindepth 1 -print -quit) ]]; then
        die 'Storage directory is not empty.'
    fi
    free_bytes=$(python3 -c 'import os; s=os.statvfs("/var/lib"); print(s.f_bavail*s.f_frsize)')
    (( free_bytes >= CONTAINER_BYTES + RESERVE_BYTES )) || die 'Need at least 104 GiB free; use a fresh fully expanded 128 GB card.'
fi

if [[ -f /boot/firmware/config.txt ]]; then
    boot_config=/boot/firmware/config.txt
elif [[ -f /boot/config.txt ]]; then
    boot_config=/boot/config.txt
else
    die 'Cannot locate the Raspberry Pi boot config.txt.'
fi
configure_boot_file() {
python3 - "$boot_config" "$BASE/config.txt.before-install" "$1" <<'XPENGUSB_BOOT_CONFIG'
import os
from pathlib import Path
import re
import sys
import tempfile


def plan_boot_config(text):
    """Apply documented model filters for the original Pi Zero W only.

    Fresh Bookworm includes a CM5-only host-mode dwc2 overlay. Model filters
    replace each other; other filter types can combine, so uncertain active
    overlays/includes require manual review rather than a guessed override.
    https://www.raspberrypi.com/documentation/computers/config_txt.html#conditional-filters
    """
    zero_models = {"pi0", "pi0w"}
    other_models = {"pi1", "pi2", "pi3", "pi3+", "pi4", "pi5", "pi400", "pi500",
                    "cm0", "cm1", "cm3", "cm3+", "cm4", "cm4s", "cm5", "pi02"}
    model_active, uncertain = True, False
    overlays = []
    for number, original in enumerate(text.splitlines(), 1):
        line = original.split("#", 1)[0].strip()
        section = re.fullmatch(r"\[([^\]]+)\]", line)
        if section:
            name = section.group(1).strip()
            if name == "all":
                model_active, uncertain = True, False
            elif name in zero_models:
                model_active = True
            elif name in other_models:
                model_active = False
            else:
                # Includes board-type, GPIO, serial, EDID, tryboot and none.
                # We cannot determine their effective state on a fresh install.
                model_active, uncertain = None, True
            continue
        if model_active is False:
            continue
        if re.match(r"include\s+", line):
            raise ValueError(f"Boot config include on line {number} requires manual review.")
        parameters = re.fullmatch(r"dtparam\s*=\s*(.*)", line)
        if parameters and any(value.split("=", 1)[0].strip() == "dr_mode"
                              for value in parameters.group(1).split(",")):
            # dtparam can alter the most recently loaded overlay. Do not accept
            # a later parameter override merely because dtoverlay looked safe.
            raise ValueError(f"Active or conditional dtparam=dr_mode on line {number} requires manual review.")
        overlay = re.fullmatch(r"dtoverlay\s*=\s*(.*)", line)
        if not overlay:
            continue
        values = [value.strip() for value in overlay.group(1).split(",")]
        if not values or values[0] != "dwc2":
            continue
        if uncertain or model_active is None:
            raise ValueError(f"Conditional dwc2 overlay on line {number} requires manual review.")
        if values != ["dwc2", "dr_mode=peripheral"]:
            raise ValueError(f"Active Zero W dwc2 overlay on line {number} must use only dr_mode=peripheral.")
        overlays.append(number)
    if len(overlays) > 1:
        raise ValueError("Multiple active Zero W dwc2 overlays require manual review.")
    if overlays:
        return text
    return (text.rstrip("\n") + "\n\n# XpengUSB USB peripheral mode\n"
            "[all]\n[pi0w]\ndtoverlay=dwc2,dr_mode=peripheral\n[all]\n")


def configure_boot(path, backup):
    if path.is_symlink() or not path.is_file() or backup.is_symlink():
        raise ValueError("Refusing unexpected boot configuration or backup path.")
    original = path.read_bytes()
    updated = plan_boot_config(original.decode("utf-8")).encode("utf-8")
    if updated == original:
        print("Zero W USB peripheral boot configuration already correct.")
        return
    if not backup.exists():
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    elif not backup.is_file():
        raise ValueError("Boot configuration backup is not a regular file.")
    fd, temporary = tempfile.mkstemp(prefix=".xpengusb-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Configured USB peripheral mode for the original Pi Zero W.")


if __name__ == "__main__":
    try:
        path, backup = Path(sys.argv[1]), Path(sys.argv[2])
        if path.is_symlink() or not path.is_file() or backup.is_symlink():
            raise ValueError("Refusing unexpected boot configuration or backup path.")
        if sys.argv[3] == "--check":
            plan_boot_config(path.read_text(encoding="utf-8"))
        elif sys.argv[3] == "--apply":
            configure_boot(path, backup)
        else:
            raise ValueError("Invalid boot configuration operation.")
    except (OSError, UnicodeError, ValueError) as error:
        raise SystemExit("Boot configuration unchanged or requires inspection: " + str(error))
XPENGUSB_BOOT_CONFIG
}

# Validate boot settings before package, storage, or boot-file changes.
configure_boot_file --check

# Reject conflicting fstab configuration before making any changes.
python3 - "$CONTAINER" "$STORAGE" "$FSTAB_ENTRY" <<'PY'
from pathlib import Path
import sys
path = Path("/etc/fstab")
if path.is_symlink():
    sys.exit("Refusing symlink /etc/fstab.")
for line in path.read_text().splitlines():
    fields = line.split()
    if len(fields) >= 2 and not fields[0].startswith("#"):
        if fields[0] == sys.argv[1] or fields[1] == sys.argv[2]:
            if line.strip() != sys.argv[3]:
                sys.exit("Conflicting XpengUSB fstab entry; inspect it before installing.")
PY

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y btrfs-progs dosfstools util-linux fdisk cifs-utils python3 avahi-daemon
install -d -m 0700 "$BASE" "$STORAGE" /etc/xpengusb
# Do not chmod an existing NAS mount: that could change permissions on the share.
if [[ ! -e /mnt/xpengusb-nas ]]; then
    install -d -m 0700 /mnt/xpengusb-nas
else
    [[ -d /mnt/xpengusb-nas ]] || die 'NAS mount path is not a directory.'
fi

mounted_by_installer=false
image_loop=''
image_mount=''
cleanup() {
    local status=$?
    if [[ -n $image_mount ]] && mountpoint -q "$image_mount"; then
        umount "$image_mount" || true
    fi
    if [[ -n $image_loop ]]; then
        losetup -d "$image_loop" || true
    fi
    if [[ -n $image_mount ]]; then
        rmdir "$image_mount" 2>/dev/null || true
    fi
    if (( status != 0 )); then
        printf '%s\n' 'Installation stopped. Existing files were retained. Services were not started.' >&2
        if $mounted_by_installer; then
            umount "$STORAGE" || true
        fi
    fi
}
trap cleanup EXIT

if [[ ! -e $LAYOUT ]]; then
    free_bytes=$(python3 -c 'import os; s=os.statvfs("/var/lib"); print(s.f_bavail*s.f_frsize)')
    (( free_bytes >= CONTAINER_BYTES + RESERVE_BYTES )) || die 'Package installation left insufficient free space for storage and the 8 GiB OS reserve.'
    printf '%s\n' 'Allocating 96 GiB for XpengUSB storage; this may take several minutes.'
    # noclobber prevents accidentally opening a file that appeared since preflight.
    (set -o noclobber; : > "$CONTAINER")
    chmod 0600 "$CONTAINER"
    fallocate -l "$CONTAINER_BYTES" "$CONTAINER"
    allocated=$(( $(stat -c '%b' "$CONTAINER") * 512 ))
    (( allocated >= CONTAINER_BYTES )) || die 'Storage container was not fully allocated.'
    mkfs.btrfs --label xpengusb-storage "$CONTAINER"
    mount -t btrfs -o loop,noatime "$CONTAINER" "$STORAGE"
    mounted_by_installer=true
    chmod 0700 "$STORAGE"
    install -d -m 0700 "$STORAGE/state" "$STORAGE/spool" "$STORAGE/snapshots"
    (set -o noclobber; : > "$IMAGE")
    chmod 0600 "$IMAGE"
    truncate -s "$IMAGE_BYTES" "$IMAGE"
    printf 'label: dos\nstart=2048, type=c\n' | sfdisk "$IMAGE"
    image_loop=$(losetup --find --show --partscan "$IMAGE")
    udevadm settle --timeout=10
    [[ -b ${image_loop}p1 ]] || die 'Camera partition device did not appear.'
    mkfs.vfat -F 32 -n XPENGUSB "${image_loop}p1"
    image_mount=$(mktemp -d /run/xpengusb-format.XXXXXXXX)
    mount -t vfat -o uid=0,gid=0,umask=0077 "${image_loop}p1" "$image_mount"
    mkdir "$image_mount/XP_DCIM" "$image_mount/XP_EMER_DCIM"
    sync
    umount "$image_mount"
    rmdir "$image_mount"
    image_mount=''
    losetup -d "$image_loop"
    image_loop=''
    printf '{"format":1,"container_bytes":103079215104,"camera_bytes":34359738368}\n' > "$LAYOUT"
    chmod 0600 "$LAYOUT"
    sync
elif ! mountpoint -q "$STORAGE"; then
    mount -t btrfs -o loop,noatime "$CONTAINER" "$STORAGE"
    mounted_by_installer=true
fi

[[ $(findmnt -rn -o FSTYPE --mountpoint "$STORAGE") == btrfs ]] || die 'Unexpected mounted filesystem.'
storage_loop=$(findmnt -rn -o SOURCE --mountpoint "$STORAGE")
[[ $(losetup -n -O BACK-FILE "$storage_loop") == "$CONTAINER" ]] || die 'Storage mount uses an unexpected backing file.'
[[ -f $IMAGE && ! -L $IMAGE && $(stat -c '%s' "$IMAGE") == "$IMAGE_BYTES" ]] || die 'Camera image failed validation.'
[[ -z $(losetup -j "$IMAGE") ]] || die 'Original camera image still has a local loop device.'
for directory in state spool snapshots; do
    [[ -d $STORAGE/$directory && ! -L $STORAGE/$directory ]] || die "Missing or unsafe storage directory: $directory"
done

# Append only our exact mount entry, atomically, retaining the original for rollback.
python3 - "$BASE" "$FSTAB_ENTRY" <<'PY'
import os, shutil, sys, tempfile
from pathlib import Path
path = Path("/etc/fstab")
text = path.read_text()
entry = sys.argv[2]
if entry not in text.splitlines():
    backup = Path(sys.argv[1]) / "fstab.before-install"
    if not backup.exists():
        shutil.copyfile(path, backup)
        backup.chmod(0o600)
    fd, temporary = tempfile.mkstemp(prefix=".xpengusb-fstab-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
            stream.write(text.rstrip("\n") + "\n" + entry + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
PY

configure_boot_file --apply
printf 'libcomposite\n' > /etc/modules-load.d/xpengusb.conf

install -d -m 0755 "$DEST" "$DEST/xpengusb" "$DEST/scripts"
find "$SOURCE/xpengusb" -maxdepth 1 -type f -name '*.py' -exec install -m 0644 '{}' "$DEST/xpengusb/" \;
install -m 0755 "$SOURCE/configure.py" "$DEST/configure.py"
install -m 0755 "$SOURCE/scripts/gadget.sh" "$DEST/scripts/gadget.sh"
install -m 0644 "$SOURCE/systemd/xpengusb-gadget.service" "$SOURCE/systemd/xpengusb.service" /etc/systemd/system/
if [[ ! -f /etc/xpengusb/config.json || ! -f /etc/xpengusb/smb.credentials ]]; then
    [[ -t 0 ]] || die 'Run interactively to enter NAS credentials privately.'
    python3 "$DEST/configure.py"
fi
(
    cd "$DEST"
    python3 - <<'PY'
import json, os, stat
from pathlib import Path
from xpengusb.runtime import Config
path = Path("/etc/xpengusb/config.json")
if path.is_symlink() or not path.is_file():
    raise SystemExit("Configuration file must be an ordinary private file.")
for item in (path, Path("/etc/xpengusb/smb.credentials")):
    info = item.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise SystemExit("Configuration and credentials must be root-owned with mode 0600.")
try:
    Config.from_dict(json.loads(path.read_text(encoding="utf-8")))
except (ValueError, TypeError, KeyError) as error:
    raise SystemExit("Existing configuration is invalid; run configure.py before retrying.") from error
PY
)
systemctl daemon-reload
systemctl enable xpengusb-gadget.service xpengusb.service
sync
printf '%s\n' 'Installed. Services are enabled for the next boot; neither was started.'
printf '%s\n' 'Keep the Pi out of the car. Reboot on desk power, then follow the README bench tests.'
