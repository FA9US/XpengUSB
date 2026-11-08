# Home Assistant and HACS

XpengUSB can expose recorder status and controls to Home Assistant over your local network. The Pi reuses the dashboard HTTPS service, so the integration adds no always-running service. HA reads small status responses once a minute. Reading status does not mount the USB image, scan clips or initiate a backup.

## Set up

1. Install or update the [Pi dashboard](DASHBOARD.md) from a version with the Home Assistant API. If the USB host is attached, the dashboard installer can leave the gadget running. Wait between backup cycles before restarting the archive worker.
2. On the Pi dashboard, open **Home Assistant → Show private pairing details**. You can also retrieve the token and fingerprint from a private Pi terminal:

   ~~~bash
   sudo cat /etc/xpengusb-web/ha-token
   sudo openssl x509 -in /etc/xpengusb-web/cert.pem -noout -fingerprint -sha256
   ~~~

3. In HACS, add `https://github.com/FA9US/XpengUSB` under **Custom repositories** with type **Integration**. Download **XpengUSB**, restart Home Assistant, then add **XpengUSB** in **Settings → Devices & services**. [HACS custom repositories](https://www.hacs.xyz/docs/faq/custom_repositories/)
4. Enter the Pi's HTTPS URL (for example `https://10.3.12.18:8443`), the token, and the hex fingerprint after the equals sign. The client compares the certificate fingerprint on every request. The Pi's dashboard password, Wi-Fi passwords, NAS password and Pushover credentials remain on the Pi.

The device page links to the dashboard. HA shows disk capacity, recordings, pending and verified files, sync times, Wi-Fi and NAS configuration summaries. **Sync now** queues a regular worker cycle. **Test NAS** and **Test Pushover** use saved Pi settings and show a result or error. The **Pushover alerts** switch pauses or resumes automatic messages and preserves the saved keys. Edit settings on the dashboard.

The dashboard's own tests use the values currently typed in the form. **Test NAS** checks the optional subfolder and tries a temporary write, hard link, readback and cleanup in a separate SMB mount. **Send test notification** sends a single message even if automatic alerts are paused. Testing neither saves the form nor changes the worker's existing SMB mount.

## Arrival alerts

The integration starts an arrival check after two consecutive missed polls followed by a return to the configured home Wi-Fi. Once the recorder stays reachable there for five minutes, it fires an `xpengusb_arrived` event and can request a Pushover alert if the Pi has alerts enabled. Select a **parked** binary sensor during setup to require that it also be On for those five minutes. With no sensor, the alert reports home Wi-Fi connectivity. It does not infer the vehicle's gear position. The Pi's other entities show unavailable when away, and its Online binary sensor shows Off.

HA starts a fresh observation after a restart. An initial successful connection does not send an arrival alert. The integration must observe the absence before it can notify on return. Check receipt by taking the Pi off the LAN for at least two polling intervals, reconnecting it, and leaving it on home Wi-Fi for five minutes. Use the Pushover test button first to validate delivery credentials.

To remove the link, delete the integration entry and remove the custom repository from HACS. The Pi dashboard and recording worker run independently.
