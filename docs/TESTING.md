# Acceptance tests and troubleshooting

This is the route from a software-tested development preview to a trusted installation. Do not mark a stage passed until its physical result has been observed. Use non-sensitive, disposable clips for failure tests.

## 1. Bench test

1. Prepare the Pi using the README. Record `git rev-parse HEAD`, OS release, Pi model and microSD model/capacity.
2. Connect its **USB** data port directly to a computer, using one suitable power source. It should expose one approximately 32 GiB FAT32 drive named `XPENGUSB` with both recording roots. Do not format it in Windows.
3. Copy a valid MP4 into each root, one inside a subfolder, and one with spaces in the filename. Save the source hashes. Wait for two completed capture cycles; check the journal. Five minutes is the delay between cycles, not a guaranteed completion time.
4. Open the NAS share and find `XpengUSB/<device UUID>/XP_DCIM` and `XP_EMER_DCIM`. Compare hashes and play each copied video. The suffix is a content hash, not part of the car's original filename.
5. Replace one file with different content of the same size and restore its original timestamp. Wait for two captures. Both versions must exist on the NAS. Reboot and confirm unchanged clips are not duplicated.
6. Temporarily disconnect the Pi from Wi-Fi, copy another test clip over USB, wait for captures, then restore Wi-Fi. It should upload later. A failed NAS login must leave local footage retained. Test a full/read-only destination using a separate test share, never production data.
7. Verify the Pi time with `timedatectl status`. Unsynchronized time intentionally delays uploads/expiry. Inspect `sudo journalctl -u xpengusb -n 100 --no-pager` for failures without posting credentials.
8. Safely eject in the computer, then shut the Pi down before moving it to the car.

SHA-256 comparison examples (run against source and its NAS copy):

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'C:\path\clip.mp4'
```

```sh
sha256sum -- '/path/to/clip.mp4'
```

## 2. Stationary G6 test

Use the recorder's normal controls while safely parked. Confirm disk recognition, record a normal clip, and manually trigger an emergency clip. Play both in the car, safely eject, and inspect the actual folder/filename layout on a computer if needed. Do not trigger an emergency manoeuvre or collision to test recording.

Record vehicle model/year, region, XOS version and Main View versus Main View + Surrounding View. For the initial target, the owner reports a 2025 G6 running 5.9.5.8906 and a working FAT32 physical USB drive (7 November 2026). That report is not yet a test of this Pi implementation.

## 3. Normal drive and arrival

Take a short ordinary drive without operating diagnostics while driving. On arrival within home Wi-Fi range, confirm new normal and emergency clips reach the NAS and match/play correctly. Then repeat while the backlog is uploading: recording must continue without a USB-too-slow warning, missing intervals or corrupt video. Inspect CPU temperature, kernel USB/storage errors, Wi-Fi strength and elapsed upload times after parking.

The handbook's 10 MB/s minimum is a benchmark target, not a claim this Zero W meets it. If the setup cannot keep up, return to the known-working physical USB drive. Do not improve a benchmark by disabling durability checks or skipping retention safeguards.

## 4. Parking, restart and seven days

- Observe USB power after locking for 15 minutes, one hour and overnight, separately with XGuard off/on if available. Record whether the Pi stays reachable and whether the recorder actually writes. No software timer or vehicle wake control is included.
- Confirm recording resumes after several normal car sleep/wake and Pi restart cycles. The Pi does not repair the live FAT volume while attached; persistent car disk errors require safe ejection and supervised recovery.
- Keep a verified clip on the Pi for seven real days. It must not expire earlier, and must remain if its NAS copy cannot be verified. After expiry the NAS file must still exist and play. Unit tests accelerate a test clock; never alter the production clock to force expiry.
- Measure retained bytes per day and compare them with the actual local budget. Observe a low-space case using an isolated bench configuration. Existing retained footage must survive; new local capture should pause visibly in the journal.
- If testing sudden power loss, use a spare/disposable test installation. Inspect boot, filesystem integrity, archived hashes and the last in-progress clip afterward. No claim of power-loss immunity is made.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No USB drive | Pi USB port rather than PWR IN, data cable, one power source, gadget service, exact supported Pi/OS |
| Recorder unavailable or slow | Return to working USB drive, then bench-test card/cable/write speed and capture load |
| No NAS copies | Two snapshots completed, NTP synchronized, correct server/share/account, SMB 3.1.1 support, NAS quota and hard-link support |
| Copy verification or existing-file mismatch | Preserve both copies; inspect NAS/disk errors; do not delete the local ledger to silence the error |
| Capture low-space messages | Seven-day or unarchived copies consume the budget; upload/reverify eligible files or redesign capacity, not forced eviction |
| Snapshot unmount errors | Stop worker after safe eject, inspect mount/loop users; never delete a mounted image or use lazy unmount |
| Installer says incomplete storage | It deliberately refuses automatic formatting. Save logs and existing storage; reflash only after backing up needed data |

For an update or rollback on stable desk power, first safely eject any USB host, then `sudo systemctl stop xpengusb xpengusb-gadget`. Install the reviewed current/previous checkout with `sudo bash install.sh` and reboot. Stop if a version requires incompatible data migration; do not assume older code can read a newer database.

## Automated validation

Run `python3 -m unittest discover -s tests -v` and Bash syntax/ShellCheck. The CI Linux storage smoke test exercises disposable Btrfs/FAT images and a loopback SMB share. It does not flash the Pi, emulate the car's USB host, measure ARMv6 performance, or validate power loss. Record actual CI results and the tested commit; the existence of a workflow file alone is not a passing test.
