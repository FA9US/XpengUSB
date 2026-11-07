#!/bin/bash
# Derived from marcone/teslausb run/enable_gadget.sh and disable_gadget.sh.
# Original MIT license and history are preserved in this repository.
set -euo pipefail
umask 077

readonly STORAGE=/var/lib/xpengusb/storage
readonly IMAGE="$STORAGE/cam.img"
readonly GADGET=/sys/kernel/config/usb_gadget/xpengusb

die() { printf 'XpengUSB gadget: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die 'Run as root.'
[[ $# -eq 1 && ($1 == start || $1 == stop) ]] || die 'Usage: gadget.sh start|stop'

if [[ $1 == stop ]]; then
    [[ -d $GADGET ]] || exit 0
    # Do not delete any part of a gadget until the UDC is definitely unbound.
    if [[ -n $(cat "$GADGET/UDC") ]]; then
        printf '\n' > "$GADGET/UDC"
    fi
    [[ -z $(cat "$GADGET/UDC") ]] || die 'Could not unbind USB; leaving gadget intact.'
    sync
    [[ ! -L $GADGET/configs/c.1/mass_storage.0 ]] || rm "$GADGET/configs/c.1/mass_storage.0"
    [[ ! -d $GADGET/functions/mass_storage.0 ]] || rmdir "$GADGET/functions/mass_storage.0"
    [[ ! -d $GADGET/configs/c.1/strings/0x409 ]] || rmdir "$GADGET/configs/c.1/strings/0x409"
    [[ ! -d $GADGET/configs/c.1 ]] || rmdir "$GADGET/configs/c.1"
    [[ ! -d $GADGET/strings/0x409 ]] || rmdir "$GADGET/strings/0x409"
    rmdir "$GADGET"
    exit 0
fi

[[ $(findmnt -rn -o FSTYPE --mountpoint "$STORAGE") == btrfs ]] || die 'Storage is not mounted as Btrfs.'
[[ -f $IMAGE && ! -L $IMAGE ]] || die 'Camera image is missing or unsafe.'
[[ $(stat -c '%s' "$IMAGE") == 34359738368 ]] || die 'Unexpected camera image size.'
[[ -z $(losetup -j "$IMAGE") ]] || die 'Original camera image is attached to a local loop device; refusing dual access.'
modprobe libcomposite
[[ $(findmnt -rn -o FSTYPE --mountpoint /sys/kernel/config) == configfs ]] || die 'configfs is not mounted.'

if [[ -d $GADGET ]]; then
    if [[ -n $(cat "$GADGET/UDC") ]] && [[ $(cat "$GADGET/functions/mass_storage.0/lun.0/file") == "$IMAGE" ]]; then
        printf 'XpengUSB gadget already connected; leaving it untouched.\n'
        exit 0
    fi
    die 'An incomplete gadget exists; safe-eject the car, then stop the gadget service before retrying.'
fi

mapfile -t controllers < <(find /sys/class/udc -mindepth 1 -maxdepth 1 -type l -printf '%f\n')
[[ ${#controllers[@]} -eq 1 ]] || die 'Expected exactly one USB device controller.'
[[ -z $(find /sys/kernel/config/usb_gadget -mindepth 1 -maxdepth 1 -type d -print -quit) ]] || die 'Another USB gadget is configured.'

mkdir "$GADGET"
printf '0x1d6b\n' > "$GADGET/idVendor"
printf '0x0104\n' > "$GADGET/idProduct"
printf '0x0100\n' > "$GADGET/bcdDevice"
printf '0x0200\n' > "$GADGET/bcdUSB"
mkdir "$GADGET/strings/0x409"
serial=$(sha256sum /etc/machine-id)
printf 'XpengUSB-%s\n' "${serial:0:24}" > "$GADGET/strings/0x409/serialnumber"
printf 'XpengUSB\n' > "$GADGET/strings/0x409/manufacturer"
printf 'XpengUSB Camera\n' > "$GADGET/strings/0x409/product"
mkdir "$GADGET/configs/c.1"
mkdir "$GADGET/configs/c.1/strings/0x409"
printf 'XpengUSB camera storage\n' > "$GADGET/configs/c.1/strings/0x409/configuration"
printf '250\n' > "$GADGET/configs/c.1/MaxPower"
mkdir "$GADGET/functions/mass_storage.0"
printf '0\n' > "$GADGET/functions/mass_storage.0/lun.0/ro"
printf '1\n' > "$GADGET/functions/mass_storage.0/lun.0/removable"
# Respect host FUA requests; do not trade recording durability for throughput.
printf '0\n' > "$GADGET/functions/mass_storage.0/lun.0/nofua"
printf 'XpengUSB CAMERA 0001\n' > "$GADGET/functions/mass_storage.0/lun.0/inquiry_string"
printf '%s\n' "$IMAGE" > "$GADGET/functions/mass_storage.0/lun.0/file"
ln -s "$GADGET/functions/mass_storage.0" "$GADGET/configs/c.1/mass_storage.0"
printf '%s\n' "${controllers[0]}" > "$GADGET/UDC"
printf 'XpengUSB camera USB gadget connected.\n'
