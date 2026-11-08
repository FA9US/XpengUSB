# XpengUSB

**Automatic USB recording backup for the XPENG G6, built for a Raspberry Pi Zero W.**

Record in the car. Come home to Wi-Fi. Keep verified copies on your NAS. including webGui en Hacs integration to home-Assistent.

> **Development preview — vehicle testing required.** This is an independent adaptation of [TeslaUSB](https://github.com/marcone/teslausb), not an XPENG product. The target is a 2025 G6 with XOS 5.9.5.8906, an original Pi Zero W and a 128 GB microSD card. Software tests do not establish USB compatibility or recording reliability. Keep your working USB drive available until the [acceptance tests](docs/TESTING.md) pass.

## What it does

- Presents one **32 GiB FAT32 USB drive**, with `XP_DCIM` and `XP_EMER_DCIM` at its root.
- Copies stable files from both folders, including subfolders, without assuming a particular filename or camera naming scheme.
- Uses independent snapshots while the car continues recording. It never mounts or edits the live camera filesystem on the Pi.
- Copies footage over home Wi-Fi to an **SMB 3 NAS share**, and reads it back to verify its SHA-256 checksum.
- Keeps a separate local copy for **at least seven days after successful NAS verification**. Local expiry also requires the NAS copy to verify again.
- Never automatically expires recordings on the NAS. Different footage with a reused filename gets a different archive name.

There is no vehicle login, wake command, subscription, cloud upload, web server or default password. Browse ordinary MP4 files directly on your NAS. XGuard footage held only in the car's internal storage cannot be retrieved through this USB connection.

## What you need

| Item | Requirement |
| --- | --- |
| Board | Original Raspberry Pi Zero W (ARMv6); other boards are not supported by this installer yet |
| Storage | 128 GB high-endurance microSD with a fresh OS; at least 104 GiB free before setup |
| Cable | USB **data** cable, from the car's recorder port to the Pi's **USB** micro-USB port, not `PWR IN` |
| Wi-Fi | Home 2.4 GHz Wi-Fi reaching the parked car; configure before installation |
| NAS | Existing SMB 3 share and dedicated account allowed to create, read, rename and hard-link files |
| OS | Raspberry Pi OS Lite **32-bit Bookworm**; do not select 64-bit, Bullseye or Trixie for this version |

The G6 handbook specifies at least 32 GB and 10 MB/s write speed. Test the actual Pi, card and cable, especially while snapshots and uploads run. See [XPENG's model/year-specific manuals](https://www.xpeng.com/user-manual).

## Set up your Pi

### 1. Flash a fresh operating system

Back up the microSD card. Flash **Raspberry Pi OS Lite (Legacy, 32-bit), Debian Bookworm** using [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Check the Debian version: menu labels can change. If Bookworm is absent in Imager, use the vendor's [manual downloads](https://www.raspberrypi.com/software/operating-systems/): choose **Legacy, 32-bit → Lite**, confirm **Debian 12 (bookworm)** and verify the published SHA-256 before selecting that image in Imager. Do not flash an upstream TeslaUSB image.

In Imager, choose hostname `xpengusb`, create your own login/password or SSH key, enable SSH, and enter your home **2.4 GHz** Wi-Fi and country. Boot on a stable wall supply, away from the car, and let filesystem expansion finish.

### 2. Connect and download

From your computer's terminal, replace `YOUR_USER` with the username you chose:

```sh
ssh YOUR_USER@xpengusb.local
cat /etc/os-release
uname -m
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/FA9US/XpengUSB.git
cd XpengUSB
```

Check for `VERSION_CODENAME=bookworm` and architecture `armv6l`. Read the [storage design](docs/ARCHITECTURE.md) before installation. Record `git rev-parse HEAD` with test results so the tested revision is known.

### 3. Prepare storage and enter NAS details privately

```sh
sudo bash install.sh --prepare-storage
sudo reboot
```

The installer creates its own 96 GiB storage container and 32 GiB camera image; it does not repartition the card. It refuses to reformat existing storage. Its wizard asks for the NAS server, share, username and password. Enter the password only at its hidden prompt. Keep credentials out of GitHub and issues. To change those details later, safely eject the USB host, stop both services (`sudo systemctl stop xpengusb xpengusb-gadget`), run `sudo python3 /usr/local/lib/xpengusb/configure.py`, then reboot.

For a Windows share written as `\\nas.local\dashcam`, enter **server** `nas.local` and **share** `dashcam`, separately. SMB and CIFS refer to the same client path here; SMB 3 is required, with no SMB 1 fallback. The share must already exist. XpengUSB does not configure the NAS.

### 4. Check the Pi before connecting the car

Reconnect over SSH after reboot:

```sh
systemctl status xpengusb-gadget xpengusb --no-pager
sudo journalctl -u xpengusb -n 50 --no-pager
```

Follow the [bench test](docs/TESTING.md) using a computer as USB host. Confirm files reach the intended share with matching hashes, then safely eject. Empty footage folders alone do not prove uploading works.

### 5. Test in the G6

Connect the Pi's **USB** port to the car's Driving Recorder data port. Confirm disk recognition. Record and play a normal clip and a manually triggered emergency clip. Park within home Wi-Fi range and check both folders on the NAS.

Complete the [staged car tests](docs/TESTING.md), including recording during upload, overnight power, and a failed Wi-Fi/NAS connection. A powered port does not prove the recorder is working. Use the car's **Safe Eject** before disconnecting.

## Understand the seven-day limit

Seven days refers to the Pi's **separate retained copies**, measured from first successful NAS verification, not the car's playback list. The car can loop-delete its own files sooner. Files not verified on the NAS do not qualify for local expiry.

A 128 GB card has finite capacity. The layout reserves space for the OS and a complete camera-volume rewrite; the retained-copy budget is smaller than the card. When that budget is exhausted, XpengUSB pauses capture into the retained-copy store and logs the problem. It does not silently delete unarchived or younger-than-seven-day retained files. The car can still overwrite recordings that were never captured. Measure daily recording volume during testing; seven days of heavy or continuous use may require different storage/hardware.

“Indefinite NAS retention” means **XpengUSB does not delete finished NAS recordings**. NAS quotas, capacity, snapshots, independent backups and administrator actions still matter.

## Help, updates and recovery

- [Testing and troubleshooting](docs/TESTING.md)
- [Storage, archive layout and failure handling](docs/ARCHITECTURE.md)
- [Code review and validation boundaries](docs/REVIEW.md)
- [Upstream provenance and license](NOTICE.md)

For updates, safely eject from the car, shut down, move the Pi to stable wall power, back up its configuration/storage, and review the new revision. Stop the worker and gadget (`sudo systemctl stop xpengusb xpengusb-gadget`) before running `sudo bash install.sh` from the reviewed checkout, then reboot. This updates code without recreating storage. Keep the old checkout for rollback. Never use `--prepare-storage` to resize or erase an existing installation. To restore the previous recorder, safely eject XpengUSB and reconnect your known-working USB drive. Imported upstream tags are historical TeslaUSB tags, not XpengUSB SD-card images.

## Development

For phone access after the base setup, see the optional [dashboard and fallback hotspot](docs/DASHBOARD.md).

For backup controls, recorder status and delayed home Wi-Fi arrival alerts, see the [Home Assistant and HACS setup](docs/HOME_ASSISTANT.md).

```sh
python3 -m unittest discover -s tests -v
bash -n install.sh scripts/gadget.sh
```

The [CI workflow](.github/workflows/check.yml) includes a disposable Linux storage test. Neither mocks nor loop-device tests substitute for a G6 and Zero W test. Include source commit, Pi model, OS, XOS version, recorder mode and redacted logs in issues. Do not include credentials or private footage.

## Credits

History from `marcone/teslausb` through `fb4ed15b46dc41a2ec7c8a7a2fe0783ce1d2ffd9` is preserved. Its USB gadget and snapshot approach informs this adaptation. The original MIT [LICENSE](LICENSE) is unchanged. Original copyright and historical Tesla references remain in provenance and git history; current installation and runtime use XpengUSB names.
