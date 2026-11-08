# Optional Pi dashboard

Install this add-on after the base XpengUSB setup works. It runs on the original Zero W without a second radio, container, web framework or remote account. The recorder and archive work without the dashboard.

1. Safely eject the recorder, shut down, and move the Pi to desk power. Connect it to home Wi-Fi.
2. From the reviewed checkout, run:

   ~~~bash
   sudo bash install-dashboard.sh
   ~~~

   At the hidden prompt, enter the initial WPA password for the **XpengUSB** hotspot. The installer creates a separate random dashboard password. Read it in your private terminal with `sudo cat /etc/xpengusb-web/password`. The username is **admin**.
3. On home Wi-Fi, open **https://xpengusb.local:8443**. In the car, join **XpengUSB** and open **https://10.42.0.1:8443**. If your phone asks whether to stay connected to a network without internet, stay connected. Use the certificate fingerprint printed at installation to verify the local self-signed certificate before entering credentials. The Pi's IP can also be used when local hostname resolution is unavailable, with the same fingerprint check. Do not expose port 8443 to the internet.

The dashboard shows system disk space, Btrfs archive space, and the USB volume space measured on the last read-only snapshot. It reports the number of MP4s observed in that snapshot, files queued, files verified on the NAS, and the most recent capture and sync results. A freshly booted Pi can need a synchronized clock and a completed worker cycle before it reports a successful NAS check.

When the Pi loses its Wi-Fi connection, NetworkManager starts the fallback hotspot after the boot grace period or at the next network check. The original Zero W switches its one radio between client and hotspot mode. While no phone is connected, it retries the configured home profile about every three minutes. **Retry home Wi-Fi** attempts it sooner and can disconnect your phone. If the attempt fails, reconnect to the hotspot. Saving a hotspot profile while connected can briefly disconnect the browser. Enter a WPA password each time you save a Wi-Fi profile. Use a 2.4 GHz WPA2 network for the home profile. The installer preserves the original NetworkManager connection as a recovery path.

NAS settings accept a server, SMB share, optional relative subfolder and SMB account. For example, server `nas.local`, share `dashcam`, and subfolder `cars` use `//nas.local/dashcam/cars/XpengUSB/<device-id>`. A blank password keeps the saved password only for the same share and account. Changing the destination affects pending files and future recordings. Existing NAS files and their receipts stay associated with the prior backups. The Pi keeps a local copy whenever retention cannot verify a NAS file at the configured location.

Pushover is opt-in. Enter an application token and user key privately in the dashboard. The optional device field targets one phone. The Pi sends a count when newly verified files finish draining from its queue and an alert after three unsuccessful worker checks while connected as a Wi-Fi client. It does not send clips, filenames, or credentials in the message. If the API cannot be reached, it waits before retrying. [Pushover Message API](https://pushover.net/api)

To disable the add-on from SSH on home Wi-Fi:

~~~bash
sudo systemctl disable --now xpengusb-web.service xpengusb-dashboard.socket xpengusb-network.timer
~~~

If you are on the hotspot, switch to home Wi-Fi first. The archive worker remains a separate service. Test actual fallback and reconnection, successful NAS save and backup, and receipt on the phone before relying on the add-on in the car.
