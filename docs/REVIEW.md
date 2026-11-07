# Review record

Review date: **7 November 2026**. Upstream baseline: [`fb4ed15b46dc41a2ec7c8a7a2fe0783ce1d2ffd9`](https://github.com/marcone/teslausb/tree/fb4ed15b46dc41a2ec7c8a7a2fe0783ce1d2ffd9).

## Scope and resulting implementation

The review inventoried all 146 tracked upstream files, covering the archive loop and backends; snapshots, cleanup and gadget control; OS/bootstrap/image preparation; web/CGI/JavaScript/C++ viewer; vehicle APIs and notification helpers; maintenance tools; documentation, tests and CI. Parallel reviews covered storage/data integrity, installation/security, and UI/auxiliary code. This is a source review and targeted validation, not a formal audit or proof that defects cannot remain.

Blind brand replacement would leave incompatible directory, filename, deletion and vehicle-control assumptions. The maintained tree therefore contains a smaller G6-specific implementation. Upstream source remains available in full git history, with its MIT license unchanged. Removed optional capabilities are not advertised as supported.

| Finding in upstream baseline | Evidence | Treatment in XpengUSB |
| --- | --- | --- |
| Tesla root/category/date/camera assumptions | `run/make_snapshot.sh`, `run/archiveloop`, viewer `index.html` | Exact two Xpeng roots, recursive arbitrary filenames, raw MP4 copies |
| Snapshot equality uses size/path; archive ledger uses names | `run/make_snapshot.sh`, `run/archiveloop` | SHA-256 content identity and distinct immutable NAS names |
| Low-space cleanup deletes old snapshots without seven-day verified-copy policy | `run/manage_free_space.sh`, `run/archiveloop:355` onward | Separate retained spool; verified-success age and NAS recheck before expiry; pause capture under pressure |
| Filename-derived shell evaluation and fragile whitespace handling | `run/archiveloop:493`, cleanup around `:372` | Python path handling, subprocess argument arrays, no filename `eval` |
| Repair failures ignored; lazy unmount fallback | `run/archiveloop:101`, `:213` onward | Repair only private snapshots; clean follow-up check, strict unmount/detach |
| CIFS credentials insufficiently protected; mount assembled with eval; fixed testfile deleted | `run/cifs_archive/verify-and-configure-archive.sh:54`, `:79`, `:94` onward | Private prompt/0600 credential file, explicit SMB3 mount args, no fixed NAS probe file |
| Headless resize can silently reformat existing camera image | `setup/pi/create-backingfiles.sh:300` onward | Fresh-storage flag, no repartitioning or automatic reformat/resize |
| Moving bootstrap and third-party binaries | `setup/pi/setup-teslausb`, `configure-web.sh:49`, `configure.sh` | Local reviewed checkout, distro dependencies, no remote script execution at boot |
| Unauthenticated web controls plus broad passwordless sudo; path traversal/HTML injection surface | `configure-web.sh:71`, nginx config, CGI file handlers, `filebrowser.js` | Web stack removed from maintained runtime |
| Tesla viewer changes MP4 bytes and has unsafe zero-size box parsing | `teslausb-www/cttseraser.cpp` | No MP4 byte transformations; bounded outer-box validity check only |
| Vehicle wake/control and notification credentials unrelated to G6 USB archiving | `run/tesla_api.py`, `awake_*`, notification scripts | Removed; no speculative Xpeng API integration |
| Default login/Samba password and optional network servers | `pi-gen-config`, `configure-samba.sh` | Owner creates OS login; no Samba server or access point on Pi |
| CI omits much of executable code and archive failure cases | `check.sh`, old workflow/tests | Python fault tests, all maintained shell scripts linted, isolated Linux storage/SMB test |

## Validation levels

- **Owner report:** a physical FAT32 USB drive records in the G6; `XP_DCIM` and `XP_EMER_DCIM` are the roots; home Wi-Fi reaches the parked car. Exact subfolder/file samples, recorded bitrate, power behavior and XGuard storage behavior have not been independently observed on this vehicle.
- **Source implementation:** two-snapshot content stability, atomic no-overwrite publication, verification receipts, seven-day local expiry, no finished NAS-file deletion, private configuration and conservative disk reserve.
- **Offline tests:** test content rollover, interrupted/readback-failed copy, recovery after publication before ledger insertion, later-file failure, retention boundary, clock rollback, absent/corrupt NAS data, path rejection, controller mount/unmount failure, and configuration boundaries.
- **Linux integration:** workflow runs actual Btrfs/FAT/reflink and CIFS operations in a disposable runner. Consult the Actions run for the exact source commit and actual result; workflow presence alone is not verification.
- **Not yet verified:** flashing/boot on original Zero W; USB enumeration and sustained 10 MB/s performance; G6 recording/playback under archive load; exact XOS behavior; actual NAS compatibility; overnight power; seven elapsed days and unexpected-power recovery.

## Recorded automated result

On 7 November 2026, [Actions run 42366444368](https://github.com/FA9US/XpengUSB/actions/runs/42366444368) passed for code commit [`30b5c24`](https://github.com/FA9US/XpengUSB/commit/30b5c24f094e914cb48f252191e4c779edaa4464):

- All **74 tests passed** on each of Python 3.9, 3.11 and 3.12, with no skips on Linux.
- Bash syntax checks and ShellCheck passed for both maintained shell scripts.
- The disposable Linux integration passed real Btrfs reflinks, snapshot-only FAT repair and read-only mounts, SMB 3.1.1 publication/readback, filename reuse, seven-day expiry boundaries, and missing/wrong-mount guards.

The integration exposed and helped fix an invalid GNU `cp` option combination: reflinks require `--sparse=auto`, not `--sparse=always`. It also required an isolated Samba fixture correction. These results cover the source and disposable runner, not a flashed Pi, the owner's NAS, seven elapsed days, or the vehicle.

The subsequent installer review found that the [official Bookworm boot configuration](https://github.com/RPi-Distro/pi-gen/blob/bookworm/stage1/00-boot-files/files/config.txt) includes a CM5-only USB host overlay. The installer now evaluates Zero W model filters before allocating storage, preserves inactive model settings, and rejects active or ambiguous conflicts. Ten additional regression tests cover this behavior, bringing the suite to **84 tests**. Consult the [latest Actions result](https://github.com/FA9US/XpengUSB/actions/workflows/check.yml) for validation of the current revision.

## Remaining engineering limits

1. No finite card can promise arbitrary seven-day recording volume. The conservative 128 GB layout has a limited retained-copy budget. See [architecture](ARCHITECTURE.md).
2. Active-image snapshots can miss unwritten/incomplete clips; two matching snapshots and MP4 boxes are not a media-decoder test. Short-lived loop files may disappear before ingestion.
3. Full content hashing may strain the original Pi Zero W. Measure complete capture and upload times while recording before accepting this design.
4. The OS remains writable. Power interruption can still damage state or recordings. There is no UPS or automatically proven restore procedure.
5. SMB 3.1.1 and hard-link publication must work on the actual NAS. No weaker protocol fallback is attempted.
6. Kernel FAT/CIFS/USB handling, SD wear, filesystem damage and physical power are outside the pure-Python tests. Root is needed for the controlled mount/gadget worker; there is no remotely exposed root web service.

Use the [acceptance checklist](TESTING.md) to close these gaps. Describe this revision as a development preview until that evidence exists.
